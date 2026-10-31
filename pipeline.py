"""Montaj: pauzalarni kesish, 9:16 format, zoom, rasmlar, ovozni tekislash, subtitr.

Begzodning standart presetlari asosida (video-montaj skill):
  1080x1920, 30 fps, libx264 crf 20, AAC 160k, loudnorm I=-14,
  subtitr Reels xavfsiz zonasida.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path

from stt import Word

W, H = 1080, 1920
FPS = 30
FONTS_DIR = str(Path(__file__).parent / "fonts")


# ---------------- subtitr uslublari ----------------
@dataclass
class Style:
    name: str
    font: str = "Montserrat ExtraBold"
    size: int = 74
    color: str = "FFFFFF"        # RGB
    active: str | None = "FFD53D"  # aytilayotgan so'z rangi (None = karaoke yo'q)
    outline: int = 6
    margin_v: int = 560          # pastdan masofa
    words: int = 3               # bir ekrandagi so'zlar soni
    upper: bool = False
    box: bool = False            # matn ostida qora plashka
    pop: bool = False            # har so'z "sakrab" chiqadi
    active_scale: int = 108


STYLES: dict[str, Style] = {
    "klassik": Style("Klassik — oq, aytilgan so'z sariq"),
    "katta": Style("Katta so'z — bittadan, markazda", size=118, words=1, upper=True, active=None,
                   outline=8, margin_v=720, pop=True),
    "plashka": Style("Plashka — qora fonda oq matn", size=62, words=5, active=None, outline=14,
                     margin_v=520, box=True),
    "rangli": Style("Rangli — katta harf, yashil urg'u", size=92, words=2, upper=True, active="3DE07A",
                    outline=7, margin_v=760, active_scale=116),
}


def run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
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


# ---------------- matnni tuzatish ----------------
def _norm(t: str) -> str:
    return re.sub(r"[^\w']", "", t.lower().replace("ʻ", "'").replace("ʼ", "'").replace("`", "'"))


def align_text(words: list[Word], new_text: str) -> list[Word]:
    """Foydalanuvchi tuzatgan matnni eski so'zlarning vaqtlariga joylaydi."""
    toks = new_text.split()
    if not words or not toks:
        return words
    a, b = [_norm(w.text) for w in words], [_norm(t) for t in toks]
    out: list[Word] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            out += [Word(toks[j1 + k], words[i1 + k].start, words[i1 + k].end) for k in range(i2 - i1)]
        elif op == "replace":
            s, e = words[i1].start, words[i2 - 1].end
            step = (e - s) / (j2 - j1)
            out += [Word(toks[j], s + (j - j1) * step, s + (j - j1 + 1) * step) for j in range(j1, j2)]
        elif op == "insert":
            s = words[i1 - 1].end if i1 > 0 else max(0.0, words[0].start - 0.3)
            e = words[i1].start if i1 < len(words) else s + 0.3 * (j2 - j1)
            if e - s < 0.12 * (j2 - j1):
                e = s + 0.2 * (j2 - j1)
            step = (e - s) / (j2 - j1)
            out += [Word(toks[j], s + (j - j1) * step, s + (j - j1 + 1) * step) for j in range(j1, j2)]
        # delete — so'z olib tashlanadi
    return out


# ---------------- kesimlar ----------------
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


def split_for_jumps(segs: list[tuple[float, float]], words: list[Word], every: float = 3.5) -> list[tuple[float, float]]:
    """Jump-cut zoom uchun uzun bo'laklarni so'zlar orasidan ~every soniyada bo'ladi (vaqt yo'qolmaydi)."""
    out = []
    for s, e in segs:
        cur = s
        for i in range(len(words) - 1):
            cut = (words[i].end + words[i + 1].start) / 2
            if cur + every <= cut < e - 1.0 and cut > s:
                out.append((cur, round(cut, 3)))
                cur = round(cut, 3)
        out.append((cur, e))
    return out


def remap(words: list[Word], segs: list[tuple[float, float]]) -> list[Word]:
    """So'z vaqtlarini kesilgan videoning yangi vaqtiga o'tkazadi."""
    out, offset = [], 0.0
    for s, e in segs:
        for w in words:
            if w.end > s and w.start < e:
                out.append(Word(w.text, max(w.start, s) - s + offset, min(w.end, e) - s + offset))
        offset += e - s
    return out


