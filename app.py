#!/usr/bin/env python3
"""Movy addon for Nuvio — www.movy.sx (api.wecollege.net). Getlink only."""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

APP_NAME = "Movy"
APP_ID = "org.nuvio.movy"
VERSION = "1.0.1"
PORT = 51824
STREAM_API = "https://api.wecollege.net"
CITIES = ["miami", "seattle", "denver", "atlanta", "phoenix", "portland", "boise", "austin"]
MIN_HEIGHT = 1080
TMDB_KEY = "1adf1a2b5aece0ac5106302d3299f56f"
TMDB = "https://api.themoviedb.org/3"
CINEMETA = "https://v3-cinemeta.strem.io"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HDR = {
    "User-Agent": UA,
    "Accept": "application/json,*/*",
    "Referer": "https://www.movy.sx/",
    "Origin": "https://www.movy.sx",
}
_D = [
    0x428A2F98, 0x71374491, 0xB5C0FBCF, 0xE9B5DBA5,
    0x3956C25B, 0x59F111F1, 0x923F82A4, 0xAB1C5ED5,
    0xD807AA98, 0x12835B01, 0x243185BE, 0x550C7DC3,
    0x72BE5D74, 0x80DEB1FE, 0x9BDC06A7, 0xC19BF174,
]
_MAGIC = bytes([109, 118, 109, 49])
CACHE_TTL = 180
_cache: dict = {}
_seed_cache: dict = {}
app = FastAPI(title=APP_NAME)

def cors(resp: Response) -> Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
    return resp

@app.options("/{path:path}")
async def options_all(path: str):
    return cors(Response(status_code=204))

def cache_get(key):
    item = _cache.get(key)
    if not item:
        return None
    ts, val = item
    if time.time() - ts > CACHE_TTL:
        _cache.pop(key, None)
        return None
    return val

def cache_set(key, val):
    _cache[key] = (time.time(), val)

def u32(x):
    return x & 0xFFFFFFFF

def _o(e):
    e = u32(e)
    e ^= e >> 16
    e = u32(e * 0x85EBCA6B)
    e ^= e >> 13
    e = u32(e * 0xC2B2AE35)
    e ^= e >> 16
    return u32(e)

def _rotl(e, a):
    e = u32(e)
    a &= 31
    if a == 0:
        return e
    return u32((e << a) | (e >> (32 - a)))

def streamcrypto_decrypt(cipher_b64, seed, media_id):
    e = seed
    a = int(media_id)
    b64 = cipher_b64.replace("-", "+").replace("_", "/")
    b64 += "=" * ((4 - len(b64) % 4) % 4)
    n = bytearray(base64.b64decode(b64))
    tlen = len(e)
    if (tlen * (tlen + 1) & 1) == 1:
        S = list(range(256))
        present = set(range(256))
        tt = 0
        for s in range(256):
            tt = (tt + S[s] + ord(e[s % tlen])) & 255
            S[s], S[tt] = S[tt], S[s]
        acc = 0x67452301
        for t in range(tlen):
            acc = _rotl(u32(acc ^ u32(ord(e[t]) * _D[15 & t])), 5)
        acc = _o(acc)
    else:
        S = [0] * 61
        present = set()
        n0 = 0x811C9DC5
        for t in range(tlen):
            n0 = u32((n0 ^ ord(e[t])) * 0x1000193)
        n0 = _o(n0)
        nv = _o(n0 ^ _o(u32(a) ^ 0x9E3779B9))
        for ei in range(8):
            if (ei * (ei + 1) & 1) == 0:
                ai = nv % 61
                nv = _rotl(u32(nv + 0x9E3779B9), 7 + (7 & ei))
                S[ai] = u32(nv ^ _o(nv))
                present.add(ai)
                nv = _o(u32(nv + ai))
            else:
                S[ei] = _D[15 & ei]
                present.add(ei)
        acc = _o(0xA5A5A5A5 ^ nv)
    ks = bytearray(len(n))
    l = 0
    ei = 0
    while ei < len(n):
        lv = acc
        dv = lv % 61
        iv = u32(-1) if dv in present else 0
        rv = u32(S[dv]) if dv in present else 0
        cv = u32(0x9E3779B9 * (l + 1))
        t = lv
        s = u32(rv ^ cv)
        b = u32(u32(t ^ s) | u32(t & s & iv))
        b = u32(_rotl(u32(b + lv), 31 & dv) ^ _rotl(lv, 31 & u32(dv * 7)))
        lv = _o(u32(b + 0x9E3779B9))
        S[dv] = lv
        present.add(dv)
        acc = lv
        word = lv
        l += 1
        ks[ei] = word & 255
        ei += 1
        if ei < len(n):
            ks[ei] = (word >> 8) & 255
            ei += 1
        if ei < len(n):
            ks[ei] = (word >> 16) & 255
            ei += 1
        if ei < len(n):
            ks[ei] = (word >> 24) & 255
            ei += 1
    for i in range(len(n)):
        n[i] ^= ks[i]
    if bytes(n[:4]) != _MAGIC:
        raise ValueError("STREAMCRYPTO decrypt failed")
    return bytes(n[4:]).decode("utf-8")

