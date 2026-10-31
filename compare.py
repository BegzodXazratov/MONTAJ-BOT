"""Whisper va ElevenLabs Scribe'ni bitta videoda solishtirish.

Ishlatish (serverda):
    docker compose run --rm bot python compare.py /data/video1.mp4 /data/video2.mp4

Har bir video uchun compare_<nom>.md yaratadi: ikkala matn, so'zlar soni, vaqt.
--render qo'shsangiz, ikkala variantda tayyor Reels ham chiqaradi.
"""
from __future__ import annotations

import os
import sys
import tempfile

import pipeline
import stt


def main(paths: list[str], render: bool):
    for p in paths:
        name = os.path.splitext(os.path.basename(p))[0]
        work = tempfile.mkdtemp(prefix="cmp_")
        wav = os.path.join(work, "a.wav")
        pipeline.extract_audio(p, wav)
        rows = []
        for prov in ("whisper", "eleven"):
            try:
                tr = stt.transcribe(wav, prov)
                rows.append((prov, tr))
                print(f"[{name}] {prov}: {len(tr.words)} so'z, {tr.seconds:.1f} s")
                if render:
                    out = os.path.join(os.path.dirname(os.path.abspath(p)), f"{name}_{prov}.mp4")
                    pipeline.make_reel(p, tr.words, out, work)
                    print(f"   → {out}")
            except Exception as e:
                print(f"[{name}] {prov}: XATO {e}")
        md = [f"# {name}\n"]
        for prov, tr in rows:
            md.append(f"## {prov} — {len(tr.words)} so'z, {tr.seconds:.1f} s\n\n{tr.text}\n")
        out_md = os.path.join(os.path.dirname(os.path.abspath(p)), f"compare_{name}.md")
        with open(out_md, "w", encoding="utf-8") as f:
            f.write("\n".join(md))
        print(f"   → {out_md}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--render"]
    if not args:
        sys.exit(__doc__)
    main(args, "--render" in sys.argv)
