"""Per-game insights and the trend engine.

    python -m tracker trends

Insights (data/insights.csv), one row per active game:
  monetization  pass count, median/min/max pass price, which pass types it sells
  progression   badge count, players awarded the 2nd most common badge vs the 1st (a retention proxy),
                and the most-awarded badge's past-day count (a rough new-players-per-day proxy)
  design        core mechanics, themes and title formulas (from tags)

Trends (reports/trends.json + reports/trends.md):
  For every mechanic, theme, title formula, genre and maturity level: how much of all CCU it holds,
  how much of *young-game* CCU it holds (games 60 days old or less), and momentum = young share / overall
  share. Momentum above 1 means new games in that category are winning more than its size would suggest:
  it's rising. Growth rates are folded in once there's 14 days of history.
  Combos (mechanic x theme) and gaps: rising mechanics paired with strong themes that no tracked game uses yet.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date

from . import storage as st

YOUNG_DAYS = 60
DIMENSIONS = ("mechanic", "theme", "formula", "genre", "subgenre", "maturity")


def _f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def load_context(today: date | None = None) -> dict:
    today = today or st.now_utc().date()
    games = {u: g for u, g in st.load_games().items() if str(g.get("active")) == "1"}
    tags = defaultdict(lambda: defaultdict(list))
    for t in st.read_rows(st.DATA / "tags.csv"):
        if _f(t.get("confidence"), 0) >= 0.6 and t["universe_id"] in games:
            tags[t["universe_id"]][t["tag_type"]].append(t["tag"])
    growth = {r["universe_id"]: _f(r.get("growth_7d")) for r in st.read_rows(st.REPORTS / "game_metrics.csv")}
    out = {}
    for uid, g in games.items():
        created = st.parse_iso(g.get("created", ""))
        age = (today - created.date()).days if created else None
        t = tags.get(uid, {})
        out[uid] = {
            "id": uid, "name": g.get("name", ""), "place": g.get("root_place_id", ""), "ccu": int(_f(g.get("last_ccu"), 0)),
            "age": age, "young": age is not None and age <= YOUNG_DAYS, "growth": growth.get(uid),
            "genre": [g["genre_l1"]] if g.get("genre_l1") else [], "subgenre": [g["genre_l2"]] if g.get("genre_l2") else [],
            "maturity": [g["maturity"]] if g.get("maturity") else [],
            "mechanic": sorted(t.get("mechanic", [])), "theme": sorted(t.get("theme", [])),
            "formula": sorted(t.get("formula", [])), "feature": sorted(t.get("feature", [])),
        }
    return out


def insights(ctx: dict) -> list[dict]:
    passes, badges = defaultdict(list), defaultdict(list)
    for p in st.read_rows(st.DATA / "passes.csv"):
        passes[p["universe_id"]].append(p)
    for b in st.read_rows(st.DATA / "badges.csv"):
        badges[b["universe_id"]].append(b)
    rows = []
    for uid, c in ctx.items():
        prices = sorted(x for x in (_f(p.get("price")) for p in passes.get(uid, [])) if x and x > 0)
        awarded = sorted((_f(b.get("awarded_count"), 0) for b in badges.get(uid, [])), reverse=True)
        daily_new = max((_f(b.get("past_day_awarded"), 0) for b in badges.get(uid, [])), default=None)
        rows.append({
            "universe_id": uid,
            "pass_count": len(passes.get(uid, [])) if uid in passes else "",
            "pass_price_median": int(statistics.median(prices)) if prices else "",
            "pass_price_min": int(prices[0]) if prices else "", "pass_price_max": int(prices[-1]) if prices else "",
            "pass_types": "|".join(f.replace("pass_", "") for f in c["feature"] if f.startswith("pass_")),
            "badge_count": len(badges.get(uid, [])) if uid in badges else "",
            "badge_funnel_2nd": round(awarded[1] / awarded[0], 3) if len(awarded) >= 2 and awarded[0] > 0 else "",
            "badge_daily_awarded": int(daily_new) if daily_new else "",
            "mechanics": "|".join(c["mechanic"]), "themes": "|".join(c["theme"]), "formulas": "|".join(c["formula"]),
        })
    rows.sort(key=lambda r: int(r["universe_id"]))
    st.write_rows(st.DATA / "insights.csv", INSIGHT_FIELDS, rows)
    return rows


INSIGHT_FIELDS = ["universe_id", "pass_count", "pass_price_median", "pass_price_min", "pass_price_max", "pass_types",
                  "badge_count", "badge_funnel_2nd", "badge_daily_awarded", "mechanics", "themes", "formulas"]


def _ex(c):
    return {"id": c["id"], "name": c["name"], "place": c["place"], "ccu": c["ccu"], "age": c["age"]}


def compute(ctx: dict, ins: list[dict] | None = None) -> dict:
    total = sum(c["ccu"] for c in ctx.values()) or 1
    young_total = sum(c["ccu"] for c in ctx.values() if c["young"]) or 1
    labels = st.load_json(st.CONFIG / "taxonomy.json", {})
    lab = {"mechanic": {k: v["label"] for k, v in labels.get("mechanics", {}).items()},
           "theme": {k: v["label"] for k, v in labels.get("themes", {}).items()},
           "formula": {k: v["label"] for k, v in labels.get("title_formulas", {}).items()}}
    verbs = {k: v.get("verb", "") for k, v in labels.get("mechanics", {}).items()}
    ins_by = {r["universe_id"]: r for r in (ins or [])}

    dims = {}
    for dim in DIMENSIONS:
        groups = defaultdict(list)
        for c in ctx.values():
            for key in c[dim]:
                groups[key].append(c)
        items = []
        for key, cs in groups.items():
            ccu = sum(c["ccu"] for c in cs)
            yc = [c for c in cs if c["young"]]
            yccu = sum(c["ccu"] for c in yc)
            share, yshare = ccu / total, yccu / young_total
            prior = 0.01 * young_total  # shrink small samples toward 1.0 so one lucky game can't fake a trend
            m_adj = (yccu + prior) / (share * young_total + prior) if share > 0 else None
            grs = [c["growth"] for c in cs if c["growth"] is not None]
            items.append({
                "key": key, "label": lab.get(dim, {}).get(key, key.replace("_", " ")),
                "games": len(cs), "young_games": len(yc), "ccu": ccu, "young_ccu": yccu,
                "share": round(share, 4), "young_share": round(yshare, 4),
                "momentum": round(yshare / share, 2) if share > 0 else None,
                "momentum_adj": round(m_adj, 2) if m_adj is not None else None,
                "growth": round(statistics.median(grs), 4) if len(grs) >= 3 else None,
                "young_examples": [_ex(c) for c in sorted(yc, key=lambda c: -c["ccu"])[:3]],
                "top_examples": [_ex(c) for c in sorted(cs, key=lambda c: -c["ccu"])[:3]],
            })
        items.sort(key=lambda i: -i["ccu"])
        dims[dim] = items

    def rising(dim, min_young=2, min_young_ccu=3000):
        r = [i for i in dims[dim] if i["young_games"] >= min_young and i["young_ccu"] >= min_young_ccu and (i["momentum_adj"] or 0) > 1.05]
        return sorted(r, key=lambda i: -(i["momentum_adj"] * (1 + (i["growth"] or 0))))

    # combos and gaps: which mechanic x theme pairings exist, and which promising ones nobody has built
    combos = defaultdict(list)
    for c in ctx.values():
        for m in c["mechanic"]:
            for t in c["theme"]:
                combos[(m, t)].append(c)
    combo_rows = []
    for (m, t), cs in combos.items():
        yc = [c for c in cs if c["young"]]
        combo_rows.append({"mechanic": m, "theme": t, "games": len(cs), "young_games": len(yc),
                           "ccu": sum(c["ccu"] for c in cs), "young_ccu": sum(c["ccu"] for c in yc),
                           "examples": [_ex(c) for c in sorted(cs, key=lambda c: -c["ccu"])[:2]]})
    combo_rows.sort(key=lambda r: -r["young_ccu"])
    r_mech = rising("mechanic")[:10]
    theme_items = {i["key"]: i for i in dims["theme"]}
    top_share = max((i["share"] for i in dims["theme"]), default=1e-9) or 1e-9
    strong_themes = sorted((i for i in dims["theme"] if i["games"] >= 5),
                           key=lambda i: -((i["momentum_adj"] or 0) * 0.6 + i["share"] / top_share * 0.4))[:14]
    gaps = []
    for m in r_mech:
        for t in strong_themes:
            if (m["key"], t["key"]) in combos:
                continue
            score = (m["momentum_adj"] or 0) * (0.5 + (t["momentum_adj"] or 0)) * (1 + min(t["share"] * 20, 1))
            gaps.append({"mechanic": m["key"], "mechanic_label": m["label"], "theme": t["key"], "theme_label": t["label"],
                         "score": round(score, 2), "mechanic_momentum": m["momentum_adj"], "theme_momentum": t["momentum_adj"],
                         "theme_ccu": t["ccu"]})
    gaps.sort(key=lambda g: -g["score"])

    # monetization and progression benchmarks per mechanic, from the 10 biggest games using it
    bench = {}
    for i in dims["mechanic"]:
        top = sorted((c for c in ctx.values() if i["key"] in c["mechanic"]), key=lambda c: -c["ccu"])[:10]
        rows = [ins_by[c["id"]] for c in top if c["id"] in ins_by]
        pc = [int(r["pass_count"]) for r in rows if str(r["pass_count"]).isdigit()]
        pm = [int(r["pass_price_median"]) for r in rows if str(r["pass_price_median"]).isdigit()]
        types = defaultdict(int)
        for r in rows:
            for p in filter(None, r["pass_types"].split("|")):
                types[p] += 1
        feats = defaultdict(int)
        for c in top:
            for f in c["feature"]:
                if not f.startswith("pass_"):
                    feats[f] += 1
        bench[i["key"]] = {
            "passes_median": int(statistics.median(pc)) if pc else None,
            "pass_price_median": int(statistics.median(pm)) if pm else None,
            "common_pass_types": [k for k, v in sorted(types.items(), key=lambda x: -x[1]) if v >= max(2, len(rows) // 3)][:5],
            "common_features": [k for k, v in sorted(feats.items(), key=lambda x: -x[1]) if v >= max(2, len(top) // 3)][:6],
            "verb": verbs.get(i["key"], ""),
        }

    # which mechanics successful young games combine (a natural "twist" for a new idea)
    pairs = defaultdict(lambda: {"games": 0, "young_ccu": 0})
    for c in ctx.values():
        if c["young"]:
            ms = c["mechanic"]
            for a in ms:
                for b in ms:
                    if a < b:
                        pairs[(a, b)]["games"] += 1
                        pairs[(a, b)]["young_ccu"] += c["ccu"]
    pair_rows = sorted(({"a": a, "b": b, **v} for (a, b), v in pairs.items()), key=lambda r: -r["young_ccu"])[:60]

    return {
        "as_of": st.iso(st.now_utc()), "young_days": YOUNG_DAYS, "pairs": pair_rows, "games": len(ctx), "total_ccu": total,
        "young_games": sum(1 for c in ctx.values() if c["young"]), "young_ccu": young_total if young_total > 1 else 0,
        "dimensions": dims,
        "rising": {d: rising(d)[:12] for d in ("mechanic", "theme", "formula", "subgenre")},
        "combos": combo_rows[:60], "gaps": gaps[:40], "benchmarks": bench,
    }


def write(trends: dict):
    st.save_json(st.REPORTS / "trends.json", trends)
    L = [f"# Uprising trends — {trends['as_of'][:10]}", "",
         f"{trends['games']:,} games tracked · {trends['young_games']} are {trends['young_days']} days old or less and hold "
         f"{trends['young_ccu'] / max(trends['total_ccu'], 1):.0%} of CCU.",
         "", "Momentum = a category's share of young-game CCU divided by its share of all CCU. Above 1 means new games in it are "
         "outperforming its size: it's rising.", ""]
    for dim, title in (("mechanic", "Rising core mechanics"), ("theme", "Rising themes"), ("formula", "Rising title formulas"),
                       ("subgenre", "Rising subgenres")):
        L += [f"## {title}", "", "| | Momentum | Young games | Young CCU | All games | Biggest young game |", "|---|---|---|---|---|---|"]
        for i in trends["rising"][dim]:
            ex = i["young_examples"][0] if i["young_examples"] else None
            L.append(f"| {i['label']} | {i['momentum_adj']}x | {i['young_games']} | {i['young_ccu']:,} | {i['games']} | "
                     f"{(ex['name'] + ' (' + format(ex['ccu'], ',') + ')') if ex else '–'} |")
        if not trends["rising"][dim]:
            L.append("| none yet | | | | | |")
        L.append("")
    L += ["## Untapped combinations (rising mechanic x strong theme, no tracked game)", "", "| Mechanic | Theme | Score |", "|---|---|---|"]
    L += [f"| {g['mechanic_label']} | {g['theme_label']} | {g['score']} |" for g in trends["gaps"][:20]]
    (st.REPORTS / "trends.md").write_text("\n".join(L) + "\n", encoding="utf-8")


def run() -> dict:
    ctx = load_context()
    ins = insights(ctx)
    t = compute(ctx, ins)
    write(t)
    return {"games": t["games"], "rising_mechanics": len(t["rising"]["mechanic"]), "gaps": len(t["gaps"])}
