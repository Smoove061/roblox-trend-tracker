"""Store a validated deep dive and rebuild the shared pattern library.

    python -m analyzer ingest path/to/findings.json
    python -m analyzer patterns            # rebuild reports/patterns.md only

Storage (committed to the repo; recordings and frames stay local):
  data/deep_dives/<universe_id>/<date>.json   one file per dive
  data/patterns.csv                           every pattern, where it was seen, and whether it is a trend
  reports/patterns.md                         trends, the pattern library, and onboarding timing benchmarks
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

from tracker import storage as st

from .schema import PATTERN_TYPES, TIMING_KEYS, validate

DIVES = st.DATA / "deep_dives"
PATTERN_FIELDS = ["pattern", "type", "label", "status", "games", "rising_games", "dives", "first_seen", "last_seen", "game_ids"]
TREND_MIN_RISING_GAMES = 3


def ingest(path: str) -> Path:
    f = json.loads(Path(path).read_text(encoding="utf-8"))
    errs = validate(f)
    if errs:
        raise ValueError("findings are not valid:\n  - " + "\n  - ".join(errs))
    uid = str(f["game"]["universe_id"])
    games = st.load_games()
    if not f["game"].get("name") and uid in games:
        f["game"]["name"] = games[uid].get("name", "")
    dest = DIVES / uid / f"{f['recorded']}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(f, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    rebuild()
    return dest


def load_dives() -> list[dict]:
    out = []
    for p in sorted(DIVES.rglob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except ValueError:
            continue
    return out


def rising_games() -> set[str]:
    """Games currently growing or flagged on the watchlist."""
    out = set()
    for r in st.read_rows(st.REPORTS / "game_metrics.csv"):
        try:
            growing = float(r.get("growth_7d") or 0) > 0
        except ValueError:
            growing = False
        if growing or r.get("flags"):
            out.add(r["universe_id"])
    return out


def rebuild() -> dict:
    vocab = st.load_json(st.CONFIG / "patterns.json", {})
    labels = {pid: label for t in PATTERN_TYPES for pid, label in vocab.get(t, {}).items()}
    dives = load_dives()
    rising = rising_games()
    agg = {}
    for d in dives:
        uid = str(d["game"]["universe_id"])
        for p in d.get("patterns", []):
            a = agg.setdefault(p, {"games": set(), "dives": 0, "dates": []})
            a["games"].add(uid)
            a["dives"] += 1
            a["dates"].append(d["recorded"])
    rows = []
    for p, a in agg.items():
        r_games = len(a["games"] & rising)
        status = "trend" if r_games >= TREND_MIN_RISING_GAMES else ("new" if p not in labels else "seen")
        rows.append({"pattern": p, "type": p.split(":", 1)[0], "label": labels.get(p, p.split(":", 1)[1].replace("-", " ")),
                     "status": status, "games": len(a["games"]), "rising_games": r_games, "dives": a["dives"],
                     "first_seen": min(a["dates"]), "last_seen": max(a["dates"]), "game_ids": "|".join(sorted(a["games"]))})
    rows.sort(key=lambda r: (r["status"] != "trend", -r["rising_games"], -r["games"], r["pattern"]))
    st.write_rows(st.DATA / "patterns.csv", PATTERN_FIELDS, rows)
    _report(dives, rows)
    return {"dives": len(dives), "patterns": len(rows), "trends": sum(r["status"] == "trend" for r in rows)}


def _report(dives: list[dict], rows: list[dict]):
    games = st.load_games()
    tags = {}
    for t in st.read_rows(st.DATA / "tags.csv"):
        if t["tag_type"] == "niche" and float(t.get("confidence") or 0) >= 0.6:
            tags.setdefault(t["universe_id"], []).append(t["tag"])
    L = ["# Pattern library", "", f"{len(dives)} deep dive(s) · {len(rows)} patterns · "
         f"a pattern becomes a **trend** once it appears in {TREND_MIN_RISING_GAMES}+ rising games.", ""]

    def table(title, subset):
        L.extend([f"## {title}", ""])
        if not subset:
            L.extend(["None yet.", ""])
            return
        L.extend(["| Pattern | Type | Status | Games | Rising | Last seen |", "|---|---|---|---|---|---|"])
        for r in subset:
            L.append(f"| {r['label']} (`{r['pattern']}`) | {r['type']} | {r['status']} | {r['games']} | {r['rising_games']} | {r['last_seen']} |")
        L.append("")

    table("Trends", [r for r in rows if r["status"] == "trend"])
    for t in sorted(PATTERN_TYPES):
        table(f"{t.capitalize()} patterns", [r for r in rows if r["type"] == t and r["status"] != "trend"])

    # onboarding timing benchmarks: median seconds per timing, overall and per niche
    L.extend(["## Onboarding timing benchmarks (median seconds)", ""])
    if dives:
        groups = {"all games": dives}
        for d in dives:
            for n in tags.get(str(d["game"]["universe_id"]), []):
                groups.setdefault(n, []).append(d)
        L.extend(["| Group | Dives | " + " | ".join(k[:-2].replace("_", " ") for k in TIMING_KEYS) + " |",
                  "|---|---|" + "---|" * len(TIMING_KEYS)])
        for gname, ds in groups.items():
            cells = []
            for k in TIMING_KEYS:
                vals = [d["timings"][k] for d in ds if isinstance(d.get("timings", {}).get(k), (int, float))]
                cells.append(f"{statistics.median(vals):.0f}" if vals else "–")
            L.append(f"| {gname} | {len(ds)} | " + " | ".join(cells) + " |")
        L.append("")
    else:
        L.extend(["None yet.", ""])

    L.extend(["## Dives", "", "| Date | Game | Core action | Summary |", "|---|---|---|---|"])
    for d in sorted(dives, key=lambda d: d["recorded"], reverse=True):
        uid = str(d["game"]["universe_id"])
        place = games.get(uid, {}).get("root_place_id")
        name = (d["game"].get("name") or uid).replace("|", "/")
        link = f"[{name}](https://www.roblox.com/games/{place})" if place else name
        L.append(f"| {d['recorded']} | {link} | {d['loop'].get('core_action', '').replace('|', '/')} | "
                 f"{d.get('summary', '').replace('|', '/')[:160]} |")
    st.REPORTS.mkdir(parents=True, exist_ok=True)
    (st.REPORTS / "patterns.md").write_text("\n".join(L) + "\n", encoding="utf-8")


def dived_recently(days: int = 30, today=None) -> set[str]:
    """Universe IDs with a deep dive in the last `days` days (used by the watchlist's dive queue)."""
    from datetime import date, timedelta
    today = today or st.now_utc().date()
    out = set()
    for p in DIVES.glob("*/*.json"):
        try:
            if today - date.fromisoformat(p.stem) <= timedelta(days=days):
                out.add(p.parent.name)
        except ValueError:
            continue
    return out
