"""Build the dashboard: one self-contained HTML page with the data inlined.

    python -m tracker dashboard [out.html]      # default: site/index.html

The GitHub workflow builds it after every run and publishes it with GitHub Pages,
so it is never committed (no repo growth). Locally, open the file in any browser.

Next to the page it also writes (so the published site can fetch/download them; all untracked build output):
  data/ and reports/   copies of every dataset for the "Data hub" (latest 3 days of day-partitioned folders as plain CSV,
                       runs.csv trimmed to its latest 2000 rows); a manifest of all of them is embedded in the page
  thumbs/              the icons/thumbnails referenced by reports/thumbs.json, for the "Thumbnails & styles" section
"""
from __future__ import annotations

import csv
import gzip
import html
import io
import json
import os
import re
import shutil
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import storage as st

SPARK_HOURS = 72
HUB_DAYS = 3           # day-partitioned folders: this many latest days are copied next to the page as plain CSV
RUNS_CAP = 2000        # data/runs.csv: copy only the latest rows
MAX_COPY_BYTES = 12_000_000   # any single copied CSV is trimmed to its latest rows beyond this size
DAY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.csv(\.gz)?$")
SAFE_FILE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


# ---------- data hub: every dataset the tracker produces, listed in one place ----------
# Files are copied next to the page at build time (site/data/..., site/reports/...), so the published dashboard can fetch
# and download them. Small files go as-is; day-partitioned folders copy only the latest HUB_DAYS days (as plain CSV);
# data/runs.csv is trimmed to its latest RUNS_CAP rows. The manifest below is embedded in the page so the list renders offline.

# (id, group, name, repo path, plain-English description, timestamp column, runs.csv step that rewrites the file)
_FILES = [
    ("games", "Games", "Games", "data/games.csv", "One row per tracked game: name, creator, genre, created and updated dates, maturity, description.", "first_seen", "discover"),
    ("game_status", "Games", "Game status", "data/game_status.csv", "Live status per game: active or dropped, last and peak CCU, when it was last seen.", "last_checked", None),
    ("place_universe", "Games", "Place to universe IDs", "data/place_universe.csv", "Maps place IDs (the number in a Roblox game link) to universe IDs, used to match Rolimons and chart data.", None, "third_party"),
    ("tags", "Tags & insights", "Tags", "data/tags.csv", "Genre, niche, core mechanic, theme, title-formula and feature tags for every game, rebuilt daily.", None, "classify"),
    ("insights", "Tags & insights", "Insights", "data/insights.csv", "Per-game monetization and progression summary: game passes, prices, badges, badge funnel.", None, "passes_badges"),
    ("styles", "Tags & insights", "Art and map styles", "data/styles.csv", "Art style and map style label for each game, from Claude looking at its icon and screenshots.", "updated", None),
    ("passes", "Tags & insights", "Game passes", "data/passes.csv", "Current game passes of each game with their prices.", "fetched", "passes_badges"),
    ("badges", "Tags & insights", "Badges", "data/badges.csv", "Current badges of each game with award counts and win rates.", "fetched", "passes_badges"),
    ("thumb_index", "Thumbnails", "Thumbnail index", "data/thumbs/index.csv", "Every downloaded icon and thumbnail with its measured brightness, colour mix and palette.", "fetched", None),
    ("thumbs_report", "Thumbnails", "Thumbnails report", "reports/thumbs.json", "What the leading icons and thumbnails look like in each niche, with plain-language direction.", None, None),
    ("thumb_vision", "Thumbnails", "Thumbnail vision notes", "reports/thumb_vision.json", "What Claude saw in each niche's art: recurring patterns, text use, do and avoid lists.", None, None),
    ("watchlist_md", "Reports", "Watchlist report", "reports/watchlist.md", "The daily trend report: young breakouts, rising niches, coverage spikes and movers.", None, "metrics"),
    ("watchlist_json", "Reports", "Watchlist (data)", "reports/watchlist.json", "The watchlist report as data: flagged games and niches.", None, "metrics"),
    ("game_metrics", "Reports", "Game metrics", "reports/game_metrics.csv", "Per-game signals: 7-day average CCU, growth, days rising, like ratio, YouTube coverage, flags.", None, "metrics"),
    ("niche_metrics", "Reports", "Niche metrics", "reports/niche_metrics.csv", "Per-niche size, growth, saturation and concentration.", None, "metrics"),
    ("trends_json", "Reports", "Trends", "reports/trends.json", "Uprising trends: momentum of every mechanic, theme, title formula and style, untapped combinations, benchmarks.", None, "trends"),
    ("trends_md", "Reports", "Trends (readable)", "reports/trends.md", "The trends report as a readable page.", None, "trends"),
    ("daily_ideas", "Reports", "Daily ideas", "reports/daily_ideas.json", "The game ideas generated for the day.", None, None),
    ("probe", "Reports", "Endpoint probe", "reports/probe.md", "Result of the last check of every data source endpoint.", None, None),
    ("runs", "Pipeline", "Run log", "data/runs.csv", "Log of every pipeline step: when it ran, success or failure, rows and requests.", "ts", None),
    ("state", "Pipeline", "Collector state", "data/state.json", "Bookkeeping the collector keeps between runs (what was fetched when).", None, None),
]
# day-partitioned folders: (id, group, name, folder under data/, description, timestamp column)
_PARTS = [
    ("snapshots", "Time series", "Hourly snapshots", "snapshots", "Hourly CCU, visits, favorites and votes for every tracked game. One file per day, gzipped in the repo after 3 days.", "ts"),
    ("rollups", "Time series", "Daily rollups", "rollups", "Daily summary per game: average, peak and low CCU, visits and favorites gained, like ratio.", "date"),
    ("discovery", "Time series", "Chart and search positions", "discovery", "Where games appeared on Roblox charts and searches (rank and player count) on each run.", "ts"),
    ("rolimons", "External", "Rolimons readings", "third_party/rolimons", "Rolimons' independent player counts, for cross-checking Roblox's own numbers.", "ts"),
    ("youtube", "External", "YouTube coverage", "youtube", "Videos and views per game from YouTube search, once a day.", "date"),
]
_SKIP_NAMES = {".gitkeep", ".gitignore"}
_JSON_COUNT_KEYS = ("niches", "ideas", "games", "items")


