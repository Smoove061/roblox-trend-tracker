"""Build the dashboard: one self-contained HTML page with the data inlined.

    python -m tracker dashboard [out.html]      # default: site/index.html

The GitHub workflow builds it after every run and publishes it with GitHub Pages,
so it is never committed (no repo growth). Locally, open the file in any browser.
"""
from __future__ import annotations

import html
import json
from datetime import date, timedelta
from pathlib import Path

from . import storage as st

SPARK_HOURS = 72


def _f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def build_data() -> dict:
    now = st.now_utc()
    games = st.load_games()
    active = {u: g for u, g in games.items() if str(g.get("active")) == "1"}
    niches = {}
    for t in st.read_rows(st.DATA / "tags.csv"):
        if t["tag_type"] == "niche" and _f(t.get("confidence"), 0) >= 0.6:
            niches.setdefault(t["universe_id"], []).append(t["tag"])

    # hourly CCU for the last SPARK_HOURS hours, binned by hour (last reading in each hour wins)
    start = now - timedelta(hours=SPARK_HOURS)
    hours = [(start + timedelta(hours=i + 1)).strftime("%Y-%m-%dT%H") for i in range(SPARK_HOURS)]
    hidx = {h: i for i, h in enumerate(hours)}
    spark = {}
    votes = {}
    d = start.date()
    while d <= now.date():
        for r in st.read_rows(st.day_path("snapshots", d.isoformat())):
            uid = r["universe_id"]
            i = hidx.get(r["ts"][:13])
            if i is not None and uid in active:
                spark.setdefault(uid, [None] * SPARK_HOURS)[i] = int(_f(r["playing"], 0))
            if r.get("up_votes") not in ("", None):
                votes[uid] = (_f(r["up_votes"], 0), _f(r["down_votes"], 0), _f(r.get("favorites")), _f(r.get("visits")))
        d += timedelta(days=1)

    rows = []
    for uid, g in active.items():
        s = spark.get(uid, [None] * SPARK_HOURS)
        known = [(i, v) for i, v in enumerate(s) if v is not None]
        ccu = int(_f(g.get("last_ccu"), 0))
        ch24 = None
        if known:
            last_i, last_v = known[-1]
            ref = [v for i, v in known if last_i - 26 <= i <= last_i - 22]
            if ref and ref[0] > 0:
                ch24 = round((last_v - ref[0]) / ref[0], 4)
        created = st.parse_iso(g.get("created", ""))
        up, down, fav, visits = votes.get(uid, (None, None, None, None))
        rows.append({
            "id": uid, "place": g.get("root_place_id", ""), "name": g.get("name", ""), "creator": g.get("creator_name", ""),
            "genre": g.get("genre_l1") or "Unknown", "sub": g.get("genre_l2", ""), "niches": sorted(niches.get(uid, [])),
            "ccu": ccu, "peak": int(_f(g.get("peak_ccu"), 0)), "ch24": ch24,
            "age": (now.date() - created.date()).days if created else None,
            "like": round(up / (up + down), 3) if up is not None and down is not None and up + down > 0 else None,
            "fav": int(fav) if fav else None, "visits": int(visits) if visits else None,
            "spark": s,
        })
    rows.sort(key=lambda r: -r["ccu"])

    # cohort line: combined CCU of games with a reading in every one of the last N hours that have data
    hours_with_data = [i for i in range(SPARK_HOURS) if any(r["spark"][i] is not None for r in rows)]
    cohort = [r for r in rows if hours_with_data and all(r["spark"][i] is not None for i in hours_with_data)]
    line = [{"h": hours[i] + ":00Z", "v": sum(r["spark"][i] for r in cohort)} for i in hours_with_data]

    genres = {}
    for r in rows:
        g = genres.setdefault(r["genre"], {"name": r["genre"], "ccu": 0, "games": 0, "top": r["name"]})
        g["ccu"] += r["ccu"]
        g["games"] += 1
    niche_agg = {}
    for r in rows:
        for n in r["niches"]:
            a = niche_agg.setdefault(n, {"name": n, "ccu": 0, "games": 0, "top": r["name"], "top_ccu": r["ccu"]})
            a["ccu"] += r["ccu"]
            a["games"] += 1
    for a in niche_agg.values():
        a["top_share"] = round(a["top_ccu"] / a["ccu"], 3) if a["ccu"] else 0

    wl = st.load_json(st.REPORTS / "watchlist.json", {})
    yt_rows = []
    for i in range(0, 8):
        day = (now.date() - timedelta(days=i)).isoformat()
        got = st.read_rows(st.day_path("youtube", day))
        if got:
            yt_rows = [{"id": y["universe_id"], "name": games.get(y["universe_id"], {}).get("name", ""),
                        "videos": int(_f(y["videos"], 0)), "views": int(_f(y["views"], 0)),
                        "top": y.get("top_video_id", ""), "date": day} for y in got]
            break
    yt_rows.sort(key=lambda y: -y["views"])
    rollup_days = len({p.stem for p in (st.DATA / "rollups").rglob("*.csv")})
    return {
        "updated": st.iso(now), "days_of_data": rollup_days, "floor": st.settings().get("ccu_floor", 300),
        "games": rows, "line": line, "cohort_size": len(cohort),
        "genres": sorted(genres.values(), key=lambda g: -g["ccu"]),
        "niches": sorted(niche_agg.values(), key=lambda n: -n["ccu"]),
        "watch": wl.get("games", []), "watch_niches": wl.get("niches", []),
        "youtube": yt_rows[:40],
    }


def build(out: str = "site/index.html") -> Path:
    data = build_data()
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    page = TEMPLATE.replace("__DATA__", payload).replace("__UPDATED__", html.escape(data["updated"]))
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(page, encoding="utf-8")
    return p


TEMPLATE = (Path(__file__).with_name("dashboard_template.html")).read_text(encoding="utf-8") \
    if (Path(__file__).with_name("dashboard_template.html")).exists() else "__DATA__"
