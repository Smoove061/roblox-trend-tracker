"""Hourly work: discovery of new games, CCU snapshots, and the Rolimons cross-check."""
from __future__ import annotations

from datetime import timedelta

from . import storage as st
from .sources import roblox, rolimons


def _to_int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def upsert_game(games: dict, uid: str, d: dict, now_iso: str) -> dict:
    g = games.get(uid) or {f: "" for f in st.GAME_FIELDS}
    creator = d.get("creator") or {}

    def keep(new, field):
        """Missing or null in the response never wipes a value we already have. Stored as text."""
        return g.get(field, "") if new in (None, "") else str(new)

    g.update({
        "universe_id": uid,
        "root_place_id": keep(d.get("rootPlaceId"), "root_place_id"),
        "name": keep(d.get("name"), "name"),
        "creator_name": keep(creator.get("name"), "creator_name"),
        "creator_type": keep(creator.get("type"), "creator_type"),
        "creator_id": keep(creator.get("id"), "creator_id"),
        "genre": keep(d.get("genre"), "genre"),
        "genre_l1": keep(d.get("genre_l1"), "genre_l1"),
        "genre_l2": keep(d.get("genre_l2"), "genre_l2"),
        "created": keep(d.get("created"), "created"),
        "updated": keep(d.get("updated"), "updated"),
        "max_players": keep(d.get("maxPlayers"), "max_players"),
        "price": keep(d.get("price"), "price"),
        "description": keep((d.get("description") or "")[:1000] or None, "description"),
    })
    if not g.get("first_seen"):
        g["first_seen"] = now_iso
    g["last_checked"] = now_iso
    g["misses"] = 0
    games[uid] = g
    return g


def apply_ccu(g: dict, playing: int, now, floor: int, inactive_after_h: int):
    now_iso = st.iso(now)
    g["last_ccu"] = playing
    g["last_seen"] = now_iso
    g["peak_ccu"] = max(_to_int(g.get("peak_ccu"), 0), playing)
    if playing >= floor:
        g["below_floor_since"] = ""
        g["active"] = 1
    else:
        since = st.parse_iso(g.get("below_floor_since", ""))
        if since is None:
            g["below_floor_since"] = now_iso
            since = now
        if now - since >= timedelta(hours=inactive_after_h):
            g["active"] = 0


def add_candidates(http, games: dict, candidate_ids, cfg, now) -> list[str]:
    """Look up candidate universes and start tracking those at or above the CCU floor."""
    floor = cfg["ccu_floor"]
    ids = [u for u in candidate_ids if str(games.get(u, {}).get("active", "")) != "1"]
    if not ids:
        return []
    details = roblox.game_details(http, ids)
    added = []
    for uid, d in details.items():
        playing = _to_int(d.get("playing"), 0)
        if playing >= floor:
            g = upsert_game(games, uid, d, st.iso(now))
            g["active"] = 1
            g["below_floor_since"] = ""
            added.append(uid)
    return added


def discover(http, games: dict, cfg: dict, now) -> dict:
    session = roblox.new_session_id()
    ts = st.iso(now)
    rows, seen = [], {}
    errors = []

    def note(source, list_id, items):
        for rank, it in enumerate(items, 1):
            uid = it["universe_id"]
            rows.append({"ts": ts, "universe_id": uid, "source": source, "list_id": list_id,
                         "rank": rank, "player_count": it.get("player_count") or ""})
            pc = _to_int(it.get("player_count"))
            seen[uid] = max(seen.get(uid, -1), pc if pc is not None else -1)

    try:
        sorts = roblox.explore_sorts(http, session, cfg.get("device", "computer"), cfg.get("country", "all"),
                                     cfg.get("max_sort_pages", 10))
    except Exception as e:  # noqa: BLE001 - keep going with other sources
        sorts, errors = [], [f"get-sorts: {e}"]
    for sort_id, items in sorts:
        try:
            more = roblox.explore_sort_content(http, session, sort_id, cfg.get("device", "computer"),
                                               cfg.get("country", "all"), cfg.get("max_sort_content_pages", 5))
            if len(more) >= len(items):
                items = more
        except Exception as e:  # noqa: BLE001
            errors.append(f"sort {sort_id}: {e}")
        note("explore", sort_id, items)

    streak = 0
    for kw in cfg.get("omni_search_keywords", []):
        try:
            note("search", kw, roblox.omni_search(http, session, kw, cfg.get("omni_search_pages", 2)))
            streak = 0
        except Exception as e:  # noqa: BLE001
            errors.append(f"search {kw}: {e}")
            streak += 1
            if streak >= 3:
                errors.append("search: stopped after 3 failures in a row")
                break

    st.append_rows(st.day_path("discovery", ts[:10]), st.DISCOVERY_FIELDS, rows)
    floor = cfg["ccu_floor"]
    # unknown counts (-1) are checked too; known counts below 80% of the floor are skipped
    candidates = [u for u, pc in seen.items() if pc < 0 or pc >= floor * 0.8]
    added = add_candidates(http, games, candidates, cfg, now)
    return {"listed": len(rows), "unique": len(seen), "added": len(added), "errors": errors}


