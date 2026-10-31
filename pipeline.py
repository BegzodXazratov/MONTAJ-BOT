"""Montaj: jimliklarni kesish, 9:16 format, ovozni tekislash, karaoke subtitr.

Begzodning standart presetlari asosida (video-montaj skill):
  1080x1920, 30 fps, libx264 crf 20, AAC 160k, loudnorm I=-14,
  subtitr Reels xavfsiz zonasida (pastdan ~420px dan yuqorida).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from stt import Word

W, H = 1080, 1920
FONTS_DIR = str(Path(__file__).parent / "fonts")


@dataclass
class Style:
    font: str = "Montserrat ExtraBold"
    size: int = 74
    color: str = "FFFFFF"        # oddiy so'z
    active: str = "FFD53D"       # aytilayotgan so'z, RGB hex — sariq
    outline: int = 6
    margin_v: int = 560          # pastdan masofa (Reels xavfsiz zona)
    words_per_line: int = 3
    upper: bool = False


def run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} xatosi:\n{p.stderr[-1500:]}")
    return p.stdout


def probe(path: str) -> dict:
    out = json.loads(run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path]))
    v = next((s for s in out["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in out["streams"] if s["codec_type"] == "audio"), None)
    if not v:
        raise RuntimeError("Faylda video yo'q")
    if not a:
        raise RuntimeError("Videoda ovoz yo'q — subtitr va kesish uchun ovoz kerak")
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []):
        if "rotation" in sd:
            rot = int(sd["rotation"])
    rot = int(v.get("tags", {}).get("rotate", rot))
    if abs(rot) % 180 == 90:
        w, h = h, w
    return {"width": w, "height": h, "duration": float(out["format"]["duration"])}


def extract_audio(src: str, wav: str) -> None:
    run(["ffmpeg", "-y", "-v", "error", "-i", src, "-vn", "-ac", "1", "-ar", "16000", wav])


# ---------------- jimliklarni kesish ----------------
def plan_segments(words: list[Word], duration: float, max_gap: float = 0.45, pad: float = 0.12) -> list[tuple[float, float]]:
    """So'zlar orasidagi max_gap dan uzun pauzalarni kesib tashlaydi."""
    segs: list[list[float]] = []
    for w in words:
        s, e = max(0.0, w.start - pad), min(duration, w.end + pad)
        if segs and s - segs[-1][1] <= max_gap:
            segs[-1][1] = max(segs[-1][1], e)
        else:
            segs.append([s, e])
    return [(round(s, 3), round(e, 3)) for s, e in segs if e - s > 0.05]


def remap(words: list[Word], segs: list[tuple[float, float]]) -> list[Word]:
    """So'z vaqtlarini kesilgan videoning yangi vaqtiga o'tkazadi."""
    out, offset, i = [], 0.0, 0
    for s, e in segs:
        while i < len(words) and words[i].start < e:
            w = words[i]
            if w.end > s:
                out.append(Word(w.text, max(w.start, s) - s + offset, min(w.end, e) - s + offset))
            i += 1
        offset += e - s
    return out


# ---------------- subtitr (ASS, karaoke) ----------------
def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _ass_color(rgb: str) -> str:
    r, g, b = rgb[0:2], rgb[2:4], rgb[4:6]
    return f"&H00{b}{g}{r}&"


def _esc(t: str) -> str:
    return t.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def group_words(words: list[Word], n: int) -> list[list[Word]]:
    groups, cur = [], []
    for w in words:
        if cur and (len(cur) >= n or w.start - cur[-1].end > 0.6):
            groups.append(cur)
            cur = []
        cur.append(w)
        if re.search(r"[.!?…]$", w.text):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def build_ass(words: list[Word], path: str, st: Style) -> None:
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{st.font},{st.size},{_ass_color(st.color)},{_ass_color(st.color)},&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,{st.outline},2,2,90,90,{st.margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    act = _ass_color(st.active)
    for g in group_words(words, st.words_per_line):
        g_end = g[-1].end + 0.15
        for k, w in enumerate(g):
            start = w.start if k else g[0].start
            end = g[k + 1].start if k + 1 < len(g) else g_end
            parts = []
            for j, x in enumerate(g):
                t = _esc(x.text.upper() if st.upper else x.text)
                parts.append(f"{{\\c{act}\\fscx108\\fscy108}}{t}{{\\r}}" if j == k else t)
            lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Main,,0,0,0,,{' '.join(parts)}")
    Path(path).write_text(head + "\n".join(lines) + "\n", encoding="utf-8")


# ---------------- render ----------------
def render(src: str, segs: list[tuple[float, float]], ass: str | None, out: str, info: dict, workdir: str) -> None:
    parts, labels = [], []
    for i, (s, e) in enumerate(segs):
        parts.append(f"[0:v]trim=start={s}:end={e},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a]atrim=start={s}:end={e},asetpts=PTS-STARTPTS[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    parts.append(f"{''.join(labels)}concat=n={len(segs)}:v=1:a=1[vc][ac]")

    portrait = info["height"] / info["width"] >= 1.5
    if portrait:  # telefon videosi — to'ldirib qirqamiz
        parts.append(f"[vc]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps=30[vf]")
    else:  # gorizontal — xira fon + markazda asl video
        parts.append("[vc]split[bg][fg]")
        parts.append(f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:3[bgb]")
        parts.append(f"[fg]scale={W}:-2[fgs]")
        parts.append("[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30[vf]")
    if ass:
        ass_esc = ass.replace("\\", "/").replace(":", "\\:")
        fonts_esc = FONTS_DIR.replace(":", "\\:")
        parts.append(f"[vf]ass='{ass_esc}':fontsdir='{fonts_esc}'[vout]")
    else:
        parts.append("[vf]null[vout]")
    parts.append("[ac]highpass=f=80,loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]")

    script = os.path.join(workdir, "filter.txt")
    Path(script).write_text(";\n".join(parts), encoding="utf-8")
    preset = os.getenv("X264_PRESET", "veryfast")
    run([
        "ffmpeg", "-y", "-v", "error", "-i", src, "-filter_complex_script", script,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", preset, "-crf", "20", "-maxrate", "9M", "-bufsize", "18M",
        "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", out,
    ])


def make_reel(src: str, words: list[Word], out: str, workdir: str, style: Style | None = None,
              cut: bool = True, subs: bool = True) -> dict:
    """To'liq jarayon. Qaytaradi: {'before': sek, 'after': sek, 'cuts': n}."""
    st = style or Style()
    info = probe(src)
    if not words:
        raise RuntimeError("Videoda nutq topilmadi")
    segs = plan_segments(words, info["duration"]) if cut else [(0.0, info["duration"])]
    mapped = remap(words, segs)
    ass = os.path.join(workdir, "subs.ass") if subs else None
    if ass:
        build_ass(mapped, ass, st)
    render(src, segs, ass, out, info, workdir)
    after = sum(e - s for s, e in segs)
    return {"before": info["duration"], "after": after, "cuts": max(0, len(segs) - 1)}
