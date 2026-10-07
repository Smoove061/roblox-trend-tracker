"""Once-a-day work: rollups (with catch-up for missed days), tagging, passes/badges, YouTube coverage."""
from __future__ import annotations

import os
import re
from datetime import date, timedelta

from . import storage as st
from .sources import roblox, youtube


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- rollups

def _last_values(rows):
    """Last non-empty visits/favorites/votes per universe, in file order (chronological)."""
    last = {}
    for r in rows:
        d = last.setdefault(r["universe_id"], {})
        for k in ("visits", "favorites", "up_votes", "down_votes"):
            if r.get(k) not in ("", None):
                d[k] = _f(r[k])
    return last


def rollup_day(day: str) -> int:
    rows = st.read_rows(st.day_path("snapshots", day))
    if not rows:
        st.write_rows(st.day_path("rollups", day), st.ROLLUP_FIELDS, [])
        return 0
    # baseline for "gained": the latest earlier day-end value within a week, so a missed day doesn't understate gains
    d0 = date.fromisoformat(day)
    prev = {}
    for i in range(1, 8):
        for r in st.read_rows(st.day_path("rollups", (d0 - timedelta(days=i)).isoformat())):
            p = prev.setdefault(r["universe_id"], {})
            for src, k in (("visits_end", "visits"), ("favorites_end", "favorites")):
                if k not in p and r.get(src) not in ("", None):
                    p[k] = _f(r[src])
    for uid, vals in _last_values(st.read_rows(st.day_path("snapshots", (d0 - timedelta(days=1)).isoformat()))).items():
        for k, v in vals.items():
            prev.setdefault(uid, {}).setdefault(k, v)
    first = {}
    groups = {}
    for r in rows:
        groups.setdefault(r["universe_id"], []).append(r)
        f = first.setdefault(r["universe_id"], {})
        for k in ("visits", "favorites"):
            if k not in f and r.get(k) not in ("", None):
                f[k] = _f(r[k])
    last = _last_values(rows)
    out = []
    for uid, rs in groups.items():
        ccu = [x for x in (_f(r["playing"]) for r in rs) if x is not None]
        if not ccu:
            continue
        L, P, F = last.get(uid, {}), prev.get(uid, {}), first.get(uid, {})

        def gained(k):
            end = L.get(k)
            base = P.get(k, F.get(k))
            return "" if end is None or base is None else int(end - base)

        up, down = L.get("up_votes"), L.get("down_votes")
        ratio = round(up / (up + down), 4) if up is not None and down is not None and (up + down) > 0 else ""
        out.append({
            "date": day, "universe_id": uid, "samples": len(ccu),
            "avg_ccu": round(sum(ccu) / len(ccu), 1), "peak_ccu": int(max(ccu)), "low_ccu": int(min(ccu)),
            "visits_end": "" if L.get("visits") is None else int(L["visits"]), "visits_gained": gained("visits"),
            "favorites_end": "" if L.get("favorites") is None else int(L["favorites"]), "favorites_gained": gained("favorites"),
            "up_votes": "" if up is None else int(up), "down_votes": "" if down is None else int(down), "like_ratio": ratio,
        })
    out.sort(key=lambda r: -r["avg_ccu"])
    st.write_rows(st.day_path("rollups", day), st.ROLLUP_FIELDS, out)
    return len(out)


def pending_rollup_days(today: date, last_done: str | None, max_back: int = 60) -> list[str]:
    """Every completed UTC day since the last rollup, so missed days are caught up."""
    start = date.fromisoformat(last_done) + timedelta(days=1) if last_done else today - timedelta(days=1)
    start = max(start, today - timedelta(days=max_back))
    days = []
    d = start
    while d < today:
        days.append(d.isoformat())
        d += timedelta(days=1)
    return days


# ---------------------------------------------------------------- tagging

def _compile(rules: dict):
    """Whole-word, case-insensitive matchers. Lookarounds instead of \\b so keywords like "+1" work."""
    out = {}
    for tag, kws in rules.items():
        if isinstance(kws, dict):
            kws = kws.get("keywords", [])
        if kws:
            out[tag] = re.compile(r"(?<!\w)(" + "|".join(re.escape(k) for k in kws) + r")(?!\w)", re.I)
    return out


