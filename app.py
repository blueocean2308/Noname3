#!/usr/bin/env python3
"""Vidy / Movy-style Stremio addon — pipeline vidy.st + api.wecollege.net + enc-dec.app"""

from __future__ import annotations

import json
import os
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional
from urllib.parse import quote

from flask import Flask, Response, jsonify, request

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except Exception:
    HAS_CFFI = False

APP_NAME = "Vidy"
VERSION = "2.0.0"
PORT = int(os.environ.get("PORT", "51824"))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36"
)
VIDY_REFERER = "https://www.vidy.st/"
API_BASE = os.environ.get("VIDY_API_BASE", "https://api.wecollege.net").rstrip("/")
DB_BASE = os.environ.get("VIDY_DB_BASE", "https://db.wecollege.net").rstrip("/")
ENC_DEC = os.environ.get("VIDY_ENC_DEC", "https://enc-dec.app/api").rstrip("/")
SEED_PROXY = os.environ.get("VIDY_SEED_PROXY", "").rstrip("/")

CITIES = [
    "miami", "boise", "vegas", "reno", "oakland",
    "seattle", "phoenix", "atlanta", "portland",
    "austin", "dallas", "tampa", "orlando",
]

CACHE_TTL = 600
_cache: dict[str, tuple[float, Any]] = {}

app = Flask(__name__)


def _headers(extra: Optional[dict] = None) -> dict:
    h = {
        "Accept": "*/*",
        "Origin": "https://vidy.st",
        "Referer": VIDY_REFERER,
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
    }
    if extra:
        h.update(extra)
    return h


