"""Montaj bot.

Jarayon: video → matn (tasdiqlash yoki tuzatish) → pauzalar kesilsinmi → subtitr uslubi (ko'rinishi bilan)
→ zoom → rasmlar va joylashuvi → yakuniy tasdiq → montaj → tayyor Reels.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, Message)
from dotenv import load_dotenv

import pipeline
import stt
from pipeline import STYLES, Options

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("montaj")

TOKEN = os.environ["BOT_TOKEN"]
DEFAULT_STT = os.getenv("STT", "eleven")
ALLOWED = {int(x) for x in os.getenv("ALLOWED_IDS", "").replace(" ", "").split(",") if x}
LOCAL_API = os.getenv("TELEGRAM_API_URL")
MAX_MB = 2000 if LOCAL_API else 20
MAX_IMAGES = 10

session = AiohttpSession(api=TelegramAPIServer.from_base(LOCAL_API, is_local=True)) if LOCAL_API else None
bot = Bot(TOKEN, session=session) if session else Bot(TOKEN)
dp = Dispatcher()
queue: asyncio.Queue[Callable[[], Awaitable[None]]] = asyncio.Queue()
user_stt: dict[int, str] = {}

MOTIONS = {"none": "Oddiy (zoomsiz)", "soft": "Yumshoq zoom", "jump": "Jump-cut zoom"}
LAYOUTS = {"under": "Subtitr ostida", "split": "Ekran 50/50"}
STYLE_KEYS = list(STYLES)


@dataclass
class Session:
    uid: int
    work: str
    src: str = ""
    words: list = field(default_factory=list)
    provider: str = ""
    # cut → style → motion → images → layout → confirm → (matn kutilmoqda) → text → render
    stage: str = "cut"
    opt: Options = field(default_factory=Options)
    stt_done: bool = False
    stt_error: str = ""
    ready: asyncio.Event = field(default_factory=asyncio.Event)  # video yuklab olindi


sessions: dict[int, Session] = {}


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r] for r in rows])


def allowed(uid: int | None) -> bool:
    return not ALLOWED or (uid in ALLOWED)


def drop(uid: int):
    s = sessions.pop(uid, None)
    if s:
        shutil.rmtree(s.work, ignore_errors=True)


HELLO = (
    "Salom! Men montaj botman 🎬\n\n"
    "Talking head video tashlang. Avval hamma narsani tanlab olasiz:\n"
    "1. pauzalar kesilsinmi\n"
    "2. subtitr uslubi (4 xil namuna ko'rsataman)\n"
    "3. zoom — yumshoq yoki jump-cut\n"
    "4. rasmlar — subtitr ostida yoki ekran 50/50\n"
    "Shu orada men nutqni matnga o'giraman — oxirida matnni tasdiqlaysiz va montaj boshlanadi.\n\n"
    f"Video {MAX_MB} MB gacha. Bekor qilish: /bekor"
)


@dp.message(Command("start", "help"))
async def start(m: Message):
    await m.answer(HELLO)


@dp.message(Command("bekor"))
async def cancel(m: Message):
    drop(m.from_user.id)
    await m.answer("Bekor qilindi. Yangi video tashlashingiz mumkin.")


@dp.message(Command("stt"))
async def set_stt(m: Message):
    if not allowed(m.from_user.id):
        return
    arg = (m.text or "").split(maxsplit=1)[1:] or [""]
    if arg[0] in stt.PROVIDERS:
        user_stt[m.from_user.id] = arg[0]
        await m.answer(f"Endi nutq {arg[0]} orqali o'giriladi.")
    else:
        await m.answer(f"Hozir: {user_stt.get(m.from_user.id, DEFAULT_STT)}. O'zgartirish: /stt whisper yoki /stt eleven")


# ---------------- 1. video qabul qilish va matn ----------------
@dp.message(F.video | F.video_note | (F.document & F.document.mime_type.startswith("video/")))
async def on_video(m: Message):
    if not allowed(m.from_user.id):
        return await m.answer("Bot hozircha yopiq sinovda. Tez orada ochiladi!")
    media = m.video or m.video_note or m.document
    size_mb = (media.file_size or 0) / 1024 / 1024
    if size_mb > MAX_MB:
        return await m.answer(f"Video {size_mb:.0f} MB — hozircha {MAX_MB} MB gacha qabul qilaman.\n"
                              "Videoni fayl emas, oddiy video qilib yuboring — Telegram o'zi siqadi.")
    uid = m.from_user.id
    drop(uid)
    s = Session(uid=uid, work=tempfile.mkdtemp(prefix="mb_"))
    s.src = os.path.join(s.work, "in.mp4")
    sessions[uid] = s
    await m.answer("✅ Video qabul qilindi. Montaj sozlamalarini tanlaymiz — shu orada matn tayyorlanadi.")

    async def job():  # fonda: nutq → matn
        try:
            wav = os.path.join(s.work, "a.wav")
            await asyncio.to_thread(pipeline.extract_audio, s.src, wav)
            tr = await asyncio.to_thread(stt.transcribe, wav, user_stt.get(uid, DEFAULT_STT))
            if not tr.words:
                raise RuntimeError("Videoda nutq topilmadi")
            s.words, s.provider = tr.words, tr.provider
        except Exception as e:
            log.exception("stt failed")
            s.stt_error = str(e)[:300]
        s.stt_done = True
        if sessions.get(uid) is s and s.stage == "wait_text":
            await show_text(m, s)

    async def download():  # fonda: videoni yuklab olish, keyin matn navbatga
        try:
            f = await bot.get_file(media.file_id)
            if LOCAL_API and f.file_path and os.path.isabs(f.file_path):
                shutil.copy(f.file_path, s.src)
            else:
                await bot.download_file(f.file_path, s.src, timeout=300)
            log.info("downloaded user=%s %.1f MB", uid, os.path.getsize(s.src) / 1048576)
        except Exception as e:
            log.exception("download failed")
            s.stt_error, s.stt_done = f"Videoni yuklab bo'lmadi: {str(e)[:200]}", True
            s.ready.set()
            return
        s.ready.set()
        await queue.put(job)

    asyncio.create_task(download())
    await ask_cut(m)


async def show_text(m: Message, s: Session):
    """Sozlamalar tanlangan va matn tayyor bo'lganda — matnni tasdiqlatish."""
    if s.stt_error:
        drop(s.uid)
        return await m.answer(f"❌ Nutqni o'girishda xato: {s.stt_error}")
    s.stage = "text"
    await send_text(m, s)


