"""Trend confidence: one-hit and thin trends are flagged low, broad ones high."""
from __future__ import annotations

import unittest

from tracker.trends import add_confidence


def item(young_games, young_ccu, top, m=2.0):
    return {"young_games": young_games, "young_ccu": young_ccu, "momentum_adj": m,
            "young_examples": [{"ccu": top}] if top else []}


class Confidence(unittest.TestCase):
    def test_one_hit_is_low(self):
        i = add_confidence(item(3, 30000, 27000))
        self.assertEqual(i["confidence"], "low")
        self.assertIn("only 3 new games", i["confidence_note"])
        self.assertIn("90%", i["confidence_note"])

    def test_broad_is_high(self):
        self.assertEqual(add_confidence(item(27, 75000, 14000))["confidence"], "high")

    def test_mid_is_medium(self):
        self.assertEqual(add_confidence(item(7, 14000, 6000))["confidence"], "medium")

    def test_falling_momentum_downgrades(self):
        i = add_confidence(item(20, 100000, 30000, m=1.8), prev=2.4)
        self.assertEqual(i["confidence"], "medium")
        self.assertEqual(i["momentum_change"], -0.6)

    def test_no_examples(self):
        self.assertEqual(add_confidence(item(0, 0, 0))["confidence"], "low")


if __name__ == "__main__":
    unittest.main()