def http_get(url: str, timeout: int = 20, headers: Optional[dict] = None) -> tuple[int, str]:
    final = url
    if SEED_PROXY and "wecollege.net" in url:
        final = f"{SEED_PROXY}?url={quote(url, safe='')}"
    hdrs = headers or _headers()
    try:
        if HAS_CFFI:
            r = cffi_requests.get(
                final, headers=hdrs, impersonate="chrome131", timeout=timeout
            )
            return r.status_code, r.text
        import urllib.request
        req = urllib.request.Request(final, headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def http_post_json(url: str, payload: dict, timeout: int = 25) -> tuple[int, Any]:
    try:
        if HAS_CFFI:
            r = cffi_requests.post(
                url,
                headers={
                    "User-Agent": UA,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Origin": "https://vidy.st",
                    "Referer": VIDY_REFERER,
                },
                json=payload,
                impersonate="chrome131",
                timeout=timeout,
            )
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, r.text
        import urllib.request
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "User-Agent": UA,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(body)
            except Exception:
                return resp.status, body
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


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


def cors(resp: Response) -> Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,HEAD,OPTIONS"
    return resp


@app.after_request
def after(resp):
    return cors(resp)


@app.route("/")
@app.route("/manifest.json")
def manifest():
    return jsonify(
        {
            "id": "org.nuvio.vidy",
            "version": VERSION,
            "name": APP_NAME,
            "description": "Streams via vidy.st / wecollege (seed + city sources + enc-dec)",
            "logo": "https://www.google.com/s2/favicons?domain=vidy.st&sz=128",
            "resources": ["stream", "meta"],
            "types": ["movie", "series"],
            "idPrefixes": ["tt", "tmdb:"],
            "catalogs": [],
            "behaviorHints": {"configurable": False, "configurationRequired": False},
        }
    )


def parse_stremio_id(raw: str):
    raw = (raw or "").replace(".json", "").strip()
    season = episode = None
    imdb = None
    tmdb = None
    if raw.startswith("tmdb:"):
        parts = raw.split(":")
        tmdb = parts[1] if len(parts) > 1 else None
        if len(parts) >= 4:
            season, episode = int(parts[2]), int(parts[3])
        return tmdb, imdb, season, episode
    m = re.match(r"^(tt\d+)(?::(\d+):(\d+))?$", raw)
    if m:
        imdb = m.group(1)
        if m.group(2):
            season, episode = int(m.group(2)), int(m.group(3))
        return tmdb, imdb, season, episode
    return None, None, None, None


def find_by_imdb(imdb_id: str) -> Optional[dict]:
    ck = f"find:{imdb_id}"
    c = cache_get(ck)
    if c is not None:
        return c
    url = f"{DB_BASE}/3/find/{imdb_id}?external_source=imdb_id"
    code, text = http_get(url, headers=_headers({"Accept": "application/json"}))
    if code != 200:
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None
    cache_set(ck, data)
    return data


def resolve_tmdb(imdb_id: Optional[str], tmdb_id: Optional[str], is_series: bool):
    if tmdb_id:
        return str(tmdb_id)
    if not imdb_id:
        return None
    data = find_by_imdb(imdb_id)
    if not data:
        return None
    key = "tv_results" if is_series else "movie_results"
    arr = data.get(key) or []
    if not arr and is_series:
        arr = data.get("movie_results") or []
    if not arr and not is_series:
        arr = data.get("tv_results") or []
    if not arr:
        return None
    return str(arr[0].get("id"))


def details(tmdb_id: str, media_type: str) -> dict:
    ck = f"det:{media_type}:{tmdb_id}"
    c = cache_get(ck)
    if c is not None:
        return c
    path = "tv" if media_type == "tv" else "movie"
    url = f"{DB_BASE}/3/{path}/{tmdb_id}?append_to_response=external_ids&language=en"
    code, text = http_get(url, headers=_headers({"Accept": "application/json"}))
    if code != 200:
        return {}
    try:
        data = json.loads(text)
    except Exception:
        return {}
    cache_set(ck, data)
    return data


def get_seed(tmdb_id: str) -> Optional[str]:
    ck = f"seed:{tmdb_id}"
    c = cache_get(ck)
    if c is not None:
        return c
    url = f"{API_BASE}/seed?mediaId={tmdb_id}"
    code, text = http_get(url)
    if code != 200:
        print("SEED_FAIL", code, text[:180].replace("\n", " "))
        return None
    try:
        data = json.loads(text)
        seed = data.get("seed")
        if seed:
            cache_set(ck, seed)
        return seed
    except Exception as e:
        print("SEED_JSON", e, text[:120])
        return None


def fetch_city_encrypted(
    city: str,
    title: str,
    media_type: str,
    year: str,
    tmdb_id: str,
    imdb_id: str,
    season: Optional[int],
    episode: Optional[int],
    seed: str,
) -> Optional[str]:
    enc_title = quote(quote(title, safe=""), safe="")
    qs = (
        f"title={enc_title}&mediaType={media_type}&year={quote(str(year or ''))}"
        f"&tmdbId={tmdb_id}&imdbId={imdb_id or ''}&enc=2&seed={quote(str(seed))}"
    )
    if media_type == "tv" and season is not None and episode is not None:
        qs += f"&seasonId={season}&episodeId={episode}"
    url = f"{API_BASE}/{city}/sources?{qs}"
    code, text = http_get(url)
    if code != 200 or not text or text.lstrip().startswith("<!"):
        print(f"CITY_FAIL {city}", code, text[:100].replace("\n", " "))
        return None
    return text


def decrypt_videasy(enc_text: str, tmdb_id: str, seed: str) -> Any:
    code, data = http_post_json(
        f"{ENC_DEC}/dec-videasy",
        {"text": enc_text, "id": tmdb_id, "seed": seed},
    )
    if code != 200:
        print("DEC_HTTP", code, data)
        return None
    if isinstance(data, dict):
        if data.get("status") == 200 and "result" in data:
            return data["result"]
        if data.get("result") is not None:
            return data["result"]
        return data
    return None


def extract_streams_from_decrypted(dec: Any) -> list[dict]:
    out = []
    if dec is None:
        return out

    def walk(obj, server="Vidy"):
        if isinstance(obj, dict):
            url = obj.get("url") or obj.get("file") or obj.get("src") or obj.get("stream")
            if isinstance(url, str) and url.startswith("http"):
                q = (
                    obj.get("quality")
                    or obj.get("label")
                    or obj.get("res")
                    or obj.get("title")
                    or "Auto"
                )
                out.append(
                    {
                        "url": url,
                        "quality": str(q),
                        "server": obj.get("server") or obj.get("provider") or server,
                        "type": "hls" if ".m3u8" in url.lower() else "http",
                    }
                )
            for k, v in obj.items():
                if k in ("url", "file", "src", "stream"):
                    continue
                walk(v, server=str(k) if isinstance(k, str) else server)
        elif isinstance(obj, list):
            for it in obj:
                walk(it, server)

    walk(dec)
    seen = set()
    uniq = []
    for s in out:
        if s["url"] in seen:
            continue
        seen.add(s["url"])
        uniq.append(s)
    return uniq


def quality_rank(q: str) -> int:
    s = (q or "").lower()
    if "2160" in s or "4k" in s or "uhd" in s:
        return 0
    if "1080" in s or "fhd" in s:
        return 1
    if "720" in s or "hd" in s:
        return 2
    if "480" in s or "360" in s:
        return 4
    return 3


def build_streams(
    tmdb_id: str,
    imdb_id: str,
    media_type: str,
    season: Optional[int],
    episode: Optional[int],
) -> list[dict]:
    det = details(tmdb_id, media_type)
    title = (det.get("title") or det.get("name") or f"TMDB {tmdb_id}").strip()
    year = ""
    date = det.get("release_date") or det.get("first_air_date") or ""
    if date and len(date) >= 4:
        year = date[:4]
    if not imdb_id:
        imdb_id = (det.get("external_ids") or {}).get("imdb_id") or det.get("imdb_id") or ""

    seed = get_seed(tmdb_id)
    if not seed:
        print("NO_SEED", tmdb_id)
        return []

    streams: list[dict] = []
    seen_urls: set[str] = set()

    def one_city(city: str):
        enc = fetch_city_encrypted(
            city, title, media_type, year, tmdb_id, imdb_id, season, episode, seed
        )
        if not enc:
            return []
        dec = decrypt_videasy(enc, tmdb_id, seed)
        items = extract_streams_from_decrypted(dec)
        for it in items:
            it["server"] = f"{city.title()} · {it.get('server') or 'Vidy'}"
        return items

    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(one_city, c): c for c in CITIES[:8]}
        for fut in as_completed(futs):
            try:
                for it in fut.result() or []:
                    u = it["url"]
                    if u in seen_urls:
                        continue
                    seen_urls.add(u)
                    streams.append(it)
            except Exception as e:
                print("CITY_EX", futs[fut], e)

    streams.sort(key=lambda x: quality_rank(x.get("quality", "")))

    result = []
    for it in streams[:16]:
        label = it.get("quality") or "Auto"
        server = it.get("server") or "Vidy"
        lines = [title]
        if episode is not None:
            lines.append(f"Tập {int(episode)} · {label} · {server}")
        else:
            lines.append(f"{label} · {server}")
        lines.append((it.get("type") or "http").upper())
        result.append(
            {
                "name": f"Vidy {label}",
                "title": "\n".join(lines),
                "url": it["url"],
                "behaviorHints": {
                    "notWebReady": True,
                    "bingeGroup": f"vidy-{tmdb_id}-{season}-{episode}",
                    "proxyHeaders": {
                        "request": {
                            "User-Agent": UA,
                            "Referer": VIDY_REFERER,
                            "Origin": "https://vidy.st",
                        }
                    },
                },
            }
        )
    return result