async def send_text(m: Message, s: Session):
    text = " ".join(w.text for w in s.words)
    await m.answer(
        f"📝 Matn ({s.provider}):\n\n{text[:3500]}\n\n"
        "To'g'rimi? Xato bo'lsa — matnni nusxalab, tuzatib, shu yerga yozib yuboring.",
        reply_markup=kb([("✅ Matn to'g'ri — montajni boshlash", "ok_text")]),
    )


@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(m: Message):
    s = sessions.get(m.from_user.id)
    if not s:
        return await m.answer("Video tashlang — montajni boshlaymiz 🎬")
    if s.stage != "text":
        return await m.answer("Hozir yuqoridagi tugmalardan tanlang. Matnni oxirida tasdiqlaysiz.")
    s.words = pipeline.align_text(s.words, m.text)
    s.provider = "tuzatilgan"
    await send_text(m, s)


# ---------------- 2–6. tanlovlar ----------------
async def ask_cut(m: Message):
    await m.answer("✂️ Pauzalarni kesaymi?\n(Ataylab qilingan pauzalar bo'lsa — kesmang)",
                   reply_markup=kb([("✂️ Ha, kesilsin", "cut:1"), ("⏸ Yo'q, qoldirilsin", "cut:0")]))


async def ask_style(m: Message, s: Session):
    wait = await m.answer("🎨 Subtitr uslublari tayyorlanmoqda…")
    await s.ready.wait()
    try:
        media = []
        mid = pipeline.probe(s.src)["duration"] / 2
        sample = [stt.Word(t, mid - 0.6 + i * 0.35, mid - 0.35 + i * 0.35)
                  for i, t in enumerate("Subtitr mana shunaqa ko'rinadi".split())]
        for i, k in enumerate(STYLE_KEYS, 1):
            jpg = os.path.join(s.work, f"pv_{k}.jpg")
            await asyncio.to_thread(pipeline.preview, s.src, sample, k, jpg, s.work)
            media.append(InputMediaPhoto(media=FSInputFile(jpg), caption=f"{i}. {STYLES[k].name}"))
        await m.answer_media_group(media)
    except Exception:
        log.exception("preview failed")
    await wait.delete()
    rows = [[(f"{i}. {STYLES[k].name.split(' —')[0]}", f"style:{k}") for i, k in enumerate(STYLE_KEYS[j:j + 2], j + 1)]
            for j in range(0, len(STYLE_KEYS), 2)]
    await m.answer("🎨 Qaysi subtitr uslubi?", reply_markup=kb(*rows, [("🚫 Subtitrsiz", "style:-")]))


async def ask_motion(m: Message):
    await m.answer("🎥 Kamera harakati:\n• Yumshoq zoom — butun video davomida sekin yaqinlashadi\n"
                   "• Jump-cut zoom — har kesimda kadr navbat bilan yaqinlashib-uzoqlashadi",
                   reply_markup=kb([("Oddiy", "motion:none")], [("🔍 Yumshoq zoom", "motion:soft"), ("⚡ Jump-cut", "motion:jump")]))


async def ask_images(m: Message, s: Session):
    n = len(s.opt.images)
    txt = ("🖼 Videoga rasm qo'shasizmi? Rasmlarni shu yerga yuboring (10 tagacha), keyin «Tayyor» ni bosing.\n"
           "Rasmlar video bo'ylab teng taqsimlanib chiqadi." if not n else f"🖼 {n} ta rasm qabul qilindi. Yana yuboring yoki «Tayyor».")
    await m.answer(txt, reply_markup=kb([("⏭ Rasmsiz", "img:skip"), (f"✅ Tayyor ({n})", "img:done")]))


