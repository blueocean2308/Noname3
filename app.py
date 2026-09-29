#!/usr/bin/env python3
"""Atlantic addon for Nuvio/Stremio — Helios + Aphrodite (atlantic.st).

Getlink only. Stream URL trỏ CDN trực tiếp (không stream video qua VPS).
Upload: https://raw.githubusercontent.com/blueocean2308/Noname3/refs/heads/main/app.py
"""
from __future__ import annotations

import binascii
import re
import time
from typing import Any, Optional
from urllib.parse import urljoin

import httpx
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover
    AESGCM = None  # type: ignore

APP_NAME = "Atlantic"
APP_ID = "org.nuvio.atlantic"
VERSION = "1.0.1"
PUBLIC_HOST = "168.138.176.147:51824"
PORT = 51824

TMDB_KEY = "1adf1a2b5aece0ac5106302d3299f56f"
TMDB = "https://api.themoviedb.org/3"
CINEMETA = "https://v3-cinemeta.strem.io"

HELIOS = "https://stream.hls.lol/helios"
APHRODITE = "https://cdn.hls.lol"
# Key from atlantic.st frontend (AES-GCM, hex)
HELIOS_KEY_HEX = "117c358bcfcaf8fe2cfca57c9d2238a300e1c4de2efb83a5012ba84d8a31f1dd"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HDR = {
    "User-Agent": UA,
    "Accept": "application/json,*/*",
    "Referer": "https://atlantic.st/",
    "Origin": "https://atlantic.st",
}
MIN_HEIGHT = 1080
CACHE_TTL = 240
_cache: dict[str, tuple[float, Any]] = {}

app = FastAPI(title=APP_NAME)


def cors(resp: Response) -> Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
    return resp


@app.options("/{path:path}")
async def options_all(path: str):
    return cors(Response(status_code=204))


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


def decrypt_helios(payload: str) -> Optional[str]:
    if not payload:
        return None
    if not payload.startswith("hl_"):
        return payload
    if AESGCM is None:
        return None
    try:
        raw = binascii.unhexlify(payload[3:])
        if len(raw) < 28:
            return None
        key = binascii.unhexlify(HELIOS_KEY_HEX)
        iv, ct = raw[:12], raw[12:]
        return AESGCM(key).decrypt(iv, ct, None).decode("utf-8")
    except Exception:
        return None


def height_label(h: int) -> str:
    if h >= 2160:
        return "4K 2160p"
    if h >= 1440:
        return "1440p"
    if h >= 1080:
        return "1080p"
    if h >= 720:
        return "720p"
    return f"{h}p" if h else "Auto"


