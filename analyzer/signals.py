"""Measure a gameplay recording, second by second.

Video: motion, brightness flashes, colour saturation, and which screen region
changed (top bar, bottom bar, left rail, right rail, centre), sampled at 4 fps.
Audio: loudness and sound onsets (spectral flux), each onset tagged low / mid /
bright from its spectral centroid. Optional OCR of on-screen words.

Needs ffmpeg + numpy. Tesseract is optional.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import numpy as np

W, H, FPS = 160, 90, 4
SR = 16000
REGIONS = {  # (y0, y1, x0, x1) as fractions of the frame
    "top": (0.0, 0.14, 0.0, 1.0),
    "bottom": (0.84, 1.0, 0.0, 1.0),
    "left": (0.14, 0.84, 0.0, 0.16),
    "right": (0.14, 0.84, 0.84, 1.0),
    "center": (0.25, 0.75, 0.25, 0.75),
}
OCR_KEYWORDS = {
    "robux": r"\brobux\b|\bR\$",
    "buy": r"\bbuy\b|\bpurchase\b",
    "shop": r"\bshop\b|\bstore\b",
    "rebirth": r"\brebirth\b|\bprestige\b",
    "upgrade": r"\bupgrade\b|\blevel up\b",
    "claim": r"\bclaim\b|\bcollect\b",
    "daily": r"\bdaily\b|\bstreak\b",
    "spin": r"\bspin\b|\bwheel\b",
    "offer": r"\boffer\b|\bsale\b|\b\d+% off\b|\blimited\b",
    "luck": r"\bluck\b|\blucky\b",
    "sell": r"\bsell\b",
    "hatch": r"\bhatch\b|\begg\b|\bopen\b",
    "trade": r"\btrade\b",
    "steal": r"\bsteal\b",
    "multiplier": r"\b\d+x\b|\bx\d+\b",
    "rare": r"\brare\b|\bepic\b|\blegendary\b|\bmythic\b|\bsecret\b|\bgodly\b",
    "big_number": r"\b\d+(\.\d+)?\s?[KMBT]\b",
}


def probe(video: str) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", video],
                         capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    a = next((s for s in info["streams"] if s.get("codec_type") == "audio"), None)
    return {"duration": float(info["format"].get("duration", 0)), "width": v.get("width"), "height": v.get("height"),
            "fps": v.get("r_frame_rate"), "has_audio": a is not None}


def _frames(video: str) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", video, "-vf", f"fps={FPS},scale={W}:{H}",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    n = len(raw) // (W * H * 3)
    return np.frombuffer(raw[: n * W * H * 3], dtype=np.uint8).reshape(n, H, W, 3).astype(np.float32) / 255.0


def video_signals(video: str) -> dict:
    f = _frames(video)
    if len(f) < 2:
        return {"t": [], "motion": [], "brightness": [], "saturation": [], "regions": {k: [] for k in REGIONS}}
    mx, mn = f.max(axis=3), f.min(axis=3)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0).mean(axis=(1, 2))
    bright = f.mean(axis=(1, 2, 3))
    diff = np.abs(np.diff(f, axis=0)).mean(axis=3)  # (n-1, H, W)
    motion = np.concatenate([[0.0], diff.mean(axis=(1, 2))])
    regions = {}
    for name, (y0, y1, x0, x1) in REGIONS.items():
        r = diff[:, int(y0 * H):int(y1 * H), int(x0 * W):int(x1 * W)].mean(axis=(1, 2))
        regions[name] = np.concatenate([[0.0], r]).tolist()
    bright_up = np.concatenate([[0.0], np.maximum(0, np.diff(bright))])  # flashes are brightness *increases*
    t = (np.arange(len(f)) / FPS).tolist()
    return {"t": t, "motion": motion.tolist(), "brightness": bright.tolist(), "bright_up": bright_up.tolist(),
            "saturation": sat.tolist(), "regions": regions}


def peak_times(vs: dict, duration: float) -> list[float]:
    """For each second, the 4-fps sample time with the most going on (motion + flash), so short effects aren't missed."""
    t = np.array(vs["t"])
    if not len(t):
        return []
    m = np.array(vs["motion"])
    b = np.array(vs.get("bright_up", [0] * len(t)))
    score = m / (np.median(m) + 1e-4) + 3 * b / (np.median(b) + 1e-3)
    out = []
    for s in range(int(np.ceil(duration))):
        sel = np.where((t >= s) & (t < s + 1))[0]
        out.append(round(float(t[sel[np.argmax(score[sel])]]), 2) if len(sel) else float(s))
    return out


