#!/usr/bin/env python3
"""KissAsian (kissasian.cam) stream addon for Nuvio/Stremio — justplay.cam bypass."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from html import unescape
from urllib.parse import quote, urljoin, urlparse

from flask import Flask, Response, request

try:
    from curl_cffi import requests as creq
    HAS_CFFI = True
except ImportError:
    import requests as creq
    HAS_CFFI = False

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

app = Flask(__name__)

ADDON_VERSION = "1.0.0"
ADDON_NAME = "KissAsian"
ADDON_ID = "org.nuvio.kissasian"
PUBLIC_HOST = "168.138.176.147:51824"

BASE = "https://kissasian.cam"
JUSTPLAY = "https://justplay.cam"
TMDB_KEY = "1adf1a2b5aece0ac5106302d3299f56f"
TMDB = "https://api.themoviedb.org/3"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

CACHE: dict = {}
CACHE_TTL = 600
TMDB_TTL = 86400
TOKEN_TTL = 1500  # ~25 min, playback ~15 min

# ─── helpers ───────────────────────────────────────────────

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(s: str) -> bytes:
    s = str(s).replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def cors(resp: Response) -> Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
    return resp


def j(data, status=200):
    return cors(
        Response(json.dumps(data, ensure_ascii=False), status=status, mimetype="application/json")
    )


def cache_get(key):
    item = CACHE.get(key)
    if not item:
        return None
    if item[1] <= time.time():
        CACHE.pop(key, None)
        return None
    return item[0]


def cache_put(key, data, ttl=CACHE_TTL):
    if len(CACHE) > 500:
        now = time.time()
        for k in [k for k, v in list(CACHE.items()) if v[1] <= now][:100]:
            CACHE.pop(k, None)
    CACHE[key] = (data, time.time() + ttl)


def http_get(url: str, referer: str = BASE + "/", timeout: int = 25, headers: dict | None = None):
    h = {
        "User-Agent": UA,
        "Accept": "text/html,application/json,application/xhtml+xml,*/*",
        "Referer": referer,
        "Origin": urlparse(referer).scheme + "://" + urlparse(referer).netloc,
    }
    if headers:
        h.update(headers)
    if HAS_CFFI:
        r = creq.get(url, headers=h, impersonate="chrome120", timeout=timeout)
    else:
        r = creq.get(url, headers=h, timeout=timeout)
    return r


def http_post_json(url: str, payload: dict, referer: str = JUSTPLAY + "/", extra_headers: dict | None = None, timeout: int = 25):
    h = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Referer": referer,
        "Origin": urlparse(referer).scheme + "://" + urlparse(referer).netloc,
    }
    if extra_headers:
        h.update(extra_headers)
    body = json.dumps(payload)
    if HAS_CFFI:
        r = creq.post(url, data=body, headers=h, impersonate="chrome120", timeout=timeout)
    else:
        r = creq.post(url, data=body, headers=h, timeout=timeout)
    return r


# ─── PoW (justplay byse) ───────────────────────────────────

_BE, _LT, _DR = 512, 511, 2
_LR, _HR = 2654435761, 2246822519


def _re_rot(t, e):
    return ((t << e) | (t >> (32 - e))) & 0xFFFFFFFF


def _ht(t, e):
    return (t * e) & 0xFFFFFFFF


def _ye(t):
    t[0] = (t[0] + t[1]) & 0xFFFFFFFF
    t[3] = _re_rot(t[3] ^ t[0], 16)
    t[2] = (t[2] + t[3]) & 0xFFFFFFFF
    t[1] = _re_rot(t[1] ^ t[2], 12)
    t[0] = (t[0] + t[1]) & 0xFFFFFFFF
    t[3] = _re_rot(t[3] ^ t[0], 8)
    t[2] = (t[2] + t[3]) & 0xFFFFFFFF
    t[1] = _re_rot(t[1] ^ t[2], 7)


def _gr(tb):
    e = [1779033703, 3144134277, 1013904242, 2773480762]
    for b in tb:
        e[0] = (e[0] + b) & 0xFFFFFFFF
        e[0] = _re_rot(e[0], 7)
        _ye(e)
    for _ in range(8):
        _ye(e)
    r = [0] * _BE
    for i in range(_BE):
        _ye(e)
        r[i] = (e[0] ^ e[2]) & 0xFFFFFFFF
    for _ in range(_DR):
        for s in range(_BE):
            a = r[s] & _LT
            c = (r[s] + r[a]) & 0xFFFFFFFF
            c = _re_rot(c, 13)
            c = (c ^ _ht(r[(s + 1) & _LT], _LR)) & 0xFFFFFFFF
            r[s] = c
            e[0] = (e[0] ^ c) & 0xFFFFFFFF
            _ye(e)
    n = [0] * 8
    o = _BE // 8
    for i in range(8):
        _ye(e)
        s = e[0]
        a = i * o
        for c in range(o):
            d = r[a + c]
            s = (s + d) & 0xFFFFFFFF
            s = _re_rot(s, 5)
            s = (s ^ _ht(d, _HR)) & 0xFFFFFFFF
        n[i] = (s ^ e[2]) & 0xFFFFFFFF
    return n


def _wr(t):
    z = 0
    for n in t:
        if n == 0:
            z += 32
            continue
        return z + (32 - n.bit_length())
    return z


def _pow_worker(args):
    nonce, difficulty, start, count = args
    prefix = nonce + ":"
    for s in range(start, start + count):
        tb = [ord(c) & 255 for c in prefix + str(s)]
        if _wr(_gr(tb)) >= difficulty:
            return s
    return None


def solve_pow(nonce: str, difficulty: int, timeout_s: float = 90) -> str | None:
    if difficulty <= 0:
        return "0"
    t0 = time.time()
    chunk = 2000
    start = 0
    workers = 2
    while time.time() - t0 < timeout_s:
        tasks = [(nonce, difficulty, start + i * chunk, chunk) for i in range(workers)]
        try:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                futs = [pool.submit(_pow_worker, t) for t in tasks]
                for f in as_completed(futs):
                    res = f.result()
                    if res is not None:
                        return str(res)
        except Exception:
            # fallback single-thread
            prefix = nonce + ":"
            s = start
            end = start + workers * chunk
            while s < end and time.time() - t0 < timeout_s:
                tb = [ord(c) & 255 for c in prefix + str(s)]
                if _wr(_gr(tb)) >= difficulty:
                    return str(s)
                s += 1
        start += workers * chunk
    return None


# ─── justplay access + playback ────────────────────────────

def _fake_hash(s: str) -> str:
    return b64url(hashlib.sha256(s.encode()).digest())


def justplay_attest() -> dict:
    hit = cache_get("jp:attest")
    if hit:
        return hit
    priv = ec.generate_private_key(ec.SECP256R1(), default_backend())
    nums = priv.public_key().public_numbers()
    public_jwk = {
        "kty": "EC",
        "crv": "P-256",
        "x": b64url(nums.x.to_bytes(32, "big")),
        "y": b64url(nums.y.to_bytes(32, "big")),
    }
    r = http_post_json(f"{JUSTPLAY}/api/videos/access/challenge", {}, referer=f"{JUSTPLAY}/")
    if r.status_code != 200:
        raise RuntimeError(f"challenge {r.status_code}")
    ch = r.json()
    nonce = ch["nonce"]
    sig_der = priv.sign(nonce.encode(), ec.ECDSA(hashes.SHA256()))
    rr, ss = decode_dss_signature(sig_der)
    signature = b64url(rr.to_bytes(32, "big") + ss.to_bytes(32, "big"))
    client = {
        "user_agent": UA,
        "hardware_concurrency": 8,
        "device_memory": 8,
        "canvas_hash": _fake_hash("c"),
        "audio_hash": _fake_hash("a"),
        "pointer_type": "fine",
        "webgl_renderer": "ANGLE",
    }
    payload = {
        "viewer_id": "",
        "device_id": "",
        "challenge_id": ch["challenge_id"],
        "nonce": nonce,
        "signature": signature,
        "public_key": public_jwk,
        "client": client,
        "storage": {},
        "attributes": {"entropy": "low"},
    }
    r2 = http_post_json(f"{JUSTPLAY}/api/videos/access/attest", payload, referer=f"{JUSTPLAY}/")
    if r2.status_code != 200:
        raise RuntimeError(f"attest {r2.status_code} {r2.text[:200]}")
    att = r2.json()
    fp = {
        "token": att["token"],
        "viewer_id": att["viewer_id"],
        "device_id": att["device_id"],
    }
    cache_put("jp:attest", fp, TOKEN_TTL)
    return fp


def decrypt_playback(play: dict) -> dict:
    parts = play.get("key_parts") or []
    try:
        n = int(str(play.get("version") or "0"))
    except Exception:
        n = 0
    a, i = n ^ 0, 31 - (n ^ 0)
    if 1 <= a <= len(parts) and 1 <= i <= len(parts):
        selected = [parts[a - 1], parts[i - 1]]
    else:
        selected = parts
    key = b"".join(b64url_decode(p) for p in selected if p)
    iv = b64url_decode(play["iv"])
    payload = b64url_decode(play["payload"])
    pt = AESGCM(key).decrypt(iv, payload, None)
    return json.loads(pt.decode())


def justplay_sources(file_code: str, page_url: str) -> list[dict]:
    ck = f"jp:src:{file_code}"
    hit = cache_get(ck)
    if hit is not None:
        return hit

    fp = justplay_attest()
    embed_ref = f"{JUSTPLAY}/e/{file_code}/"
    extra = {
        "X-Embed-Origin": "kissasian.cam",
        "X-Embed-Referer": page_url or (BASE + "/"),
    }

    r = http_post_json(
        f"{JUSTPLAY}/api/videos/{file_code}/embed/captcha",
        {"fingerprint": fp},
        referer=embed_ref,
        extra_headers=extra,
    )
    if r.status_code != 200:
        raise RuntimeError(f"captcha {r.status_code}")
    powc = r.json()
    sol = solve_pow(powc["pow_nonce"], int(powc["pow_difficulty"]), timeout_s=90)
    if not sol:
        raise RuntimeError("pow timeout")

    r2 = http_post_json(
        f"{JUSTPLAY}/api/videos/{file_code}/embed/captcha/verify",
        {"pow_token": powc["pow_token"], "solution": sol, "fingerprint": fp},
        referer=embed_ref,
        extra_headers=extra,
    )
    if r2.status_code != 200:
        raise RuntimeError(f"verify {r2.status_code}")
    ver = r2.json()
    if ver.get("status") != "ok" or not ver.get("token"):
        raise RuntimeError(f"verify fail {ver}")

    extra2 = dict(extra)
    extra2["X-Captcha-Token"] = ver["token"]
    r3 = http_post_json(
        f"{JUSTPLAY}/api/videos/{file_code}/embed/playback",
        {"fingerprint": fp},
        referer=embed_ref,
        extra_headers=extra2,
    )
    if r3.status_code != 200:
        raise RuntimeError(f"playback {r3.status_code}")
    pb = r3.json()
    play = pb.get("playback")
    if not play:
        raise RuntimeError("no playback")
    data = decrypt_playback(play)
    sources = data.get("sources") or []
    out = []
    for s in sources:
        url = s.get("url")
        if not url:
            continue
        out.append(
            {
                "url": url,
                "quality": str(s.get("quality") or ""),
                "label": str(s.get("label") or s.get("quality") or "HD"),
                "height": s.get("height") or 0,
            }
        )
    out.sort(key=lambda x: -(x.get("height") or 0))
    cache_put(ck, out, min(TOKEN_TTL, 800))
    return out


# ─── kissasian scrape ─────────────────────────────────────

def extract_series_from_search(html: str) -> list[dict]:
    items = []
    seen = set()
    for m in re.finditer(
        r'href="(https?://kissasian\.cam/series/([^"/]+)/?)"[^>]*>',
        html,
        re.I,
    ):
        url, slug = m.group(1), m.group(2)
        if slug in seen:
            continue
        seen.add(slug)
        items.append({"slug": slug, "url": url.rstrip("/") + "/"})
    # title near link
    for it in items:
        slug = it["slug"]
        m = re.search(
            rf'href="https?://kissasian\.cam/series/{re.escape(slug)}/?[^"]*"[^>]*>\s*(?:<[^>]+>\s*)*([^<]{{2,120}})',
            html,
            re.I,
        )
        title = unescape(m.group(1)).strip() if m else slug.replace("-", " ").title()
        it["title"] = re.sub(r"\s+", " ", title)
    return items


def search_kissasian(keyword: str) -> list[dict]:
    ck = f"ka:search:{keyword.lower()}"
    hit = cache_get(ck)
    if hit is not None:
        return hit
    url = f"{BASE}/?s={quote(keyword)}"
    r = http_get(url)
    if r.status_code != 200:
        return []
    items = extract_series_from_search(r.text)
    cache_put(ck, items, 300)
    return items


def parse_episode_list(series_html: str, slug: str) -> list[dict]:
    eps = []
    seen = set()
    for m in re.finditer(
        rf'href="(https?://kissasian\.cam/{re.escape(slug)}-episode-(\d+)/?)"',
        series_html,
        re.I,
    ):
        url, num = m.group(1), int(m.group(2))
        if num in seen:
            continue
        seen.add(num)
        eps.append({"episode": num, "url": url.rstrip("/") + "/"})
    eps.sort(key=lambda x: x["episode"])
    return eps


def get_series_meta(slug: str) -> dict | None:
    ck = f"ka:series:{slug}"
    hit = cache_get(ck)
    if hit is not None:
        return hit
    url = f"{BASE}/series/{slug}/"
    r = http_get(url)
    if r.status_code != 200:
        return None
    html = r.text
    title_m = re.search(r"<h1[^>]*>([^<]+)</h1>", html, re.I)
    title = unescape(title_m.group(1)).strip() if title_m else slug.replace("-", " ").title()
    poster_m = re.search(r'property="og:image"\s+content="([^"]+)"', html, re.I)
    poster = poster_m.group(1) if poster_m else ""
    year_m = re.search(r"(20\d{2}|19\d{2})", html)
    year = year_m.group(1) if year_m else ""
    eps = parse_episode_list(html, slug)
    meta = {
        "slug": slug,
        "title": title,
        "poster": poster,
        "year": year,
        "url": url,
        "episodes": eps,
    }
    cache_put(ck, meta, 600)
    return meta


def extract_justplay_code(episode_url: str) -> str | None:
    ck = f"ka:epcode:{episode_url}"
    hit = cache_get(ck)
    if hit is not None:
        return hit
    r = http_get(episode_url)
    if r.status_code != 200:
        return None
    html = r.text
    m = re.search(r"https?://justplay\.cam/e/([a-zA-Z0-9]+)", html)
    if not m:
        m = re.search(r'src="([^"]*justplay\.cam/e/([a-zA-Z0-9]+)[^"]*)"', html)
        if m:
            code = m.group(2)
            cache_put(ck, code, 1800)
            return code
        return None
    code = m.group(1)
    cache_put(ck, code, 1800)
    return code


# ─── TMDB ──────────────────────────────────────────────────

def tmdb_find_imdb(imdb_id: str):
    ck = f"tmdb:find:{imdb_id}"
    hit = cache_get(ck)
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
                cache_put(ck, res, TMDB_TTL)
                return res
    except Exception:
        pass
    return None


def tmdb_from_id(tmdb_id: int, prefer: str = "tv"):
    ck = f"tmdb:id:{prefer}:{tmdb_id}"
    hit = cache_get(ck)
    if hit is not None:
        return hit
    for mtype in (prefer, "movie" if prefer == "tv" else "tv"):
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
                cache_put(ck, res, TMDB_TTL)
                return res
        except Exception:
            continue
    return None


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
    m = re.match(r"^kissasian:([a-z0-9-]+)(?::(\d+))?$", raw, re.I)
    if m:
        return "kissasian", m.group(1), None, int(m.group(2)) if m.group(2) else None
    return None, None, None, None


def pick_series(candidates: list[dict], title: str, year: str) -> dict | None:
    if not candidates:
        return None
    t = re.sub(r"[^a-z0-9]+", "", title.lower())
    best = None
    best_score = -1
    for c in candidates:
        ct = re.sub(r"[^a-z0-9]+", "", (c.get("title") or "").lower())
        score = 0
        if ct == t:
            score = 100
        elif t in ct or ct in t:
            score = 80
        else:
            # token overlap
            ta = set(re.findall(r"[a-z0-9]+", title.lower()))
            tb = set(re.findall(r"[a-z0-9]+", (c.get("title") or "").lower()))
            if ta and tb:
                score = int(100 * len(ta & tb) / max(len(ta), len(tb)))
        if year and year in (c.get("title") or ""):
            score += 5
        if score > best_score:
            best_score = score
            best = c
    return best if best_score >= 40 else candidates[0]


# ─── routes ────────────────────────────────────────────────

@app.route("/")
def root():
    return j({"ok": True, "version": ADDON_VERSION, "name": ADDON_NAME, "manifest": "/manifest.json"})


@app.route("/manifest.json")
def manifest():
    return j(
        {
            "id": ADDON_ID,
            "version": ADDON_VERSION,
            "name": ADDON_NAME,
            "description": "KissAsian (kissasian.cam) · Asian drama · justplay streams",
            "logo": "https://www.google.com/s2/favicons?domain=kissasian.cam&sz=128",
            "resources": ["catalog", "meta", "stream"],
            "types": ["series", "movie"],
            "idPrefixes": ["tt", "tmdb:", "kissasian:"],
            "catalogs": [
                {
                    "type": "series",
                    "id": "kissasian-search",
                    "name": "KissAsian Search",
                    "extra": [{"name": "search", "isRequired": True}],
                },
                {
                    "type": "series",
                    "id": "kissasian-home",
                    "name": "KissAsian Hot",
                },
            ],
            "behaviorHints": {"configurable": False, "adult": False},
        }
    )


@app.route("/catalog/<ctype>/<cid>.json")
@app.route("/catalog/<ctype>/<cid>/<path:extra>.json")
def catalog(ctype, cid, extra=""):
    search_q = None
    if extra:
        m = re.search(r"search=([^&/]+)", extra)
        if m:
            from urllib.parse import unquote

            search_q = unquote(m.group(1))
    # also query string
    search_q = search_q or request.args.get("search")

    metas = []
    if cid == "kissasian-search" and search_q:
        for it in search_kissasian(search_q)[:30]:
            slug = it["slug"]
            metas.append(
                {
                    "id": f"kissasian:{slug}",
                    "type": "series",
                    "name": it.get("title") or slug,
                    "poster": None,
                }
            )
    elif cid == "kissasian-home":
        try:
            r = http_get(BASE + "/")
            if r.status_code == 200:
                for it in extract_series_from_search(r.text)[:24]:
                    metas.append(
                        {
                            "id": f"kissasian:{it['slug']}",
                            "type": "series",
                            "name": it.get("title") or it["slug"],
                        }
                    )
        except Exception:
            pass
    return j({"metas": metas})


@app.route("/meta/<mtype>/<path:mid>")
def meta(mtype, mid):
    mid = mid.replace(".json", "")
    kind, ident, season, episode = parse_stremio_id(mid)
    if kind == "kissasian":
        sm = get_series_meta(ident)
        if not sm:
            return j({"meta": {}})
        videos = []
        for ep in sm.get("episodes") or []:
            videos.append(
                {
                    "id": f"kissasian:{ident}:{ep['episode']}",
                    "title": f"Episode {ep['episode']}",
                    "season": 1,
                    "episode": ep["episode"],
                }
            )
        return j(
            {
                "meta": {
                    "id": f"kissasian:{ident}",
                    "type": "series",
                    "name": sm["title"],
                    "poster": sm.get("poster"),
                    "releaseInfo": sm.get("year"),
                    "videos": videos,
                }
            }
        )
    return j({"meta": {}})


@app.route("/stream/<stype>/<path:sid>")
def stream(stype, sid):
    sid = sid.replace(".json", "")
    kind, ident, season, episode = parse_stremio_id(sid)

    title, year = "", ""
    ep_num = episode
    slug = None

    if kind == "kissasian":
        slug = ident
        if ep_num is None:
            ep_num = 1
        sm = get_series_meta(slug)
        if sm:
            title = sm.get("title") or slug
            year = sm.get("year") or ""
    elif kind in ("imdb", "tmdb"):
        if kind == "imdb":
            found = tmdb_find_imdb(ident)
        else:
            found = tmdb_from_id(int(ident), prefer="tv" if stype == "series" else "movie")
        if not found:
            return j({"streams": []})
        mtype, tid, title, year = found
        if stype == "series" and ep_num is None:
            ep_num = 1
        # search by title
        cands = search_kissasian(title)
        if not cands and year:
            cands = search_kissasian(f"{title} {year}")
        picked = pick_series(cands, title, year)
        if not picked:
            return j({"streams": []})
        slug = picked["slug"]
        sm = get_series_meta(slug)
        if sm:
            title = sm.get("title") or title
    else:
        return j({"streams": []})

    if not slug:
        return j({"streams": []})

    # movie: try episode 0/1 or series page only
    ep_num = ep_num or 1
    ep_url = f"{BASE}/{slug}-episode-{ep_num}/"
    # verify exists via series meta
    sm = get_series_meta(slug) or {}
    ep_map = {e["episode"]: e["url"] for e in sm.get("episodes") or []}
    if ep_num in ep_map:
        ep_url = ep_map[ep_num]
    elif sm.get("episodes"):
        # closest
        ep_url = sm["episodes"][0]["url"]
        ep_num = sm["episodes"][0]["episode"]

    try:
        code = extract_justplay_code(ep_url)
        if not code:
            return j({"streams": []})
        sources = justplay_sources(code, ep_url)
    except Exception as e:
        return j({"streams": [], "error": str(e)[:200]})

    streams = []
    for i, s in enumerate(sources):
        q = s.get("label") or "HD"
        detail = title or slug
        if year:
            detail += f" ({year})"
        detail += f" · Ep {ep_num}\n{q}\nKissAsian · justplay"
        streams.append(
            {
                "name": f"KissAsian {q}",
                "title": detail,
                "url": s["url"],
                "behaviorHints": {
                    "notWebReady": True,
                    "bingeGroup": f"kissasian-{slug}-{ep_num}",
                    "proxyHeaders": {
                        "request": {
                            "Referer": JUSTPLAY + "/",
                            "Origin": JUSTPLAY,
                            "User-Agent": UA,
                        }
                    },
                },
            }
        )
    return j({"streams": streams})


@app.route("/<path:_any>", methods=["OPTIONS"])
@app.route("/", methods=["OPTIONS"])
def options(_any=None):
    return cors(Response(status=204))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=51824, debug=False)