def _iso_mtime(p: Path) -> str:
    return st.iso(datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc))


def _decode(p: Path) -> bytes:
    raw = p.read_bytes()
    return gzip.decompress(raw) if p.name.endswith(".gz") else raw


def _table(raw: bytes) -> tuple[list[str], list[list[str]]]:
    rd = csv.reader(io.StringIO(raw.decode("utf-8", errors="replace"), newline=""))
    header = next(rd, [])
    return header, [r for r in rd if r]


def _latest(rows: list[list[str]], header: list[str], col: str | None) -> str:
    if not col or col not in header:
        return ""
    i = header.index(col)
    vals = [r[i] for r in rows if len(r) > i and r[i]]
    return max(vals) if vals else ""


def _write_csv(dst: Path, header: list[str], rows: list[list[str]]):
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def _safe_target(out_dir: Path | None) -> bool:
    """Never copy into (or clear) the tracker's own data/reports folders, e.g. when the page is built into the repo root."""
    if out_dir is None:
        return False
    out = out_dir.resolve()
    own = {st.DATA.resolve(), st.REPORTS.resolve(), st.ROOT.resolve()}
    return out not in own and (out / "data").resolve() not in own and (out / "reports").resolve() not in own


def _run_times() -> dict:
    """{step: timestamp of its last successful run} from data/runs.csv."""
    out = {}
    for r in st.read_rows(st.DATA / "runs.csv"):
        if r.get("ok") == "1":
            out[(r.get("task") or "").split(" ")[0]] = r.get("ts", "")
    return out