def audio_signals(video: str) -> dict:
    proc = subprocess.run(["ffmpeg", "-v", "error", "-i", video, "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                          capture_output=True)
    x = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    if len(x) < SR:
        return {"loudness_db": [], "onsets": [], "has_audio": False}
    hop, win = 512, 1024
    n = 1 + (len(x) - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    frames = x[idx] * np.hanning(win)[None, :]
    mag = np.abs(np.fft.rfft(frames, axis=1))
    freqs = np.fft.rfftfreq(win, 1 / SR)
    flux = np.maximum(0, np.diff(mag, axis=0)).sum(axis=1)
    flux = np.concatenate([[0.0], flux])
    rms = np.sqrt((frames ** 2).mean(axis=1))
    # onset peaks: local maxima above an adaptive threshold, at least 150 ms apart
    thr = np.median(flux) + 2.5 * flux.std()
    min_gap = int(0.15 * SR / hop)
    onsets, last = [], -min_gap
    for i in range(1, n - 1):
        if flux[i] > thr and flux[i] >= flux[i - 1] and flux[i] >= flux[i + 1] and i - last >= min_gap:
            spec = mag[i]
            centroid = float((spec * freqs).sum() / max(spec.sum(), 1e-9))
            tone = "bright" if centroid > 3000 else "low" if centroid < 600 else "mid"
            onsets.append({"t": round(i * hop / SR, 2), "strength": round(float(flux[i] / (thr or 1)), 2),
                           "centroid_hz": round(centroid), "tone": tone,
                           "loudness_db": round(float(20 * np.log10(max(rms[i], 1e-6))), 1)})
            last = i
    per_sec = int(SR / hop)
    loud = [round(float(20 * np.log10(max(rms[s:s + per_sec].mean(), 1e-6))), 1) for s in range(0, n, per_sec)]
    return {"loudness_db": loud, "onsets": onsets, "has_audio": bool(rms.max() > 1e-4)}


def ocr_frames(frame_paths: list[str]) -> dict[int, dict]:
    """Words and keyword hits per second-frame (index = second). Skipped if tesseract is missing."""
    if not shutil.which("tesseract"):
        return {}
    out = {}
    for sec, p in enumerate(frame_paths):
        try:
            text = subprocess.run(["tesseract", p, "stdout", "--psm", "11"], capture_output=True, text=True,
                                  timeout=20).stdout
        except (subprocess.SubprocessError, OSError):
            continue
        text = " ".join(text.split())
        hits = [k for k, rx in OCR_KEYWORDS.items() if re.search(rx, text, re.I)]
        if text:
            out[sec] = {"text": text[:300], "keywords": hits}
    return out


def per_second(vs: dict, au: dict, ocr: dict, duration: float) -> list[dict]:
    """Collapse everything into one row per second, with z-scored flags for spikes."""
    secs = int(np.ceil(duration))
    t = np.array(vs["t"]) if vs["t"] else np.array([])

    def agg(series, fn=np.max):
        arr = np.array(series)
        return [float(fn(arr[(t >= s) & (t < s + 1)])) if ((t >= s) & (t < s + 1)).any() else 0.0 for s in range(secs)]

    motion, bright, sat = agg(vs["motion"]), agg(vs["brightness"], np.mean), agg(vs["saturation"], np.max)
    bup = agg(vs.get("bright_up", [0] * len(t)))
    regions = {k: agg(v) for k, v in vs["regions"].items()}

    def z(xs):
        """Robust z-score (median / MAD), so a few big spikes don't hide the smaller ones."""
        a = np.array(xs, dtype=float)
        if not len(a):
            return []
        med = np.median(a)
        mad = 1.4826 * np.median(np.abs(a - med))
        scale = max(mad, 0.05 * (np.abs(med) + 1e-3), 1e-4)
        return ((a - med) / scale).tolist()

    mz, bz, sz = z(motion), z(bup), z(sat)
    rz = {k: z(v) for k, v in regions.items()}
    edges = ("top", "bottom", "left", "right")
    onsets = au.get("onsets", [])
    rows = []
    for s in range(secs):
        os_ = [o for o in onsets if s - 0.05 <= o["t"] < s + 0.95]  # onsets are timed at frame start; allow a hair early
        edge_mean = np.mean([regions[k][s] for k in edges]) if secs else 0
        # a region counts as "UI changed" when it spikes AND changed clearly more than the frame as a whole
        ui = [k for k in edges if rz[k][s] > 5 and regions[k][s] > 1.8 * motion[s]]
        center_change = rz["center"][s] > 4 and regions["center"][s] > 2.5 * max(edge_mean, 1e-4)  # popup, not a full-screen flash
        flags = []
        if mz[s] > 4 and (bz[s] > 4 or sz[s] > 3) and not center_change:  # a popup opening is UI, not VFX
            flags.append("vfx_burst")
        if bz[s] > 6 and bup[s] > 0.06 and not center_change:  # a real (6%+) brightening, not noise or a popup
            flags.append("flash")
        if center_change:
            flags.append("center_popup")
        if ui:
            flags.append("ui_edge_change")
        if len(os_) >= 3:
            flags.append("sfx_cluster")
        o = ocr.get(s, {})
        score = (2 * ("vfx_burst" in flags) + 2 * center_change + len(ui) + min(len(os_), 4) * 0.5
                 + ("flash" in flags) + 1.5 * bool(o.get("keywords")))
        rows.append({
            "t": s, "motion": round(motion[s], 4), "brightness": round(bright[s], 3), "saturation": round(sat[s], 3),
            "ui_regions_changed": ui, "sfx_onsets": len(os_), "sfx_tones": sorted({x["tone"] for x in os_}),
            "ocr_keywords": o.get("keywords", []), "ocr_text": o.get("text", ""), "flags": flags, "score": round(score, 2),
        })
    return rows
