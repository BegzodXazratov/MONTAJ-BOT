"""Montaj bot — eng oddiy versiya.

Foydalanuvchi talking head video tashlaydi → bot jimliklarni kesadi, 9:16 qiladi,
ovozni tekislaydi, o'zbekcha karaoke subtitr qo'yadi va tayyor Reels'ni qaytaradi.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message
from dotenv import load_dotenv

import pipeline
import stt

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("montaj")

TOKEN = os.environ["BOT_TOKEN"]
DEFAULT_STT = os.getenv("STT", "eleven")
ALLOWED = {int(x) for x in os.getenv("ALLOWED_IDS", "").replace(" ", "").split(",") if x}
LOCAL_API = os.getenv("TELEGRAM_API_URL")  # o'z Bot API serverimiz bo'lsa (20MB cheklovisiz)
MAX_MB = 2000 if LOCAL_API else 20

session = AiohttpSession(api=TelegramAPIServer.from_base(LOCAL_API, is_local=True)) if LOCAL_API else None
bot = Bot(TOKEN, session=session) if session else Bot(TOKEN)
dp = Dispatcher()
queue: asyncio.Queue = asyncio.Queue()
user_stt: dict[int, str] = {}

HELLO = (
    "Salom! Men montaj botman 🎬\n\n"
    "Talking head video tashlang (vertikal yoki gorizontal), men:\n"
    "• jimlik va pauzalarni kesaman\n"
    "• 9:16 Reels formatiga keltiraman\n"
    "• ovozni tekislayman\n"
    "• o'zbekcha karaoke subtitr qo'yaman\n\n"
    f"Video {MAX_MB} MB gacha bo'lsin. 1 daqiqalik video ~1–3 daqiqada tayyor bo'ladi."
)


def allowed(m: Message) -> bool:
    return not ALLOWED or (m.from_user and m.from_user.id in ALLOWED)


@dp.message(Command("start", "help"))
async def start(m: Message):
    await m.answer(HELLO)


@dp.message(Command("stt"))
async def set_stt(m: Message):
    """Sinov uchun: /stt whisper yoki /stt eleven"""
    if not allowed(m):
        return
    arg = (m.text or "").split(maxsplit=1)[1:] or [""]
    if arg[0] in stt.PROVIDERS:
        user_stt[m.from_user.id] = arg[0]
        await m.answer(f"Endi nutq {arg[0]} orqali o'giriladi.")
    else:
        cur = user_stt.get(m.from_user.id, DEFAULT_STT)
        await m.answer(f"Hozir: {cur}. O'zgartirish: /stt whisper yoki /stt eleven")


@dp.message(F.video | F.video_note | (F.document & F.document.mime_type.startswith("video/")))
async def on_video(m: Message):
    if not allowed(m):
        return await m.answer("Bot hozircha yopiq sinovda. Tez orada ochiladi!")
    media = m.video or m.video_note or m.document
    size_mb = (media.file_size or 0) / 1024 / 1024
    if size_mb > MAX_MB:
        return await m.answer(
            f"Video {size_mb:.0f} MB — men hozircha {MAX_MB} MB gacha qabul qilaman.\n"
            "Videoni fayl emas, oddiy video qilib yuboring — Telegram o'zi siqadi."
        )
    pos = queue.qsize() + 1
    note = await m.answer("✅ Qabul qilindi. Montaj boshlandi…" if pos == 1 else f"✅ Qabul qilindi. Navbatda: {pos}-o'rin")
    await queue.put((m, media.file_id, note))


async def process(m: Message, file_id: str, note: Message):
    work = tempfile.mkdtemp(prefix="mb_")
    t0 = time.time()
    provider = user_stt.get(m.from_user.id, DEFAULT_STT)
    try:
        src, wav, out = (os.path.join(work, n) for n in ("in.mp4", "a.wav", "reel.mp4"))
        f = await bot.get_file(file_id)
        if LOCAL_API and f.file_path and os.path.isabs(f.file_path):
            shutil.copy(f.file_path, src)
        else:
            await bot.download_file(f.file_path, src)

        await note.edit_text("🎧 Nutq matnga o'girilmoqda…")
        await asyncio.to_thread(pipeline.extract_audio, src, wav)
        tr = await asyncio.to_thread(stt.transcribe, wav, provider)

        await note.edit_text("✂️ Kesilmoqda va subtitr qo'yilmoqda…")
        res = await asyncio.to_thread(pipeline.make_reel, src, tr.words, out, work)

        took = time.time() - t0
        cap = (
            f"Tayyor ✅  {res['before']:.0f} s → {res['after']:.0f} s, {res['cuts']} ta pauza kesildi.\n"
            f"Nutq: {tr.provider} ({tr.seconds:.0f} s), jami {took:.0f} s."
        )
        await m.answer_video(FSInputFile(out, filename="reel.mp4"), caption=cap, width=1080, height=1920,
                             supports_streaming=True)
        await m.answer(f"📝 Matn ({tr.provider}):\n\n{tr.text[:3500]}")
        await note.delete()
        log.info("done user=%s stt=%s %.1fs words=%d", m.from_user.id, provider, took, len(tr.words))
    except Exception as e:  # foydalanuvchiga tushunarli xabar, logga to'liq xato
        log.exception("failed user=%s", m.from_user.id)
        await note.edit_text(f"❌ Xatolik: {str(e)[:300]}\nBoshqa video bilan urinib ko'ring.")
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def worker():
    while True:
        m, file_id, note = await queue.get()
        try:
            await process(m, file_id, note)
        finally:
            queue.task_done()


async def main():
    asyncio.create_task(worker())
    log.info("bot ishga tushdi, stt=%s, max=%sMB", DEFAULT_STT, MAX_MB)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