def _hub_file(meta, out_dir, copy, runs) -> dict:
    ds_id, group, name, rel, desc, ts_col, task = meta
    src = st.ROOT / rel
    d = {"id": ds_id, "group": group, "name": name, "src": rel, "desc": desc, "kind": "json" if rel.endswith(".json") else "md" if rel.endswith(".md") else "csv",
         "path": None, "rows": None, "unit": "rows", "bytes": 0, "updated": "", "basis": "", "note": "", "available": src.is_file()}
    if not src.is_file():
        d["note"] = "Not collected yet: appears after the first run that produces it."
        return d
    d["bytes"] = src.stat().st_size
    out_rel = rel
    dst = (out_dir / rel) if copy else None
    updated, basis = "", ""
    if d["kind"] == "csv":
        header, rows = _table(src.read_bytes())
        d["rows"] = len(rows)
        updated = _latest(rows, header, ts_col)
        cap = RUNS_CAP if ds_id == "runs" else None
        if cap is None and d["bytes"] > MAX_COPY_BYTES:
            cap = max(1000, int(len(rows) * MAX_COPY_BYTES / d["bytes"] * 0.9))
        if cap and len(rows) > cap:
            d["note"] = f"Copy holds the latest {cap:,} of {len(rows):,} rows; the full file is in the repository."
            if dst:
                _write_csv(dst, header, rows[-cap:])
        elif dst:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
        if updated:
            basis = f"latest {ts_col}"
    else:
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if d["kind"] == "md":
            d["rows"], d["unit"] = len(text.splitlines()), "lines"
        else:
            try:
                obj = json.loads(text)
            except ValueError:
                obj = None
            if isinstance(obj, list):
                d["rows"], d["unit"] = len(obj), "items"
            elif isinstance(obj, dict):
                key = next((k for k in _JSON_COUNT_KEYS if isinstance(obj.get(k), (list, dict)) and obj[k]), None)
                d["rows"], d["unit"] = (len(obj[key]), key) if key else (len(obj), "keys")
                updated = next((str(obj[k]) for k in ("as_of", "date", "updated") if isinstance(obj.get(k), str) and obj[k]), "")
                basis = "as_of" if updated else ""
        if dst:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
    if not updated and task and runs.get(task):
        updated, basis = runs[task], f"last {task} run"
    if not updated:
        updated, basis = _iso_mtime(src), "file time"
    d["updated"], d["basis"] = updated, basis
    d["path"] = out_rel if copy else None
    return d


def _day_files(folder: Path) -> list[tuple[str, Path]]:
    """[(YYYY-MM-DD, file)] newest first; one per day (a plain .csv wins over its .gz)."""
    found: dict[str, Path] = {}
    for p in folder.rglob("*"):
        m = DAY_RE.match(p.name)
        if m and p.is_file() and (m.group(1) not in found or not m.group(2)):
            found[m.group(1)] = p
    return sorted(found.items(), reverse=True)


def _hub_partition(ds_id, group, name, folder_rel, desc, ts_col, out_dir, copy) -> dict | None:
    folder = st.DATA / folder_rel
    if _safe_target(out_dir):  # day files copied by an older build would go stale
        shutil.rmtree(out_dir / "data" / folder_rel, ignore_errors=True)
    days = _day_files(folder) if folder.is_dir() else []
    d = {"id": ds_id, "group": group, "name": name, "src": f"data/{folder_rel}/YYYY/MM/*.csv", "desc": desc, "kind": "days", "path": None,
         "rows": None, "unit": "rows", "bytes": sum(p.stat().st_size for _, p in days), "updated": "", "basis": "", "note": "",
         "available": bool(days), "files": [], "total_days": len(days)}
    if not days:
        d["note"] = "Not collected yet: appears after the first run that produces it."
        return d
    rows_copied = 0
    for i, (day, p) in enumerate(days[:400]):
        entry = {"date": day, "name": p.name, "bytes": p.stat().st_size, "path": None, "rows": None, "repo": p.relative_to(st.ROOT).as_posix()}
        if i < HUB_DAYS:
            raw = _decode(p)
            header, rows = _table(raw)
            entry["rows"] = len(rows)
            rows_copied += len(rows)
            if i == 0:
                d["updated"] = _latest(rows, header, ts_col) or day
                d["basis"] = f"latest {ts_col}" if ts_col else "day"
            if copy:
                rel = p.relative_to(st.DATA).parent / f"{day}.csv"
                dst = out_dir / "data" / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(raw)
                entry["path"] = ("data" / rel).as_posix()
        d["files"].append(entry)
    d["rows"] = rows_copied
    d["unit"] = f"rows in the latest {min(HUB_DAYS, len(days))} day(s)"
    older = len(days) - HUB_DAYS
    d["note"] = f"{len(days)} day file(s) in the repository, {days[-1][0]} to {days[0][0]}. The latest {min(HUB_DAYS, len(days))} are published here as plain CSV" + (f"; the other {older} are listed by date (gzipped in the repo)." if older > 0 else ".")
    d["path"] = d["files"][0]["path"]
    return d