def classify(games: dict) -> int:
    cfg = st.load_json(st.CONFIG / "niches.json", {})
    manual = st.load_json(st.CONFIG / "tags_manual.json", {})
    niches, feats, pass_feats = (_compile(cfg.get(k, {})) for k in ("niches", "features", "pass_features"))
    tax = st.load_json(st.CONFIG / "taxonomy.json", {})
    mechs, themes = _compile(tax.get("mechanics", {})), _compile(tax.get("themes", {}))
    formulas = {k: re.compile(v["regex"], re.I) for k, v in tax.get("title_formulas", {}).items()}
    passes, badges = {}, {}
    for p in st.read_rows(st.DATA / "passes.csv"):
        passes.setdefault(p["universe_id"], []).append(p.get("name", ""))
    for b in st.read_rows(st.DATA / "badges.csv"):
        badges.setdefault(b["universe_id"], []).append(b.get("name", ""))
    rows = []
    for uid, g in games.items():
        name, desc = g.get("name", ""), g.get("description", "")
        tags = {}

        def add(tag, ttype, source, conf):
            key = (tag, ttype)
            if key not in tags or tags[key][1] < conf:
                tags[key] = (source, conf)

        for col in ("genre_l1", "genre_l2", "genre"):
            if g.get(col) and g[col] != "All":
                add(g[col], "genre" if col != "genre_l2" else "subgenre", "roblox", 1.0)
        for rules, ttype in ((niches, "niche"), (feats, "feature")):
            for tag, rx in rules.items():
                if rx.search(name):
                    add(tag, ttype, "keyword:name", 0.9)
                else:
                    hits = len(rx.findall(desc))
                    if hits:
                        # one stray word in a description is weak evidence for a niche; niche totals use >= 0.6
                        conf = 0.6 if ttype == "feature" or hits >= 2 else 0.4
                        add(tag, ttype, "keyword:description", conf)
        pass_text = " | ".join(passes.get(uid, []))
        if pass_text:
            for tag, rx in pass_feats.items():
                if rx.search(pass_text):
                    add(tag, "feature", "keyword:passes", 0.7)
        # core mechanics and themes: name is strong evidence, passes/badges good, description needs repeats
        extra_text = pass_text + " | " + " | ".join(badges.get(uid, []))
        for rules, ttype in ((mechs, "mechanic"), (themes, "theme")):
            for tag, rx in rules.items():
                if rx.search(name):
                    add(tag, ttype, "keyword:name", 0.9)
                elif ttype == "mechanic" and len(rx.findall(extra_text)) >= 2:
                    add(tag, ttype, "keyword:passes_badges", 0.7)
                else:
                    hits = len(rx.findall(desc))
                    if hits:
                        add(tag, ttype, "keyword:description", 0.6 if hits >= 2 else 0.4)
        for tag, rx in formulas.items():
            if rx.search(name):
                add(tag, "formula", "regex:name", 1.0)
        m = manual.get(uid, {})
        for tag in m.get("remove", []):
            for k in [k for k in tags if k[0] == tag]:
                del tags[k]
        for tag in m.get("add_niche", []):
            add(tag, "niche", "manual", 1.0)
        for tag in m.get("add_feature", []):
            add(tag, "feature", "manual", 1.0)
        for tag in m.get("add_mechanic", []):
            add(tag, "mechanic", "manual", 1.0)
        for tag in m.get("add_theme", []):
            add(tag, "theme", "manual", 1.0)
        for (tag, ttype), (source, conf) in tags.items():
            rows.append({"universe_id": uid, "tag": tag, "tag_type": ttype, "source": source, "confidence": conf})
    rows.sort(key=lambda r: (int(r["universe_id"]), r["tag_type"], r["tag"]))
    st.write_rows(st.DATA / "tags.csv", st.TAG_FIELDS, rows)
    return len(rows)


# ---------------------------------------------------------------- passes and badges

