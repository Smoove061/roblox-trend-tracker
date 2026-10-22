"""Market overview: week-vs-week on matched games, ranked against the market."""
from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest import mock

from tracker import overview


def fake_series(end, n_days, games):
    days = [(end - timedelta(days=i)).isoformat() for i in range(n_days - 1, -1, -1)]
    series = {}
    for uid, (a, b) in games.items():
        for i, d in enumerate(days):
            series.setdefault(uid, {})[d] = {"ccu": a if i < n_days - 7 else b, "visits": 10.0}
    return days, series


class Overview(unittest.TestCase):
    def test_week_vs_week_ranked_against_market(self):
        end = date(2026, 10, 21)
        meta = {
            "1": {"name": "Up", "place": "", "created": date(2025, 1, 1), "genre": ["A"], "subgenre": [], "mechanic": ["m1"], "theme": [], "formula": []},
            "2": {"name": "Flat", "place": "", "created": date(2025, 1, 1), "genre": ["B"], "subgenre": [], "mechanic": ["m2"], "theme": [], "formula": []},
            "3": {"name": "Down", "place": "", "created": date(2025, 1, 1), "genre": ["C"], "subgenre": [], "mechanic": ["m3"], "theme": [], "formula": []},
        }
        meta.update({str(i): {"name": f"g{i}", "place": "", "created": date(2025, 1, 1), "genre": ["A"], "subgenre": [], "mechanic": ["m1"], "theme": [], "formula": []} for i in (4, 5)})
        meta.update({str(i): {"name": f"g{i}", "place": "", "created": date(2025, 1, 1), "genre": ["B"], "subgenre": [], "mechanic": ["m2"], "theme": [], "formula": []} for i in (6, 7)})
        meta.update({str(i): {"name": f"g{i}", "place": "", "created": date(2025, 1, 1), "genre": ["C"], "subgenre": [], "mechanic": ["m3"], "theme": [], "formula": []} for i in (8, 9)})
        games = {"1": (3000, 6000), "4": (1000, 2000), "5": (1000, 2000),
                 "2": (3000, 3000), "6": (1000, 1000), "7": (1000, 1000),
                 "3": (3000, 1500), "8": (1000, 500), "9": (1000, 500)}
        with mock.patch.object(overview, "load_series", lambda e, n: fake_series(e, n, games)):
            o = overview.compute_window(7, end, meta, {})
        self.assertFalse(o.get("empty"))
        self.assertAlmostEqual(o["headline"]["ccu_change"], 0.1667, places=3)  # 15000 -> 17500
        g = o["categories"]["genre"]
        self.assertEqual(g["gainers"][0]["label"], "A")
        self.assertEqual(g["fallers"][0]["label"], "C")
        self.assertEqual([r["label"] for r in g["fallers"]], ["C", "B"])  # flat lags a rising market
        self.assertEqual(o["climbers"][0]["name"], "Up")

    def test_short_history_is_empty(self):
        with mock.patch.object(overview, "load_series", lambda e, n: fake_series(e, 4, {"1": (1, 1)})):
            o = overview.compute_window(7, date(2026, 10, 21), {}, {})
        self.assertTrue(o["empty"])


if __name__ == "__main__":
    unittest.main()