# ---------------- subtitr (ASS) ----------------
def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _c(rgb: str, alpha: str = "00") -> str:
    return f"&H{alpha}{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}&"


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
    border = 3 if st.box else 1
    outline_col = "&H50000000&" if st.box else "&H00000000&"
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{st.font},{st.size},{_c(st.color)},{_c(st.color)},{outline_col},&H64000000&,0,0,0,0,100,100,0,0,{border},{st.outline},{0 if st.box else 2},2,90,90,{st.margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    pop = "{\\fscx125\\fscy125\\t(0,110,\\fscx100\\fscy100)}" if st.pop else ""
    for g in group_words(words, st.words):
        g_end = g[-1].end + 0.15
        txt = [_esc(x.text.upper() if st.upper else x.text) for x in g]
        if not st.active:
            lines.append(f"Dialogue: 0,{_ts(g[0].start)},{_ts(g_end)},Main,,0,0,0,,{pop}{' '.join(txt)}")
            continue
        for k in range(len(g)):
            start = g[k].start if k else g[0].start
            end = g[k + 1].start if k + 1 < len(g) else g_end
            parts = [f"{{\\c{_c(st.active)}\\fscx{st.active_scale}\\fscy{st.active_scale}}}{t}{{\\r}}" if j == k else t
                     for j, t in enumerate(txt)]
            lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Main,,0,0,0,,{' '.join(parts)}")
    Path(path).write_text(head + "\n".join(lines) + "\n", encoding="utf-8")


def _fpath(p: str) -> str:
    """ffmpeg filtri ichida fayl yo'li (Windows uchun ham)."""
    return p.replace("\\", "/").replace(":", "\\:")


# ---------------- render ----------------
@dataclass
class Options:
    cut: bool = True
    style: str | None = "klassik"     # None = subtitrsiz
    motion: str = "none"              # none | soft | jump
    images: list[str] = field(default_factory=list)
    layout: str = "under"             # under (subtitr ostida) | split (50/50)


def _frame(label_in: str, label_out: str, info: dict, fw: int, fh: int, crop_y: str = "(ih-oh)/2") -> list[str]:
    """Videoni fw x fh ramkaga moslash."""
    portrait = info["height"] / info["width"] >= 1.5
    if portrait or fh < 1200:
        return [f"[{label_in}]scale={fw}:{fh}:force_original_aspect_ratio=increase,crop={fw}:{fh}:(iw-ow)/2:{crop_y},setsar=1,fps={FPS}[{label_out}]"]
    return [
        f"[{label_in}]split[{label_out}b][{label_out}f]",
        f"[{label_out}b]scale={fw}:{fh}:force_original_aspect_ratio=increase,crop={fw}:{fh},boxblur=30:3[{label_out}bb]",
        f"[{label_out}f]scale={fw}:-2[{label_out}fs]",
        f"[{label_out}bb][{label_out}fs]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={FPS}[{label_out}]",
    ]


def render(src: str, pieces: list[tuple[float, float]], ass: str | None, out: str, info: dict,
           workdir: str, opt: Options) -> None:
    split = bool(opt.images) and opt.layout == "split"
    fw, fh = (W, H // 2) if split else (W, H)
    crop_y = "ih*0.08" if split else "(ih-oh)/2"
    parts, labels = [], []
    for i, (s, e) in enumerate(pieces):
        parts.append(f"[0:v]trim=start={s}:end={e},setpts=PTS-STARTPTS[t{i}]")
        parts += _frame(f"t{i}", f"f{i}", info, fw, fh, crop_y)
        if opt.motion == "jump" and i % 2 == 1:
            z = 1.12
            parts.append(f"[f{i}]crop=iw/{z}:ih/{z},scale={fw}:{fh},setsar=1[v{i}]")
        else:
            parts.append(f"[f{i}]null[v{i}]")
        parts.append(f"[0:a]atrim=start={s}:end={e},asetpts=PTS-STARTPTS[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    parts.append(f"{''.join(labels)}concat=n={len(pieces)}:v=1:a=1[vc][ac]")
    total = sum(e - s for s, e in pieces)

    cur = "vc"
    if opt.motion == "soft":  # butun video bo'ylab sekin yaqinlashish (1.0 → 1.10)
        frames = max(1, int(total * FPS))
        parts.append(f"[vc]scale={fw * 2}:{fh * 2},zoompan=z='1+0.10*on/{frames}':d=1:"
                     f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={fw}x{fh}:fps={FPS},setsar=1[vz]")
        cur = "vz"

    inputs: list[str] = []
    if opt.images:
        n = len(opt.images)
        slot = total / n
        for k, img in enumerate(opt.images):
            inputs += ["-loop", "1", "-t", f"{total:.3f}", "-i", img]
            a, b = k * slot, (k + 1) * slot if k + 1 < n else total + 1
            if split:
                parts.append(f"[{k + 1}:v]scale={W}:{H // 2}:force_original_aspect_ratio=increase,crop={W}:{H // 2},setsar=1,fps={FPS}[im{k}]")
            else:
                parts.append(f"[{k + 1}:v]scale=820:560:force_original_aspect_ratio=decrease,"
                             f"pad=iw+12:ih+12:6:6:white,setsar=1,fps={FPS}[im{k}]")
            opt_enable = f"enable='between(t,{a:.3f},{b:.3f})'"
            if split:
                base = "base" if k == 0 else f"c{k - 1}"
                if k == 0:
                    parts.append(f"color=c=black:s={W}x{H}:r={FPS}:d={total:.3f}[base]")
                parts.append(f"[{base}][im{k}]overlay=0:0:{opt_enable}[c{k}]")
            else:
                prev = cur if k == 0 else f"c{k - 1}"
                parts.append(f"[{prev}][im{k}]overlay=(W-w)/2:H-430-h:{opt_enable}[c{k}]")
        if split:
            parts.append(f"[c{n - 1}][{cur}]overlay=0:{H // 2}:shortest=1[cm]")
            cur = "cm"
        else:
            cur = f"c{n - 1}"

    if ass:
        parts.append(f"[{cur}]ass='{_fpath(ass)}':fontsdir='{_fpath(FONTS_DIR)}'[vout]")
    else:
        parts.append(f"[{cur}]null[vout]")
    parts.append("[ac]highpass=f=80,loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]")

    graph = ";".join(parts)
    if len(graph) < 28000:
        fc = ["-filter_complex", graph]
    else:
        script = os.path.join(workdir, "filter.txt")
        Path(script).write_text(graph, encoding="utf-8")
        fc = ["-/filter_complex", script]
    run([
        "ffmpeg", "-y", "-v", "error", "-i", src, *inputs, *fc,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", os.getenv("X264_PRESET", "veryfast"), "-crf", "20",
        "-maxrate", "9M", "-bufsize", "18M", "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", out,
    ])


def style_for(opt: Options) -> Style | None:
    if not opt.style:
        return None
    st = STYLES[opt.style]
    if opt.images and opt.layout == "split":
        st = replace(st, margin_v=H // 2 - 60 - st.size // 2)       # ikki yarim chegarasida
    elif opt.images:
        st = replace(st, margin_v=430 + 572 + 40)                   # rasm ustida
    return st


def make_reel(src: str, words: list[Word], out: str, workdir: str, opt: Options | None = None) -> dict:
    """To'liq jarayon. Qaytaradi: {'before': sek, 'after': sek, 'cuts': n}."""
    opt = opt or Options()
    info = probe(src)
    if not words:
        raise RuntimeError("Videoda nutq topilmadi")
    segs = plan_segments(words, info["duration"]) if opt.cut else [(0.0, info["duration"])]
    pieces = split_for_jumps(segs, words) if opt.motion == "jump" else segs
    mapped = remap(words, pieces)
    st = style_for(opt)
    ass = None
    if st:
        ass = os.path.join(workdir, "subs.ass")
        build_ass(mapped, ass, st)
    render(src, pieces, ass, out, info, workdir, opt)
    after = sum(e - s for s, e in segs)
    return {"before": info["duration"], "after": after, "cuts": max(0, len(segs) - 1)}


def preview(src: str, words: list[Word], style_key: str, out_jpg: str, workdir: str) -> None:
    """Bitta kadrda subtitr uslubini ko'rsatish (asl video vaqtida)."""
    info = probe(src)
    groups = [g for g in group_words(words, 3) if len(g) >= 2] or [words]
    g = groups[min(1, len(groups) - 1)]
    t = (g[0].start + g[-1].end) / 2
    ass = os.path.join(workdir, f"prev_{style_key}.ass")
    build_ass(words, ass, STYLES[style_key])
    vf = ";".join(_frame("0:v", "fr", info, W, H)) + f";[fr]ass='{_fpath(ass)}':fontsdir='{_fpath(FONTS_DIR)}',scale=540:960[o]"
    run(["ffmpeg", "-y", "-v", "error", "-i", src, "-filter_complex", vf, "-map", "[o]",
         "-ss", f"{t:.2f}", "-frames:v", "1", "-q:v", "3", out_jpg])