def snapshot(http, games: dict, cfg: dict, now) -> dict:
    floor = cfg["ccu_floor"]
    recheck = timedelta(hours=cfg.get("inactive_recheck_hours", 24))
    ids = []
    for uid, g in games.items():
        if str(g.get("active")) == "1":
            ids.append(uid)
        else:
            last = st.parse_iso(g.get("last_checked", ""))
            if last is None or now - last >= recheck:
                ids.append(uid)
    if not ids:
        return {"polled": 0}
    details = roblox.game_details(http, ids)
    try:
        votes = roblox.game_votes(http, list(details))
    except Exception:  # noqa: BLE001 - votes are nice to have
        votes = {}
    ts = st.iso(now)
    rows = []
    for uid, d in details.items():
        g = upsert_game(games, uid, d, ts)
        playing = _to_int(d.get("playing"), 0)
        apply_ccu(g, playing, now, floor, cfg.get("inactive_after_hours", 72))
        v = votes.get(uid, {})
        rows.append({
            "ts": ts, "universe_id": uid, "playing": playing, "visits": d.get("visits", ""),
            "favorites": d.get("favoritedCount", ""), "up_votes": v.get("upVotes", ""), "down_votes": v.get("downVotes", ""),
        })
    if details:  # only count misses when the API answered for other games (not during an outage)
        for uid in set(ids) - set(details):
            g = games.get(uid)
            if g is None:
                continue
            g["last_checked"] = ts
            g["misses"] = _to_int(g.get("misses"), 0) + 1
            if g["misses"] >= 24:  # gone from the API for a day: deleted or made private
                g["active"] = 0
    st.append_rows(st.day_path("snapshots", ts[:10]), st.SNAPSHOT_FIELDS, rows)
    return {"polled": len(rows), "active": sum(1 for g in games.values() if str(g.get("active")) == "1")}


def third_party(http, games: dict, cfg: dict, now, max_resolve: int = 150) -> dict:
    if not cfg.get("rolimons_enabled", True):
        return {"skipped": True}
    floor = cfg["ccu_floor"]
    listing = rolimons.fetch_gamelist(http)
    cache_path = st.DATA / "place_universe.csv"
    cache = {r["place_id"]: r["universe_id"] for r in st.read_rows(cache_path)}
    by_place = {g["root_place_id"]: uid for uid, g in games.items() if g.get("root_place_id")}
    new_cache, candidates, resolved = [], [], 0
    ts = st.iso(now)
    rows = []
    for it in sorted(listing, key=lambda x: -x["playing"]):  # spend the lookup cap on the biggest games first
        pid = it["place_id"]
        uid = by_place.get(pid) or cache.get(pid, "")
        if uid == "-":  # known to have no resolvable universe
            uid = ""
        elif not uid and pid not in cache and it["playing"] >= floor and resolved < max_resolve:
            resolved += 1
            try:
                uid = roblox.place_to_universe(http, pid) or ""
                negative = not uid
            except Exception as e:  # noqa: BLE001
                uid, negative = "", getattr(e, "status", 0) in (400, 404)
            if uid or negative:
                cache[pid] = uid or "-"
                new_cache.append({"place_id": pid, "universe_id": uid or "-"})
        if it["playing"] >= floor:
            rows.append({"ts": ts, "place_id": pid, "universe_id": uid, "name": it["name"], "playing": it["playing"]})
            if uid and str(games.get(uid, {}).get("active")) != "1":
                candidates.append(uid)
    st.append_rows(cache_path, st.PLACE_FIELDS, new_cache)
    st.append_rows(st.day_path("third_party/rolimons", ts[:10]), st.ROLI_FIELDS, rows)
    added = add_candidates(http, games, candidates, cfg, now)
    return {"listed": len(listing), "above_floor": len(rows), "resolved": resolved, "added": len(added)}
