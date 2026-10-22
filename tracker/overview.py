"""Weekly and monthly market overview, built from daily rollups.

    python -m tracker overview

For each window (last 7 and last 30 days) it reports, per genre, subgenre, mechanic, theme and title formula:
  change     how the category's average CCU moved from the start of the window to the end, measured on the same
             games at both ends (games the tracker found mid-window, or that launched mid-window, don't inflate it)
  share      its share of all tracked CCU each day (for sparklines; share is fair even as tracking grows)
  visits     plays gained during the window
  launches   games created inside the window, and the CCU they hold at the end
plus headline numbers, the biggest climbers and fallers, and the top new launches.
Writes reports/overview.json and reports/overview.md.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from . import storage as st

WINDOWS = (7, 30)
BLOCK = 7  # compare whole-week averages
DIMS = ("genre", "subgenre", "mechanic", "theme", "formula")
MIN_START_CCU = {"genre": 3000, "subgenre": 2000, "mechanic": 2000, "theme": 2000, "formula": 2000}
MIN_GAMES = 3


def _f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def latest_rollup_day(today: date | None = None, look_back: int = 10) -> date | None:
    today = today or st.now_utc().date()
    for i in range(look_back):
        d = today - timedelta(days=i)
        p = st.day_path("rollups", d.isoformat())
        if p.exists() or p.with_name(p.name + ".gz").exists():
            return d
    return None


def load_series(end: date, days: int) -> tuple[list[str], dict[str, dict[str, dict]]]:
    """Days that have rollups (oldest first) and {universe_id: {day: {"ccu", "visits"}}}."""
    have, series = [], {}
    for i in range(days - 1, -1, -1):
        d = (end - timedelta(days=i)).isoformat()
        rows = st.read_rows(st.day_path("rollups", d))
        if not rows:
            continue
        have.append(d)
        for r in rows:
            series.setdefault(r["universe_id"], {})[d] = {"ccu": _f(r.get("avg_ccu"), 0.0), "visits": _f(r.get("visits_gained"), 0.0)}
    return have, series


def _labels():
    tax = st.load_json(st.CONFIG / "taxonomy.json", {})
    return {"mechanic": {k: v["label"] for k, v in tax.get("mechanics", {}).items()},
            "theme": {k: v["label"] for k, v in tax.get("themes", {}).items()},
            "formula": {k: v["label"] for k, v in tax.get("title_formulas", {}).items()}}


def _game_meta() -> dict[str, dict]:
    games = st.load_games()
    tags = defaultdict(lambda: defaultdict(list))
    for t in st.read_rows(st.DATA / "tags.csv"):
        if _f(t.get("confidence"), 0) >= 0.6 and t["tag_type"] in ("mechanic", "theme", "formula"):
            tags[t["universe_id"]][t["tag_type"]].append(t["tag"])
    out = {}
    for uid, g in games.items():
        created = st.parse_iso(g.get("created", ""))
        t = tags.get(uid, {})
        out[uid] = {"name": g.get("name", ""), "place": g.get("root_place_id", ""),
                    "created": created.date() if created else None,
                    "genre": [g["genre_l1"]] if g.get("genre_l1") else [], "subgenre": [g["genre_l2"]] if g.get("genre_l2") else [],
                    "mechanic": sorted(set(t.get("mechanic", []))), "theme": sorted(set(t.get("theme", []))),
                    "formula": sorted(set(t.get("formula", [])))}
    return out


def compute_window(window: int, end: date, meta: dict, labels: dict) -> dict:
    """Compare whole weeks so the weekend swing (Roblox CCU is far higher on weekends) cancels out:
    7 days = the last 7 days vs the 7 before them; 30 days = the last 7 days vs the first full week of the window."""
    span = window + BLOCK if window <= BLOCK else window
    all_days, series = load_series(end, span)
    days = [d for d in all_days if date.fromisoformat(d) > end - timedelta(days=window)]
    out = {"window": window, "end": end.isoformat(), "days": days, "days_of_data": len(days)}
    # the tracker's first days covered far fewer games; only compare days where coverage was near full
    count = {d: 0 for d in all_days}
    for s in series.values():
        for d in s:
            count[d] += 1
    full = [d for d in all_days if count[d] >= 0.8 * max(count.values() or [1])]
    tail = all_days[-BLOCK:]
    head = [d for d in full if d not in tail and (window > BLOCK or d not in days)][:BLOCK]
    if len(days) < 2 or len(head) < 3 or len(tail) < 3:
        out["empty"] = True
        out["note"] = f"Needs at least {window + BLOCK if window <= BLOCK else 10} days of data; have {len(all_days)}."
        return out
    # use the same weekdays on both sides (matters while history is short and the early block isn't a full week)
    wd = lambda d: date.fromisoformat(d).weekday()  # noqa: E731
    common = {wd(d) for d in head} & {wd(d) for d in tail}
    head, tail = [d for d in head if wd(d) in common], [d for d in tail if wd(d) in common]
    out["share_days"] = [d for d in days if d in full]  # sparklines skip the tracker's thin first days
    out["compare"] = {"from": [head[0], head[-1]], "to": [tail[0], tail[-1]], "days_each": len(head), "full_weeks": len(head) == BLOCK}
    start_day = date.fromisoformat(head[0])

    total_by_day = {d: 0.0 for d in days}
    for s in series.values():
        for d, v in s.items():
            if d in total_by_day:
                total_by_day[d] += v["ccu"]

    def launched(uid):
        c = meta.get(uid, {}).get("created")
        return c is not None and c >= start_day

    def block_avg(s, block):
        vals = [s[d]["ccu"] for d in block if d in s]
        return sum(vals) / len(block) if len(vals) >= (len(block) + 1) // 2 else None  # missing days count as 0 players

    # per game: start/end CCU (weekly averages), visits in the window, launches
    games = []
    for uid, s in series.items():
        m = meta.get(uid)
        if not m:
            continue
        a, b = block_avg(s, head), block_avg(s, tail)
        visits = sum(s[d]["visits"] for d in days if d in s)
        games.append({"id": uid, "name": m["name"], "place": m["place"], "start": a, "end": b, "visits": visits,
                      "launched": launched(uid), "matched": a is not None and b is not None and not launched(uid),
                      "created": m["created"].isoformat() if m["created"] else None})

    matched = [g for g in games if g["matched"]]
    m_start, m_end = sum(g["start"] for g in matched), sum(g["end"] for g in matched)
    market = m_end / m_start - 1 if m_start else 0.0
    launches = sorted((g for g in games if g["launched"] and g["end"]), key=lambda g: -g["end"])
    end_total = sum(g["end"] or 0 for g in games)
    out["headline"] = {
        "ccu_change": round(m_end / m_start - 1, 4) if m_start else None,
        "ccu_end": round(end_total), "visits": round(sum(g["visits"] for g in games)),
        "launches": len(launches), "launch_ccu": round(sum(g["end"] for g in launches)),
        "launch_share": round(sum(g["end"] for g in launches) / end_total, 4) if end_total else None,
        "games": len(games), "matched_games": len(matched),
    }

    def gm(g):
        return {"name": g["name"], "place": g["place"], "start": round(g["start"] or 0), "end": round(g["end"] or 0),
                "change": round(g["end"] / g["start"] - 1, 3) if g["start"] else None, "created": g["created"]}

    movers = [g for g in matched if g["start"] >= 300]
    out["climbers"] = [gm(g) for g in sorted(movers, key=lambda g: -(g["end"] - g["start"]))[:8] if g["end"] > g["start"]]
    # new games (30 days old or less at the end of the window) that gained the most players during it
    young_cut = end - timedelta(days=30)
    young = [g for g in games if g["created"] and date.fromisoformat(g["created"]) >= young_cut and g["end"]]
    out["breakouts"] = [gm(g) for g in sorted(young, key=lambda g: -(g["end"] - (g["start"] or 0)))[:8]]
    out["fallers"] = [gm(g) for g in sorted(movers, key=lambda g: g["end"] - g["start"])[:8] if g["end"] < g["start"]]
    out["launches"] = [gm(g) for g in launches[:8]]

    cats = {}
    for dim in DIMS:
        groups = defaultdict(list)
        for g in games:
            for k in meta[g["id"]][dim]:
                groups[k].append(g)
        rows = []
        for k, gs in groups.items():
            mg = [g for g in gs if g["matched"]]
            a, b = sum(g["start"] for g in mg), sum(g["end"] for g in mg)
            lg = [g for g in gs if g["launched"] and g["end"]]
            share = []
            for d in out["share_days"]:
                tot = total_by_day[d]
                v = sum(series[g["id"]][d]["ccu"] for g in gs if d in series[g["id"]])  # noqa: B023
                share.append(round(v / tot, 5) if tot else 0)
            end_ccu = sum(g["end"] or 0 for g in gs)
            top = max(gs, key=lambda g: g["end"] or 0)
            rows.append({
                "key": k, "label": labels.get(dim, {}).get(k, k.replace("_", " ")), "games": len(gs),
                "start_ccu": round(a), "end_ccu": round(end_ccu),
                "change": round(b / a - 1, 4) if a >= MIN_START_CCU[dim] and len(mg) >= MIN_GAMES else None,
                "vs_market": round((b / a) / (1 + market) - 1, 4) if a >= MIN_START_CCU[dim] and len(mg) >= MIN_GAMES else None,
                "visits": round(sum(g["visits"] for g in gs)),
                "launches": len(lg), "launch_ccu": round(sum(g["end"] for g in lg)),
                "share": share, "share_end": share[-1] if share else 0,
                "top": top["name"] if top.get("end") else "",
            })
        rows.sort(key=lambda r: -r["end_ccu"])
        ranked = [r for r in rows if r["change"] is not None]
        cats[dim] = {
            "biggest": rows[:12],
            # ranked against the whole market, so a market-wide dip (or a holiday spike) doesn't hide who is winning
            "gainers": sorted((r for r in ranked if r["vs_market"] > 0.02), key=lambda r: -r["vs_market"])[:8],
            "fallers": sorted((r for r in ranked if r["vs_market"] < -0.02), key=lambda r: r["vs_market"])[:8],
            "launch_leaders": sorted((r for r in rows if r["launches"]), key=lambda r: -r["launch_ccu"])[:8],
        }
    out["categories"] = cats
    return out


def compute(today: date | None = None) -> dict:
    end = latest_rollup_day(today)
    if end is None:
        return {"as_of": st.iso(st.now_utc()), "windows": {}}
    meta, labels = _game_meta(), _labels()
    return {"as_of": st.iso(st.now_utc()), "end": end.isoformat(),
            "windows": {str(w): compute_window(w, end, meta, labels) for w in WINDOWS}}


def _pct(v):
    return "–" if v is None else f"{v:+.0%}"


def write(ov: dict):
    st.save_json(st.REPORTS / "overview.json", ov)
    L = [f"# Market overview — through {ov.get('end', '?')}", ""]
    for w, o in ov.get("windows", {}).items():
        L += [f"## Last {w} days", ""]
        if o.get("empty"):
            L += ["Not enough data yet.", ""]
            continue
        h = o["headline"]
        L += [f"{o['days_of_data']} days of data · whole market (same games) {_pct(h['ccu_change'])} · {h['visits']:,} visits · "
              f"{h['launches']} new games launched, holding {h['launch_share'] or 0:.1%} of CCU", ""]
        for dim, title in (("genre", "Genres"), ("mechanic", "Mechanics"), ("theme", "Themes")):
            c = o["categories"][dim]
            L += [f"### {title} (ranked against the market)", "", "| Beating the market | Change | | Lagging | Change |", "|---|---|---|---|---|"]
            for i in range(max(len(c["gainers"][:6]), len(c["fallers"][:6]))):
                g = c["gainers"][i] if i < len(c["gainers"]) else None
                f = c["fallers"][i] if i < len(c["fallers"]) else None
                L.append(f"| {g['label'] if g else ''} | {_pct(g['change']) if g else ''} | | {f['label'] if f else ''} | {_pct(f['change']) if f else ''} |")
            L.append("")
        L += ["### New games gaining the most (30 days old or less)", ""] + [f"- {g['name']}: {g['start']:,} → {g['end']:,} CCU" for g in o["breakouts"][:6]] + [""]
    (st.REPORTS / "overview.md").write_text("\n".join(L) + "\n", encoding="utf-8")


def run() -> dict:
    ov = compute()
    write(ov)
    return {w: o.get("days_of_data", 0) for w, o in ov.get("windows", {}).items()}
