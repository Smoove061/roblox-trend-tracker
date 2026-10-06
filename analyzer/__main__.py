"""Session Analyzer.

  python -m analyzer prepare VIDEO --game UNIVERSE_ID [--name NAME] [--out DIR] [--ocr auto|off]
  python -m analyzer validate FINDINGS.json
  python -m analyzer ingest FINDINGS.json
  python -m analyzer patterns
"""
import argparse
import json
import sys

from . import ingest as ing
from .schema import validate


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="analyzer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("video")
    p.add_argument("--game", required=True, help="universe ID")
    p.add_argument("--name", default="")
    p.add_argument("--out", default="dives")
    p.add_argument("--ocr", default="auto", choices=["auto", "off"])
    v = sub.add_parser("validate")
    v.add_argument("findings")
    i = sub.add_parser("ingest")
    i.add_argument("findings")
    sub.add_parser("patterns")
    a = ap.parse_args(argv)
    if a.cmd == "prepare":
        from .prepare import prepare
        out = prepare(a.video, a.game, a.name, a.out, a.ocr)
        print(f"dive pack: {out}\nnext: read {out}/ANALYSIS.md, fill findings.template.json -> findings.json, then ingest")
        return 0
    if a.cmd == "validate":
        errs = validate(json.load(open(a.findings, encoding="utf-8")))
        print("valid" if not errs else "\n".join(errs))
        return 0 if not errs else 1
    if a.cmd == "ingest":
        try:
            print(f"stored {ing.ingest(a.findings)}")
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        return 0
    if a.cmd == "patterns":
        print(ing.rebuild())
        return 0
    return 2


sys.exit(main())
