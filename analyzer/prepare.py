"""Turn a gameplay recording into a "dive pack" an analyst (Claude) can read quickly.

    python -m analyzer prepare recording.mp4 --game 994732206 [--name "Blox Fruits"] [--out dives/]

Pack contents:
  pack.json            video facts + game + paths
  timeline.json        one row per second: motion, colour, UI region changes, sound onsets, OCR words, flags
  moments.json         the highest-scoring seconds (likely rewards, popups, VFX, shop prompts)
  frames/0000.jpg      one frame per second (640 px wide)
  keyframes/*.jpg      full-resolution frames at the top moments
  sheets/sheet_NN.jpg  12-second contact sheets with each frame's signals printed under it
  strip.png            motion / colour / loudness over time with moments marked
  ANALYSIS.md          the brief: what to look at and the findings template to fill
  findings.template.json
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import signals
from .schema import template

THUMB_W, THUMB_H = 320, 180
COLS, ROWS = 4, 3


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "game"


def extract_frames(video: str, out: Path, peaks: list[float]) -> list[str]:
    """One frame per second: the most eventful of that second's 4 samples (so a 0.3 s flash still shows)."""
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "_4fps"
    tmp.mkdir(exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", video, "-vf", f"fps={signals.FPS},scale=640:-2", "-q:v", "4",
                    "-start_number", "0", str(tmp / "%05d.jpg")], check=True)
    samples = sorted(tmp.glob("*.jpg"))
    paths = []
    for s, pt in enumerate(peaks):
        i = min(int(round(pt * signals.FPS)), len(samples) - 1)
        if i < 0:
            break
        dst = out / f"{s:04d}.jpg"
        samples[i].replace(dst) if samples[i].exists() else None
        paths.append(str(dst))
    for p in tmp.glob("*.jpg"):
        p.unlink()
    tmp.rmdir()
    return [p for p in paths if Path(p).exists()]


def extract_keyframes(video: str, seconds: list[int], peaks: list[float], out: Path) -> list[str]:
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for s in seconds:
        p = out / f"t{s:04d}.jpg"
        at = peaks[s] if s < len(peaks) else s + 0.5
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{at:.2f}", "-i", video, "-frames:v", "1", "-q:v", "3",
                        str(p)], check=True)
        if p.exists():
            paths.append(str(p))
    return paths


def _font(size: int):
    from PIL import ImageFont
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf",
                 "/Library/Fonts/Arial.ttf", "C:/Windows/Fonts/arial.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _fmt(t: int) -> str:
    return f"{t // 60}:{t % 60:02d}"


