"""Thumbnail tracker: what the icons and thumbnails of the top games in each niche look like.

    python -m tracker thumbs                 # fetch/refresh images now and rebuild reports/thumbs.json
    python -m tracker vision-sheets [dir]    # contact sheets for Claude to look at (styles + niche thumbnails)
    python -m tracker vision-ingest <file>   # store what Claude saw

Each day, for every core mechanic, theme and genre, the 8 biggest games plus up to 4 young risers (60 days or less)
are picked. Their icon (the square tile in discovery) and first 3 gallery thumbnails (the wide images) are
downloaded from Roblox's thumbnail API, shrunk, and saved under data/thumbs/img/ (only re-downloaded when Roblox
serves a new image). Each icon and lead thumbnail gets measured: brightness, saturation, colourfulness, contrast,
busyness (edge density), centre focus, warm vs cool, dominant hue families and a 5-colour palette.

reports/thumbs.json then compares each niche to the whole tracked set and turns the differences into plain
direction ("go brighter", "one big central subject"), and adds what Claude saw in the images (characters, text,
emotion, composition) once a vision pass has run. Image work needs Pillow; without it the step skips itself.
"""
from __future__ import annotations

import hashlib
import io
import json
import statistics
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

from . import storage as st
from .sources import roblox

THUMBS = st.DATA / "thumbs"
IMG = THUMBS / "img"
INDEX = THUMBS / "index.csv"
METRICS = ["brightness", "saturation", "colorfulness", "contrast", "busyness", "center_focus", "warm", "dark", "white"]
INDEX_FIELDS = ["universe_id", "kind", "idx", "url", "file", "file_url", "fetched", "last_selected", *METRICS, "hues", "palette"]
HUES = [("red", 0, 15), ("orange", 15, 40), ("yellow", 40, 70), ("green", 70, 160), ("cyan", 160, 200),
        ("blue", 200, 255), ("purple", 255, 290), ("pink", 290, 345), ("red", 345, 361)]
ICON_SIZE, MEDIA_SIZE = (150, 150), (384, 216)
NICHE_DIMS = ("mechanic", "theme", "genre")


def pillow():
    try:
        from PIL import Image, ImageFilter, ImageStat  # noqa: F401
        return True
    except ImportError:
        return False


def _f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


# ---------- choosing games ----------

def select(ctx: dict, cfg: dict) -> dict:
    """{niche_key: {type, key, label, top: [uid], young: [uid]}} for every niche with at least 3 games."""
    tax = st.load_json(st.CONFIG / "taxonomy.json", {})
    labels = {"mechanic": {k: v["label"] for k, v in tax.get("mechanics", {}).items()},
              "theme": {k: v["label"] for k, v in tax.get("themes", {}).items()}}
    n_top, n_young = cfg.get("thumbs_top_per_niche", 8), cfg.get("thumbs_young_per_niche", 4)
    groups = defaultdict(list)
    for c in ctx.values():
        for dim in NICHE_DIMS:
            for k in c.get(dim, []):
                groups[(dim, k)].append(c)
    out = {}
    for (dim, k), cs in groups.items():
        if len(cs) < 3:
            continue
        cs = sorted(cs, key=lambda c: -c["ccu"])
        top = [c["id"] for c in cs[:n_top]]
        young = [c["id"] for c in cs if c["young"] and c["id"] not in top][:n_young]
        out[f"{dim}:{k}"] = {"type": dim, "key": k, "label": labels.get(dim, {}).get(k, k), "top": top, "young": young}
    return out


# ---------- fetching and measuring ----------

def _load_index() -> dict:
    return {(r["universe_id"], r["kind"], r["idx"]): r for r in st.read_rows(INDEX)}