def _pass_row(uid, p, ts):
    price = p.get("price")
    if price is None:
        info = p.get("priceInformation") or {}
        price = info.get("defaultPriceInRobux") if isinstance(info, dict) else None
    if price is None:
        price = p.get("priceInRobux")
    return {"universe_id": uid, "pass_id": p.get("id") or p.get("gamePassId") or "",
            "name": p.get("name") or p.get("displayName") or "", "price": "" if price is None else price, "fetched": ts}


def _badge_row(uid, b, ts):
    s = b.get("statistics") or {}
    return {"universe_id": uid, "badge_id": b.get("id", ""), "name": b.get("name", ""),
            "awarded_count": s.get("awardedCount", ""), "past_day_awarded": s.get("pastDayAwardedCount", ""),
            "win_rate": s.get("winRatePercentage", ""), "created": b.get("created", ""), "fetched": ts}


def passes_and_badges(http, games: dict, state: dict, limit: int, now) -> dict:
    """Refresh passes and badges for a rotating batch: never-fetched games first, then the stalest."""
    fetched = state.setdefault("extras_fetched", {})
    active = [u for u, g in games.items() if str(g.get("active")) == "1"]
    active.sort(key=lambda u: (fetched.get(u, ""), -int(float(games[u].get("last_ccu") or 0))))
    batch = active[:limit]
    if not batch:
        return {"games": 0}
    ts = st.iso(now)
    tables = {
        "passes": {"fn": roblox.game_passes, "row": _pass_row, "file": "passes.csv", "fields": st.PASS_FIELDS, "new": [], "done": set()},
        "badges": {"fn": roblox.game_badges, "row": _badge_row, "file": "badges.csv", "fields": st.BADGE_FIELDS, "new": [], "done": set()},
    }
    errors, streak, stopped = 0, 0, ""
    for uid in batch:
        if http.out_of_time():
            stopped = "deadline"
            break
        ok_any = False
        for t in tables.values():
            try:
                rows = [t["row"](uid, item, ts) for item in t["fn"](http, uid)]
            except Exception as e:  # noqa: BLE001
                errors += 1
                if type(e).__name__ == "BudgetExceeded":
                    stopped = "budget"
                continue
            t["new"].extend(rows)  # only a complete fetch replaces a game's stored rows
            t["done"].add(uid)
            ok_any = True
        fetched[uid] = ts  # rotate even on failure, so one broken game can't block the queue
        streak = 0 if ok_any else streak + 1
        if stopped or streak >= 5:
            stopped = stopped or "5 games in a row failed"
            break
    for t in tables.values():
        path = st.DATA / t["file"]
        keep = [r for r in st.read_rows(path) if r["universe_id"] not in t["done"]]
        st.write_rows(path, t["fields"], keep + t["new"])
    return {"games": len(tables["passes"]["done"] | tables["badges"]["done"]), "passes": len(tables["passes"]["new"]),
            "badges": len(tables["badges"]["new"]), "errors": errors, "stopped": stopped}


# ---------------------------------------------------------------- youtube

def youtube_coverage(http, games: dict, cfg: dict, now, priority: list[str]) -> dict:
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        return {"skipped": "no YOUTUBE_API_KEY"}
    n = cfg.get("youtube_games_per_day", 30)
    picked = []
    for uid in priority + sorted(games, key=lambda u: -float(games[u].get("last_ccu") or 0)):
        if uid in games and uid not in picked and str(games[uid].get("active")) == "1":
            picked.append(uid)
        if len(picked) >= n:
            break
    after = st.iso(now - timedelta(days=cfg.get("youtube_lookback_days", 7)))
    rows, errors = [], 0
    for uid in picked:
        try:
            c = youtube.coverage(http, key, games[uid]["name"], after)
        except Exception as e:  # noqa: BLE001 - quota (403) or repeated errors: stop, keep what we have
            errors += 1
            if getattr(e, "status", 0) == 403 or errors >= 3 or type(e).__name__ == "BudgetExceeded":
                break
            continue
        rows.append({"date": st.iso(now)[:10], "universe_id": uid, **c})
    st.append_rows(st.day_path("youtube", st.iso(now)[:10]), st.YT_FIELDS, rows)
    return {"games": len(rows)}