async def ask_layout(m: Message):
    await m.answer("📐 Rasm qayerda chiqsin?\n• Subtitr ostida — rasm pastda kartochka bo'lib, subtitr uning ustida\n"
                   "• Ekran 50/50 — tepada rasm, pastda siz",
                   reply_markup=kb([("⬇️ Subtitr ostida", "layout:under"), ("🌓 Ekran 50/50", "layout:split")]))


async def ask_confirm(m: Message, s: Session):
    o = s.opt
    lines = [
        "📋 Montaj sozlamalari:",
        f"• Pauzalar: {'kesiladi' if o.cut else 'qoldiriladi'}",
        "• Subtitr: " + (STYLES[o.style].name.split(" —")[0] if o.style else "yo'q"),
        f"• Kamera: {MOTIONS[o.motion]}",
        f"• Rasmlar: {len(o.images)} ta" + (f", {LAYOUTS[o.layout]}" if o.images else ""),
    ]
    await m.answer("\n".join(lines), reply_markup=kb([("✅ Tasdiqlash", "go")], [("🔄 Qaytadan tanlash", "redo")]))


@dp.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def on_photo(m: Message):
    s = sessions.get(m.from_user.id)
    if not s or s.stage != "images":
        return await m.answer("Rasmni videoni sozlash paytida, «rasm qo'shasizmi?» bosqichida yuboring.")
    if len(s.opt.images) >= MAX_IMAGES:
        return await m.answer(f"Ko'pi bilan {MAX_IMAGES} ta rasm.")
    fid = m.photo[-1].file_id if m.photo else m.document.file_id
    path = os.path.join(s.work, f"img_{len(s.opt.images)}.jpg")
    f = await bot.get_file(fid)
    await bot.download_file(f.file_path, path)
    s.opt.images.append(path)
    await ask_images(m, s)


@dp.callback_query()
async def on_cb(c: CallbackQuery):
    s = sessions.get(c.from_user.id)
    await c.answer()
    if not s:
        return await c.message.answer("Sessiya tugagan. Videoni qayta tashlang.")
    data = c.data or ""
    key, _, val = data.partition(":")
    m = c.message
    try:
        await m.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if key == "ok_text" and s.stage == "text":
        s.stage = "render"
        await start_render(m, s)
    elif key == "cut" and s.stage == "cut":
        s.opt.cut = val == "1"
        s.stage = "style"
        await ask_style(m, s)
    elif key == "style" and s.stage == "style":
        s.opt.style = None if val == "-" else val
        s.stage = "motion"
        await ask_motion(m)
    elif key == "motion" and s.stage == "motion":
        s.opt.motion = val
        s.stage = "images"
        await ask_images(m, s)
    elif key == "img" and s.stage == "images":
        if val == "skip":
            s.opt.images = []
        if s.opt.images:
            s.stage = "layout"
            await ask_layout(m)
        else:
            s.stage = "confirm"
            await ask_confirm(m, s)
    elif key == "layout" and s.stage == "layout":
        s.opt.layout = val
        s.stage = "confirm"
        await ask_confirm(m, s)
    elif key == "redo" and s.stage == "confirm":
        s.opt = Options()
        s.stage = "cut"
        await ask_cut(m)
    elif key == "go" and s.stage == "confirm":
        if s.stt_done:
            await show_text(m, s)
        else:
            s.stage = "wait_text"
            await m.answer("⏳ Sozlamalar saqlandi. Matn tayyor bo'lishi bilan yuboraman — tasdiqlasangiz montaj boshlanadi.")


# ---------------- 7. montaj ----------------
async def start_render(m: Message, s: Session):
    pos = queue.qsize() + 1
    note = await m.answer("🎬 Montaj boshlandi…" if pos == 1 else f"🎬 Navbatda: {pos}-o'rin")

    async def job():
        t0 = time.time()
        try:
            await note.edit_text("🎬 Montaj qilinmoqda… (1 daqiqalik videoga ~1–3 daqiqa)")
            out = os.path.join(s.work, "reel.mp4")
            res = await asyncio.to_thread(pipeline.make_reel, s.src, s.words, out, s.work, s.opt)
            cap = (f"Tayyor ✅  {res['before']:.0f} s → {res['after']:.0f} s"
                   + (f", {res['cuts']} ta pauza kesildi" if s.opt.cut else "") + f". ({time.time() - t0:.0f} s)")
            await m.answer_video(FSInputFile(out, filename="reel.mp4"), caption=cap, width=1080, height=1920,
                                 supports_streaming=True)
            await note.delete()
            log.info("done user=%s opt=%s %.1fs", s.uid, s.opt, time.time() - t0)
        except Exception as e:
            log.exception("render failed")
            await note.edit_text(f"❌ Xatolik: {str(e)[:300]}")
        finally:
            drop(s.uid)

    await queue.put(job)


async def worker():
    while True:
        job = await queue.get()
        try:
            await job()
        except Exception:
            log.exception("job failed")
        finally:
            queue.task_done()


async def main():
    asyncio.create_task(worker())
    log.info("bot ishga tushdi, stt=%s, max=%sMB", DEFAULT_STT, MAX_MB)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
