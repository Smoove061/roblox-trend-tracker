"""Session Analyzer tests: a synthetic 40-second "gameplay" clip with known events.

Events baked into the clip:
  flashes (VFX) + bright chimes at 5 s, 15 s, 25 s, 35 s
  a centre popup reading "BUY 2x LUCK 99 Robux" from 10 s to 13 s
  a low thud at 20 s
Skipped automatically if ffmpeg is not installed.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FONT = next((p for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                         "/System/Library/Fonts/Supplemental/Arial Bold.ttf"] if os.path.exists(p)), None)


def make_clip(path: Path):
    flash = "between(t,5,5.4)+between(t,15,15.4)+between(t,25,25.4)+between(t,35,35.4)"
    vf = [f"drawbox=x=0:y=0:w=iw:h=ih:color=yellow@0.85:t=fill:enable='{flash}'",
          "drawbox=x=iw*0.3:y=ih*0.3:w=iw*0.4:h=ih*0.4:color=white@1:t=fill:enable='between(t,10,13)'"]
    if FONT:
        vf.append(f"drawtext=fontfile='{FONT}':text='BUY 2x LUCK 99 Robux':fontsize=44:fontcolor=black:"
                  "x=(w-text_w)/2:y=(h-text_h)/2:enable='between(t,10,13)'")
    chime = "0.7*sin(2*PI*4200*t)*lt(mod(t-5,10),0.08)*gte(t,5)"
    thud = "0.9*sin(2*PI*70*t)*between(t,20,20.2)"
    subprocess.run(["ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=40",
                    "-f", "lavfi", "-i", f"aevalsrc='{chime}+{thud}+0.003*sin(2*PI*220*t)':s=44100:d=40",
                    "-vf", ",".join(vf), "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path)],
                   check=True)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class AnalyzerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "config", cls.tmp / "config")
        os.environ["TRACKER_ROOT"] = str(cls.tmp)
        for m in [m for m in sys.modules if m.startswith(("tracker", "analyzer"))]:
            del sys.modules[m]
        sys.path.insert(0, str(REPO))
        cls.prep = importlib.import_module("analyzer.prepare")
        cls.schema = importlib.import_module("analyzer.schema")
        cls.ingest = importlib.import_module("analyzer.ingest")
        cls.clip = cls.tmp / "clip.mp4"
        make_clip(cls.clip)
        cls.pack = cls.prep.prepare(str(cls.clip), "123456", "Test Game", str(cls.tmp / "dives"))
        cls.rows = json.loads((cls.pack / "timeline.json").read_text())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        os.environ.pop("TRACKER_ROOT", None)

    def test_pack_files(self):
        for f in ("pack.json", "timeline.json", "moments.json", "ANALYSIS.md", "findings.template.json", "strip.png"):
            self.assertTrue((self.pack / f).exists(), f)
        self.assertEqual(len(list((self.pack / "frames").glob("*.jpg"))), 40)
        self.assertEqual(len(list((self.pack / "sheets").glob("*.jpg"))), 4)  # 40 s / 12 per sheet
        self.assertTrue(list((self.pack / "keyframes").glob("*.jpg")))

    def test_flashes_detected_as_vfx(self):
        for s in (5, 15, 25, 35):
            self.assertTrue({"vfx_burst", "flash"} & set(self.rows[s]["flags"]), (s, self.rows[s]))

    def test_short_flash_visible_in_sheet_frame(self):
        from PIL import Image, ImageStat
        lum = lambda s: ImageStat.Stat(Image.open(self.pack / "frames" / f"{s:04d}.jpg").convert("L")).mean[0]
        self.assertGreater(lum(5), lum(4) + 20, "the 0.4 s flash at 5 s must appear in that second's frame")

    def test_flash_offset_not_counted(self):
        self.assertNotIn("flash", self.rows[6]["flags"])

    def test_popup_detected(self):
        self.assertTrue(any("center_popup" in self.rows[s]["flags"] for s in (10, 13)), [self.rows[s] for s in (10, 13)])

    def test_sound_onsets_and_tones(self):
        for s in (5, 15, 25, 35):
            self.assertGreaterEqual(self.rows[s]["sfx_onsets"], 1, s)
            self.assertIn("bright", self.rows[s]["sfx_tones"], s)
        self.assertIn("low", self.rows[20]["sfx_tones"])
        quiet = [r for r in self.rows if r["t"] in (2, 8, 18, 28) and r["sfx_onsets"]]
        self.assertEqual(quiet, [], "no onsets in silent stretches")

    @unittest.skipUnless(FONT and shutil.which("tesseract"), "needs a font and tesseract")
    def test_ocr_finds_shop_words(self):
        words = set()
        for s in (10, 11, 12):
            words |= set(self.rows[s]["ocr_keywords"])
        self.assertTrue({"buy", "robux", "luck"} & words, words)

    def test_moments_cover_events(self):
        m = json.loads((self.pack / "pack.json").read_text())["moments"]
        for s in (5, 15, 25, 35):
            self.assertTrue(any(abs(x - s) <= 1 for x in m), (s, m))

    def test_template_is_invalid_until_filled(self):
        tpl = json.loads((self.pack / "findings.template.json").read_text())
        self.assertTrue(self.schema.validate(tpl))

    def test_validate_and_ingest(self):
        f = json.loads((self.pack / "findings.template.json").read_text())
        f.update({
            "summary": "Hit lucky blocks for random brainrots; strong reveal moments every 10 seconds.",
            "loop": {"core_action": "hit lucky blocks", "reward": "random brainrot", "spend": "upgrades", "unlock": "",
                     "prestige": "", "retention_hooks": ["daily reward"], "social_layer": [], "monetization_points": ["2x luck pass"]},
            "timings": {"first_input_s": 1, "first_reward_s": 5, "first_upgrade_s": None, "first_shop_prompt_s": 10,
                        "first_purchase_prompt_s": 10, "first_social_s": None, "first_rare_reward_s": None},
            "ui": [{"t": 10, "element": "2x luck offer popup", "category": "popup", "position": "center", "notes": ""}],
            "vfx": [{"t": 5, "effect": "full-screen yellow flash", "trigger": "reward", "stages": ["impact", "fade"], "notes": ""}],
            "sfx": [{"t": 5, "sound": "bright chime", "role": "reward", "trigger": "reward"}],
            "patterns": ["loop:hit-for-drops", "ui:limited-offer-popup", "sfx:coin-chime", "vfx:brand-new-thing"],
            "confidence": "high",
        })
        self.assertEqual(self.schema.validate(f), [])
        bad = dict(f, patterns=["nonsense"], vfx=[{"t": 999, "effect": "x", "stages": ["boom"]}])
        errs = self.schema.validate(bad)
        self.assertTrue(any("pattern" in e for e in errs) and any("vfx[0].t" in e for e in errs) and any("stages" in e for e in errs))
        path = self.tmp / "findings.json"
        path.write_text(json.dumps(f))
        dest = self.ingest.ingest(str(path))
        self.assertTrue(dest.exists())
        md = (self.tmp / "reports" / "patterns.md").read_text()
        self.assertIn("Hit, swing or shoot targets", md)
        rows = {r["pattern"]: r for r in importlib.import_module("tracker.storage").read_rows(self.tmp / "data" / "patterns.csv")}
        self.assertEqual(rows["vfx:brand-new-thing"]["status"], "new")
        self.assertEqual(rows["loop:hit-for-drops"]["status"], "seen")
        self.assertIn("| all games |", md)
        self.assertIn("first shop prompt", md)

    def test_trend_after_three_rising_games(self):
        st = importlib.import_module("tracker.storage")
        st.write_rows(st.REPORTS / "game_metrics.csv", ["universe_id", "growth_7d", "flags"],
                      [{"universe_id": u, "growth_7d": 0.3, "flags": ""} for u in ("901", "902", "903")])
        base = json.loads((self.pack / "findings.template.json").read_text())
        for u in ("901", "902", "903"):
            f = dict(base, game={"universe_id": u, "name": f"G{u}"}, summary="A growing game with a strong loop here.",
                     loop=dict(base["loop"], core_action="hatch eggs"), ui=[], vfx=[], sfx=[],
                     patterns=["loop:egg-hatch-gacha"], confidence="medium")
            p = self.tmp / f"f{u}.json"
            p.write_text(json.dumps(f))
            self.ingest.ingest(str(p))
        rows = {r["pattern"]: r for r in st.read_rows(st.DATA / "patterns.csv")}
        self.assertEqual(rows["loop:egg-hatch-gacha"]["status"], "trend")


if __name__ == "__main__":
    unittest.main()