def contact_sheets(frame_paths: list[str], rows: list[dict], moments: set[int], out: Path) -> list[str]:
    from PIL import Image, ImageDraw

    out.mkdir(parents=True, exist_ok=True)
    per = COLS * ROWS
    label_h = 50
    f1, f2 = _font(15), _font(13)
    sheets = []
    for start in range(0, len(frame_paths), per):
        sheet = Image.new("RGB", (COLS * THUMB_W, ROWS * (THUMB_H + label_h)), (18, 18, 22))
        d = ImageDraw.Draw(sheet)
        for i, p in enumerate(frame_paths[start:start + per]):
            sec = start + i
            x, y = (i % COLS) * THUMB_W, (i // COLS) * (THUMB_H + label_h)
            im = Image.open(p).convert("RGB")
            im.thumbnail((THUMB_W, THUMB_H))
            sheet.paste(im, (x, y))
            r = rows[sec] if sec < len(rows) else {}
            star = sec in moments
            if star:
                d.rectangle([x, y, x + THUMB_W - 1, y + THUMB_H - 1], outline=(255, 196, 0), width=4)
            line1 = f"{'* ' if star else ''}{_fmt(sec)}  " + " ".join(r.get("flags", []))
            bits = []
            if r.get("sfx_onsets"):
                bits.append(f"sfx x{r['sfx_onsets']} {','.join(r.get('sfx_tones', []))}")
            if r.get("ui_regions_changed"):
                bits.append("ui:" + ",".join(r["ui_regions_changed"]))
            if r.get("ocr_keywords"):
                bits.append("txt:" + ",".join(r["ocr_keywords"][:4]))
            d.text((x + 6, y + THUMB_H + 5), line1[:40], fill=(255, 220, 120) if star else (230, 230, 230), font=f1)
            d.text((x + 6, y + THUMB_H + 27), "  ".join(bits)[:44], fill=(160, 200, 255), font=f2)
        p = out / f"sheet_{start // per + 1:02d}.jpg"
        sheet.save(p, quality=85)
        sheets.append(str(p))
    return sheets


def signal_strip(vs: dict, au: dict, moments: list[int], out: Path) -> str | None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    fig, axes = plt.subplots(3, 1, figsize=(14, 6), sharex=True)
    t = vs["t"]
    axes[0].plot(t, vs["motion"], lw=0.8, color="#3a7bd5")
    axes[0].set_ylabel("motion")
    axes[1].plot(t, vs["brightness"], lw=0.8, color="#e0a100", label="brightness")
    axes[1].plot(t, vs["saturation"], lw=0.8, color="#c0392b", label="saturation")
    axes[1].legend(loc="upper right", fontsize=7)
    loud = au.get("loudness_db", [])
    axes[2].plot(range(len(loud)), loud, lw=0.8, color="#555")
    for o in au.get("onsets", []):
        axes[2].axvline(o["t"], color={"bright": "#2e86de", "mid": "#27ae60", "low": "#8e44ad"}[o["tone"]], lw=0.4, alpha=0.6)
    axes[2].set_ylabel("loudness dB")
    axes[2].set_xlabel("seconds")
    for ax in axes:
        for m in moments:
            ax.axvline(m, color="#ffb400", lw=1, alpha=0.5)
        ax.grid(alpha=0.2)
    fig.tight_layout()
    p = out / "strip.png"
    fig.savefig(p, dpi=110)
    plt.close(fig)
    return str(p)


def pick_moments(rows: list[dict], n: int = 18) -> list[int]:
    """Top-scoring seconds, at least 3 s apart, plus the first appearance of each OCR keyword."""
    chosen = []
    for r in sorted(rows, key=lambda r: -r["score"]):
        if r["score"] <= 0 or len(chosen) >= n:
            break
        if all(abs(r["t"] - c) >= 3 for c in chosen):
            chosen.append(r["t"])
    seen = set()
    for r in rows:
        for k in r["ocr_keywords"]:
            if k not in seen:
                seen.add(k)
                if all(abs(r["t"] - c) >= 2 for c in chosen):
                    chosen.append(r["t"])
    return sorted(chosen)[:n + 8]


def prepare(video: str, game: str, name: str = "", out_root: str = "dives", ocr: str = "auto") -> Path:
    info = signals.probe(video)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = Path(out_root) / f"{_slug(name or game)}-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    vs = signals.video_signals(video)
    peaks = signals.peak_times(vs, info["duration"])
    frames = extract_frames(video, out / "frames", peaks)
    au = signals.audio_signals(video) if info["has_audio"] else {"loudness_db": [], "onsets": [], "has_audio": False}
    ocr_data = signals.ocr_frames(frames) if ocr != "off" else {}
    rows = signals.per_second(vs, au, ocr_data, info["duration"])
    for r in rows:
        r["frame_t"] = peaks[r["t"]] if r["t"] < len(peaks) else r["t"]
    moments = pick_moments(rows)
    keyframes = extract_keyframes(video, moments, peaks, out / "keyframes")
    sheets = contact_sheets(frames, rows, set(moments), out / "sheets")
    strip = signal_strip(vs, au, [peaks[m] if m < len(peaks) else m for m in moments], out)

    (out / "timeline.json").write_text(json.dumps(rows, indent=1))
    (out / "moments.json").write_text(json.dumps([rows[m] for m in moments if m < len(rows)], indent=1))
    first = {}
    for r in rows:
        for k in r["ocr_keywords"]:
            first.setdefault(k, r["t"])
    pack = {
        "game": {"universe_id": str(game), "name": name}, "video": str(Path(video).name), "prepared": stamp,
        "duration_s": round(info["duration"], 1), "resolution": f"{info['width']}x{info['height']}",
        "has_audio": au.get("has_audio", False), "sfx_onsets_total": len(au.get("onsets", [])),
        "ocr": bool(ocr_data), "moments": moments, "first_seen_words": first,
        "flag_counts": {f: sum(f in r["flags"] for r in rows) for f in
                        ("vfx_burst", "flash", "center_popup", "ui_edge_change", "sfx_cluster")},
        "files": {"sheets": [Path(s).name for s in sheets], "keyframes": [Path(k).name for k in keyframes],
                  "strip": Path(strip).name if strip else None},
    }
    (out / "pack.json").write_text(json.dumps(pack, indent=2))
    tpl = template(str(game), name, stamp, info["duration"])
    (out / "findings.template.json").write_text(json.dumps(tpl, indent=2))
    (out / "ANALYSIS.md").write_text(_brief(pack, rows, moments), encoding="utf-8")
    return out


def _brief(pack: dict, rows: list[dict], moments: list[int]) -> str:
    lines = [
        f"# Dive brief — {pack['game']['name'] or pack['game']['universe_id']}",
        "",
        f"Recording {pack['duration_s']} s at {pack['resolution']} · audio: {'yes' if pack['has_audio'] else 'NO'} · "
        f"sound onsets: {pack['sfx_onsets_total']} · OCR: {'on' if pack['ocr'] else 'off'}",
        "",
        "Follow `analyzer/ANALYST.md`. Look at `strip.png`, every sheet in `sheets/`, then each keyframe. "
        "Fill `findings.template.json` and save it as `findings.json`.",
        "",
        "## Signal counts",
        "",
        "| Signal | Seconds |", "|---|---|",
    ]
    lines += [f"| {k} | {v} |" for k, v in pack["flag_counts"].items()]
    if pack["first_seen_words"]:
        lines += ["", "## First time each on-screen word appeared (OCR)", "", "| Word | At |", "|---|---|"]
        lines += [f"| {k} | {_fmt(v)} |" for k, v in sorted(pack["first_seen_words"].items(), key=lambda x: x[1])]
    lines += ["", "## Key moments to inspect (keyframes/)", "", "| At | Flags | Sounds | UI regions | Words |", "|---|---|---|---|---|"]
    for m in moments:
        if m < len(rows):
            r = rows[m]
            lines.append(f"| {_fmt(m)} | {', '.join(r['flags']) or '–'} | {r['sfx_onsets']} {','.join(r['sfx_tones'])} | "
                         f"{', '.join(r['ui_regions_changed']) or '–'} | {', '.join(r['ocr_keywords']) or '–'} |")
    return "\n".join(lines) + "\n"