def pipeline_health(now) -> dict:
    """Per step of data/runs.csv: last run, ok or failed, and failures in the last 24 hours."""
    cut = st.iso(now - timedelta(hours=24))
    steps: dict[str, list[dict]] = {}
    for r in st.read_rows(st.DATA / "runs.csv"):
        if r.get("ts") and r.get("task"):
            steps.setdefault(r["task"].split(" ")[0], []).append(r)
    out = []
    for task, rs in steps.items():
        rs.sort(key=lambda r: r["ts"])
        last = rs[-1]
        ok_runs = [r for r in rs if r.get("ok") == "1"]
        recent = [r for r in rs if r["ts"] >= cut]
        out.append({"task": task, "last": last["ts"], "ok": last.get("ok") == "1", "last_ok": ok_runs[-1]["ts"] if ok_runs else None,
                    "runs24": len(recent), "fails24": sum(1 for r in recent if r.get("ok") != "1"),
                    "count": int(_f(last.get("count"), 0)), "requests": int(_f(last.get("requests"), 0)),
                    "detail": "" if last.get("ok") == "1" else (last.get("detail") or "")[:200]})
    out.sort(key=lambda s: (s["ok"], s["task"]))
    return {"steps": out, "runs": sum(len(v) for v in steps.values())}


def build_hub(out_dir: Path | None, now) -> dict:
    copy = _safe_target(out_dir)
    runs = _run_times()
    datasets, seen, seen_parts = [], set(), set()
    for meta in _FILES:
        seen.add(meta[3])
        datasets.append(_hub_file(meta, out_dir, copy, runs))
    for ds_id, group, name, folder, desc, ts_col in _PARTS:
        seen_parts.add(folder)
        datasets.append(_hub_partition(ds_id, group, name, folder, desc, ts_col, out_dir, copy))
    # anything else the tracker has written (new datasets show up without touching this list)
    for base, rel_dir in ((st.DATA, "data"), (st.REPORTS, "reports")):
        if not base.is_dir():
            continue
        for p in sorted(base.iterdir()):
            rel = f"{rel_dir}/{p.name}"
            if p.is_file() and p.name not in _SKIP_NAMES and p.suffix in (".csv", ".json", ".md") and rel not in seen and not p.name.endswith(".tmp"):
                datasets.append(_hub_file((re.sub(r"\W+", "_", rel), "Other", p.stem.replace("_", " ").capitalize(), rel,
                                           "Other file written by the tracker.", None, None), out_dir, copy, runs))
    if st.DATA.is_dir():
        known = {m[3] for m in _PARTS}
        folders = {}
        for p in st.DATA.rglob("*"):
            if DAY_RE.match(p.name) and p.is_file() and len(p.relative_to(st.DATA).parts) >= 4:
                folders.setdefault("/".join(p.relative_to(st.DATA).parts[:-3]), None)
        for folder in sorted(folders):
            if folder not in known:
                part = _hub_partition(re.sub(r"\W+", "_", folder), "Other", folder.split("/")[-1].replace("_", " ").capitalize(), folder,
                                      "Other day-partitioned data written by the tracker.", None, out_dir, copy)
                datasets.append(part)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    return {"datasets": datasets, "health": pipeline_health(now), "copied": copy, "hub_days": HUB_DAYS, "runs_cap": RUNS_CAP,
            "repo_blob": f"https://github.com/{repo}/blob/main/" if re.fullmatch(r"[\w.-]+/[\w.-]+", repo) else ""}


# ---------- thumbnails & styles ----------

def _slim_summary(s, full=True):
    if not s:
        return None
    out = {"n": s.get("n", 0), "med": s.get("med", {})}
    if full:
        out.update(hues=s.get("hues", []), lead_hue=s.get("lead_hue", {}), palette=s.get("palette", []))
    return out


def _img(name):
    return name if isinstance(name, str) and SAFE_FILE.match(name) else ""


