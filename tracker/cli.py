"""Command line entry point.

  python -m tracker run        # the hourly job: everything that is due
  python -m tracker probe      # check every endpoint once and write reports/probe.md
  python -m tracker report     # rebuild metrics and the watchlist from existing rollups
  python -m tracker build-db   # load all CSVs into tracker.db (SQLite) for your own queries
  python -m tracker thumbs     # fetch/refresh niche icons + thumbnails now and rebuild reports/thumbs.json
  python -m tracker vision-sheets [dir]   # contact sheets for Claude: niche thumbnails + game art/map styles
  python -m tracker vision-ingest <file>  # store Claude's answers: {"niches": {...}, "styles": [...]}
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import traceback
from datetime import timedelta

from . import collect, daily, metrics
from . import storage as st
from .http import Http
from .sources import roblox, rolimons


def make_http(cfg) -> Http:
    # the deadline stops new requests well before the workflow's timeout, so data is always saved and committed
    return Http(cfg.get("user_agent", "TrendTracker/0.1"), cfg.get("request_interval_sec", 0.5),
                cfg.get("max_requests_per_run", 3000), deadline_sec=cfg.get("run_deadline_minutes", 18) * 60)


def run(http: Http | None = None) -> int:
    cfg = st.settings()
    http = http or make_http(cfg)
    now = st.now_utc()
    state = st.load_state()
    games = st.load_games()
    failed_core = False

    def step(name, fn, core=False):
        nonlocal failed_core
        before = http.count
        try:
            res = fn()
            st.log_run(name, True, count=_count(res), requests=http.count - before, detail=json.dumps(res, default=str))
            print(f"[ok] {name}: {res}")
            return res
        except Exception as e:  # noqa: BLE001 - one failing step must not lose the others' data
            st.log_run(name, False, requests=http.count - before, detail=f"{type(e).__name__}: {e}")
            print(f"[fail] {name}: {e}", file=sys.stderr)
            traceback.print_exc()
            failed_core = failed_core or core
            return None
        finally:
            try:
                st.save_games(games)
            except Exception as e:  # noqa: BLE001 - a save problem must never stop the run
                print(f"[warn] saving games failed: {e}", file=sys.stderr)

    last_disc = st.parse_iso(state.get("last_discovery", ""))
    if not games or last_disc is None or now - last_disc >= timedelta(hours=cfg.get("discovery_every_hours", 6)):
        res = step("discover", lambda: collect.discover(http, games, cfg, now))
        if res and res.get("listed"):  # a total outage retries next hour instead of waiting 6
            state["last_discovery"] = st.iso(now)
    step("third_party", lambda: collect.third_party(http, games, cfg, now))
    step("snapshot", lambda: collect.snapshot(http, games, cfg, now), core=True)

    today = now.date()
    state.setdefault("last_rollup_day", (today - timedelta(days=1)).isoformat())  # first run: start rolling up from today
    for day in daily.pending_rollup_days(today, state.get("last_rollup_day")):
        if step(f"rollup {day}", lambda d=day: {"games": daily.rollup_day(d)}) is None:
            break  # never skip past a failed day; it is retried next run
        state["last_rollup_day"] = day
        st.save_state(state)

    if state.get("last_daily") != today.isoformat():
        tagged = step("classify", lambda: {"tags": daily.classify(games)})
        step("passes_badges", lambda: daily.passes_and_badges(http, games, state, cfg.get("passes_badges_per_day", 300), now))
        step("maturity", lambda: collect.fill_maturity(http, games, cfg.get("maturity_lookups_per_day", 300), state, st.iso(now)))
        priority = step("metrics", lambda: _metrics(games, cfg, today - timedelta(days=1)))
        step("trends", lambda: _trends())
        step("youtube", lambda: daily.youtube_coverage(http, games, cfg, now, (priority or {}).get("priority", [])))
        step("archive", lambda: {"archived": st.archive_old_days(today=today)})
        if tagged is not None and priority is not None:  # otherwise retry the daily work next hour
            state["last_daily"] = today.isoformat()

    if cfg.get("thumbs_enabled", True):  # runs every hour: URLs refresh daily, downloads continue until caught up
        step("thumbs", lambda: _thumbs(http, cfg, now, state))

    state["last_run"] = st.iso(now)
    st.save_state(state)
    print(f"done: {http.count} requests, {len(games)} games known")
    return 1 if failed_core else 0


def _metrics(games, cfg, end_day):
    res = metrics.compute(games, cfg, end_day)
    priority = metrics.write_reports(res, cfg)
    return {"games": len(res["games"]), "niches": len(res["niches"]), "flagged": len(priority),
            "days_of_data": res["days_of_data"], "priority": priority}


def _thumbs(http, cfg, now, state):
    from . import thumbs
    return thumbs.collect(http, cfg, now, state)


def _trends():
    from . import trends
    return trends.run()


def _count(res):
    if isinstance(res, dict):
        for k in ("polled", "added", "games", "tags", "listed"):
            if isinstance(res.get(k), int):
                return res[k]
    return 0


def probe(http: Http | None = None) -> int:
    """Hit each endpoint once and record status + response shape, so field names can be confirmed live."""
    cfg = st.settings()
    http = http or make_http(cfg)
    sid = roblox.new_session_id()
    uid, pid = "994732206", "2753915549"
    checks = [
        ("games (details)", lambda: http.get_json(roblox.GAMES, {"universeIds": uid})),
        ("votes", lambda: http.get_json(roblox.VOTES, {"universeIds": uid})),
        ("explore get-sorts", lambda: http.get_json(roblox.EXPLORE_SORTS, {"sessionId": sid, "device": "computer", "country": "all"})),
        ("explore get-sort-content", lambda: http.get_json(roblox.EXPLORE_CONTENT, {"sessionId": sid, "sortId": "top-playing-now", "device": "computer", "country": "all"})),
        ("omni-search", lambda: http.get_json(roblox.OMNI, {"searchQuery": "brainrot", "sessionId": sid, "pageType": "all"})),
        ("place -> universe", lambda: http.get_json(roblox.PLACE_UNIVERSE.format(place_id=pid))),
        ("game passes", lambda: http.get_json(roblox.PASSES.format(uid=uid), {"passView": "Full", "pageSize": 10})),
        ("badges", lambda: http.get_json(roblox.BADGES.format(uid=uid), {"limit": 10})),
        ("age guidelines (POST)", lambda: http.post_json(roblox.GUIDELINES, {"universeId": uid})),
        ("rolimons gamelist", lambda: http.get_json(rolimons.GAMELIST)),
        ("game icons", lambda: http.get_json(roblox.THUMB_ICONS, {"universeIds": uid, "size": "150x150", "format": "Png", "isCircular": "false"})),
        ("game thumbnails", lambda: http.get_json(roblox.THUMB_MEDIA, {"universeIds": uid, "countPerUniverse": 3, "defaults": "true", "size": "384x216", "format": "Png", "isCircular": "false"})),
    ]
    lines = [f"# Endpoint probe — {st.iso(st.now_utc())}", "", "| Endpoint | Result | Shape |", "|---|---|---|"]
    ok_all = True
    for name, fn in checks:
        try:
            resp = fn()
            n = len(list(roblox.walk_universes(resp)))
            lines.append(f"| {name} | OK ({n} universe objects) | `{_shape(resp)}` |")
        except Exception as e:  # noqa: BLE001
            ok_all = False
            lines.append(f"| {name} | FAILED: {str(e)[:160].replace('|', '/')} | – |")
    out = "\n".join(lines) + "\n"
    (st.REPORTS / "probe.md").parent.mkdir(parents=True, exist_ok=True)
    (st.REPORTS / "probe.md").write_text(out, encoding="utf-8")
    print(out)
    return 0 if ok_all else 1


def _shape(obj, depth=0):
    """Compact description of JSON structure: keys and the shape of the first list item."""
    if depth > 3:
        return "…"
    if isinstance(obj, dict):
        parts = []
        for k, v in list(obj.items())[:60]:
            parts.append(f"{k}:{_shape(v, depth + 1)}" if isinstance(v, (dict, list)) else k)
        return "{" + ", ".join(parts) + "}"
    if isinstance(obj, list):
        return "[" + (_shape(obj[0], depth + 1) if obj else "") + "]"
    return type(obj).__name__


def youtube_now() -> int:
    """Run the YouTube coverage step on demand (it normally runs once a day inside `run`)."""
    cfg = st.settings()
    http = make_http(cfg)
    games = st.load_games()
    wl = st.load_json(st.REPORTS / "watchlist.json", {})
    priority = [g["universe_id"] for g in wl.get("games", [])]
    res = daily.youtube_coverage(http, games, cfg, st.now_utc(), priority)
    st.log_run("youtube", "skipped" not in res, count=res.get("games", 0), requests=http.count, detail=json.dumps(res))
    print(res)
    return 0 if res.get("games") else 1


def report() -> int:
    cfg = st.settings()
    games = st.load_games()
    today = st.now_utc().date()
    daily.classify(games)
    print(_metrics(games, cfg, today - timedelta(days=1)))
    return 0


def build_db(path: str = "tracker.db") -> int:
    con = sqlite3.connect(path)

    def days(kind):  # plain and gzipped day files, as the plain path (read_rows opens either)
        return sorted({p if p.suffix == ".csv" else p.with_suffix("") for p in (st.DATA / kind).rglob("*.csv*")})

    games_rows = sorted(st.load_games().values(), key=lambda r: int(r["universe_id"]))
    tables = {
        "games": [games_rows], "tags": [st.DATA / "tags.csv"], "passes": [st.DATA / "passes.csv"],
        "badges": [st.DATA / "badges.csv"], "styles": [st.DATA / "styles.csv"], "thumbs": [st.DATA / "thumbs" / "index.csv"],
        "snapshots": days("snapshots"), "rollups": days("rollups"), "discovery": days("discovery"),
        "rolimons": days("third_party/rolimons"), "youtube": days("youtube"),
    }
    for table, files in tables.items():
        con.execute(f'DROP TABLE IF EXISTS "{table}"')
        cols = None
        n = 0
        for f in files:
            rows = f if isinstance(f, list) else st.read_rows(f)
            if not rows:
                continue
            if cols is None:
                cols = list(rows[0].keys())
                con.execute(f'CREATE TABLE "{table}" ({", ".join(chr(34) + c + chr(34) for c in cols)})')
            con.executemany(f'INSERT INTO "{table}" VALUES ({",".join("?" * len(cols))})',
                            [[_num(str(r.get(c, "") or "")) for c in cols] for r in rows])
            n += len(rows)
        print(f"{table}: {n} rows")
    for idx in ("snapshots(universe_id, ts)", "rollups(universe_id, date)", "tags(tag)"):
        try:
            con.execute(f"CREATE INDEX IF NOT EXISTS ix_{idx.split('(')[0]} ON {idx}")
        except sqlite3.OperationalError:
            pass
    con.commit()
    con.close()
    print(f"wrote {path}")
    return 0


_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")


def _num(v):
    """Only plain numbers become numbers; names like 'Infinity' or '1_000' stay text."""
    if v in (None, ""):
        return None
    if _NUMERIC.match(v):
        return float(v) if "." in v else int(v)
    return v


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "run"
    if cmd == "run":
        return run()
    if cmd == "probe":
        return probe()
    if cmd == "report":
        return report()
    if cmd == "youtube":
        return youtube_now()
    if cmd == "trends":
        daily.classify(st.load_games())
        print(_trends())
        return 0
    if cmd == "idea-lab":
        from .idea_lab import build as build_lab
        ideas = argv[argv.index("--ideas") + 1] if "--ideas" in argv else None
        out = next((a for a in argv[1:] if not a.startswith("--") and a != ideas), "site/idea-lab.html")
        print(f"wrote {build_lab(out, ideas)}")
        return 0
    if cmd == "dashboard":
        from .dashboard import build
        print(f"wrote {build(argv[1] if len(argv) > 1 else 'site/index.html')}")
        return 0
    if cmd == "thumbs":
        cfg = st.settings()
        cfg["thumbs_downloads_per_run"] = cfg.get("thumbs_downloads_manual", 1400)  # on-demand run: nothing else competes for time
        cfg["request_interval_sec"] = min(cfg.get("request_interval_sec", 0.5), 0.3)
        state = st.load_state()
        res = _thumbs(make_http(cfg), cfg, st.now_utc(), state)
        st.save_state(state)
        print(res)
        return 0
    if cmd == "vision-sheets":
        from . import thumbs
        print(f"wrote {thumbs.vision_sheets(argv[1] if len(argv) > 1 else 'vision')}")
        return 0
    if cmd == "vision-ingest":
        from . import styles, thumbs
        res = json.loads(open(argv[1], encoding="utf-8").read())
        n_s = styles.ingest(res.get("styles", []))
        n_n = thumbs.ingest_niches(res.get("niches", {}))
        if n_s:
            _trends()
            thumbs.report()
        print({"styles": n_s, "niches": n_n})
        return 0
    if cmd == "build-db":
        return build_db(argv[1] if len(argv) > 1 else "tracker.db")
    print(__doc__)
    return 2
