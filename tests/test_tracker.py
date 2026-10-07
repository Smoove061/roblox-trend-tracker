"""End-to-end simulation against a fake Roblox, Rolimons and YouTube.

Runs the real hourly job every 6 simulated hours for 16 days, skipping day 10
entirely (as if GitHub never ran), then checks storage, catch-up, tagging,
metrics and the watchlist. Run: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import csv
import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
START = datetime(2026, 9, 1, 0, 30, tzinfo=timezone.utc)

# universe -> (name, place, created_days_before_start, ccu(t_days), description, genre_l1, genre_l2)
WORLD = {
    1: ("Steal a Brainrot", 101, 400, lambda t: 50000, "Steal brainrots from other bases! Rebirth and trade.", "Simulation", "Tycoon"),
    2: ("Lucky Block Golf", 102, 20, lambda t: 400 + 300 * t, "Golf balls at lucky blocks to win brainrots. Daily rewards!", "Sports & Racing", ""),
    3: ("Escape Tsunami For Brainrots", 103, 90, lambda t: 3000 * (1 + 0.05 * t), "Run from the tsunami, collect brainrots, rebirth.", "Simulation", ""),
    4: ("Grow a Garden", 104, 300, lambda t: 20000 * (1 - 0.03 * t), "Plant seeds, grow a garden, mutations and pets.", "Simulation", ""),
    5: ("Tiny Obby", 105, 10, lambda t: 100, "A small obby.", "Obby & Platformer", ""),
    6: ("Night Shift Horror", 106, 200, lambda t: 800, "Survive the night shift at a gas station. Scary!", "Survival", ""),
    7: ("Fisch Tycoon", 107, 150, lambda t: 2000, "Fishing tycoon with pets.", "Simulation", "Tycoon"),
    8: ("Fading Game", 108, 500, lambda t: 1000 if t < 5 else 120, "Old game.", "Roleplay & Avatar Sim", ""),
}
EXPLORE_IDS = [1, 2, 3, 4, 5, 6, 8]   # 7 is only discoverable through Rolimons


def sim_t():
    now = datetime.fromisoformat(os.environ["TRACKER_NOW"])
    return (now - START).total_seconds() / 86400


def game_obj(uid):
    name, place, age, ccu, desc, g1, g2 = WORLD[uid]
    t = sim_t()
    return {"id": uid, "rootPlaceId": place, "name": name, "description": desc,
            "creator": {"id": 9000 + uid, "name": f"Studio{uid}", "type": "Group"}, "price": None,
            "playing": int(ccu(t)), "visits": int(1_000_000 * uid + 5000 * t * uid), "maxPlayers": 12,
            "created": (START - timedelta(days=age)).isoformat().replace("+00:00", "Z"),
            "updated": "2026-09-01T00:00:00Z", "genre": "All", "genre_l1": g1, "genre_l2": g2,
            "favoritedCount": int(10000 * uid + 100 * t * uid)}


def listing(uid):
    g = game_obj(uid)
    return {"universeId": uid, "rootPlaceId": g["rootPlaceId"], "name": g["name"], "playerCount": g["playing"]}


class FakeNet:
    def __init__(self):
        self.calls = []
        self.fail = set()

    def __call__(self, url, headers, timeout):
        self.calls.append(url)
        p = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(p.query))
        host, path = p.netloc, p.path
        for f in self.fail:
            if f in url:
                return 500, {}, b"boom"
        body = None
        if host == "games.roblox.com" and path == "/v1/games":
            body = {"data": [game_obj(int(u)) for u in q["universeIds"].split(",") if int(u) in WORLD]}
        elif host == "games.roblox.com" and path == "/v1/games/votes":
            body = {"data": [{"id": int(u), "upVotes": 900 * int(u), "downVotes": 100} for u in q["universeIds"].split(",")]}
        elif path.endswith("/get-sorts"):
            body = {"sorts": [{"sortId": "top-playing-now", "games": [listing(u) for u in EXPLORE_IDS[:3]]},
                              {"sortId": "up-and-coming", "topicLayoutData": {}, "games": [listing(u) for u in EXPLORE_IDS[3:]]},
                              {"sortId": "filters", "filters": []}],
                    "nextSortsPageToken": ""}
        elif path.endswith("/get-sort-content"):
            ids = EXPLORE_IDS[:3] if q["sortId"] == "top-playing-now" else EXPLORE_IDS[3:] if q["sortId"] == "up-and-coming" else []
            body = {"games": [listing(u) for u in ids], "nextPageToken": ""}
        elif path.endswith("/omni-search"):
            hits = [u for u in EXPLORE_IDS if q["searchQuery"].split()[0].lower() in WORLD[u][0].lower()]
            body = {"searchResults": [{"contentGroupType": "Game", "contents": [listing(u) for u in hits]}], "nextPageToken": ""}
        elif host == "api.rolimons.com":
            t = sim_t()
            body = {"success": True, "games": {str(WORLD[u][1]): [WORLD[u][0], int(WORLD[u][3](t)), "thumb"] for u in (1, 7, 5)}}
        elif "/universes/v1/places/" in path:
            place = int(path.split("/")[4])
            body = {"universeId": next(u for u, w in WORLD.items() if w[1] == place)}
        elif "/game-passes" in path:
            uid = int(path.split("/")[4])
            body = {"gamePasses": [{"id": uid * 10, "name": "2x Luck", "price": 99}, {"id": uid * 10 + 1, "name": "VIP", "price": 299}], "nextPageToken": None}
        elif host == "badges.roblox.com":
            body = {"data": [{"id": 1, "name": "Welcome", "created": "2026-01-01T00:00:00Z",
                              "statistics": {"awardedCount": 100, "pastDayAwardedCount": 5, "winRatePercentage": 0.5}}], "nextPageCursor": None}
        elif host == "www.googleapis.com" and path.endswith("/search"):
            body = {"items": [{"id": {"videoId": "v1"}}, {"id": {"videoId": "v2"}}]}
        elif host == "www.googleapis.com" and path.endswith("/videos"):
            t = sim_t()
            spike = 5000 * (6 if t > 9 else 1)  # Night Shift (and everyone) gets coverage growth after day 9
            body = {"items": [{"id": "v1", "statistics": {"viewCount": str(spike)}}, {"id": "v2", "statistics": {"viewCount": "100"}}]}
        if body is None:
            return 404, {}, b"not found"
        return 200, {}, json.dumps(body).encode()


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class Simulation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "config", cls.tmp / "config")
        os.environ["TRACKER_ROOT"] = str(cls.tmp)
        os.environ["YOUTUBE_API_KEY"] = "test-key"
        s = json.loads((cls.tmp / "config/settings.json").read_text())
        s["youtube_games_per_day"] = 10
        (cls.tmp / "config/settings.json").write_text(json.dumps(s))
        for m in [m for m in sys.modules if m.startswith("tracker")]:
            del sys.modules[m]
        sys.path.insert(0, str(REPO))
        cls.st = importlib.import_module("tracker.storage")
        cls.cli = importlib.import_module("tracker.cli")
        cls.Http = importlib.import_module("tracker.http").Http
        cls.net = FakeNet()
        cls.codes = []
        for day in range(16):
            if day == 10:
                continue  # the whole day is missed
            for hour in (0, 6, 12, 18):
                os.environ["TRACKER_NOW"] = (START + timedelta(days=day, hours=hour)).isoformat()
                http = cls.Http("test", interval=0, budget=5000, transport=cls.net, sleep=lambda s: None)
                cls.codes.append(cls.cli.run(http))
        cls.data = cls.tmp / "data"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("TRACKER_ROOT", "TRACKER_NOW", "YOUTUBE_API_KEY"):
            os.environ.pop(k, None)

    def test_all_runs_succeed(self):
        self.assertTrue(all(c == 0 for c in self.codes), self.codes)
        fails = [r for r in read_csv(self.data / "runs.csv") if r["ok"] != "1"]
        self.assertEqual(fails, [])

    def test_tracked_games_respect_floor(self):
        games = self.st.load_games()
        self.assertNotIn("5", games, "a 100-CCU game must not be tracked")
        for uid in ("1", "2", "3", "4", "6", "7"):
            self.assertEqual(games[uid]["active"], "1", uid)
        self.assertEqual(games["8"]["active"], "0", "game below floor for 72h+ goes inactive")
        self.assertEqual(games["7"]["name"], "Fisch Tycoon", "Rolimons-only game was discovered")
        self.assertEqual(games["1"]["genre_l1"], "Simulation")

    def test_inactive_game_rechecked_daily_not_hourly(self):
        rows = read_csv(self.data / "snapshots/2026/09/2026-09-15.csv")
        self.assertEqual(sum(1 for r in rows if r["universe_id"] == "8"), 1)
        self.assertEqual(sum(1 for r in rows if r["universe_id"] == "1"), 4)

    def test_missed_day_is_caught_up(self):
        self.assertFalse((self.data / "snapshots/2026/09/2026-09-11.csv").exists())
        self.assertTrue((self.data / "rollups/2026/09/2026-09-11.csv").exists())
        r12 = {r["universe_id"]: r for r in read_csv(self.data / "rollups/2026/09/2026-09-12.csv")}
        self.assertEqual(r12["1"]["samples"], "4")
        self.assertNotEqual(r12["1"]["visits_gained"], "")
        for d in range(1, 16):
            self.assertTrue((self.data / f"rollups/2026/09/2026-09-{d:02d}.csv").exists(), d)

    def test_rollup_math(self):
        r = {x["universe_id"]: x for x in read_csv(self.data / "rollups/2026/09/2026-09-03.csv")}
        self.assertEqual(float(r["1"]["avg_ccu"]), 50000.0)
        self.assertEqual(r["1"]["like_ratio"], "0.9")
        self.assertGreater(int(r["2"]["peak_ccu"]), int(r["2"]["low_ccu"]))
        self.assertEqual(int(r["1"]["visits_gained"]), 5000)  # 5000/day for universe 1

    def test_tags(self):
        tags = read_csv(self.data / "tags.csv")
        t1 = {(t["tag"], t["tag_type"]) for t in tags if t["universe_id"] == "1"}
        for expected in [("brainrot", "niche"), ("steal_a", "niche"), ("rebirth", "feature"),
                         ("trading", "feature"), ("pass_luck", "feature"), ("Simulation", "genre"), ("Tycoon", "subgenre")]:
            self.assertIn(expected, t1)
        t6 = {t["tag"] for t in tags if t["universe_id"] == "6"}
        self.assertIn("horror", t6)
        self.assertNotIn("roleplay_city", t6, "short keywords must match whole words only")

    def test_passes_and_badges(self):
        passes = read_csv(self.data / "passes.csv")
        self.assertEqual(len({p["universe_id"] for p in passes}), len({p["universe_id"] for p in passes}))
        self.assertTrue(any(p["name"] == "2x Luck" and p["price"] == "99" for p in passes))
        self.assertTrue(read_csv(self.data / "badges.csv"))
        self.assertEqual(len([p for p in passes if p["universe_id"] == "1"]), 2, "refresh replaces, never duplicates")

    def test_watchlist(self):
        metrics = {r["universe_id"]: r for r in read_csv(self.tmp / "reports/game_metrics.csv")}
        self.assertIn("young_breakout", metrics["2"]["flags"])
        self.assertNotIn("young_breakout", metrics["1"]["flags"])
        self.assertLess(float(metrics["4"]["growth_7d"]), 0)
        self.assertIn("coverage_spike", metrics["6"]["flags"])
        md = (self.tmp / "reports/watchlist.md").read_text()
        self.assertIn("Lucky Block Golf", md)
        niches = {r["niche"]: r for r in read_csv(self.tmp / "reports/niche_metrics.csv")}
        self.assertIn("brainrot", niches)
        self.assertEqual(niches["brainrot"]["window_days"], "7")

    def test_build_db(self):
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            self.cli.build_db(str(self.tmp / "t.db"))
        finally:
            os.chdir(cwd)
        import sqlite3
        con = sqlite3.connect(self.tmp / "t.db")
        n = con.execute("select count(*) from snapshots where universe_id = 1").fetchone()[0]
        self.assertEqual(n, 15 * 4)
        con.close()


class Resilience(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "config", self.tmp / "config")
        os.environ["TRACKER_ROOT"] = str(self.tmp)
        os.environ.pop("YOUTUBE_API_KEY", None)
        os.environ["TRACKER_NOW"] = START.isoformat()
        for m in [m for m in sys.modules if m.startswith("tracker")]:
            del sys.modules[m]
        self.cli = importlib.import_module("tracker.cli")
        self.Http = importlib.import_module("tracker.http").Http

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("TRACKER_ROOT", None)
        os.environ.pop("TRACKER_NOW", None)

    def http(self, net):
        return self.Http("test", interval=0, budget=5000, transport=net, sleep=lambda s: None)

    def test_discovery_outage_does_not_stop_snapshots(self):
        net = FakeNet()
        net.fail = {"explore-api", "omni-search"}
        code = self.cli.run(self.http(net))
        self.assertEqual(code, 0)
        games = read_csv(self.tmp / "data/games.csv")
        self.assertEqual({g["universe_id"] for g in games}, {"1", "7"}, "Rolimons still discovers games")

    def test_snapshot_failure_exits_nonzero_but_keeps_data(self):
        net = FakeNet()
        self.cli.run(self.http(net))
        net.fail = {"games.roblox.com/v1/games?"}
        os.environ["TRACKER_NOW"] = (START + timedelta(hours=1)).isoformat()
        self.assertEqual(self.cli.run(self.http(net)), 1)
        self.assertTrue((self.tmp / "data/games.csv").exists())

    def test_broken_unicode_cannot_stall_the_run(self):
        net = FakeNet()
        orig = WORLD[1]
        WORLD[1] = (orig[0], orig[1], orig[2], orig[3], "Steal brainrots \ud83d now", orig[5], orig[6])
        try:
            self.assertEqual(self.cli.run(self.http(net)), 0)
        finally:
            WORLD[1] = orig
        self.assertIn("1", {g["universe_id"] for g in read_csv(self.tmp / "data/games.csv")})

    def test_vanished_game_goes_inactive(self):
        net = FakeNet()
        self.cli.run(self.http(net))
        saved = WORLD.pop(3)
        try:
            for h in range(1, 26):
                os.environ["TRACKER_NOW"] = (START + timedelta(hours=h)).isoformat()
                self.cli.run(self.http(net))
        finally:
            WORLD[3] = saved
        st = importlib.import_module("tracker.storage")
        self.assertEqual(str(st.load_games()["3"]["active"]), "0")
        self.assertEqual(str(st.load_games()["1"]["active"]), "1")

    def test_badges_failure_does_not_duplicate_passes(self):
        net = FakeNet()
        net.fail = {"badges.roblox.com"}
        for d in range(3):
            os.environ["TRACKER_NOW"] = (START + timedelta(days=d)).isoformat()
            self.cli.run(self.http(net))
        passes = read_csv(self.tmp / "data/passes.csv")
        ids = [(p["universe_id"], p["pass_id"]) for p in passes]
        self.assertTrue(passes)
        self.assertEqual(len(ids), len(set(ids)))

    def test_archived_days_still_readable(self):
        net = FakeNet()
        for d in range(6):
            os.environ["TRACKER_NOW"] = (START + timedelta(days=d)).isoformat()
            self.cli.run(self.http(net))
        st = importlib.import_module("tracker.storage")
        p = st.day_path("snapshots", "2026-09-01")
        self.assertFalse(p.exists())
        self.assertTrue(p.with_name(p.name + ".gz").exists())
        self.assertTrue(st.read_rows(p))

    def test_api_key_redacted_in_errors(self):
        h = self.Http("t", interval=0, max_retries=0, transport=lambda u, hd, to: (403, {}, b"quota"), sleep=lambda s: None)
        with self.assertRaises(Exception) as cm:
            h.get_json("https://www.googleapis.com/youtube/v3/search", {"q": "x", "key": "SECRET123"})
        self.assertNotIn("SECRET123", str(cm.exception))

    def test_retry_after_429(self):
        seq = [(429, {"Retry-After": "2"}, b""), (200, {}, b'{"ok": 1}')]
        waits = []
        h = self.Http("t", interval=0, transport=lambda u, hd, to: seq.pop(0), sleep=waits.append)
        self.assertEqual(h.get_json("https://x.test/a"), {"ok": 1})
        self.assertIn(2.0, waits)


if __name__ == "__main__":
    unittest.main()


class DashboardBuild(unittest.TestCase):
    def test_builds_from_simulated_data(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            shutil.copytree(REPO / "config", tmp / "config")
            os.environ["TRACKER_ROOT"] = str(tmp)
            os.environ["TRACKER_NOW"] = START.isoformat()
            for m in [m for m in sys.modules if m.startswith("tracker")]:
                del sys.modules[m]
            cli = importlib.import_module("tracker.cli")
            Http = importlib.import_module("tracker.http").Http
            net = FakeNet()
            for h in range(0, 6):
                os.environ["TRACKER_NOW"] = (START + timedelta(hours=h)).isoformat()
                cli.run(Http("t", interval=0, budget=5000, transport=net, sleep=lambda s: None))
            dash = importlib.import_module("tracker.dashboard")
            out = dash.build(str(tmp / "site" / "index.html"))
            page = out.read_text()
            self.assertNotIn("__DATA__", page)
            data = json.loads(page.split('<script id="data" type="application/json">')[1].split("</script>")[0].replace("<\\/", "</"))
            self.assertEqual({g["id"] for g in data["games"]}, {"1", "2", "3", "4", "6", "7", "8"})
            self.assertGreaterEqual(len(data["line"]), 5)
            self.assertEqual(data["genres"][0]["name"], "Simulation")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            os.environ.pop("TRACKER_ROOT", None)
            os.environ.pop("TRACKER_NOW", None)