def parse_stream_id(raw):
    raw = raw.replace(".json", "")
    season = episode = None
    if ":" in raw:
        parts = raw.split(":")
        if parts[0] == "tmdb":
            id_core = "tmdb:" + parts[1]
            if len(parts) >= 4:
                season, episode = int(parts[2]), int(parts[3])
        else:
            id_core = parts[0]
            if len(parts) >= 3:
                season, episode = int(parts[1]), int(parts[2])
    else:
        id_core = raw
    return id_core, season, episode

def height_from_text(s):
    """Ưu tiên path URL (index-s1080p /s720p/ ...) — nhãn API hay sai."""
    s = (s or "").lower()
    if re.search(r"(2160p|4k|uhd)", s):
        return 2160
    if re.search(r"1440p", s):
        return 1440
    if re.search(r"1080p|fhd", s):
        return 1080
    if re.search(r"(720p|/720/)", s):
        return 720
    if re.search(r"(480p|360p|240p|/480/|/360/)", s):
        return 480
    m = re.search(r"(\d{3,4})\s*p", s)
    return int(m.group(1)) if m else 0

def height_label(h):
    if h >= 2160:
        return "4K 2160p"
    if h >= 1440:
        return "1440p"
    if h >= 1080:
        return "1080p"
    if h >= 720:
        return "720p"
    return f"{h}p" if h else "Auto"

async def tmdb_get(client, path, params=None):
    p = {"api_key": TMDB_KEY, "language": "en-US"}
    if params:
        p.update(params)
    r = await client.get(TMDB + path, params=p)
    return r.json() if r.status_code == 200 else None

