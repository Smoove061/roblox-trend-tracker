"""Plain-text storage: CSV files partitioned by day, so git stores them efficiently
and nothing is lost if a run fails half way. `build-db` turns them into SQLite."""
from __future__ import annotations

import csv
import gzip
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("TRACKER_ROOT", Path(__file__).resolve().parent.parent))
DATA = ROOT / "data"
CONFIG = ROOT / "config"
REPORTS = ROOT / "reports"

# Slow-changing metadata (games.csv) is kept apart from the hourly status (game_status.csv)
# so the big file only changes in git when a game's details actually change.
META_FIELDS = [
    "universe_id", "root_place_id", "name", "creator_name", "creator_type", "creator_id",
    "genre", "genre_l1", "genre_l2", "created", "updated", "max_players", "price",
    "first_seen", "description",
]
STATUS_FIELDS = [
    "universe_id", "active", "last_ccu", "peak_ccu", "last_seen", "last_checked",
    "below_floor_since", "misses",
]
GAME_FIELDS = META_FIELDS + STATUS_FIELDS[1:]
SNAPSHOT_FIELDS = ["ts", "universe_id", "playing", "visits", "favorites", "up_votes", "down_votes"]
DISCOVERY_FIELDS = ["ts", "universe_id", "source", "list_id", "rank", "player_count"]
ROLLUP_FIELDS = [
    "date", "universe_id", "samples", "avg_ccu", "peak_ccu", "low_ccu",
    "visits_end", "visits_gained", "favorites_end", "favorites_gained", "up_votes", "down_votes", "like_ratio",
]
TAG_FIELDS = ["universe_id", "tag", "tag_type", "source", "confidence"]
PASS_FIELDS = ["universe_id", "pass_id", "name", "price", "fetched"]
BADGE_FIELDS = ["universe_id", "badge_id", "name", "awarded_count", "past_day_awarded", "win_rate", "created", "fetched"]
ROLI_FIELDS = ["ts", "place_id", "universe_id", "name", "playing"]
YT_FIELDS = ["date", "universe_id", "query", "videos", "views", "top_video_id", "top_video_views"]
RUN_FIELDS = ["ts", "task", "ok", "count", "requests", "detail"]
PLACE_FIELDS = ["place_id", "universe_id"]


def now_utc() -> datetime:
    override = os.environ.get("TRACKER_NOW")
    if override:
        return datetime.fromisoformat(override).astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def day_path(kind: str, day: str) -> Path:
    y, m, _ = day.split("-")
    return DATA / kind / y / m / f"{day}.csv"


def read_rows(path: Path) -> list[dict]:
    """Read a CSV, or its gzipped archive (path + '.gz') if the plain file was archived."""
    if path.exists():
        with path.open(newline="", encoding="utf-8", errors="replace") as f:
            return list(csv.DictReader(f))
    gz = path.with_name(path.name + ".gz")
    if gz.exists():
        with gzip.open(gz, "rt", newline="", encoding="utf-8", errors="replace") as f:
            return list(csv.DictReader(f))
    return []


def archive_old_days(kinds=("snapshots", "discovery", "third_party/rolimons"), keep_days: int = 3, today=None) -> int:
    """Gzip day files older than keep_days (about 8x smaller); read_rows reads them transparently."""
    today = today or now_utc().date()
    n = 0
    for kind in kinds:
        for p in (DATA / kind).rglob("*.csv"):
            try:
                day = datetime.strptime(p.stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            if (today - day).days > keep_days:
                gz = p.with_name(p.name + ".gz")
                with p.open("rb") as src, gzip.GzipFile(gz, "wb", mtime=0) as dst:
                    dst.write(src.read())
                p.unlink()
                n += 1
    return n


def append_rows(path: Path, fields: list[str], rows: list[dict]):
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: _clean(r.get(k, "")) for k in fields})


def write_rows(path: Path, fields: list[str], rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: _clean(r.get(k, "")) for k in fields})
    tmp.replace(path)


def _clean(v):
    if v is None:
        return ""
    if isinstance(v, str):
        # one line per record keeps git diffs readable; replace broken unicode (lone surrogates) so a write can never fail
        return " ".join(v.encode("utf-8", "replace").decode("utf-8").split())
    return v


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


def settings() -> dict:
    return load_json(CONFIG / "settings.json", {})


def load_games() -> dict[str, dict]:
    games = {r["universe_id"]: r for r in read_rows(DATA / "games.csv")}
    for r in read_rows(DATA / "game_status.csv"):
        if r["universe_id"] in games:
            games[r["universe_id"]].update(r)
    return games


def save_games(games: dict[str, dict]):
    rows = sorted(games.values(), key=lambda r: int(r["universe_id"]))
    write_rows(DATA / "games.csv", META_FIELDS, rows)
    write_rows(DATA / "game_status.csv", STATUS_FIELDS, rows)


def load_state() -> dict:
    return load_json(DATA / "state.json", {})


def save_state(state: dict):
    save_json(DATA / "state.json", state)


def log_run(task: str, ok: bool, count: int = 0, requests: int = 0, detail: str = ""):
    append_rows(DATA / "runs.csv", RUN_FIELDS, [{
        "ts": iso(now_utc()), "task": task, "ok": int(ok), "count": count,
        "requests": requests, "detail": detail[:500],
    }])