def collect(http, cfg: dict, now, state: dict, ctx: dict | None = None) -> dict:
    if not pillow():
        return {"skipped": "Pillow not installed"}
    from . import trends
    ctx = ctx if ctx is not None else trends.load_context(now.date())
    niches = select(ctx, cfg)
    st.save_json(THUMBS / "niches.json", niches)
    union = sorted({u for n in niches.values() for u in n["top"] + n["young"]}, key=lambda u: -ctx[u]["ccu"])
    idx = _load_index()
    ts, today = st.iso(now), now.date().isoformat()
    have = {k[0] for k in idx}
    refreshed = 0
    if state.get("thumbs_urls_day") != today or any(u not in have for u in union):
        todo = union if state.get("thumbs_urls_day") != today else [u for u in union if u not in have]
        icons = roblox.game_icons(http, todo)
        media = roblox.game_media(http, todo, cfg.get("thumbs_media_per_game", 3))
        for u in todo:
            urls = [("icon", "0", icons.get(u))] + [("media", str(i), m) for i, m in enumerate(media.get(u, []))]
            for kind, i, url in urls:
                if not url:
                    continue
                row = idx.setdefault((u, kind, i), {f: "" for f in INDEX_FIELDS} | {"universe_id": u, "kind": kind, "idx": i})
                row["url"] = url
            # gallery shrank: forget the extra images
            for i in range(len(media.get(u, [])), 6):
                if u in media and (u, "media", str(i)) in idx:
                    _drop(idx.pop((u, "media", str(i))))
            refreshed += 1
        state["thumbs_urls_day"] = today
    sel = set(union)
    for r in idx.values():
        if r["universe_id"] in sel:
            r["last_selected"] = ts
    pending = [r for r in idx.values() if r["url"] and r["url"] != r.get("file_url") and r["universe_id"] in sel]
    pending.sort(key=lambda r: (r["kind"] != "icon", r["idx"], -ctx.get(r["universe_id"], {}).get("ccu", 0)))
    got, errors = 0, 0
    IMG.mkdir(parents=True, exist_ok=True)
    for r in pending[: cfg.get("thumbs_downloads_per_run", 300)]:
        if http.out_of_time():
            break
        try:
            raw = http.get_bytes(r["url"])
            jpg, m = process(raw, r["kind"], measure=r["kind"] == "icon" or r["idx"] == "0")
        except Exception as e:  # noqa: BLE001 - one bad image never stops the rest
            errors += 1
            if type(e).__name__ == "BudgetExceeded":
                break
            continue
        name = f"{r['universe_id']}_{'i' if r['kind'] == 'icon' else 'm' + r['idx']}.jpg"
        (IMG / name).write_bytes(jpg)
        r.update({"file": name, "file_url": r["url"], "fetched": ts, **{k: m.get(k, "") for k in METRICS},
                  "hues": "|".join(f"{h}:{v}" for h, v in m.get("hues", [])),
                  "palette": "|".join(f"{c}:{v}" for c, v in m.get("palette", []))})
        got += 1
    # games out of every niche for two weeks: delete their images so the repo doesn't grow forever
    cutoff = now - timedelta(days=cfg.get("thumbs_keep_days", 14))
    for k in [k for k, r in idx.items() if (st.parse_iso(r.get("last_selected", "")) or now) < cutoff]:
        _drop(idx.pop(k))
    st.write_rows(INDEX, INDEX_FIELDS, sorted(idx.values(), key=lambda r: (int(r["universe_id"]), r["kind"], r["idx"])))
    rep = report(ctx)
    left = sum(1 for r in idx.values() if r["url"] and r["url"] != r.get("file_url") and r["universe_id"] in sel)
    return {"niches": len(niches), "games": len(union), "urls_refreshed": refreshed, "downloaded": got,
            "errors": errors, "pending": left, "report_niches": rep}


def _drop(r):
    if r.get("file"):
        try:
            (IMG / r["file"]).unlink()
        except FileNotFoundError:
            pass


def process(raw: bytes, kind: str, measure: bool = True):
    """Decode, shrink to storage size, and (optionally) measure. Returns (jpeg bytes, metrics)."""
    from PIL import Image
    im = Image.open(io.BytesIO(raw))
    im.load()
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, im)
    im = im.convert("RGB")
    size = ICON_SIZE if kind == "icon" else MEDIA_SIZE
    if im.size != size:
        im = im.resize(size, Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80 if kind == "icon" else 74, optimize=True)
    return buf.getvalue(), (measure_image(im) if measure else {})


