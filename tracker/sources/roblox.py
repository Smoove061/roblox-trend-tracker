"""Roblox public web endpoints (no login).

Field names for the explore and search APIs are not officially documented, so
parsing is deliberately defensive: `walk_universes` finds any object carrying a
universe ID anywhere in a response. Run `python -m tracker probe` to see the
live shapes.
"""
from __future__ import annotations

import uuid

GAMES = "https://games.roblox.com/v1/games"
VOTES = "https://games.roblox.com/v1/games/votes"
EXPLORE_SORTS = "https://apis.roblox.com/explore-api/v1/get-sorts"
EXPLORE_CONTENT = "https://apis.roblox.com/explore-api/v1/get-sort-content"
OMNI = "https://apis.roblox.com/search-api/omni-search"
PLACE_UNIVERSE = "https://apis.roblox.com/universes/v1/places/{place_id}/universe"
PASSES = "https://apis.roblox.com/game-passes/v1/universes/{uid}/game-passes"
BADGES = "https://badges.roblox.com/v1/universes/{uid}/badges"

BATCH = 50

UID_KEYS = ("universeId", "universeID", "universe_id")
CCU_KEYS = ("playerCount", "playing", "players", "playerCountNumber")
NAME_KEYS = ("name", "title")
PLACE_KEYS = ("rootPlaceId", "placeId", "root_place_id")


def new_session_id() -> str:
    return str(uuid.uuid4())


def _first(d: dict, keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def walk_universes(obj):
    """Yield {universe_id, player_count, name, root_place_id} for every object that has a universe ID."""
    if isinstance(obj, dict):
        uid = _first(obj, UID_KEYS)
        if uid is not None and str(uid).isdigit():
            yield {
                "universe_id": str(uid),
                "player_count": _first(obj, CCU_KEYS),
                "name": _first(obj, NAME_KEYS),
                "root_place_id": _first(obj, PLACE_KEYS),
            }
        for v in obj.values():
            if isinstance(v, (dict, list)):
                yield from walk_universes(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_universes(v)


def _page_token(resp, *keys):
    if isinstance(resp, dict):
        for k in keys:
            if resp.get(k):
                return resp[k]
    return None


def explore_sorts(http, session_id, device="computer", country="all", max_pages=10):
    """Return [(sort_id, [universe dicts in rank order])] for every sort on the Charts/Home explore feed."""
    out = []
    token = None
    for _ in range(max_pages):
        resp = http.get_json(EXPLORE_SORTS, {
            "sessionId": session_id, "device": device, "country": country, "sortsPageToken": token,
        })
        sorts = resp.get("sorts", []) if isinstance(resp, dict) else []
        for s in sorts:
            if not isinstance(s, dict):
                continue
            sort_id = s.get("sortId") or s.get("id") or s.get("topicId")
            if not sort_id:
                continue
            out.append((str(sort_id), list(walk_universes(s.get("games") or s.get("contents") or []))))
        token = _page_token(resp, "nextSortsPageToken", "nextPageToken")
        if not token:
            break
    return out


def explore_sort_content(http, session_id, sort_id, device="computer", country="all", max_pages=5):
    games = []
    token = None
    for _ in range(max_pages):
        resp = http.get_json(EXPLORE_CONTENT, {
            "sessionId": session_id, "sortId": sort_id, "device": device, "country": country, "pageToken": token,
        })
        games.extend(walk_universes(resp.get("games", resp) if isinstance(resp, dict) else resp))
        token = _page_token(resp, "nextPageToken")
        if not token:
            break
    return games


def omni_search(http, session_id, query, pages=2):
    results = []
    token = None
    for _ in range(pages):
        resp = http.get_json(OMNI, {"searchQuery": query, "sessionId": session_id, "pageToken": token, "pageType": "all"})
        results.extend(walk_universes(resp))
        token = _page_token(resp, "nextPageToken")
        if not token:
            break
    return results


def _batches(ids, n=BATCH):
    ids = list(ids)
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def _bulk(http, url, universe_ids):
    """Fetch in batches of 50. A failed batch is skipped; only a total failure raises."""
    out, last_err, streak = {}, None, 0
    for chunk in _batches(universe_ids):
        try:
            resp = http.get_json(url, {"universeIds": ",".join(chunk)})
        except Exception as e:  # noqa: BLE001
            last_err, streak = e, streak + 1
            if streak >= 3 or type(e).__name__ == "BudgetExceeded":
                break  # an outage or throttling: stop instead of burning the run's time
            continue
        streak = 0
        for item in (resp or {}).get("data", []):
            out[str(item.get("id"))] = item
    if not out and last_err is not None:
        raise last_err
    return out


def game_details(http, universe_ids):
    return _bulk(http, GAMES, universe_ids)


def game_votes(http, universe_ids):
    return _bulk(http, VOTES, universe_ids)


def place_to_universe(http, place_id):
    resp = http.get_json(PLACE_UNIVERSE.format(place_id=place_id))
    uid = (resp or {}).get("universeId")
    return str(uid) if uid else None


def game_passes(http, uid, max_pages=5):
    passes = []
    token = None
    for _ in range(max_pages):
        resp = http.get_json(PASSES.format(uid=uid), {"passView": "Full", "pageSize": 100, "pageToken": token})
        items = (resp or {}).get("gamePasses") or (resp or {}).get("data") or []
        passes.extend(items)
        token = _page_token(resp, "nextPageToken")
        if not token:
            break
    return passes


def game_badges(http, uid, max_pages=5):
    badges = []
    cursor = None
    for _ in range(max_pages):
        resp = http.get_json(BADGES.format(uid=uid), {"limit": 100, "sortOrder": "Asc", "cursor": cursor})
        badges.extend((resp or {}).get("data", []))
        cursor = _page_token(resp, "nextPageCursor")
        if not cursor:
            break
    return badges
