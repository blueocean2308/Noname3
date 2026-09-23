#!/usr/bin/env python3
"""VidCore stream addon for Nuvio/Stremio — Rigel (movish.to) → m3u8 · 1080p+ only."""
from __future__ import annotations

import json
import re
import time

from flask import Flask, Response, request

try:
    from curl_cffi import requests as creq
    HAS_CFFI = True
except ImportError:
    import requests as creq
    HAS_CFFI = False

app = Flask(__name__)

ADDON_VERSION = "1.0.2"
ADDON_NAME = "VidCore"
ADDON_ID = "org.nuvio.vidcore"
PUBLIC_HOST = "168.138.176.147:51824"

TMDB_KEY = "1adf1a2b5aece0ac5106302d3299f56f"
TMDB = "https://api.themoviedb.org/3"
RIGEL = "https://movish.to/player-sources/rigel"
REFERER = "https://www.vidcore.org/"
ORIGIN = "https://www.vidcore.org"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

API_CACHE = {}
API_TTL = 300
TMDB_TTL = 86400


def quality_rank(q: str) -> int:
    """Only 1080p / 4K / 8K pass. Everything else rejected."""
    if not q:
        return -1
    s = q.lower().strip().replace(" ", "")
    if "8k" in s or "4320" in s:
        return 400
    if "4k" in s or "2160" in s or "uhd" in s:
        return 300
    if "1080" in s or "fhd" in s or "fullhd" in s:
        return 200
    return -1


def cors(resp: Response) -> Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
    return resp


def j(data, status=200):
    return cors(
        Response(json.dumps(data, ensure_ascii=False), status=status, mimetype="application/json")
    )


def _prune(store: dict, limit=400):
    now = time.time()
    for k in [k for k, v in list(store.items()) if v[1] <= now]:
        store.pop(k, None)
    if len(store) > limit:
        for k, _ in sorted(store.items(), key=lambda x: x[1][1])[: max(1, len(store) - limit)]:
            store.pop(k, None)


def api_get(key):
    item = API_CACHE.get(key)
    if not item:
        return None
    if item[1] <= time.time():
        API_CACHE.pop(key, None)
        return None
    return item[0]


def api_put(key, data, ttl):
    _prune(API_CACHE)
    API_CACHE[key] = (data, time.time() + ttl)


def http_get(url: str, referer: str = REFERER, timeout: int = 20):
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": referer,
        "Origin": ORIGIN,
    }
    if HAS_CFFI:
        return creq.get(url, headers=headers, impersonate="chrome120", timeout=timeout)
    return creq.get(url, headers=headers, timeout=timeout)


def tmdb_find_imdb(imdb_id: str):
    key = f"tmdb_find:{imdb_id}"
    hit = api_get(key)
    if hit is not None:
        return hit
    url = f"{TMDB}/find/{imdb_id}?api_key={TMDB_KEY}&external_source=imdb_id"
    try:
        r = http_get(url, referer="https://www.themoviedb.org/")
        if r.status_code != 200:
            return None
        d = r.json()
        for field, mtype in (("movie_results", "movie"), ("tv_results", "tv")):
            arr = d.get(field) or []
            if arr:
                m = arr[0]
                title = m.get("title") or m.get("name") or ""
                date = m.get("release_date") or m.get("first_air_date") or ""
                res = (mtype, m.get("id"), title, date[:4] if date else "")
                api_put(key, res, TMDB_TTL)
                return res
    except Exception:
        pass
    return None


def tmdb_from_tmdb_id(tmdb_id: int, prefer: str = "movie"):
    key = f"tmdb_id:{prefer}:{tmdb_id}"
    hit = api_get(key)
    if hit is not None:
        return hit
    for mtype in (prefer, "tv" if prefer == "movie" else "movie"):
        try:
            r = http_get(
                f"{TMDB}/{mtype}/{tmdb_id}?api_key={TMDB_KEY}",
                referer="https://www.themoviedb.org/",
            )
            if r.status_code == 200:
                m = r.json()
                title = m.get("title") or m.get("name") or ""
                date = m.get("release_date") or m.get("first_air_date") or ""
                res = (mtype, tmdb_id, title, date[:4] if date else "")
                api_put(key, res, TMDB_TTL)
                return res
        except Exception:
            continue
    return None


