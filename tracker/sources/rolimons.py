"""Rolimons game list: a free, keyless third-party feed of current player counts.

Undocumented, so parsing accepts either a list or a dict per game. Used as a
discovery source and a cross-check on Roblox's own CCU numbers.
"""
from __future__ import annotations

GAMELIST = "https://api.rolimons.com/games/v1/gamelist"


def fetch_gamelist(http):
    resp = http.get_json(GAMELIST)
    games = (resp or {}).get("games", {}) if isinstance(resp, dict) else {}
    out = []
    items = games.items() if isinstance(games, dict) else enumerate(games)
    for key, v in items:
        place_id, name, playing = None, None, None
        if isinstance(v, list):
            place_id = key
            name = v[0] if len(v) > 0 else None
            playing = v[1] if len(v) > 1 else None
        elif isinstance(v, dict):
            place_id = v.get("place_id") or v.get("placeId") or key
            name = v.get("name")
            playing = v.get("players") or v.get("playing") or v.get("player_count")
        try:
            playing = int(playing)
        except (TypeError, ValueError):
            continue
        if place_id is not None and str(place_id).isdigit():
            out.append({"place_id": str(place_id), "name": name or "", "playing": playing})
    return out