def build_thumbs(out_dir: Path | None) -> dict | None:
    """reports/thumbs.json trimmed for the page; the images it references are copied to <out>/thumbs/."""
    tj = st.load_json(st.REPORTS / "thumbs.json", {})
    niches = tj.get("niches") or {}
    if not niches:
        return None
    files, out = set(), {}
    for key, n in niches.items():
        games = []
        for g in n.get("games", []):
            icon, media = _img(g.get("icon")), [m for m in (_img(x) for x in g.get("media", [])[:6]) if m]
            files.update([icon, *media])
            games.append({"id": str(g.get("id", "")), "name": g.get("name", ""), "ccu": g.get("ccu", 0), "age": g.get("age"), "young": bool(g.get("young")),
                          "icon": icon, "media": media, "art": g.get("art_style", ""), "map": g.get("map_style", ""), "src": g.get("style_source", "")})
        out[key] = {"type": n.get("type", ""), "key": n.get("key", key), "label": n.get("label", key), "games": games,
                    "icon": _slim_summary(n.get("icon")), "media": _slim_summary(n.get("media")),
                    "icon_young": _slim_summary(n.get("icon_young"), False), "media_young": _slim_summary(n.get("media_young"), False),
                    "icon_direction": n.get("icon_direction", []), "media_direction": n.get("media_direction", []),
                    "art_styles": n.get("art_styles", {}), "map_styles": n.get("map_styles", {}), "vision": n.get("vision")}
    files.discard("")
    copied = 0
    if _safe_target(out_dir):
        dst = out_dir / "thumbs"
        dst.mkdir(parents=True, exist_ok=True)
        for old in dst.iterdir():  # drop images no niche uses any more
            if old.is_file() and old.name not in files:
                old.unlink()
        src_dir = st.DATA / "thumbs" / "img"
        for f in files:
            s = src_dir / f
            if s.is_file() and not ((dst / f).exists() and (dst / f).stat().st_size == s.stat().st_size):
                shutil.copyfile(s, dst / f)
            copied += (dst / f).is_file()
    return {"as_of": tj.get("as_of", ""), "baseline": {k: _slim_summary(v) for k, v in (tj.get("baseline") or {}).items()},
            "niches": out, "styles": tj.get("styles") or {}, "images": len(files), "images_copied": copied}


def _styles_by_game():
    """{universe_id: {art_style, map_style, art_source, map_source}} and the style labels, or empty if the style code isn't usable."""
    try:
        from . import styles, trends
        return styles.load(trends.load_context()), styles.labels()
    except Exception as e:  # the dashboard must still build when a data/style file is broken
        print(f"dashboard: styles unavailable ({e})", file=sys.stderr)
        return {}, {"art_styles": {}, "map_styles": {}}