def _hue_family(h_deg):
    for name, lo, hi in HUES:
        if lo <= h_deg < hi:
            return name
    return "red"


def measure_image(im) -> dict:
    """Simple, explainable visual measures on a 64-px-wide copy. All 0..1 except colorfulness (0..~1.5)."""
    from PIL import ImageFilter, ImageStat
    w = 64
    small = im.resize((w, max(1, round(im.size[1] * w / im.size[0]))))
    px = list(small.getdata())
    hsv = list(small.convert("HSV").getdata())
    n = len(px)
    L = small.convert("L")
    lum = list(L.getdata())
    brightness = sum(lum) / n / 255
    contrast = statistics.pstdev(lum) / 128
    sat = sum(p[1] for p in hsv) / n / 255
    rg = [p[0] - p[1] for p in px]
    yb = [(p[0] + p[1]) / 2 - p[2] for p in px]
    colorfulness = (((statistics.pstdev(rg) ** 2 + statistics.pstdev(yb) ** 2) ** 0.5)
                    + 0.3 * ((statistics.fmean(rg) ** 2 + statistics.fmean(yb) ** 2) ** 0.5)) / 255 * 1.5
    edges = L.filter(ImageFilter.FIND_EDGES)
    ev = list(edges.getdata())
    W, H = edges.size
    busy = sum(ev) / n / 255
    centre = [ev[y * W + x] for y in range(H // 4, H - H // 4) for x in range(W // 4, W - W // 4)]
    center_focus = (sum(centre) / max(len(centre), 1) / 255) / busy if busy > 0 else 1.0
    fam = Counter()
    colored = 0
    dark = white = 0
    for h, s, v in hsv:
        if v < 51:
            dark += 1
        elif s < 38 and v > 217:
            white += 1
        elif s >= 64 and v >= 51:
            fam[_hue_family(h * 360 / 255)] += 1
            colored += 1
    warm = sum(fam[k] for k in ("red", "orange", "yellow", "pink")) / colored if colored else 0
    hues = [(k, round(v / n, 3)) for k, v in fam.most_common(4)]
    q = small.quantize(5, method=Image_MEDIANCUT())
    pal = q.getpalette()[:15]
    counts = sorted(q.getcolors() or [], reverse=True)
    palette = [("#%02x%02x%02x" % tuple(pal[i * 3:i * 3 + 3]), round(c / n, 3)) for c, i in counts[:5]]
    _ = ImageStat  # imported for callers that extend this
    return {"brightness": round(brightness, 3), "saturation": round(sat, 3), "colorfulness": round(colorfulness, 3),
            "contrast": round(contrast, 3), "busyness": round(busy, 3), "center_focus": round(center_focus, 2),
            "warm": round(warm, 3), "dark": round(dark / n, 3), "white": round(white / n, 3), "hues": hues, "palette": palette}


def Image_MEDIANCUT():
    from PIL import Image
    return getattr(Image, "Quantize", Image).MEDIANCUT


# ---------- niche report ----------

def _parse_pairs(s, cast=float):
    out = []
    for part in filter(None, (s or "").split("|")):
        k, _, v = part.rpartition(":")
        out.append((k, cast(v)))
    return out


def _med(rows, k):
    vals = [_f(r.get(k)) for r in rows if _f(r.get(k)) is not None]
    return round(statistics.median(vals), 3) if vals else None


def _summary(rows):
    if not rows:
        return None
    fams = Counter()
    lead = Counter()
    for r in rows:
        hs = _parse_pairs(r.get("hues"))
        for h, v in hs:
            fams[h] += v
        if hs:
            lead[hs[0][0]] += 1
    tot = sum(fams.values()) or 1
    return {"n": len(rows), "med": {k: _med(rows, k) for k in METRICS},
            "hues": [[h, round(v / tot, 2)] for h, v in fams.most_common(4)], "lead_hue": dict(lead.most_common(4)),
            "palette": _palette(rows)}


def _palette(rows, k=6):
    """Merge every image's palette into k representative colours (coarse 4-level bins, weighted by area)."""
    bins = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for r in rows:
        for hexc, share in _parse_pairs(r.get("palette")):
            try:
                rgb = [int(hexc[i:i + 2], 16) for i in (1, 3, 5)]
            except ValueError:
                continue
            key = tuple(c // 64 for c in rgb)
            b = bins[key]
            for i in range(3):
                b[i] += rgb[i] * share
            b[3] += share
    best = sorted(bins.values(), key=lambda b: -b[3])[:k]
    return ["#%02x%02x%02x" % tuple(round(b[i] / b[3]) for i in range(3)) for b in best if b[3] > 0]


def direction(s: dict | None, base: dict | None, young: dict | None, what: str) -> list[str]:
    """Plain-language rules from the numbers. Each line says what to do and the evidence."""
    if not s or not base:
        return []
    m, b = s["med"], base["med"]
    out = []

    def rel(k):
        return (m[k] / b[k] - 1) if m.get(k) is not None and b.get(k) else 0

    r = rel("brightness")
    if r >= 0.08:
        out.append(f"Go bright: top {what}s here are {r:.0%} brighter than the tracked average.")
    elif r <= -0.08:
        out.append(f"Go darker and moodier: top {what}s here are {-r:.0%} darker than average.")
    r = rel("saturation")
    if r >= 0.08:
        out.append(f"Push saturation: colours are {r:.0%} more saturated than average.")
    elif r <= -0.10:
        out.append(f"Keep colours muted: {-r:.0%} less saturated than average.")
    r = rel("busyness")
    if r >= 0.12:
        out.append(f"Packed compositions win: {r:.0%} more detail and edges than average (lots of items, effects, text).")
    elif r <= -0.12:
        out.append(f"Keep it clean: {-r:.0%} less clutter than average; one clear subject on a simple background.")
    cf = m.get("center_focus")
    if cf is not None and cf >= 1.25:
        out.append("Put the subject dead centre: detail is concentrated in the middle of the frame.")
    r = rel("contrast")
    if r >= 0.1:
        out.append(f"High contrast: {r:.0%} more light/dark contrast than average (strong outlines, rim light, dark backgrounds).")
    if s["hues"]:
        lead = sorted(s["lead_hue"].items(), key=lambda x: -x[1])
        if lead and lead[0][1] >= max(3, s["n"] * 0.35):
            out.append(f"Colour to own: {lead[0][0]} leads in {lead[0][1]} of {s['n']} {what}s.")
        warm = m.get("warm")
        if warm is not None and b.get("warm") is not None:
            if warm >= b["warm"] + 0.12:
                out.append(f"Warm palette (reds/oranges/yellows): {warm:.0%} of coloured area vs {b['warm']:.0%} average.")
            elif warm <= b["warm"] - 0.12:
                out.append(f"Cool palette (blues/greens/purples): only {warm:.0%} warm vs {b['warm']:.0%} average.")
    if young and young["n"] >= 2:
        ym = young["med"]
        shifts = []
        for k, up, down in (("brightness", "brighter", "darker"), ("saturation", "more saturated", "more muted"),
                            ("busyness", "busier", "cleaner"), ("contrast", "higher contrast", "flatter")):
            if ym.get(k) and m.get(k):
                d = ym[k] / m[k] - 1
                if abs(d) >= 0.12:
                    shifts.append(f"{up if d > 0 else down} ({d:+.0%})")
        if shifts:
            out.append(f"Shift underway: rising newcomers' {what}s are " + ", ".join(shifts[:3]) + " compared with the leaders.")
    if not out:
        out.append(f"No strong visual signal: {what}s here look like the tracked average, so style is not the differentiator.")
    return out


def report(ctx: dict | None = None) -> int:
    from . import styles, trends
    ctx = ctx if ctx is not None else trends.load_context()
    niches = st.load_json(THUMBS / "niches.json", {})
    idx = _load_index()
    by = defaultdict(dict)
    for (u, kind, i), r in idx.items():
        if r.get("file") and (kind == "icon" or i == "0"):
            by[u][kind] = r
    sty = styles.load(ctx)
    vision = st.load_json(st.REPORTS / "thumb_vision.json", {}).get("niches", {})
    all_icons = [d["icon"] for d in by.values() if "icon" in d]
    all_media = [d["media"] for d in by.values() if "media" in d]
    base = {"icon": _summary(all_icons), "media": _summary(all_media)}
    out = {}
    for key, n in niches.items():
        members = n["top"] + n["young"]
        games = []
        for u in members:
            c = ctx.get(u, {})
            s = sty.get(u, {})
            games.append({"id": u, "name": c.get("name", ""), "ccu": c.get("ccu", 0), "age": c.get("age"), "young": u in n["young"],
                          "icon": by.get(u, {}).get("icon", {}).get("file", ""),
                          "media": [r["file"] for (uu, kind, i), r in sorted(idx.items()) if uu == u and kind == "media" and r.get("file")],
                          "art_style": s.get("art_style", ""), "map_style": s.get("map_style", ""),
                          "style_source": s.get("source", "")})
        top_ic = [by[u]["icon"] for u in n["top"] if "icon" in by.get(u, {})]
        yng_ic = [by[u]["icon"] for u in n["young"] if "icon" in by.get(u, {})]
        top_md = [by[u]["media"] for u in n["top"] if "media" in by.get(u, {})]
        yng_md = [by[u]["media"] for u in n["young"] if "media" in by.get(u, {})]
        si, sm = _summary(top_ic), _summary(top_md)
        art = Counter(g["art_style"] for g in games if g["art_style"] and g["style_source"] in ("vision", "manual", "keywords"))
        maps = Counter(g["map_style"] for g in games if g["map_style"])
        out[key] = {
            "type": n["type"], "key": n["key"], "label": n["label"], "games": games,
            "icon": si, "icon_young": _summary(yng_ic), "media": sm, "media_young": _summary(yng_md),
            "icon_direction": direction(si, base["icon"], _summary(yng_ic), "icon"),
            "media_direction": direction(sm, base["media"], _summary(yng_md), "thumbnail"),
            "art_styles": dict(art.most_common()), "map_styles": dict(maps.most_common()),
            "members_hash": hashlib.sha1(",".join(sorted(members)).encode()).hexdigest()[:10],
            "vision": vision.get(key),
        }
    st.save_json(st.REPORTS / "thumbs.json", {"as_of": st.iso(st.now_utc()), "baseline": base, "niches": out,
                                              "styles": styles.labels()})
    return len(out)


# ---------- vision passes (Claude looks at contact sheets) ----------

NICHE_PROMPT = """You are looking at a contact sheet of Roblox game art from one niche ({label}).
Top section: the game ICONS (square tiles players see in discovery), biggest games first, each labelled with its number, CCU, and NEW if it is 60 days old or less.
Bottom section: each game's lead THUMBNAIL (the wide image on its page), same numbering.
Find the visual patterns these winning games share, so a new game in this niche knows exactly what its icon and thumbnail should look like.
Look for: subject (character, creature, item, face), expression/emotion, framing (close-up, full body, scene), text on the image (words, numbers, size), colours and lighting, backgrounds, effects (glow, sparkles, money, explosions), arrows/circles, art style (realistic, stylized cartoon, bright simulator, low poly, classic studded Roblox, voxel, anime, dark/gritty, meme collage), and anything the NEW games do differently from the leaders.
Count how many of the games show each pattern; only report patterns at least 3 games share, or a clear difference in the NEW games.
Reply with only JSON: {{"summary": "one sentence", "icon_patterns": [{{"pattern": "...", "count": "6/12"}}], "thumbnail_patterns": [{{"pattern": "...", "count": "5/10"}}], "art_style": "the dominant art style and how common", "text_usage": "how text is used", "newcomer_shift": "what NEW games do differently, or empty", "do": ["3-5 concrete instructions for our icon/thumbnail"], "avoid": ["1-3 things"], "direction": "two or three sentences: exactly what our icon and thumbnail should show"}}"""

STYLE_PROMPT = """Each numbered row is one Roblox game: its square icon, then up to 3 gallery screenshots.
For each game classify:
- art_style: one of {arts}
- map_style: one of {maps}
Use the screenshots for both (icons are often promo renders); if the screenshots are pure promo art, judge from what the world in them shows and lower confidence. Notes: under 12 words on what you saw (materials, lighting, camera, layout).
Reply with only JSON: [{{"n": 1, "art_style": "key", "map_style": "key", "confidence": 0.0-1.0, "notes": "..."}}]"""


def _plain(s):
    """Sheet labels: drop emoji and symbols the default font can't draw (names are in jobs.json in full)."""
    return "".join(ch for ch in s if ord(ch) < 0x2000).strip() or "?"


def _font(size):
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _open(name, size):
    from PIL import Image
    p = IMG / name
    if not name or not p.exists():
        return None
    im = Image.open(p).convert("RGB")
    return im.resize(size, Image.LANCZOS) if im.size != size else im


def niche_sheet(n: dict, path: Path) -> Path:
    """Icons in rows of 6 (150px) on top; lead thumbnails 3 per row (300x169) below. Labelled with number/CCU/NEW."""
    from PIL import Image, ImageDraw
    games = n["games"]
    cols, iw, mw, mh, lab, pad = 6, 150, 300, 169, 22, 8
    W = cols * (iw + pad) + pad
    irows = (len(games) + cols - 1) // cols
    media = [(i, g) for i, g in enumerate(games, 1) if g["media"]]
    mrows = (len(media) + 2) // 3
    H = 34 + irows * (iw + lab + pad) + 30 + mrows * (mh + lab + pad) + pad
    sheet = Image.new("RGB", (W, H), (24, 24, 28))
    d = ImageDraw.Draw(sheet)
    d.text((pad, 8), f"{_plain(n['label'])} - icons (top) and lead thumbnails (bottom)", fill=(255, 255, 255), font=_font(18))
    y0 = 34

    def tag(i, g):
        return f"{i}. {g['ccu']:,}{' NEW' if g['young'] else ''}"
    for i, g in enumerate(games, 1):
        r, c = divmod(i - 1, cols)
        x, y = pad + c * (iw + pad), y0 + r * (iw + lab + pad)
        d.text((x, y), tag(i, g), fill=(255, 220, 120) if g["young"] else (220, 220, 220), font=_font(14))
        im = _open(g["icon"], (iw, iw))
        if im:
            sheet.paste(im, (x, y + lab))
    y1 = y0 + irows * (iw + lab + pad) + 30
    d.text((pad, y1 - 24), "Lead thumbnails", fill=(255, 255, 255), font=_font(16))
    for j, (i, g) in enumerate(media):
        r, c = divmod(j, 3)
        x, y = pad + c * (mw + pad + 12), y1 + r * (mh + lab + pad)
        d.text((x, y), tag(i, g), fill=(255, 220, 120) if g["young"] else (220, 220, 220), font=_font(14))
        im = _open(g["media"][0], (mw, mh))
        if im:
            sheet.paste(im, (x, y + lab))
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path, "JPEG", quality=85)
    return path


def style_sheet(games: list[dict], path: Path) -> Path:
    from PIL import Image, ImageDraw
    iw, mw, mh, lab, pad = 150, 267, 150, 22, 8
    W = pad + iw + 3 * (mw + pad) + pad
    rowh = lab + mh + pad
    sheet = Image.new("RGB", (W, len(games) * rowh + pad), (24, 24, 28))
    d = ImageDraw.Draw(sheet)
    for i, g in enumerate(games):
        y = pad + i * rowh
        d.text((pad, y), f"{i + 1}. {_plain(g['name'])[:70]}", fill=(255, 255, 255), font=_font(15))
        im = _open(g["icon"], (mh, mh))
        if im:
            sheet.paste(im, (pad, y + lab))
        for j, m in enumerate(g["media"][:3]):
            im = _open(m, (mw, mh))
            if im:
                sheet.paste(im, (pad + iw + pad + j * (mw + pad), y + lab))
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path, "JPEG", quality=85)
    return path


def vision_sheets(out_dir: str = "vision", niche_limit: int = 8, style_limit: int = 48, per_sheet: int = 4) -> Path:
    """Write contact sheets + jobs.json describing what to look at and how to answer."""
    from . import styles
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rep = st.load_json(st.REPORTS / "thumbs.json", {})
    niches = rep.get("niches", {})
    tr = st.load_json(st.REPORTS / "trends.json", {})
    rising = [f"{d}:{i['key']}" for d in ("mechanic", "theme") for i in tr.get("rising", {}).get(d, [])]
    today = st.now_utc().date()

    def stale(k):
        v = niches[k].get("vision") or {}
        when = st.parse_iso(v.get("date", "") + "T00:00:00Z") if v.get("date") else None
        return not v or v.get("members_hash") != niches[k]["members_hash"] or when is None or (today - when.date()).days >= 7

    order = [k for k in rising if k in niches] + sorted((k for k in niches if k not in rising),
                                                        key=lambda k: -sum(g["ccu"] for g in niches[k]["games"]))
    jobs = {"niche_prompt": NICHE_PROMPT, "style_prompt": None, "niches": [], "styles": []}
    for k in [k for k in order if stale(k) and any(g["icon"] for g in niches[k]["games"])][:niche_limit]:
        f = niche_sheet(niches[k], out / f"niche_{k.replace(':', '_')}.jpg")
        jobs["niches"].append({"niche": k, "label": niches[k]["label"], "sheet": str(f), "members_hash": niches[k]["members_hash"],
                               "games": [{"n": i, "id": g["id"], "name": g["name"]} for i, g in enumerate(niches[k]["games"], 1)]})
    todo = styles.needs_vision(niches)[:style_limit]
    labs = styles.labels()
    jobs["style_prompt"] = STYLE_PROMPT.format(
        arts=", ".join(f"{k} ({v['desc']})" for k, v in labs["art_styles"].items()),
        maps=", ".join(f"{k} ({v['desc']})" for k, v in labs["map_styles"].items()))
    for s in range(0, len(todo), per_sheet):
        chunk = todo[s:s + per_sheet]
        f = style_sheet(chunk, out / f"styles_{s // per_sheet + 1:02d}.jpg")
        jobs["styles"].append({"sheet": str(f), "games": [{"n": i, "id": g["id"], "name": g["name"], "images_key": g["images_key"]}
                                                          for i, g in enumerate(chunk, 1)]})
    (out / "jobs.json").write_text(json.dumps(jobs, indent=1, ensure_ascii=False), encoding="utf-8")
    return out / "jobs.json"


def ingest_niches(results: dict, source: str = "vision") -> int:
    """results: {niche_key: {...answer..., members_hash}} -> reports/thumb_vision.json (merged)."""
    path = st.REPORTS / "thumb_vision.json"
    cur = st.load_json(path, {"niches": {}})
    n = 0
    for k, v in results.items():
        if not isinstance(v, dict) or not v.get("direction"):
            continue
        keep = {f: v.get(f) for f in ("summary", "icon_patterns", "thumbnail_patterns", "art_style", "text_usage",
                                       "newcomer_shift", "do", "avoid", "direction", "members_hash")}
        keep["date"] = v.get("date") or st.now_utc().date().isoformat()
        keep["source"] = source
        cur["niches"][k] = keep
        n += 1
    st.save_json(path, cur)
    report()
    return n