@app.route("/stream/<media_type>/<path:video_id>.json")
@app.route("/stream/<media_type>/<path:video_id>")
def stream(media_type: str, video_id: str):
    try:
        is_series = media_type in ("series", "tv")
        tmdb, imdb, season, episode = parse_stremio_id(video_id)
        tmdb = resolve_tmdb(imdb, tmdb, is_series)
        if not tmdb:
            return jsonify({"streams": []})
        api_type = "tv" if is_series else "movie"
        if not is_series:
            season = episode = None
        streams = build_streams(tmdb, imdb or "", api_type, season, episode)
        return jsonify({"streams": streams})
    except Exception:
        traceback.print_exc()
        return jsonify({"streams": []})


@app.route("/meta/<media_type>/<path:meta_id>.json")
@app.route("/meta/<media_type>/<path:meta_id>")
def meta(media_type: str, meta_id: str):
    try:
        is_series = media_type in ("series", "tv")
        tmdb, imdb, _, _ = parse_stremio_id(meta_id)
        tmdb = resolve_tmdb(imdb, tmdb, is_series)
        if not tmdb:
            return jsonify({"meta": {}})
        api_type = "tv" if is_series else "movie"
        det = details(tmdb, api_type)
        if not det:
            return jsonify({"meta": {}})
        poster = det.get("poster_path")
        backdrop = det.get("backdrop_path")
        meta_obj = {
            "id": meta_id.replace(".json", ""),
            "type": "series" if is_series else "movie",
            "name": det.get("title") or det.get("name") or "",
            "poster": f"https://image.tmdb.org/t/p/w500{poster}" if poster else None,
            "background": f"https://image.tmdb.org/t/p/w1280{backdrop}" if backdrop else None,
            "description": det.get("overview") or "",
            "releaseInfo": (det.get("release_date") or det.get("first_air_date") or "")[:4],
            "imdbRating": str(det.get("vote_average") or ""),
        }
        return jsonify({"meta": meta_obj})
    except Exception:
        traceback.print_exc()
        return jsonify({"meta": {}})


@app.route("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "version": VERSION,
            "cffi": HAS_CFFI,
            "seed_proxy": bool(SEED_PROXY),
        }
    )


if __name__ == "__main__":
    print(f"{APP_NAME} {VERSION} on :{PORT} cffi={HAS_CFFI} proxy={bool(SEED_PROXY)}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