async def resolve_meta(client, id_core, is_tv, notes):
    if id_core.startswith("tmdb:"):
        tmdb_id = id_core.split(":", 1)[1]
        path = f"/tv/{tmdb_id}" if is_tv else f"/movie/{tmdb_id}"
        m = await tmdb_get(client, path, {"append_to_response": "external_ids"})
        notes.append(f"tmdb={bool(m)}")
        if not m:
            return None
        return {
            "title": m.get("title") or m.get("name") or "",
            "year": str(m.get("release_date") or m.get("first_air_date") or "")[:4],
            "tmdb": str(tmdb_id),
            "imdb": (m.get("external_ids") or {}).get("imdb_id") or "",
            "poster": f"https://image.tmdb.org/t/p/w500{m['poster_path']}" if m.get("poster_path") else "",
            "background": f"https://image.tmdb.org/t/p/w1280{m['backdrop_path']}" if m.get("backdrop_path") else "",
            "description": m.get("overview") or "",
            "genres": [g.get("name") for g in (m.get("genres") or []) if g.get("name")],
        }
    imdb = id_core
    kind = "series" if is_tv else "movie"
    try:
        r = await client.get(f"{CINEMETA}/meta/{kind}/{imdb}.json")
        notes.append(f"cinemeta={r.status_code}")
        if r.status_code == 200:
            meta = (r.json() or {}).get("meta") or {}
            tmdb_id = meta.get("moviedb_id")
            year = ""
            ym = re.search(r"\d{4}", str(meta.get("releaseInfo") or meta.get("year") or ""))
            if ym:
                year = ym.group(0)
            if not tmdb_id:
                find = await tmdb_get(client, f"/find/{imdb}", {"external_source": "imdb_id"})
                arr = ((find or {}).get("tv_results" if is_tv else "movie_results") or [])
                if arr:
                    tmdb_id = arr[0].get("id")
            if tmdb_id:
                return {
                    "title": meta.get("name") or "",
                    "year": year,
                    "tmdb": str(tmdb_id),
                    "imdb": imdb,
                    "poster": meta.get("poster") or "",
                    "background": meta.get("background") or "",
                    "description": meta.get("description") or "",
                    "genres": meta.get("genres") or [],
                }
    except Exception as ex:
        notes.append(f"cinemeta EX {ex}")
    find = await tmdb_get(client, f"/find/{imdb}", {"external_source": "imdb_id"})
    arr = ((find or {}).get("tv_results" if is_tv else "movie_results") or [])
    if not arr:
        return None
    h = arr[0]
    return {
        "title": h.get("title") or h.get("name") or "",
        "year": str(h.get("release_date") or h.get("first_air_date") or "")[:4],
        "tmdb": str(h.get("id")),
        "imdb": imdb,
        "poster": f"https://image.tmdb.org/t/p/w500{h['poster_path']}" if h.get("poster_path") else "",
        "background": f"https://image.tmdb.org/t/p/w1280{h['backdrop_path']}" if h.get("backdrop_path") else "",
        "description": h.get("overview") or "",
        "genres": [],
    }

async def get_seed(client, media_id):
    now = time.time()
    cached = _seed_cache.get(media_id)
    if cached and cached[0] > now:
        return cached[1]
    try:
        r = await client.get(f"{STREAM_API}/seed", params={"mediaId": media_id}, timeout=15.0)
        if r.status_code != 200:
            return None
        j = r.json()
        seed = j.get("seed")
        ttl = min(int(j.get("ttlMs") or 30000) / 1000.0, 25.0)
        if seed:
            _seed_cache[media_id] = (now + ttl - 3, seed)
        return seed
    except Exception:
        return None

async def probe_m3u8(client, url):
    """True nếu playlist sống và là HLS."""
    try:
        r = await client.get(url, timeout=12.0, headers=HDR)
        if r.status_code != 200:
            return False
        body = (r.text or "")[:200].lstrip()
        return body.startswith("#EXTM3U")
    except Exception:
        return False


async def fetch_city(client, city, meta, is_tv, season, episode, seed):
    params = {
        "title": meta["title"],
        "mediaType": "tv" if is_tv else "movie",
        "year": meta.get("year") or "",
        "tmdbId": meta["tmdb"],
        "imdbId": meta.get("imdb") or "",
        "enc": "2",
        "seed": seed,
    }
    if is_tv:
        params["seasonId"] = season or 1
        params["episodeId"] = episode or 1
    try:
        r = await client.get(f"{STREAM_API}/{city}/sources", params=params, timeout=20.0)
        if r.status_code != 200:
            return []
        raw = r.text.strip()
        if raw.startswith("{"):
            return []
        data = json.loads(streamcrypto_decrypt(raw, seed, meta["tmdb"]))
        candidates = []
        for s in data.get("sources") or []:
            url = s.get("url") or ""
            if not url.startswith("http"):
                continue
            h = height_from_text(url) or height_from_text(str(s.get("quality") or ""))
            if h < MIN_HEIGHT:
                continue
            candidates.append({
                "city": city,
                "height": h,
                "url": url,
                "quality": s.get("quality") or height_label(h),
            })
        if not candidates:
            return []
        ok_flags = await asyncio.gather(*[probe_m3u8(client, c["url"]) for c in candidates])
        return [c for c, ok in zip(candidates, ok_flags) if ok]
    except Exception:
        return []


