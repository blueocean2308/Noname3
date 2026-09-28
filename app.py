#!/usr/bin/env python3
"""StreamVault-style / K20-like addon via Videasy CDN (vidcore stack).
VPS FastAPI — only 1080p + 2160p HLS.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Optional
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

APP_NAME = "K20-Lite"
APP_ID = "org.nuvio.k20lite"
VERSION = "1.0.0"
PORT_HINT = 51823

TMDB_KEY = "1adf1a2b5aece0ac5106302d3299f56f"
TMDB = "https://api.themoviedb.org/3"
CINEMETA = "https://v3-cinemeta.strem.io"
VIDEASY = "https://vidrack.created.app/api/sources/videasy"
VIDZEN = "https://vidzen.fun"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# quality keep: >= 1080
MIN_HEIGHT = 1080

# simple TTL cache
_cache: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 600  # 10 min

app = FastAPI(title=APP_NAME)


def cors(resp: Response) -> Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    return resp


@app.options("/{path:path}")
async def options_all(path: str):
    return cors(Response(status_code=204))


@app.get("/")
@app.get("/manifest.json")
async def manifest():
    return cors(
        JSONResponse(
            {
                "id": APP_ID,
                "version": VERSION,
                "name": APP_NAME,
                "description": "Videasy CDN 1080p/4K HLS (VidCore stack)",
                "resources": ["stream"],
                "types": ["movie", "series"],
                "idPrefixes": ["tt", "tmdb:"],
                "catalogs": [],
            }
        )
    )


def cache_get(key: str):
    item = _cache.get(key)
    if not item:
        return None
    ts, val = item
    if time.time() - ts > CACHE_TTL:
        _cache.pop(key, None)
        return None
    return val


def cache_set(key: str, val: Any):
    _cache[key] = (time.time(), val)


def parse_stream_id(raw: str):
    """Return (id_core, season, episode). id_core is tt... or tmdb:N"""
    raw = raw.replace(".json", "")
    season = episode = None
    if ":" in raw:
        parts = raw.split(":")
        if parts[0] == "tmdb":
            id_core = "tmdb:" + parts[1]
            if len(parts) >= 4:
                season = int(parts[2])
                episode = int(parts[3])
        else:
            id_core = parts[0]
            if len(parts) >= 3:
                season = int(parts[1])
                episode = int(parts[2])
    else:
        id_core = raw
    return id_core, season, episode


async def tmdb_find_imdb(client: httpx.AsyncClient, imdb: str, is_tv: bool):
    r = await client.get(
        f"{TMDB}/find/{imdb}",
        params={"api_key": TMDB_KEY, "external_source": "imdb_id"},
    )
    if r.status_code != 200:
        return None
    j = r.json()
    if is_tv:
        arr = j.get("tv_results") or []
    else:
        arr = j.get("movie_results") or []
    return arr[0] if arr else None


async def resolve_meta(
    client: httpx.AsyncClient, id_core: str, is_tv: bool, notes: list
) -> Optional[dict]:
    if id_core.startswith("tmdb:"):
        tmdb_id = id_core.split(":", 1)[1]
        path = f"/tv/{tmdb_id}" if is_tv else f"/movie/{tmdb_id}"
        r = await client.get(
            TMDB + path,
            params={
                "api_key": TMDB_KEY,
                "language": "en-US",
                "append_to_response": "external_ids",
            },
        )
        notes.append(f"tmdb={r.status_code}")
        if r.status_code != 200:
            return None
        m = r.json()
        return {
            "title": m.get("title") or m.get("name") or "",
            "year": str(m.get("release_date") or m.get("first_air_date") or "")[:4],
            "tmdb": str(tmdb_id),
            "imdb": (m.get("external_ids") or {}).get("imdb_id") or "",
        }

    imdb = id_core
    # cinemeta first
    kind = "series" if is_tv else "movie"
    try:
        r = await client.get(f"{CINEMETA}/meta/{kind}/{imdb}.json")
        notes.append(f"cinemeta={r.status_code}")
        if r.status_code == 200:
            meta = (r.json() or {}).get("meta") or {}
            if meta.get("name"):
                tmdb_id = meta.get("moviedb_id")
                year = ""
                ym = re.search(r"\d{4}", str(meta.get("releaseInfo") or meta.get("year") or ""))
                if ym:
                    year = ym.group(0)
                if not tmdb_id:
                    hit = await tmdb_find_imdb(client, imdb, is_tv)
                    if hit:
                        tmdb_id = hit.get("id")
                if tmdb_id:
                    return {
                        "title": meta.get("name") or "",
                        "year": year,
                        "tmdb": str(tmdb_id),
                        "imdb": imdb,
                    }
    except Exception as e:
        notes.append(f"cinemeta EX {e}")

    hit = await tmdb_find_imdb(client, imdb, is_tv)
    if not hit:
        notes.append("no tmdb")
        return None
    return {
        "title": hit.get("title") or hit.get("name") or "",
        "year": str(hit.get("release_date") or hit.get("first_air_date") or "")[:4],
        "tmdb": str(hit.get("id")),
        "imdb": imdb,
    }


def height_from_quality(q: str) -> int:
    s = (q or "").lower()
    if any(x in s for x in ("2160", "4k", "uhd")):
        return 2160
    if "1440" in s or "2k" in s:
        return 1440
    if "1080" in s or "fhd" in s:
        return 1080
    if "720" in s:
        return 720
    if "480" in s or "360" in s:
        return 480
    m = re.search(r"(\d{3,4})", s)
    return int(m.group(1)) if m else 0


def height_from_url(url: str) -> int:
    u = url or ""
    m = re.search(r"index-s(2160|1440|1080|720|480|360)p", u, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"[/_-](2160|1440|1080|720|480|360)p?[/_-]", u, re.I)
    if m:
        return int(m.group(1))
    return 0


async def fetch_videasy(
    client: httpx.AsyncClient,
    tmdb_id: str,
    is_tv: bool,
    season: Optional[int],
    episode: Optional[int],
    notes: list,
) -> list[dict]:
    params: dict[str, Any] = {"type": "tv" if is_tv else "movie", "id": tmdb_id}
    if is_tv:
        params["season"] = season or 1
        params["episode"] = episode or 1
    try:
        r = await client.get(VIDEASY, params=params)
        notes.append(f"videasy={r.status_code}")
        if r.status_code != 200:
            notes.append(r.text[:200])
            return []
        j = r.json()
        return j.get("sources") or []
    except Exception as e:
        notes.append(f"videasy EX {e}")
        return []


async def fetch_vidzen(
    client: httpx.AsyncClient,
    tmdb_id: str,
    is_tv: bool,
    season: Optional[int],
    episode: Optional[int],
    notes: list,
) -> list[dict]:
    """Optional backup — relative /api/stream URLs on vidzen.fun."""
    if is_tv:
        url = f"{VIDZEN}/api/sources"
        params = {
            "type": "tv",
            "id": tmdb_id,
            "season": season or 1,
            "episode": episode or 1,
        }
    else:
        url = f"{VIDZEN}/api/sources"
        params = {"type": "movie", "id": tmdb_id}
    try:
        r = await client.get(url, params=params)
        notes.append(f"vidzen={r.status_code}")
        if r.status_code != 200:
            return []
        j = r.json()
        out = []
        for st in j.get("sources") or []:
            u = st.get("url") or ""
            if u.startswith("/"):
                u = VIDZEN + u
            if not u:
                continue
            out.append(
                {
                    "url": u,
                    "quality": st.get("label") or st.get("quality") or "auto",
                    "type": st.get("type") or "hls",
                    "provider": "vidzen",
                    "label": st.get("label") or "VidZen",
                }
            )
        return out
    except Exception as e:
        notes.append(f"vidzen EX {e}")
        return []


def build_stream_obj(
    *,
    name: str,
    title: str,
    url: str,
    binge: str,
    filename: str,
) -> dict:
    return {
        "name": name,
        "title": title,
        "url": url,
        "behaviorHints": {
            "notWebReady": False,
            "bingeGroup": binge,
            "filename": filename,
            "proxyHeaders": {
                "request": {
                    "User-Agent": UA,
                    "Referer": "https://vidcore.org/",
                    "Origin": "https://vidcore.org",
                }
            },
        },
    }


@app.get("/stream/{type}/{id_path:path}")
async def stream(type: str, id_path: str, request: Request, debug: int = 0):
    notes: list[str] = []
    id_path = id_path.rstrip("/")
    if id_path.endswith(".json"):
        id_path = id_path[:-5]

    id_core, season, episode = parse_stream_id(id_path)
    is_tv = type.lower() in ("series", "tv") or season is not None
    notes.append(f"id={id_core} tv={is_tv} s={season} e={episode}")

    cache_key = f"stream:{type}:{id_path}"
    cached = cache_get(cache_key)
    if cached is not None and not debug:
        return cors(JSONResponse(cached))

    headers = {
        "User-Agent": UA,
        "Accept": "application/json,*/*",
        "Referer": "https://vidcore.org/",
    }
    async with httpx.AsyncClient(timeout=25.0, headers=headers, follow_redirects=True) as client:
        meta = await resolve_meta(client, id_core, is_tv, notes)
        if not meta or not meta.get("tmdb"):
            out = {"streams": [], "notes": notes} if debug else {"streams": []}
            return cors(JSONResponse(out))

        notes.append(
            f"title={meta['title']} year={meta['year']} tmdb={meta['tmdb']} imdb={meta['imdb']}"
        )

        sources = await fetch_videasy(
            client, meta["tmdb"], is_tv, season, episode, notes
        )
        if not sources:
            sources = await fetch_vidzen(
                client, meta["tmdb"], is_tv, season, episode, notes
            )

        streams = []
        seen = set()
        for st in sources:
            url = st.get("url") or ""
            if not url.startswith("http"):
                continue
            if st.get("type") == "torrent" or st.get("infoHash"):
                continue

            h = height_from_url(url) or height_from_quality(
                str(st.get("quality") or st.get("label") or "")
            )
            if h < MIN_HEIGHT:
                notes.append(f"skip h={h} {st.get('quality')}")
                continue

            key = url[:120]
            if key in seen:
                continue
            seen.add(key)

            if h >= 2160:
                badge = "4K"
                qlabel = "4K 2160p"
                res = "3840×2160"
            elif h >= 1440:
                badge = "2K"
                qlabel = "1440p"
                res = "2560×1440"
            else:
                badge = "FHD"
                qlabel = "1080p"
                res = "1920×1080"

            ep = ""
            if is_tv:
                ep = f" S{int(season or 1):02d}E{int(episode or 1):02d}"
            head = f"{meta['title']}{ep}"
            if meta.get("year") and not is_tv:
                head = f"{meta['title']} ({meta['year']})"

            imdb = meta.get("imdb") or ""
            title_block = "\n".join(
                [
                    f"{qlabel} · HLS",
                    f"H.264 · {res}",
                    "English",
                    f"TMDb {meta['tmdb']}"
                    + (f" · IMDb {imdb}" if imdb else ""),
                ]
            )
            # Nuvio shows name + title; keep K20-ish
            full_title = f"{head}\n{title_block}"

            streams.append(
                build_stream_obj(
                    name=f"{APP_NAME} · {qlabel}",
                    title=full_title,
                    url=url,
                    binge=f"k20lite-{meta['tmdb']}-{h}",
                    filename=f"{meta['title']}{ep} [{badge}].m3u8".replace("/", "-"),
                )
            )

        # 4K first
        streams.sort(
            key=lambda s: 0 if "4K" in s["name"] else 1 if "1080" in s["name"] else 2
        )

        out: dict[str, Any] = {"streams": streams}
        if debug:
            out["notes"] = notes
        cache_set(cache_key, {"streams": streams})
        return cors(JSONResponse(out))


@app.get("/health")
async def health():
    return cors(JSONResponse({"ok": True, "version": VERSION}))


@app.get("/test/unabomber")
async def test_unabomber():
    # tt6933238 / tmdb 1492640
    return await stream("movie", "tt6933238.json", Request, debug=1)  # type: ignore


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=51823)