def build_data(out_dir: Path | None = None) -> dict:
    now = st.now_utc()
    games = st.load_games()
    active = {u: g for u, g in games.items() if str(g.get("active")) == "1"}
    niches, tagmap = {}, {}
    for t in st.read_rows(st.DATA / "tags.csv"):
        if _f(t.get("confidence"), 0) >= 0.6:
            if t["tag_type"] == "niche":
                niches.setdefault(t["universe_id"], []).append(t["tag"])
            elif t["tag_type"] in ("mechanic", "theme", "formula"):
                tagmap.setdefault(t["universe_id"], {}).setdefault(t["tag_type"], []).append(t["tag"])
    ins = {r["universe_id"]: r for r in st.read_rows(st.DATA / "insights.csv")}
    sty, style_labels = _styles_by_game()
    yt_by = {}
    for i in range(7, -1, -1):
        for y in st.read_rows(st.day_path("youtube", (now.date() - timedelta(days=i)).isoformat())):
            yt_by[y["universe_id"]] = y

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
        tm, gi, yy = tagmap.get(uid, {}), ins.get(uid, {}), yt_by.get(uid)
        gs = sty.get(uid, {})

        def num(v):
            n = _f(v)
            return None if n is None else (int(n) if float(n).is_integer() else n)
        rows.append({
            "id": uid, "place": g.get("root_place_id", ""), "name": g.get("name", ""), "creator": g.get("creator_name", ""),
            "genre": g.get("genre_l1") or "Unknown", "sub": g.get("genre_l2", ""), "niches": sorted(niches.get(uid, [])),
            "ccu": ccu, "peak": int(_f(g.get("peak_ccu"), 0)), "ch24": ch24,
            "age": (now.date() - created.date()).days if created else None,
            "like": round(up / (up + down), 3) if up is not None and down is not None and up + down > 0 else None,
            "fav": int(fav) if fav else None, "visits": int(visits) if visits else None,
            "spark": s,
            "mat": g.get("maturity") or None, "min_age": num(g.get("min_age")),
            "art": gs.get("art_style") or None, "mapst": gs.get("map_style") or None,
            "asrc": gs.get("art_source") or None, "msrc": gs.get("map_source") or None,
            "mech": sorted(tm.get("mechanic", [])), "theme": sorted(tm.get("theme", [])), "form": sorted(tm.get("formula", [])),
            "passes": num(gi.get("pass_count")), "price": num(gi.get("pass_price_median")),
            "ptypes": [x for x in (gi.get("pass_types") or "").split("|") if x],
            "badges": num(gi.get("badge_count")), "funnel": num(gi.get("badge_funnel_2nd")), "bday": num(gi.get("badge_daily_awarded")),
            "ytv": num(yy["views"]) if yy else None, "ytn": num(yy["videos"]) if yy else None,
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
    tr = st.load_json(st.REPORTS / "trends.json", {})
    tax = st.load_json(st.CONFIG / "taxonomy.json", {})
    slim = lambda items: [{k: i.get(k) for k in ("key", "label", "games", "young_games", "ccu", "young_ccu", "share",
                                                 "young_share", "momentum_adj", "growth", "young_examples")} for i in items]
    trends = {
        "rising": {d: slim(v) for d, v in tr.get("rising", {}).items()},
        "mechanics": slim(tr.get("dimensions", {}).get("mechanic", [])), "themes": slim(tr.get("dimensions", {}).get("theme", [])),
        "formulas": slim(tr.get("dimensions", {}).get("formula", [])),
        "art_styles": slim(tr.get("dimensions", {}).get("art_style", [])), "map_styles": slim(tr.get("dimensions", {}).get("map_style", [])),
        "style_coverage": tr.get("style_coverage", {}), "n_games": tr.get("games", 0),
        "gaps": tr.get("gaps", [])[:40], "pairs": tr.get("pairs", [])[:60], "benchmarks": tr.get("benchmarks", {}),
        "combos": [{"m": c["mechanic"], "t": c["theme"], "n": c["games"]} for c in tr.get("combos", [])] if tr else [],
        "young_days": tr.get("young_days", 60),
    }
    taxonomy = {
        "mechanics": {k: {"label": v["label"], "verb": v.get("verb", ""), "title_verb": v.get("title_verb", ""), "scope": v.get("scope", "medium"),
                          "fits": v.get("fits", {"primary": [], "ok": []})}
                      for k, v in tax.get("mechanics", {}).items()},
        "themes": {k: {"label": v["label"], "noun": v.get("noun", ""), "noun_plural": v.get("noun_plural", ""),
                       "kinds": v.get("kinds", [])} for k, v in tax.get("themes", {}).items()},
        "formulas": {k: v["label"] for k, v in tax.get("title_formulas", {}).items()},
    }
    return {
        "updated": st.iso(now), "days_of_data": rollup_days, "floor": st.settings().get("ccu_floor", 300),
        "games": rows, "line": line, "cohort_size": len(cohort),
        "genres": sorted(genres.values(), key=lambda g: -g["ccu"]),
        "niches": sorted(niche_agg.values(), key=lambda n: -n["ccu"]),
        "watch": wl.get("games", []), "watch_niches": wl.get("niches", []),
        "youtube": yt_rows[:40], "trends": trends, "taxonomy": taxonomy,
        "idea_lab_url": st.settings().get("idea_lab_url", ""),
        "styles": {"art": style_labels["art_styles"], "map": style_labels["map_styles"]},
        "hub": build_hub(out_dir, now), "thumbs": build_thumbs(out_dir),
    }


def build(out: str = "site/index.html") -> Path:
    p = Path(out)
    data = build_data(p.parent)
    page = st.fill_template(TEMPLATE, {"DATA": st.embed_json(data), "UPDATED": html.escape(data["updated"]), "HUBDAYS": str(HUB_DAYS)})
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(page, encoding="utf-8")
    return p


TEMPLATE = (Path(__file__).with_name("dashboard_template.html")).read_text(encoding="utf-8") \
    if (Path(__file__).with_name("dashboard_template.html")).exists() else "__DATA__"
