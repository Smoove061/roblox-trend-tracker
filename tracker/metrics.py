"""Trend signals and the watchlist, built from daily rollups."""
from __future__ import annotations

from datetime import date, timedelta

from . import storage as st


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def load_rollups(end_day: date, days: int = 28) -> dict[str, dict[str, dict]]:
    """{universe_id: {date: rollup row}} for the `days` days ending on end_day."""
    out = {}
    for i in range(days):
        d = (end_day - timedelta(days=i)).isoformat()
        for r in st.read_rows(st.day_path("rollups", d)):
            out.setdefault(r["universe_id"], {})[d] = r
    return out


def _window(series: dict, end_day: date, start_offset: int, length: int, col="avg_ccu"):
    vals = []
    for i in range(start_offset, start_offset + length):
        r = series.get((end_day - timedelta(days=i)).isoformat())
        if r:
            vals.append(_f(r.get(col)))
    return vals


def _latest_youtube(end_day: date):
    """Latest and a ~week-older YouTube record per game."""
    latest, older = {}, {}
    for i in range(0, 21):
        d = (end_day - timedelta(days=i)).isoformat()
        for r in st.read_rows(st.day_path("youtube", d)):
            uid = r["universe_id"]
            if uid not in latest:
                latest[uid] = r
            elif uid not in older and i >= 6:
                older[uid] = r
    return latest, older


def compute(games: dict, cfg: dict, end_day: date) -> dict:
    w = cfg.get("watch", {})
    floor = cfg.get("ccu_floor", 300)
    roll = load_rollups(end_day, 28)
    days_of_data = len({d for s in roll.values() for d in s})
    tags = {}
    for t in st.read_rows(st.DATA / "tags.csv"):
        if t["tag_type"] == "niche" and _f(t["confidence"], 0) >= 0.6:
            tags.setdefault(t["universe_id"], []).append(t["tag"])
    yt_latest, yt_older = _latest_youtube(end_day)
    manual = {str(m.get("universe_id")): m.get("reason", "added by hand")
              for m in st.load_json(st.CONFIG / "watchlist_manual.json", {}).get("games", [])}

    game_rows = []
    for uid, series in roll.items():
        g = games.get(uid, {})
        if str(g.get("active")) != "1" and uid not in manual:
            continue  # games that fell below the floor stay in the data, not on the watchlist
        last7 = _window(series, end_day, 0, 7)
        prev7 = _window(series, end_day, 7, 7)
        avg7, avgp = _mean(last7), _mean(prev7)
        growth = (avg7 - avgp) / avgp if avg7 is not None and avgp and len(prev7) >= 4 and len(last7) >= 4 else None
        chrono = list(reversed(last7))
        rising = sum(1 for a, b in zip(chrono, chrono[1:]) if a is not None and b is not None and b > a)
        created = st.parse_iso(g.get("created", ""))
        age = (end_day - created.date()).days if created else None
        fav7 = sum(_f(x, 0) for x in _window(series, end_day, 0, 7, "favorites_gained"))
        latest = series.get(end_day.isoformat()) or series[max(series)]
        yl, yo = yt_latest.get(uid), yt_older.get(uid)
        flags = []
        if (age is not None and age <= w.get("young_days", 60) and avg7 is not None and avg7 >= floor
                and len(last7) >= min(w.get("young_growth_days", 7), days_of_data) - 2  # tolerate 2 missed days
                and (growth is None or growth > 0) and rising >= max(1, len(last7) - 3)):
            flags.append("young_breakout")
        if yl and yo:
            v_new, v_old = _f(yl.get("views"), 0), _f(yo.get("views"), 0)
            if v_old > 0 and v_new / v_old >= w.get("coverage_spike_ratio", 2.0) and (growth is None or growth < 0.2):
                flags.append("coverage_spike")
        if uid in manual:
            flags.append("manual")
        game_rows.append({
            "universe_id": uid, "name": g.get("name", ""), "root_place_id": g.get("root_place_id", ""),
            "genre": g.get("genre_l1") or g.get("genre", ""), "subgenre": g.get("genre_l2", ""),
            "niches": "|".join(sorted(tags.get(uid, []))),
            "avg_ccu_7d": None if avg7 is None else round(avg7, 1),
            "avg_ccu_prev_7d": None if avgp is None else round(avgp, 1),
            "growth_7d": None if growth is None else round(growth, 4),
            "days_rising": rising, "age_days": age,
            "like_ratio": latest.get("like_ratio", ""),
            "favorites_per_ccu_7d": round(fav7 / avg7, 3) if avg7 else None,
            "yt_videos_7d": yl.get("videos", "") if yl else "", "yt_views_7d": yl.get("views", "") if yl else "",
            "flags": "|".join(flags), "manual_reason": manual.get(uid, ""),
        })
    game_rows.sort(key=lambda r: -(r["avg_ccu_7d"] or 0))

    # niches: compare the latest window against the previous one, vs the platform as a whole
    span = 14 if days_of_data >= 28 else 7 if days_of_data >= 14 else None
    niche_rows = []
    if span:
        def total(uids, off):
            return sum(_mean(_window(roll[u], end_day, off, span)) or 0 for u in uids)
        all_ids = list(roll)
        plat_now, plat_prev = total(all_ids, 0), total(all_ids, span)
        plat_growth = (plat_now - plat_prev) / plat_prev if plat_prev else None
        by_niche = {}
        for uid in roll:
            for n in tags.get(uid, []):
                by_niche.setdefault(n, []).append(uid)
        for n, uids in by_niche.items():
            now_t, prev_t = total(uids, 0), total(uids, span)
            shares = sorted((_mean(_window(roll[u], end_day, 0, span)) or 0 for u in uids), reverse=True)
            top = shares[0] / now_t if now_t else 0
            top3 = sum(shares[:3]) / now_t if now_t else 0
            growth = (now_t - prev_t) / prev_t if prev_t else None
            flag = (growth is not None and plat_growth is not None and growth > plat_growth
                    and top < w.get("niche_max_top_share", 0.5) and len(uids) >= 3)
            niche_rows.append({
                "niche": n, "games": len(uids), "total_avg_ccu": round(now_t), "prev_total_avg_ccu": round(prev_t),
                "growth": None if growth is None else round(growth, 4),
                "platform_growth": None if plat_growth is None else round(plat_growth, 4),
                "top_share": round(top, 3), "top3_share": round(top3, 3), "window_days": span,
                "flags": "niche_rising" if flag else "",
            })
        niche_rows.sort(key=lambda r: -(r["total_avg_ccu"]))
    return {"games": game_rows, "niches": niche_rows, "days_of_data": days_of_data, "end_day": end_day.isoformat()}


