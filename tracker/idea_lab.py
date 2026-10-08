"""Build the Claude Idea Lab page: trend data + today's ideas inlined into one HTML file.

    python -m tracker idea-lab [out.html] [--ideas ideas.json]

The page is published as a claude.ai artifact with the `sample` capability, so
viewers can ask Claude for new idea sets and full design docs. Artifact pages
can't fetch data, so a daily scheduled task rebuilds and republishes it.
`--ideas` embeds a starter set of ideas (written by that task).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import storage as st


def _f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def build_pack() -> dict:
    tr = st.load_json(st.REPORTS / "trends.json", {})
    tax = st.load_json(st.CONFIG / "taxonomy.json", {})
    games = {u: g for u, g in st.load_games().items() if str(g.get("active")) == "1"}
    tags = {}
    for t in st.read_rows(st.DATA / "tags.csv"):
        if _f(t.get("confidence"), 0) >= 0.6 and t["tag_type"] in ("mechanic", "theme"):
            tags.setdefault(t["universe_id"], {}).setdefault(t["tag_type"], []).append(t["tag"])
    now = st.now_utc().date()
    rows = []
    for uid, g in games.items():
        created = st.parse_iso(g.get("created", ""))
        rows.append({"id": uid, "name": g.get("name", ""), "place": g.get("root_place_id", ""), "ccu": int(_f(g.get("last_ccu"), 0)),
                     "age": (now - created.date()).days if created else None, "genre": g.get("genre_l1", ""), "sub": g.get("genre_l2", ""),
                     "mech": sorted(tags.get(uid, {}).get("mechanic", [])), "theme": sorted(tags.get(uid, {}).get("theme", []))})
    rows.sort(key=lambda r: -r["ccu"])
    young = [r for r in rows if r["age"] is not None and r["age"] <= tr.get("young_days", 60)][:40]

    def slim(items, n=10):
        return [{"key": i["key"], "label": i["label"], "momentum": i.get("momentum_adj"), "young_games": i["young_games"],
                 "young_ccu": i["young_ccu"], "games": i["games"], "share": i["share"],
                 "examples": [{"name": e["name"], "ccu": e["ccu"], "age": e["age"]} for e in i.get("young_examples", [])[:3]]}
                for i in items[:n]]
    return {
        "as_of": tr.get("as_of", st.iso(st.now_utc())), "games_tracked": len(rows),
        "young_days": tr.get("young_days", 60), "young_games": tr.get("young_games", 0),
        "young_ccu_share": round(tr.get("young_ccu", 0) / max(tr.get("total_ccu", 1), 1), 3),
        "rising": {d: slim(v) for d, v in tr.get("rising", {}).items()},
        "top_mechanics": slim(tr.get("dimensions", {}).get("mechanic", []), 14),
        "gaps": [{"mechanic": g["mechanic_label"], "theme": g["theme_label"], "score": g["score"]} for g in tr.get("gaps", [])[:20]],
        "pairs": [{"a": tax.get("mechanics", {}).get(p["a"], {}).get("label", p["a"]), "b": tax.get("mechanics", {}).get(p["b"], {}).get("label", p["b"]),
                   "young_games": p["games"], "young_ccu": p["young_ccu"]} for p in tr.get("pairs", [])[:15]],
        "benchmarks": {tax.get("mechanics", {}).get(k, {}).get("label", k): v for k, v in tr.get("benchmarks", {}).items()
                       if v.get("passes_median") is not None},
        "young_games_top": young,
        "top_games": [{"name": r["name"], "ccu": r["ccu"], "age": r["age"], "genre": r["genre"]} for r in rows[:25]],
        "mechanic_labels": {k: v["label"] for k, v in tax.get("mechanics", {}).items()},
        "theme_labels": {k: v["label"] for k, v in tax.get("themes", {}).items()},
        "names": [r["name"] for r in rows],
        "places": {r["name"]: r["place"] for r in rows if r["place"]},
    }


def build(out: str = "site/idea-lab.html", ideas_path: str | None = None) -> Path:
    pack = build_pack()
    ideas = {"date": None, "ideas": []}
    if ideas_path and Path(ideas_path).exists():
        ideas = json.loads(Path(ideas_path).read_text(encoding="utf-8"))
    elif (st.REPORTS / "daily_ideas.json").exists():
        ideas = st.load_json(st.REPORTS / "daily_ideas.json", ideas)
    tpl = Path(__file__).with_name("idea_lab_template.html").read_text(encoding="utf-8")
    page = st.fill_template(tpl, {"PACK": st.embed_json(pack), "IDEAS": st.embed_json(ideas)})
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(page, encoding="utf-8")
    return p