def parse_master_variants(text: str, base_url: str) -> list[tuple[int, str]]:
    """Return list of (height, absolute_url) from HLS master."""
    out: list[tuple[int, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            hm = re.search(r"RESOLUTION=\d+x(\d+)", line, re.I)
            h = int(hm.group(1)) if hm else 0
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith("#")):
                j += 1
            if j < len(lines):
                u = lines[j].strip()
                if not u.startswith("http"):
                    u = urljoin(base_url, u)
                out.append((h, u))
            i = j + 1
            continue
        i += 1
    return out


async def fetch_text(client: httpx.AsyncClient, url: str) -> Optional[str]:
    try:
        r = await client.get(url, headers=HDR, timeout=25.0)
        if r.status_code != 200:
            return None
        return r.text
    except Exception:
        return None


async def helios_streams(
    client: httpx.AsyncClient,
    tmdb_id: str,
    is_tv: bool,
    season: Optional[int],
    episode: Optional[int],
    notes: list,
) -> list[dict]:
    params: dict[str, Any] = {"tmdbId": tmdb_id, "type": "tv" if is_tv else "movie"}
    if is_tv:
        params["seasonId"] = season or 1
        params["episodeId"] = episode or 1
    try:
        r = await client.get(HELIOS, params=params, timeout=20.0)
        notes.append(f"helios={r.status_code}")
        if r.status_code != 200:
            return []
        j = r.json() or {}
        sources = (j.get("sources") or {})
        # Prefer Moscow; keep any other servers if present
        items = []
        if isinstance(sources, dict):
            for name, obj in sources.items():
                if isinstance(obj, dict) and obj.get("url"):
                    items.append((name, obj))
        if not items:
            notes.append("helios empty")
            return []

        streams = []
        for server_name, obj in items:
            raw = obj.get("url") or ""
            plain = decrypt_helios(raw)
            if not plain or not plain.startswith("http"):
                notes.append(f"helios decrypt fail {server_name}")
                continue
            text = await fetch_text(client, plain)
            if not text or not text.lstrip().startswith("#EXTM3U"):
                notes.append(f"helios playlist fail {server_name}")
                continue

            variants = parse_master_variants(text, plain)
            if variants:
                keep = [v for v in variants if v[0] >= MIN_HEIGHT]
                if not keep:
                    keep = [max(variants, key=lambda x: x[0])]
                for h, u in sorted(keep, key=lambda x: -x[0]):
                    streams.append(
                        {
                            "src": "Helios",
                            "server": server_name,
                            "height": h,
                            "url": u,
                            "master": plain,
                        }
                    )
            else:
                # media playlist
                streams.append(
                    {
                        "src": "Helios",
                        "server": server_name,
                        "height": 1080,
                        "url": plain,
                        "master": plain,
                    }
                )
        notes.append(f"helios_n={len(streams)}")
        return streams
    except Exception as e:
        notes.append(f"helios EX {e}")
        return []


async def aphrodite_streams(
    client: httpx.AsyncClient,
    tmdb_id: str,
    is_tv: bool,
    season: Optional[int],
    episode: Optional[int],
    notes: list,
) -> list[dict]:
    if is_tv:
        path = f"/content/tv/{tmdb_id}/{season or 1}/{episode or 1}"
    else:
        path = f"/content/movie/{tmdb_id}"
    try:
        r = await client.get(APHRODITE + path, timeout=20.0)
        notes.append(f"aphrodite={r.status_code}")
        if r.status_code != 200:
            return []
        j = r.json() or {}
        if not j.get("found"):
            notes.append("aphrodite not found")
            return []
        u = j.get("hls") or j.get("url") or ""
        if not u.startswith("http"):
            return []
        text = await fetch_text(client, u)
        if not text or not text.lstrip().startswith("#EXTM3U"):
            notes.append("aphrodite playlist fail")
            # vẫn trả URL gốc — player tự thử
            return [
                {
                    "src": "Aphrodite",
                    "server": "Aphrodite",
                    "height": 1080,
                    "url": u,
                    "master": u,
                }
            ]
        variants = parse_master_variants(text, u)
        if variants:
            keep = [v for v in variants if v[0] >= MIN_HEIGHT]
            if not keep:
                keep = [max(variants, key=lambda x: x[0])]
            out = []
            for h, vu in sorted(keep, key=lambda x: -x[0]):
                out.append(
                    {
                        "src": "Aphrodite",
                        "server": "Aphrodite",
                        "height": h,
                        "url": vu,
                        "master": u,
                    }
                )
            notes.append(f"aphrodite_n={len(out)}")
            return out
        notes.append("aphrodite_n=1 media")
        return [
            {
                "src": "Aphrodite",
                "server": "Aphrodite",
                "height": 1080,
                "url": u,
                "master": u,
            }
        ]
    except Exception as e:
        notes.append(f"aphrodite EX {e}")
        return []


async def tmdb_get(client: httpx.AsyncClient, path: str, params: Optional[dict] = None):
    p = {"api_key": TMDB_KEY, "language": "en-US"}
    if params:
        p.update(params)
    r = await client.get(TMDB + path, params=p)
    if r.status_code != 200:
        return None
    return r.json()


async def resolve_meta(client: httpx.AsyncClient, id_core: str, is_tv: bool, notes: list):
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
    except Exception as e:
        notes.append(f"cinemeta EX {e}")

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


def meta_obj(m: dict, typ: str, videos=None):
    out = {
        "id": m.get("imdb") or f"tmdb:{m['tmdb']}",
        "type": typ,
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
    return out


def build_stream_entry(meta: dict, is_tv: bool, season, episode, item: dict) -> dict:
    h = int(item.get("height") or 1080)
    qlabel = height_label(h)
    if h >= 2160:
        res = "3840×2160"
    elif h >= 1440:
        res = "2560×1440"
    else:
        res = "1920×1080"
    src = item.get("src") or "Atlantic"
    server = item.get("server") or src
    if is_tv:
        head = f"{meta['title']} S{int(season or 1):02d}E{int(episode or 1):02d}"
    else:
        head = f"{meta['title']} ({meta['year']})" if meta.get("year") else meta["title"]
    imdb = meta.get("imdb") or ""
    title = "\n".join(
        [
            head,
            f"{qlabel} · HLS · {src} · {server}",
            f"H.264 · {res}",
            f"TMDb {meta['tmdb']}" + (f" · IMDb {imdb}" if imdb else ""),
        ]
    )
    # Master playlist nếu có — player tự chọn quality; CDN bắt buộc Referer
    play_url = item.get("master") or item.get("url") or ""
    return {
        "name": f"{APP_NAME} · {qlabel}",
        "title": title,
        "url": play_url,
        "behaviorHints": {
            "notWebReady": True,
            "bingeGroup": f"atlantic-{meta['tmdb']}-{src}-{h}",
            "filename": f"{meta['title']} [{qlabel}].m3u8".replace("/", "-"),
            "proxyHeaders": {
                "request": {
                    "User-Agent": UA,
                    "Referer": "https://atlantic.st/",
                    "Origin": "https://atlantic.st",
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                }
            },
        },
    }


@app.get("/")
@app.get("/manifest.json")
async def manifest():
    return cors(
        JSONResponse(
            {
                "id": APP_ID,
                "version": VERSION,
                "name": APP_NAME,
                "description": "Atlantic.st — Helios + Aphrodite (≥1080 / 4K khi có)",
                "logo": "https://atlantic.st/icon.svg",
                "resources": ["catalog", "meta", "stream"],
                "types": ["movie", "series"],
                "idPrefixes": ["tt", "tmdb:"],
                "catalogs": [
                    {
                        "type": "movie",
                        "id": "atlantic-movie-popular",
                        "name": "Atlantic Movies",
                        "extra": [
                            {"name": "skip"},
                            {"name": "search", "isRequired": False},
                        ],
                    },
                    {
                        "type": "series",
                        "id": "atlantic-series-popular",
                        "name": "Atlantic Series",
                        "extra": [
                            {"name": "skip"},
                            {"name": "search", "isRequired": False},
                        ],
                    },
                ],
            }
        )
    )


@app.get("/health")
async def health():
    return cors(
        JSONResponse(
            {
                "ok": True,
                "version": VERSION,
                "aesgcm": AESGCM is not None,
                "streamViaVps": False,
            }
        )
    )


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
            path = "/search/tv" if is_tv else "/search/movie"
            data = await tmdb_get(client, path, {"query": search, "page": page})
        else:
            path = "/tv/popular" if is_tv else "/movie/popular"
            data = await tmdb_get(client, path, {"page": page})
        metas = []
        for it in (data or {}).get("results") or []:
            tid = it.get("id")
            title = it.get("title") or it.get("name") or ""
            poster = (
                f"https://image.tmdb.org/t/p/w500{it['poster_path']}"
                if it.get("poster_path")
                else ""
            )
            year = str(it.get("release_date") or it.get("first_air_date") or "")[:4]
            metas.append(
                {
                    "id": f"tmdb:{tid}",
                    "type": "series" if is_tv else "movie",
                    "name": title,
                    "poster": poster,
                    "releaseInfo": year,
                }
            )
        return cors(JSONResponse({"metas": metas}))


@app.get("/meta/{type}/{id_path:path}")
async def meta(type: str, id_path: str):
    id_path = id_path.replace(".json", "")
    id_core, _, _ = parse_stream_id(id_path)
    is_tv = type.lower() in ("series", "tv")
    notes: list[str] = []
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
                    videos.append(
                        {
                            "id": f"tmdb:{m['tmdb']}:{sn}:{en}",
                            "title": ep.get("name") or f"Tập {en}",
                            "season": sn,
                            "episode": en,
                            "released": (
                                (ep.get("air_date") or "") + "T00:00:00.000Z"
                                if ep.get("air_date")
                                else None
                            ),
                            "thumbnail": (
                                f"https://image.tmdb.org/t/p/w300{ep['still_path']}"
                                if ep.get("still_path")
                                else ""
                            ),
                        }
                    )
        return cors(
            JSONResponse({"meta": meta_obj(m, "series" if is_tv else "movie", videos)})
        )


@app.get("/stream/{type}/{id_path:path}")
async def stream(type: str, id_path: str, debug: int = 0):
    notes: list[str] = [f"ver={VERSION}", f"aesgcm={AESGCM is not None}"]
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

        helios = await helios_streams(
            client, meta["tmdb"], is_tv, season, episode, notes
        )
        aphro = await aphrodite_streams(
            client, meta["tmdb"], is_tv, season, episode, notes
        )

        # Ưu tiên height cao, rồi Helios trước Aphrodite
        combined = helios + aphro
        combined.sort(key=lambda x: (-int(x.get("height") or 0), 0 if x.get("src") == "Helios" else 1))

        seen = set()
        streams = []
        for item in combined:
            u = item.get("url") or ""
            if not u.startswith("http") or u in seen:
                continue
            if int(item.get("height") or 0) and int(item["height"]) < MIN_HEIGHT:
                continue
            seen.add(u)
            streams.append(build_stream_entry(meta, is_tv, season, episode, item))

        out: dict[str, Any] = {"streams": streams}
        if debug:
            out["notes"] = notes
        cache_set(cache_key, {"streams": streams})
        return cors(JSONResponse(out))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