def build_stream_entry(meta, is_tv, season, episode, item):
    h = int(item.get("height") or 1080)
    qlabel = height_label(h)
    res = "3840×2160" if h >= 2160 else ("2560×1440" if h >= 1440 else "1920×1080")
    city = item.get("city") or "Movy"
    if is_tv:
        head = f"{meta['title']} S{int(season or 1):02d}E{int(episode or 1):02d}"
    else:
        head = f"{meta['title']} ({meta['year']})" if meta.get("year") else meta["title"]
    imdb = meta.get("imdb") or ""
    title = "\n".join([
        head,
        f"{qlabel} · HLS · Movy · {city.title()}",
        f"H.264 · {res}",
        f"TMDb {meta['tmdb']}" + (f" · IMDb {imdb}" if imdb else ""),
    ])
    return {
        "name": f"{APP_NAME} · {qlabel}",
        "title": title,
        "url": item["url"],
        "behaviorHints": {
            "notWebReady": True,
            "bingeGroup": f"movy-{meta['tmdb']}-{city}-{h}",
            "filename": f"{meta['title']} [{qlabel}].m3u8".replace("/", "-"),
            "proxyHeaders": {
                "request": {
                    "User-Agent": UA,
                    "Referer": "https://www.movy.sx/",
                    "Origin": "https://www.movy.sx",
                    "Accept": "*/*",
                }
            },
        },
    }

@app.get("/")
@app.get("/manifest.json")
async def manifest():
    return cors(JSONResponse({
        "id": APP_ID,
        "version": VERSION,
        "name": APP_NAME,
        "description": "Movy.sx — multi-server HLS (≥1080 / 4K khi có)",
        "logo": "https://www.movy.sx/favicon.svg",
        "resources": ["catalog", "meta", "stream"],
        "types": ["movie", "series"],
        "idPrefixes": ["tt", "tmdb:"],
        "catalogs": [
            {"type": "movie", "id": "movy-movie-popular", "name": "Movy Movies",
             "extra": [{"name": "skip"}, {"name": "search", "isRequired": False}]},
            {"type": "series", "id": "movy-series-popular", "name": "Movy Series",
             "extra": [{"name": "skip"}, {"name": "search", "isRequired": False}]},
        ],
    }))

@app.get("/health")
async def health():
    return cors(JSONResponse({"ok": True, "version": VERSION, "streamViaVps": False}))

@app.get("/catalog/{type}/{id_path:path}")
async def catalog(type: str, id_path: str):
    raw = id_path.replace(".json", "")
    extra = ""
    if "/" in raw:
        _, extra = raw.split("/", 1)
    search = ""
    skip = 0
    for part in extra.split("&"):
        if part.startswith("search="):
            search = part.split("=", 1)[1]
        if part.startswith("skip="):
            try:
                skip = int(part.split("=", 1)[1])
            except Exception:
                skip = 0
    is_tv = type.lower() in ("series", "tv")
    page = skip // 20 + 1
    async with httpx.AsyncClient(timeout=20.0, headers=HDR, follow_redirects=True) as client:
        if search:
            data = await tmdb_get(client, "/search/tv" if is_tv else "/search/movie", {"query": search, "page": page})
        else:
            data = await tmdb_get(client, "/tv/popular" if is_tv else "/movie/popular", {"page": page})
        metas = []
        for it in (data or {}).get("results") or []:
            tid = it.get("id")
            title = it.get("title") or it.get("name") or ""
            poster = f"https://image.tmdb.org/t/p/w500{it['poster_path']}" if it.get("poster_path") else ""
            year = str(it.get("release_date") or it.get("first_air_date") or "")[:4]
            metas.append({
                "id": f"tmdb:{tid}",
                "type": "series" if is_tv else "movie",
                "name": title,
                "poster": poster,
                "releaseInfo": year,
            })
        return cors(JSONResponse({"metas": metas}))