def rigel_streams(media_type: str, tmdb_id: int, season=None, episode=None):
    if media_type == "tv":
        path = f"tv/{tmdb_id}/{season or 1}/{episode or 1}"
    else:
        path = f"movie/{tmdb_id}"
    key = f"rigel:{path}"
    hit = api_get(key)
    if hit is not None:
        return hit
    try:
        r = http_get(f"{RIGEL}/{path}", referer=REFERER, timeout=25)
        if r.status_code != 200:
            return []
        data = r.json()
        streams = data.get("streams") or []
        if not isinstance(streams, list):
            return []
        out = []
        seen = set()
        for st in streams:
            if not isinstance(st, dict):
                continue
            u = st.get("url")
            if not u or not isinstance(u, str) or not u.startswith("http"):
                continue
            if u in seen:
                continue
            quality = str(st.get("quality") or "").strip()
            rank = quality_rank(quality)
            if rank < 200:
                continue
            label = st.get("label") or st.get("name") or data.get("label") or "Rigel"
            stype = st.get("type") or ("hls" if ".m3u8" in u else "http")
            seen.add(u)
            out.append({
                "url": u,
                "label": str(label),
                "quality": quality,
                "type": stype,
                "rank": rank,
            })
        out.sort(key=lambda x: (-x["rank"], x["label"]))
        api_put(key, out, API_TTL)
        return out
    except Exception:
        return []


def parse_stremio_id(raw: str):
    raw = (raw or "").replace(".json", "").strip()
    season = episode = None
    m = re.match(r"^tmdb:(\d+)(?::(\d+):(\d+))?$", raw, re.I)
    if m:
        if m.group(2):
            season, episode = int(m.group(2)), int(m.group(3))
        return "tmdb", int(m.group(1)), season, episode
    m = re.match(r"^(tt\d+)(?::(\d+):(\d+))?$", raw, re.I)
    if m:
        if m.group(2):
            season, episode = int(m.group(2)), int(m.group(3))
        return "imdb", m.group(1).lower(), season, episode
    if raw.isdigit():
        return "tmdb", int(raw), None, None
    return None, None, None, None


def build_stream_objs(items, title: str, year: str, media_type: str, season, episode):
    streams = []
    for i, it in enumerate(items):
        q = it.get("quality") or "1080p"
        label = it.get("label") or "Rigel"
        ep_line = ""
        if media_type == "tv" and season is not None and episode is not None:
            ep_line = f" · S{season:02d}E{episode:02d}"
        detail = title or "VidCore"
        if year:
            detail += f" ({year})"
        detail += ep_line
        detail += f"\n{q}\nVidCore · {label}"
        streams.append({
            "name": f"VidCore {q}",
            "title": detail,
            "url": it["url"],
            "behaviorHints": {
                "notWebReady": True,
                "bingeGroup": f"vidcore-{label}-{i}",
                "proxyHeaders": {
                    "request": {
                        "Referer": REFERER,
                        "Origin": ORIGIN,
                        "User-Agent": UA,
                    }
                },
            },
        })
    return streams


@app.route("/")
def root():
    return j({"ok": True, "version": ADDON_VERSION, "manifest": "/manifest.json", "name": ADDON_NAME})


@app.route("/manifest.json")
def manifest():
    return j({
        "id": ADDON_ID,
        "version": ADDON_VERSION,
        "name": ADDON_NAME,
        "description": "VidCore · Rigel · chỉ 1080p / 4K",
        "logo": "https://www.google.com/s2/favicons?domain=vidcore.org&sz=128",
        "resources": ["stream"],
        "types": ["movie", "series"],
        "idPrefixes": ["tt", "tmdb:"],
        "catalogs": [],
        "behaviorHints": {"configurable": False, "adult": False},
    })


@app.route("/stream/<stype>/<path:sid>")
def stream(stype, sid):
    sid = sid.replace(".json", "")
    kind, ident, season, episode = parse_stremio_id(sid)
    if kind is None:
        return j({"streams": []})

    media_type = "tv" if stype == "series" else "movie"
    title, year = "", ""
    tmdb_id = None

    if kind == "imdb":
        found = tmdb_find_imdb(ident)
        if not found:
            return j({"streams": []})
        mtype, tmdb_id, title, year = found
        media_type = "tv" if season is not None else mtype
    else:
        tmdb_id = int(ident)
        meta = tmdb_from_tmdb_id(tmdb_id, prefer=media_type)
        if meta:
            media_type, tmdb_id, title, year = meta
        if season is not None:
            media_type = "tv"

    if not tmdb_id:
        return j({"streams": []})

    items = rigel_streams(media_type, tmdb_id, season, episode)
    if not items:
        return j({"streams": []})

    return j({
        "streams": build_stream_objs(
            items, title or f"TMDB {tmdb_id}", year, media_type, season, episode
        )
    })


@app.route("/<path:_any>", methods=["OPTIONS"])
@app.route("/", methods=["OPTIONS"])
def options(_any=None):
    return cors(Response(status=204))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=51824, debug=False)