GAME_COLS = ["universe_id", "name", "root_place_id", "genre", "subgenre", "niches", "avg_ccu_7d", "avg_ccu_prev_7d",
             "growth_7d", "days_rising", "age_days", "like_ratio", "favorites_per_ccu_7d", "yt_videos_7d",
             "yt_views_7d", "flags", "manual_reason"]
NICHE_COLS = ["niche", "games", "total_avg_ccu", "prev_total_avg_ccu", "growth", "platform_growth", "top_share",
              "top3_share", "window_days", "flags"]


def _pct(v):
    return "–" if v in (None, "") else f"{float(v) * 100:+.0f}%"


def _link(r):
    pid = r.get("root_place_id")
    name = (r.get("name") or r["universe_id"]).replace("|", "/")
    return f"[{name}](https://www.roblox.com/games/{pid})" if pid else name


def write_reports(result: dict, cfg: dict) -> list[str]:
    st.write_rows(st.REPORTS / "game_metrics.csv", GAME_COLS, result["games"])
    st.write_rows(st.REPORTS / "niche_metrics.csv", NICHE_COLS, result["niches"])
    flagged = [r for r in result["games"] if r["flags"]]
    st.save_json(st.REPORTS / "watchlist.json", {
        "as_of": result["end_day"], "days_of_data": result["days_of_data"],
        "games": [{k: r[k] for k in ("universe_id", "name", "flags", "avg_ccu_7d", "growth_7d", "age_days")} for r in flagged],
        "niches": [r for r in result["niches"] if r["flags"]],
    })
    floor = cfg.get("ccu_floor", 300)
    lines = [f"# Watchlist — {result['end_day']}", "",
             f"Data: {result['days_of_data']} day(s) of rollups · {len(result['games'])} games tracked · CCU floor {floor}", ""]
    if result["days_of_data"] < 14:
        lines += [f"> Still building history. Growth needs 14 days and niche trends need 14–28; "
                  f"{max(0, 14 - result['days_of_data'])} more day(s) until growth signals turn on.", ""]

    def section(title, rows, cols):
        lines.extend([f"## {title}", ""])
        if not rows:
            lines.extend(["None yet.", ""])
            return
        lines.append("| " + " | ".join(c for c, _ in cols) + " |")
        lines.append("|" + "---|" * len(cols))
        for r in rows:
            lines.append("| " + " | ".join(str(f(r)) for _, f in cols) + " |")
        lines.append("")

    game_cols = [("Game", _link), ("Avg CCU 7d", lambda r: f"{r['avg_ccu_7d']:,.0f}" if r["avg_ccu_7d"] else "–"),
                 ("Growth 7d", lambda r: _pct(r["growth_7d"])), ("Age (days)", lambda r: r["age_days"] if r["age_days"] is not None else "–"),
                 ("Niches", lambda r: r["niches"].replace("|", ", ") or "–"), ("Flags", lambda r: r["flags"].replace("|", ", "))]
    section("Young breakouts", [r for r in flagged if "young_breakout" in r["flags"]], game_cols)
    section("Rising niches", [r for r in result["niches"] if r["flags"]],
            [("Niche", lambda r: r["niche"]), ("Games", lambda r: r["games"]), ("Total avg CCU", lambda r: f"{r['total_avg_ccu']:,}"),
             ("Growth", lambda r: _pct(r["growth"])), ("Platform", lambda r: _pct(r["platform_growth"])), ("Top game share", lambda r: f"{r['top_share']:.0%}")])
    section("Coverage spikes", [r for r in flagged if "coverage_spike" in r["flags"]], game_cols)
    section("Added by hand", [r for r in flagged if "manual" in r["flags"]], game_cols)
    movers = sorted((r for r in result["games"] if r["growth_7d"] is not None and (r["avg_ccu_7d"] or 0) >= floor),
                    key=lambda r: -r["growth_7d"])[:15]
    section("Top movers (7-day growth)", movers, game_cols)
    section("Biggest games (avg CCU, 7 days)", result["games"][:15], game_cols)
    if result["niches"]:
        section("All niches", result["niches"],
                [("Niche", lambda r: r["niche"]), ("Games", lambda r: r["games"]), ("Total avg CCU", lambda r: f"{r['total_avg_ccu']:,}"),
                 ("Growth", lambda r: _pct(r["growth"])), ("Top-3 share", lambda r: f"{r['top3_share']:.0%}")])
    (st.REPORTS / "watchlist.md").parent.mkdir(parents=True, exist_ok=True)
    (st.REPORTS / "watchlist.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return list(dict.fromkeys([r["universe_id"] for r in flagged] + [r["universe_id"] for r in movers]))