@app.get("/meta/{type}/{id_path:path}")
async def meta(type: str, id_path: str):
    id_path = id_path.replace(".json", "")
    id_core, _, _ = parse_stream_id(id_path)
    is_tv = type.lower() in ("series", "tv")
    notes = []
    async with httpx.AsyncClient(timeout=20.0, headers=HDR, follow_redirects=True) as client:
        m = await resolve_meta(client, id_core, is_tv, notes)
        if not m:
            return cors(JSONResponse({"meta": {}}))
        videos = None
        if is_tv:
            videos = []
            tv = await tmdb_get(client, f"/tv/{m['tmdb']}")
            for s in (tv or {}).get("seasons") or []:
                sn = s.get("season_number")
                if sn is None or sn < 1:
                    continue
                det = await tmdb_get(client, f"/tv/{m['tmdb']}/season/{sn}")
                for ep in (det or {}).get("episodes") or []:
                    en = ep.get("episode_number")
                    videos.append({
                        "id": f"tmdb:{m['tmdb']}:{sn}:{en}",
                        "title": ep.get("name") or f"Tập {en}",
                        "season": sn,
                        "episode": en,
                        "released": ((ep.get("air_date") or "") + "T00:00:00.000Z") if ep.get("air_date") else None,
                        "thumbnail": f"https://image.tmdb.org/t/p/w300{ep['still_path']}" if ep.get("still_path") else "",
                    })
        out = {
            "id": m.get("imdb") or f"tmdb:{m['tmdb']}",
            "type": "series" if is_tv else "movie",
            "name": m.get("title") or "",
            "poster": m.get("poster") or "",
            "background": m.get("background") or "",
            "description": m.get("description") or "",
            "releaseInfo": m.get("year") or "",
            "genres": m.get("genres") or [],
            "imdb_id": m.get("imdb") or "",
        }
        if videos:
            out["videos"] = videos
        return cors(JSONResponse({"meta": out}))

@app.get("/stream/{type}/{id_path:path}")
async def stream(type: str, id_path: str, debug: int = 0):
    notes = [f"ver={VERSION}"]
    id_path = id_path.rstrip("/").replace(".json", "")
    id_core, season, episode = parse_stream_id(id_path)
    is_tv = type.lower() in ("series", "tv") or season is not None
    notes.append(f"id={id_core} tv={is_tv} s={season} e={episode}")
    cache_key = f"stream:{VERSION}:{type}:{id_path}"
    cached = cache_get(cache_key)
    if cached is not None and not debug:
        return cors(JSONResponse(cached))
    async with httpx.AsyncClient(timeout=30.0, headers=HDR, follow_redirects=True) as client:
        meta = await resolve_meta(client, id_core, is_tv, notes)
        if not meta or not meta.get("tmdb"):
            out = {"streams": [], "notes": notes} if debug else {"streams": []}
            return cors(JSONResponse(out))
        notes.append(f"title={meta['title']} tmdb={meta['tmdb']}")
        seed = await get_seed(client, meta["tmdb"])
        if not seed:
            notes.append("seed fail")
            out = {"streams": [], "notes": notes} if debug else {"streams": []}
            return cors(JSONResponse(out))
        notes.append("seed ok")
        results = await asyncio.gather(*[
            fetch_city(client, city, meta, is_tv, season, episode, seed) for city in CITIES
        ])
        combined = []
        for city, items in zip(CITIES, results):
            notes.append(f"{city}={len(items)}")
            combined.extend(items)
        combined.sort(key=lambda x: -int(x.get("height") or 0))
        seen = set()
        streams = []
        for item in combined:
            u = item.get("url") or ""
            if not u.startswith("http") or u in seen:
                continue
            seen.add(u)
            streams.append(build_stream_entry(meta, is_tv, season, episode, item))
        out = {"streams": streams}
        if debug:
            out["notes"] = notes
        cache_set(cache_key, {"streams": streams})
        return cors(JSONResponse(out))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)

