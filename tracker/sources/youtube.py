"""YouTube Data API v3: coverage velocity (videos and views per game in the last N days).

Optional. Runs only when the YOUTUBE_API_KEY secret is set. Each game costs
about 101 quota units (one search + one stats call); the free quota is
10,000 units a day.
"""
from __future__ import annotations

SEARCH = "https://www.googleapis.com/youtube/v3/search"
VIDEOS = "https://www.googleapis.com/youtube/v3/videos"


def coverage(http, api_key, game_name, published_after_iso):
    query = f"roblox {game_name}"
    resp = http.get_json(SEARCH, {
        "part": "id", "q": query, "type": "video", "order": "viewCount", "maxResults": 50,
        "publishedAfter": published_after_iso, "key": api_key,
    })
    ids = [it["id"]["videoId"] for it in (resp or {}).get("items", []) if it.get("id", {}).get("videoId")]
    views, top_id, top_views = 0, "", 0
    if ids:
        stats = http.get_json(VIDEOS, {"part": "statistics", "id": ",".join(ids), "key": api_key})
        for it in (stats or {}).get("items", []):
            v = int(it.get("statistics", {}).get("viewCount", 0) or 0)
            views += v
            if v > top_views:
                top_id, top_views = it.get("id", ""), v
    return {"query": query, "videos": len(ids), "views": views, "top_video_id": top_id, "top_video_views": top_views}
