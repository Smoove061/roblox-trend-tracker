"""The findings format for one deep dive, and a validator (no external dependencies).

Every observation carries a timestamp in seconds so it can be checked against the recording.
"""
from __future__ import annotations

import re

UI_CATEGORIES = {"hud", "currency", "shop", "popup", "notification", "tutorial", "leaderboard", "inventory",
                 "progress", "social", "settings", "button", "other"}
SCREEN_POSITIONS = {"top-left", "top-center", "top-right", "left", "center", "right", "bottom-left",
                    "bottom-center", "bottom-right", "fullscreen", "world"}
VFX_STAGES = {"anticipation", "buildup", "impact", "follow-through", "fade"}
SFX_ROLES = {"reward", "purchase", "ui", "impact", "ambient", "music", "alert", "rarity", "voice", "other"}
PATTERN_TYPES = {"loop", "ui", "vfx", "sfx", "monetization", "retention", "onboarding", "social"}
PATTERN_ID = re.compile(r"^(" + "|".join(sorted(PATTERN_TYPES)) + r"):[a-z0-9][a-z0-9_-]*$")
TIMING_KEYS = ["first_input_s", "first_reward_s", "first_upgrade_s", "first_shop_prompt_s",
               "first_purchase_prompt_s", "first_social_s", "first_rare_reward_s"]


def template(universe_id: str, name: str, date: str, duration: float) -> dict:
    return {
        "game": {"universe_id": universe_id, "name": name},
        "recorded": date,
        "duration_s": round(duration, 1),
        "summary": "One or two sentences: what the player does and why it is sticky.",
        "loop": {
            "core_action": "", "reward": "", "spend": "", "unlock": "", "prestige": "",
            "retention_hooks": [], "social_layer": [], "monetization_points": [],
        },
        "timings": {k: None for k in TIMING_KEYS},
        "onboarding": {"style": "", "teaches_by": "", "notes": ""},
        "ui": [{"t": 0, "element": "", "category": "hud", "position": "top-center", "notes": ""}],
        "vfx": [{"t": 0, "effect": "", "trigger": "", "stages": ["impact"], "notes": ""}],
        "sfx": [{"t": 0, "sound": "", "role": "reward", "trigger": ""}],
        "patterns": ["loop:collect-sell-upgrade"],
        "standout": ["What this game does better than its niche, with a timestamp."],
        "weaknesses": ["What feels weak or generic, with a timestamp."],
        "confidence": "medium",
    }


def validate(f: dict) -> list[str]:
    """Return a list of problems; empty means the findings are valid."""
    errs = []

    def need(cond, msg):
        if not cond:
            errs.append(msg)

    g = f.get("game") or {}
    need(str(g.get("universe_id", "")).isdigit(), "game.universe_id must be the numeric universe ID")
    dur = f.get("duration_s")
    need(isinstance(dur, (int, float)) and dur > 0, "duration_s must be a positive number")
    dur = dur if isinstance(dur, (int, float)) else 10 ** 9
    need(bool(re.match(r"^\d{4}-\d{2}-\d{2}$", str(f.get("recorded", "")))), "recorded must be YYYY-MM-DD")
    need(isinstance(f.get("summary"), str) and len(f["summary"]) >= 10 and "One or two sentences" not in f["summary"],
         "summary must be filled in")
    loop = f.get("loop") or {}
    need(bool(loop.get("core_action")), "loop.core_action is required")
    for k in ("retention_hooks", "social_layer", "monetization_points"):
        need(isinstance(loop.get(k, []), list), f"loop.{k} must be a list")
    timings = f.get("timings") or {}
    for k, v in timings.items():
        need(k in TIMING_KEYS, f"timings.{k} is not a known timing ({', '.join(TIMING_KEYS)})")
        need(v is None or (isinstance(v, (int, float)) and 0 <= v <= dur), f"timings.{k} must be null or 0..duration")

    def check_list(key, required, extra):
        items = f.get(key, [])
        need(isinstance(items, list), f"{key} must be a list")
        for i, it in enumerate(items if isinstance(items, list) else []):
            t = it.get("t")
            need(isinstance(t, (int, float)) and 0 <= t <= dur, f"{key}[{i}].t must be within the recording")
            for r in required:
                need(bool(it.get(r)), f"{key}[{i}].{r} is required")
            extra(i, it)

    check_list("ui", ["element"], lambda i, it: (
        need(it.get("category") in UI_CATEGORIES, f"ui[{i}].category must be one of {sorted(UI_CATEGORIES)}"),
        need(it.get("position") in SCREEN_POSITIONS, f"ui[{i}].position must be one of {sorted(SCREEN_POSITIONS)}")))
    check_list("vfx", ["effect"], lambda i, it: need(
        isinstance(it.get("stages", []), list) and set(it.get("stages", [])) <= VFX_STAGES,
        f"vfx[{i}].stages must be a subset of {sorted(VFX_STAGES)}"))
    check_list("sfx", ["sound"], lambda i, it: need(it.get("role") in SFX_ROLES,
                                                     f"sfx[{i}].role must be one of {sorted(SFX_ROLES)}"))
    pats = f.get("patterns", [])
    need(isinstance(pats, list) and pats, "patterns must be a non-empty list")
    for p in pats if isinstance(pats, list) else []:
        need(bool(PATTERN_ID.match(str(p))), f"pattern '{p}' must look like type:slug, type in {sorted(PATTERN_TYPES)}")
    need(f.get("confidence") in ("low", "medium", "high"), "confidence must be low, medium or high")
    return errs
