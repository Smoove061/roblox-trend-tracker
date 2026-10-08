"""Game visual styles: art style (realistic, stylized, classic studded, low poly, ...) and map style
(plots/bases, hub + zones, linear course, arena, ...). Keys and descriptions live in config/styles.json.

Where labels come from, best first:
  manual    config/styles_manual.json  {"<universe_id>": {"art_style": "...", "map_style": "..."}}
  vision    data/styles.csv, written by `python -m tracker vision-ingest` after Claude looks at each game's
            icon + gallery screenshots (contact sheets from `vision-sheets`); redone when the images change
  keywords  style words in the game's name or description ("realistic", "low poly", "classic", ...)
  inferred  map style only, from the game's core mechanics (tycoon -> plots/bases, obby -> linear course, ...)
"""
from __future__ import annotations

import hashlib
import re

from . import storage as st

STYLE_FIELDS = ["universe_id", "art_style", "map_style", "confidence", "notes", "images_key", "source", "updated"]
PATH = st.DATA / "styles.csv"


def labels() -> dict:
    cfg = st.load_json(st.CONFIG / "styles.json", {})
    return {"art_styles": {k: {"label": v["label"], "desc": v["desc"]} for k, v in cfg.get("art_styles", {}).items()},
            "map_styles": {k: {"label": v["label"], "desc": v["desc"]} for k, v in cfg.get("map_styles", {}).items()}}


def _kw(text, words):
    return any(re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", text) for w in words if w)


def load(ctx: dict) -> dict:
    """{universe_id: {art_style, map_style, source, confidence, notes}} for every game in ctx (best source wins)."""
    cfg = st.load_json(st.CONFIG / "styles.json", {})
    arts, maps = cfg.get("art_styles", {}), cfg.get("map_styles", {})
    games = st.load_games()
    vision = {r["universe_id"]: r for r in st.read_rows(PATH)}
    manual = st.load_json(st.CONFIG / "styles_manual.json", {})
    out = {}
    for uid, c in ctx.items():
        g = games.get(uid, {})
        text = f"{g.get('name', '')} {g.get('description', '')}".lower()
        rec = {"art_style": "", "map_style": "", "source": "", "art_source": "", "map_source": "", "confidence": "", "notes": ""}
        for k, v in arts.items():
            if _kw(text, v.get("keywords", [])):
                rec.update(art_style=k, art_source="keywords")
                break
        for k, v in maps.items():  # mechanics first (stronger signal than a passing word), keywords as a fallback
            if set(v.get("from_mechanics", [])) & set(c.get("mechanic", [])):
                rec.update(map_style=k, map_source="inferred")
                break
        else:
            for k, v in maps.items():
                if _kw(text, v.get("keywords", [])):
                    rec.update(map_style=k, map_source="keywords")
                    break
        v = vision.get(uid)
        if v:
            if v.get("art_style") in arts:
                rec.update(art_style=v["art_style"], art_source="vision")
            if v.get("map_style") in maps:
                rec.update(map_style=v["map_style"], map_source="vision")
            rec.update(confidence=v.get("confidence", ""), notes=v.get("notes", ""))
        m = manual.get(uid) or {}
        if m.get("art_style") in arts:
            rec.update(art_style=m["art_style"], art_source="manual")
        if m.get("map_style") in maps:
            rec.update(map_style=m["map_style"], map_source="manual")
        rec["source"] = rec["art_source"] or rec["map_source"]
        out[uid] = rec
    return out


def images_key(files: list[str], index: dict) -> str:
    urls = [index.get(f, "") for f in files]
    return hashlib.sha1("|".join(urls).encode()).hexdigest()[:10]


def needs_vision(niches: dict) -> list[dict]:
    """Games in the thumbnail set that have screenshots but no vision label for their current images, biggest first."""
    from .thumbs import INDEX
    file_url = {r["file"]: r["file_url"] for r in st.read_rows(INDEX) if r.get("file")}
    done = {r["universe_id"]: r.get("images_key") for r in st.read_rows(PATH)}
    seen, out = set(), []
    for n in niches.values():
        for g in n["games"]:
            if g["id"] in seen or not g["media"]:
                continue
            seen.add(g["id"])
            key = images_key([g["icon"], *g["media"]], file_url)
            if done.get(g["id"]) != key:
                out.append({**g, "images_key": key})
    return sorted(out, key=lambda g: -g["ccu"])


def ingest(results: list[dict], source: str = "vision") -> int:
    """results: [{id, art_style, map_style, confidence, notes, images_key}] -> data/styles.csv (upsert, validated)."""
    lab = labels()
    rows = {r["universe_id"]: r for r in st.read_rows(PATH)}
    n = 0
    for r in results:
        uid = str(r.get("id", "")).strip()
        if not uid.isdigit():
            continue
        art = r.get("art_style") if r.get("art_style") in lab["art_styles"] else ""
        mp = r.get("map_style") if r.get("map_style") in lab["map_styles"] else ""
        if not art and not mp:
            continue
        try:
            conf = max(0.0, min(1.0, float(r.get("confidence", 0.7))))
        except (TypeError, ValueError):
            conf = 0.7
        rows[uid] = {"universe_id": uid, "art_style": art, "map_style": mp, "confidence": round(conf, 2),
                     "notes": str(r.get("notes", ""))[:160], "images_key": r.get("images_key", ""), "source": source,
                     "updated": st.iso(st.now_utc())}
        n += 1
    st.write_rows(PATH, STYLE_FIELDS, sorted(rows.values(), key=lambda r: int(r["universe_id"])))
    return n
