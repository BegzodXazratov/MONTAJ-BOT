# Montaj bot 🎬

Telegram bot: talking head video tashlanadi → tayyor Reels qaytadi.

- jimlik va pauzalarni kesadi (so'zlar orasidagi 0.45 s dan uzun bo'shliqlar)
- 9:16, 1080×1920, 30 fps (gorizontal video — xira fon bilan)
- ovozni tekislaydi (loudnorm −14 LUFS)
- o'zbekcha karaoke subtitr: aytilayotgan so'z sariq, Montserrat ExtraBold, Reels xavfsiz zonasida
- matnni alohida xabar qilib ham yuboradi

Nutqni matnga o'girish ikki xil: **ElevenLabs Scribe** (pullik, aniqroq) yoki **Whisper** (bepul, serverda). Sinov paytida `/stt whisper` va `/stt eleven` bilan almashtirib solishtirasiz.

## Ishga tushirish (VPS, Ubuntu)

1. **Bot yarating:** Telegram'da @BotFather → `/newbot` → token oling.
2. **ElevenLabs kaliti:** elevenlabs.io → Profile → API Keys → yangi kalit (Speech to Text ruxsati bilan).
3. **O'z Telegram ID'ingiz:** @userinfobot ga yozing — raqamni oling.
4. **Serverga ulaning va o'rnating:**

```bash
curl -fsSL https://get.docker.com | sh
git clone https://github.com/BegzodXazratov/MONTAJ-BOT.git
cd MONTAJ-BOT
cp .env.example .env
nano .env          # BOT_TOKEN, ELEVENLABS_API_KEY, ALLOWED_IDS ni yozing → Ctrl+O, Enter, Ctrl+X
docker compose up -d --build
docker compose logs -f   # "bot ishga tushdi" chiqsa — tayyor
```

5. Botga video tashlang.

Yangilash: `git pull && docker compose up -d --build`

## Whisper vs ElevenLabs solishtirish

```bash
mkdir -p data   # videolarni shu papkaga yuklang (scp yoki sftp bilan)
docker compose run --rm bot python compare.py /data/video1.mp4 /data/video2.mp4 --render
```

Natija: `data/compare_*.md` (ikkala matn) va `data/*_whisper.mp4`, `data/*_eleven.mp4`.

## Server talabi

- ElevenLabs bilan: 2 vCPU / 4 GB RAM yetadi.
- Whisper `large-v3` bilan: 4 vCPU / 8 GB RAM tavsiya (1 daqiqalik video ~1–3 daqiqa).
  Kuchsizroq serverda `.env` da `WHISPER_MODEL=medium` qiling.

## Cheklovlar (MVP)

- Oddiy Telegram bot 20 MB gacha video qabul qiladi. Videoni **fayl emas, video** qilib yuborilsa, Telegram o'zi siqadi.
  Kattaroq fayllar uchun keyin o'z Bot API serverimizni qo'shamiz (`TELEGRAM_API_URL`).
- Bir vaqtda bitta video ishlanadi, qolganlari navbatda turadi.
- To'lov hali yo'q — `ALLOWED_IDS` bilan faqat sinovchilarga ochiq.
