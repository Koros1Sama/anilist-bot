"""
المخلافي — AniList Telegram Bot v3.0 (Vercel Serverless Webhook)
Single-file deployment for Vercel (stdlib only — urllib).

Major fixes vs v2.0:
  - Persistent storage via Upstash Redis REST API (with in-memory fallback)
    => conversation context, pending confirmations & last-action survive across
       Vercel instances. Fixes "نعم" confirmations, corrections, pronoun resolution.
  - Smart latest-season resolution via AniList relations (الجزء الجديد/الأخير/النزل قريب).
  - Correction handling (CORRECT action + code-side detector) — no more
    "لا لا هذا قد كملته..." treated as a title search.
  - progress_delta vs absolute_progress safeguard (بدأت => absolute, not delta).
  - UNDO / revert of the last mutation.
  - Null-safe everywhere (fixes 'NoneType' object has no attribute 'get').
  - Robust inline-keyboard callbacks (delete confirmation always answers).
"""

import os
import re
import json
import time
import random
import datetime
import urllib.request
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler


# ============================================================
# [0] LOCAL .env LOADER (dev only; Vercel injects env directly)
# ============================================================
def _load_local_env():
    for _p in (
        os.path.join(os.path.dirname(__file__), ".env"),
        os.path.join(os.path.dirname(__file__), "..", ".env"),
    ):
        if os.path.exists(_p):
            try:
                with open(_p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            os.environ.setdefault(k.strip(), v.strip())
            except Exception:
                pass
            break


_load_local_env()

# ============================================================
# [1] CONFIG
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
# Private bot — restrict usage to the owner only (override via env if needed).
ALLOWED_TG_USERNAME = (os.environ.get("ALLOWED_TELEGRAM_USERNAME") or "KorosSama").lower().lstrip("@")
ALLOWED_TG_ID = str(os.environ.get("ALLOWED_TELEGRAM_ID") or "803065257")  # @KorosSama
ANILIST_TOKEN = os.environ.get("ANILIST_ACCESS_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
ANILIST_URL = "https://graphql.anilist.co"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AlMakhlafi/3.0"

# Persistent storage (Upstash Redis REST / Vercel KV). Optional.
KV_URL = (
    os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL") or ""
)
KV_TOKEN = (
    os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    or os.environ.get("KV_REST_API_TOKEN")
    or ""
)
KV_ENABLED = bool(KV_URL and KV_TOKEN)

CONTEXT_MAX_MESSAGES = 8
CONTEXT_TTL = 1800  # 30 min
PENDING_TTL = 300  # 5 min
LAST_TTL = 3600  # 1 hour (for undo)
HTTP_TIMEOUT = 8


# ============================================================
# [2] HELPERS — null-safe access
# ============================================================
def sget(d, *keys, default=None):
    """Null-safe nested dict get. sget(d,'a','b') survives None at any level."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur if cur is not None else default


def _json_dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ============================================================
# [3] STORAGE LAYER — Upstash Redis REST (with in-memory fallback)
# ============================================================
_mem = {}  # in-memory fallback: {key: (value, expiry_ts)}


def _kv_post(path, body=None, headers_extra=None):
    url = KV_URL.rstrip("/") + path
    headers = {"Authorization": f"Bearer {KV_TOKEN}"}
    if headers_extra:
        headers.update(headers_extra)
    data = body.encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def store_set(key, value, ttl=None):
    """Store a JSON-serializable value. ttl in seconds (0/None = no expiry)."""
    raw = _json_dumps(value)
    if KV_ENABLED:
        try:
            path = f"/set/{urllib.parse.quote(key, safe='')}"
            qs = f"?EX={int(ttl)}" if ttl else ""
            _kv_post(path + qs, body=raw, headers_extra={"Content-Type": "text/plain"})
            return
        except Exception as e:
            print(f"[KV] set failed ({key}): {e}; using memory")
    _mem[key] = (raw, time.time() + ttl if ttl else None)


def store_get(key):
    if KV_ENABLED:
        try:
            path = f"/get/{urllib.parse.quote(key, safe='')}"
            url = KV_URL.rstrip("/") + path
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {KV_TOKEN}"}
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            res = data.get("result")
            if res is None:
                return None
            return json.loads(res)
        except Exception as e:
            print(f"[KV] get failed ({key}): {e}; using memory")
    # memory
    if key in _mem:
        raw, exp = _mem[key]
        if exp and time.time() > exp:
            _mem.pop(key, None)
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


def store_del(key):
    if KV_ENABLED:
        try:
            path = f"/del/{urllib.parse.quote(key, safe='')}"
            url = KV_URL.rstrip("/") + path
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {KV_TOKEN}"}
            )
            urllib.request.urlopen(req, timeout=HTTP_TIMEOUT).read()
            return
        except Exception as e:
            print(f"[KV] del failed ({key}): {e}")
    _mem.pop(key, None)


# ---- Conversation context ----
def get_context(cid):
    return store_get(f"ctx:{cid}") or []


def save_context(cid, role, text, extra=None):
    msgs = get_context(cid)
    msgs.append({"role": role, "text": text, "extra": extra or {}})
    msgs = msgs[-CONTEXT_MAX_MESSAGES:]
    store_set(f"ctx:{cid}", msgs, ttl=CONTEXT_TTL)


def clear_context(cid):
    store_del(f"ctx:{cid}")


def build_gemini_context(cid):
    msgs = get_context(cid)
    if not msgs:
        return ""
    lines = ["آخر سياق المحادثة (استخدمه لفهم الرسائل اللاحقة والتصحيحات والإشارات):"]
    for m in msgs:
        who = "المستخدم" if m.get("role") == "user" else "البوت"
        ex = m.get("extra") or {}
        tag = ""
        if ex.get("media_title"):
            tag = f" [أنمي: {ex['media_title']}, id: {ex.get('media_id', '')}"
            if ex.get("action"):
                tag += f", إجراء: {ex['action']}"
            if ex.get("progress") is not None:
                tag += f", تقدم: {ex['progress']}"
            tag += "]"
        lines.append(f"{who}: {m.get('text', '')}{tag}")
    return "\n".join(lines)


# ---- Pending confirmations ----
def set_pending(cid, parsed):
    store_set(f"pend:{cid}", parsed, ttl=PENDING_TTL)


def get_pending(cid):
    return store_get(f"pend:{cid}")


def clear_pending(cid):
    store_del(f"pend:{cid}")


# ---- Last action (for UNDO) ----
def set_last_action(cid, data):
    store_set(f"last:{cid}", data, ttl=LAST_TTL)


def get_last_action(cid):
    return store_get(f"last:{cid}")


# ============================================================
# [4] ANILIST GRAPHQL CLIENT
# ============================================================
def _gql(query, variables=None, token=None, timeout=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    eff_token = token or ANILIST_TOKEN
    if eff_token:
        headers["Authorization"] = f"Bearer {eff_token}"
    data = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    # AniList rate-limits shared cloud IPs (e.g. Vercel) with a 'temporarily disabled'
    # 403 after a couple of calls. That window is short, so retry once after a brief
    # backoff instead of failing straight away — this fixes 'some commands fail'.
    last_res = None
    for attempt in range(2):
        req = urllib.request.Request(ANILIST_URL, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or HTTP_TIMEOUT) as resp:
                last_res = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                last_res = json.loads(e.read().decode("utf-8"))
            except Exception:
                last_res = {"errors": [{"message": f"HTTP {e.code}"}]}
        except Exception as e:
            last_res = {"errors": [{"message": str(e)}]}
        if not _looks_disabled(_gql_errors(last_res)):
            return last_res
        if attempt == 0:
            time.sleep(1.3)
    return last_res


def search_media(title, media_type="ANIME", per_page=5):
    q = """query($s:String,$t:MediaType,$n:Int){Page(perPage:$n){media(search:$s,type:$t){
    id type format title{romaji english native} episodes chapters volumes
    season seasonYear status averageScore
    coverImage{extraLarge large medium color} siteUrl trailer{id site}
    genres}}}}"""
    res = _gql(q, {"s": title, "t": media_type, "n": per_page})
    return sget(res, "data", "Page", "media", default=[]) or []


def get_media_by_id(media_id):
    q = """query($id:Int){Media(id:$id){id type format title{romaji english native}
    episodes chapters volumes season seasonYear status averageScore
    coverImage{extraLarge large medium color} siteUrl trailer{id site} genres}}"""
    res = _gql(q, {"id": media_id})
    return sget(res, "data", "Media")


def get_entry(media_id, token):
    q = """query($m:Int){MediaList(mediaId:$m){id status score progress private repeat}}"""
    res = _gql(q, {"m": media_id}, token)
    if "errors" in res:
        return None
    return sget(res, "data", "MediaList")


def save_entry(media_id, token, status=None, score=None, progress=None, repeat=None):
    q = """mutation($m:Int,$st:MediaListStatus,$sc:Float,$p:Int,$r:Int){
    SaveMediaListEntry(mediaId:$m,status:$st,score:$sc,progress:$p,repeat:$r){id status score progress
    media{title{romaji english}}}}"""
    v = {"m": media_id}
    if status:
        v["st"] = status
    if score is not None:
        v["sc"] = float(score)
    if progress is not None:
        v["p"] = int(progress)
    if repeat is not None:
        v["r"] = int(repeat)
    res = _gql(q, v, token)
    if "errors" in res:
        return {
            "error": (res["errors"][0] if res["errors"] else {}).get(
                "message", "Unknown"
            )
        }
    return sget(res, "data", "SaveMediaListEntry")


def delete_entry(entry_id, token):
    q = "mutation($id:Int){DeleteMediaListEntry(id:$id){deleted}}"
    res = _gql(q, {"id": entry_id}, token)
    return bool(sget(res, "data", "DeleteMediaListEntry", "deleted"))


def toggle_fav(media_id, token, media_type="ANIME"):
    field = "animeId" if media_type == "ANIME" else "mangaId"
    q = f"mutation($a:Int){{ToggleFavourite({field}:$a){{anime{{nodes{{id}}}}}}}}"
    return _gql(q, {"a": media_id}, token)


def get_viewer(token):
    res = _gql("query{Viewer{id name}}", token=token)
    return sget(res, "data", "Viewer")


def get_activities(user_id, page=1, per_page=8, token=None):
    q = """query($u:Int,$p:Int,$n:Int){Page(page:$p,perPage:$n){
    activities(userId:$u,type:MEDIA_LIST,sort:ID_DESC){...on ListActivity{
    status progress createdAt media{id title{romaji english} coverImage{large} siteUrl}}}}}"""
    res = _gql(q, {"u": user_id, "p": page, "n": per_page}, token=token or ANILIST_TOKEN)
    return sget(res, "data", "Page", "activities", default=[]) or []


def get_stats(username, token=None):
    q = """query($n:String){User(name:$n){statistics{anime{count meanScore minutesWatched
    episodesWatched chaptersRead genres(limit:5,sort:COUNT_DESC){genre count meanScore}
    studios(limit:3,sort:COUNT_DESC){studio{name}count}} manga{count meanScore chaptersRead}}}}"""
    res = _gql(q, {"n": username}, token=token or ANILIST_TOKEN)
    return sget(res, "data", "User", "statistics")


def get_profile(username, token=None):
    q = """query($n:String){User(name:$n){id name about avatar{large} bannerImage siteUrl
    statistics{anime{count meanScore episodesWatched} manga{count meanScore chaptersRead}}}}"""
    res = _gql(q, {"n": username}, token=token or ANILIST_TOKEN)
    return sget(res, "data", "User")


def get_media_list(
    username=None,
    user_id=None,
    media_type="ANIME",
    status=None,
    sort=None,
    page=1,
    per_page=10,
    token=None,
):
    q = """query($un:String,$uid:Int,$t:MediaType,$st:MediaListStatus,$so:[MediaListSort],$p:Int,$n:Int){
    Page(page:$p,perPage:$n){mediaList(userName:$un,userId:$uid,type:$t,status:$st,sort:$so){
    id status score progress private media{id title{romaji english} episodes chapters genres
    averageScore coverImage{large} siteUrl}}}}"""
    v = {"t": media_type, "p": page, "n": per_page}
    if username:
        v["un"] = username
    if user_id:
        v["uid"] = user_id
    if status:
        v["st"] = status
    if sort:
        v["so"] = sort
    res = _gql(q, v, token=token or ANILIST_TOKEN)
    return sget(res, "data", "Page", "mediaList", default=[]) or []


def get_recommendations(media_id, per_page=6):
    q = """query($m:Int,$n:Int){Page(perPage:$n){recommendations(mediaId:$m,sort:RATING_DESC){
    mediaRecommendation{id title{romaji english} coverImage{large} averageScore siteUrl}}}}"""
    res = _gql(q, {"m": media_id, "n": per_page})
    recs = sget(res, "data", "Page", "recommendations", default=[]) or []
    return [r["mediaRecommendation"] for r in recs if sget(r, "mediaRecommendation")]


def get_trending(per_page=8):
    q = """query($n:Int){Page(perPage:$n){media(type:ANIME,sort:TRENDING_DESC){
    id title{romaji english} coverImage{large} averageScore siteUrl genres}}}"""
    res = _gql(q, {"n": per_page})
    return sget(res, "data", "Page", "media", default=[]) or []


def get_seasonal(season, year, per_page=8):
    q = """query($s:MediaSeason,$y:Int,$n:Int){Page(perPage:$n){
    media(type:ANIME,season:$s,seasonYear:$y,sort:POPULARITY_DESC){
    id title{romaji english} coverImage{large} averageScore siteUrl}}}"""
    res = _gql(q, {"s": season, "y": year, "n": per_page})
    return sget(res, "data", "Page", "media", default=[]) or []


def get_airing(media_id):
    q = """query($m:Int){AiringSchedule(mediaId:$m,notYetAired:true){airingAt timeUntilAiring episode}}"""
    res = _gql(q, {"m": media_id})
    return sget(res, "data", "AiringSchedule")


def get_relations(media_id, timeout=None):
    """Returns relation edges. Nodes include seasonYear/episodes/status for season resolution."""
    q = """query($m:Int){Media(id:$m){relations{edges{relationType(version:2)
    node{id title{romaji english native} type format status seasonYear episodes chapters
    coverImage{large} siteUrl}}}}}"""
    res = _gql(q, {"m": media_id}, timeout=timeout)
    return sget(res, "data", "Media", "relations", "edges", default=[]) or []


def get_favorites(username, token=None):
    q = """query($n:String){User(name:$n){favourites{anime{nodes{id title{romaji english}
    coverImage{large} siteUrl}} manga{nodes{id title{romaji english} coverImage{large}}}}}}"""
    res = _gql(q, {"n": username}, token=token or ANILIST_TOKEN)
    fav = sget(res, "data", "User", "favourites", default={}) or {}
    return (
        sget(fav, "anime", "nodes", default=[]) or [],
        sget(fav, "manga", "nodes", default=[]) or [],
    )


def search_character_q(name):
    q = """query($s:String){Character(search:$s){name{full native} siteUrl image{large}
    description(asHtml:false) age gender media(perPage:3,sort:POPULARITY_DESC){nodes{title{romaji english}}}}}"""
    res = _gql(q, {"s": name})
    return sget(res, "data", "Character")


def search_staff_q(name):
    q = """query($s:String){Staff(search:$s){name{full native} image{large} primaryOccupations
    characters(perPage:5,sort:FAVOURITES_DESC){nodes{name{full}}}
    staffMedia(perPage:5,sort:POPULARITY_DESC){nodes{title{romaji}type}}}}"""
    res = _gql(q, {"s": name})
    return sget(res, "data", "Staff")


def search_studio_q(name):
    q = """query($s:String){Studio(search:$s){name isAnimationStudio siteUrl
    media(perPage:8,sort:POPULARITY_DESC){nodes{id title{romaji english} averageScore coverImage{large}}}}}"""
    res = _gql(q, {"s": name})
    return sget(res, "data", "Studio")


def current_season():
    m = datetime.datetime.now().month
    y = datetime.datetime.now().year
    if m <= 3:
        return "WINTER", y
    elif m <= 6:
        return "SPRING", y
    elif m <= 9:
        return "SUMMER", y
    else:
        return "FALL", y


def get_following(user_id, page=1, per_page=10, token=None):
    q = """query($u:Int,$p:Int,$n:Int){Page(page:$p,perPage:$n){
    following(userId:$u){id name avatar{large} siteUrl
    statistics{anime{count episodesWatched meanScore} manga{count chaptersRead}}}}}"""
    res = _gql(q, {"u": user_id, "p": page, "n": per_page}, token=token or ANILIST_TOKEN)
    return sget(res, "data", "Page", "following", default=[]) or []


# ---- Smart season resolution ----
def resolve_franchise_parts(media_id, max_calls=8):
    """BFS over SEQUEL/PREQUEL/PARENT/SIDE_STORY edges; return dict {id: node} of all ANIME parts.
    Nodes come from get_relations (already include seasonYear/episodes/status/title/cover).
    Uses a short per-call timeout to stay well within the Vercel budget."""
    collected = {}
    base = get_media_by_id(media_id) or {"id": media_id}
    collected[media_id] = base
    queue = [media_id]
    visited = set()
    calls = 0
    while queue and calls < max_calls:
        cur = queue.pop(0)
        if cur in visited:
            continue
        visited.add(cur)
        calls += 1
        for e in get_relations(cur, timeout=5):
            node = sget(e, "node") or {}
            rt = sget(e, "relationType")
            if (
                rt in ("SEQUEL", "PREQUEL", "PARENT", "SIDE_STORY")
                and node.get("type") == "ANIME"
            ):
                nid = node.get("id")
                if nid:
                    collected[nid] = node
                    if nid not in visited:
                        queue.append(nid)
    return collected


def pick_latest_part(parts):
    """From a franchise map, pick the latest: prefer RELEASING, else max seasonYear."""
    if not parts:
        return None
    vals = list(parts.values())
    releasing = [n for n in vals if sget(n, "status") == "RELEASING"]
    pool = releasing or vals
    pool.sort(key=lambda n: sget(n, "seasonYear") or 0, reverse=True)
    return pool[0] if pool else None


def pick_nth_part(parts, n):
    """Pick the Nth part chronologically by seasonYear (1 = first season)."""
    if not parts:
        return None
    vals = [v for v in parts.values() if v.get("id")]
    vals.sort(
        key=lambda x: (sget(x, "seasonYear") or 0, (sget(x, "title", "romaji") or ""))
    )
    if 1 <= n <= len(vals):
        return vals[n - 1]
    return pick_latest_part(parts)


# ============================================================
# [5] TELEGRAM CLIENT
# ============================================================
def _tg(method, payload):
    if not TELEGRAM_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[TG Error] {method}: {e}")
        return None


def tg_send(chat_id, text, kb=None):
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if kb:
        p["reply_markup"] = kb
    return _tg("sendMessage", p)


def tg_photo(chat_id, url, caption, kb=None):
    if not url:
        return tg_send(chat_id, caption, kb)
    p = {
        "chat_id": chat_id,
        "photo": url,
        "caption": (caption or "")[:1024],
        "parse_mode": "HTML",
    }
    if kb:
        p["reply_markup"] = kb
    res = _tg("sendPhoto", p)
    if not res or not res.get("ok"):
        return tg_send(chat_id, caption, kb)
    return res


def tg_album(chat_id, items):
    if not items:
        return None
    if len(items) == 1:
        return tg_photo(chat_id, items[0].get("url"), items[0].get("caption", ""))
    media = []
    for i, item in enumerate(items[:10]):
        m = {"type": "photo", "media": item["url"], "parse_mode": "HTML"}
        if i == 0 and item.get("caption"):
            m["caption"] = item["caption"][:1024]
        media.append(m)
    return _tg("sendMediaGroup", {"chat_id": chat_id, "media": media})


def tg_answer_cb(cb_id, text=None):
    p = {"callback_query_id": cb_id}
    if text:
        p["text"] = text
    return _tg("answerCallbackQuery", p)


def tg_edit(chat_id, msg_id, text, kb=None):
    p = {"chat_id": chat_id, "message_id": msg_id, "text": text, "parse_mode": "HTML"}
    # kb=None clears any existing inline keyboard (so buttons can't be re-clicked)
    p["reply_markup"] = kb if kb is not None else {"inline_keyboard": []}
    return _tg("editMessageText", p)


def kb_make(rows):
    keyboard = []
    for row in rows:
        kr = []
        for b in row:
            btn = {"text": b["text"]}
            if "url" in b:
                btn["url"] = b["url"]
            elif "callback_data" in b:
                btn["callback_data"] = b["callback_data"]
            kr.append(btn)
        keyboard.append(kr)
    return {"inline_keyboard": keyboard}


# ============================================================
# [6] DISPLAY HELPERS
# ============================================================
def title_of(media):
    if not media:
        return "غير معروف"
    t = media.get("title") or {}
    return t.get("english") or t.get("romaji") or t.get("native") or "غير معروف"


def cover_of(media):
    if not media:
        return None
    ci = media.get("coverImage") or {}
    return ci.get("extraLarge") or ci.get("large") or ci.get("medium")


def media_label(media):
    """Title + year hint."""
    name = title_of(media)
    yr = sget(media, "seasonYear")
    parts = [name]
    if yr:
        parts.append(f"({yr})")
    return " ".join(parts)


STATUS_AR = {
    "CURRENT": "أشوفه حالياً",
    "COMPLETED": "مكتمل",
    "PLANNING": "أخطط له",
    "PAUSED": "متوقف",
    "DROPPED": "مسحوب",
    "REPEATING": "إعادة",
}


def status_ar(s):
    return STATUS_AR.get(s, s or "—")


# ============================================================
# [7] ARABIC REGEX PARSER (fallback when Gemini unavailable)
# ============================================================
ANIME_DICT = {
    # -- Popular Shounen --
    "ون بيس": "One Piece",
    "وان بيس": "One Piece",
    "ناروتو": "Naruto",
    "بوروتو": "Boruto",
    "بليتش": "Bleach",
    "دراغون بول": "Dragon Ball",
    "دراقون بول": "Dragon Ball",
    "هنتر": "Hunter x Hunter",
    "القناص": "Hunter x Hunter",
    "هجوم العمالقة": "Attack on Titan",
    "العمالقة": "Attack on Titan",
    "اتاك": "Attack on Titan",
    "اوج اون تايتن": "Attack on Titan",
    "قاتل الشياطين": "Demon Slayer",
    "ديمون سلاير": "Demon Slayer",
    "كيميتسو": "Demon Slayer",
    "اللعنات": "Jujutsu Kaisen",
    "جوجوتسو": "Jujutsu Kaisen",
    "جوجو كايسن": "Jujutsu Kaisen",
    "بوكو نو هيرو": "My Hero Academia",
    "اكاديميتي": "My Hero Academia",
    "ماي هيرو": "My Hero Academia",
    "بلاك كلوفر": "Black Clover",
    "فيري تيل": "Fairy Tail",
    "كابتن ماجد": "Captain Tsubasa",
    "كابتن تسوباسا": "Captain Tsubasa",
    "الكابتن ماجد": "Captain Tsubasa",
    "الكابتن": "Captain Tsubasa",
    # -- Isekai --
    "ري زيرو": "Re:Zero kara Hajimeru Isekai Seikatsu",
    "ريزيرو": "Re:Zero kara Hajimeru Isekai Seikatsu",
    "السلايم": "Tensei shitara Slime Datta Ken",
    "سلايم": "Tensei shitara Slime Datta Ken",
    "تنسي سلايم": "Tensei shitara Slime Datta Ken",
    "اوفرلورد": "Overlord",
    "اوفر لورد": "Overlord",
    "شيلد هيرو": "Tate no Yuusha no Nariagari",
    "بطل الدرع": "Tate no Yuusha no Nariagari",
    "سورد ارت": "Sword Art Online",
    "ساو": "Sword Art Online",
    "نو قيم نو لايف": "No Game No Life",
    "كونوسوبا": "KonoSuba",
    "مشيروو": "Mushoku Tensei",
    "موشوكو": "Mushoku Tensei",
    "بلا وظيفة": "Mushoku Tensei",
    # -- Modern Hits --
    "رجل المنشار": "Chainsaw Man",
    "تشينسو مان": "Chainsaw Man",
    "تشينسو": "Chainsaw Man",
    "سولو ليفلينج": "Solo Leveling",
    "رفع المستوى فرديا": "Solo Leveling",
    "سولو ليفلينق": "Solo Leveling",
    "فريرين": "Sousou no Frieren",
    "فرايرن": "Sousou no Frieren",
    "فريرين": "Sousou no Frieren",
    "اوشي نو كو": "Oshi no Ko",
    "نجمتي المفضلة": "Oshi no Ko",
    "كايجو": "Kaijuu 8-gou",
    "كايجو 8": "Kaijuu 8-gou",
    "وند بريكر": "Wind Breaker",
    "دان دا دان": "Dandadan",
    "داندادان": "Dandadan",
    "لورد الغوامض": "Lord of Mysteries",
    "سيد الغوامض": "Lord of Mysteries",
    "لورد اوف ميستيريز": "Lord of Mysteries",
    "ساكاموتو": "Sakamoto Days",
    # -- Classics --
    "مذكرة الموت": "Death Note",
    "ديث نوت": "Death Note",
    "كونان": "Detective Conan",
    "المحقق كونان": "Detective Conan",
    "طوكيو غول": "Tokyo Ghoul",
    "طوكيو قول": "Tokyo Ghoul",
    "كود جياس": "Code Geass",
    "ستينز قيت": "Steins;Gate",
    "شتاينز": "Steins;Gate",
    "فولميتال": "Fullmetal Alchemist: Brotherhood",
    "الخيميائي": "Fullmetal Alchemist: Brotherhood",
    "جينتاما": "Gintama",
    "كلانّاد": "Clannad",
    "مدمر الخلايا": "Cells at Work",
    "المحقق كونان": "Detective Conan",
    # -- Action/Thriller --
    "طوكيو ريفنجرز": "Tokyo Revengers",
    "توكيو ريفنجرز": "Tokyo Revengers",
    "فينلاند ساغا": "Vinland Saga",
    "موب": "Mob Psycho 100",
    "وان بانش مان": "One Punch Man",
    "وان بنش": "One Punch Man",
    "هيلسينج": "Hellsing Ultimate",
    "بيرسيرك": "Berserk",
    # -- Romance/SoL --
    "كاغويا": "Kaguya-sama wa Kokurasetai",
    "كاقويا": "Kaguya-sama wa Kokurasetai",
    "توريدورا": "Toradora!",
    "هوريميا": "Horimiya",
    "سباي فاملي": "Spy x Family",
    "سباي فاميلي": "Spy x Family",
    "مقعد بجانبي": "Tonari no Kaibutsu-kun",
    # -- Arabic dubs nostalgia --
    "عدنان ولينا": "Mirai Shounen Conan",
    "مغامرات عدنان": "Mirai Shounen Conan",
    "النمر المقنع": "Tiger Mask",
    "جراندايزر": "UFO Robo Grendizer",
    "غريندايزر": "UFO Robo Grendizer",
    "ساندي بل": "Sandy Bell",
    "ساسوكي": "Sasuke",
    "ريمي": "Ie Naki Ko",
    "الرائي": "Captain Majid",
    # -- Misc --
    "راجنا": "Ragna Crimson",
    "راغنا": "Ragna Crimson",
    "بلو لوك": "Blue Lock",
    "بلولوك": "Blue Lock",
}
GENRE_MAP = {
    "أكشن": "Action",
    "اكشن": "Action",
    "مغامرة": "Adventure",
    "كوميدي": "Comedy",
    "دراما": "Drama",
    "خيالي": "Fantasy",
    "خيال": "Fantasy",
    "غموض": "Mystery",
    "رعب": "Horror",
    "رومانسي": "Romance",
    "رومانسية": "Romance",
    "خيال علمي": "Sci-Fi",
    "رياضي": "Sports",
    "نفسي": "Psychological",
    "شريحة من الحياة": "Slice of Life",
    "إثارة": "Thriller",
    "سحر": "Supernatural",
}

# Arabic number words → digits
_AR_NUM_WORDS = {
    "وحدة": 1,
    "واحدة": 1,
    "حلقة وحدة": 1,
    "حلقتين": 2,
    "ثنتين": 2,
    "ثلاث": 3,
    "ثلاثة": 3,
    "ثلاث حلقات": 3,
    "اربع": 4,
    "أربع": 4,
    "اربعة": 4,
    "أربعة": 4,
    "خمس": 5,
    "خمسة": 5,
    "خمس حلقات": 5,
    "ست": 6,
    "ستة": 6,
    "سبع": 7,
    "سبعة": 7,
    "ثمان": 8,
    "ثمانية": 8,
    "تسع": 9,
    "تسعة": 9,
    "عشر": 10,
    "عشرة": 10,
}
# Arabic ordinals → part number
_AR_ORDINALS = {
    "الاول": 1,
    "الأول": 1,
    "الاولى": 1,
    "الأولى": 1,
    "اول": 1,
    "أول": 1,
    "الثاني": 2,
    "الثانية": 2,
    "ثاني": 2,
    "التاني": 2,
    "الثالث": 3,
    "الثالثة": 3,
    "ثالث": 3,
    "الرابع": 4,
    "الرابعة": 4,
    "رابع": 4,
    "الخامس": 5,
    "الخامسة": 5,
    "خامس": 5,
    "السادس": 6,
    "السادسة": 6,
    "سادس": 6,
    "السابع": 7,
    "السابعة": 7,
    "سابع": 7,
    "الثامن": 8,
    "الثامنة": 8,
    "ثامن": 8,
}

CORRECTION_KEYWORDS = re.compile(
    r"(كنت\s*اقصد|اقصد|قصدي|غلط|صحح|مو\s*صح|مش\s*صح|غيرت|ما\s*اقصد|الغلط|الخطا|الخطأ)"
)
LATEST_PART_KEYWORDS = re.compile(
    r"(الجزء\s*(?:الجديد|الجديده|الاخير|الأخير|الاخيرة|الأخيرة|الجدد|النزل|اللي نزل|الي نزل|الاحدث|الأحدث))"
    r"|(?:الموسم\s*(?:الجديد|الجديده|الاخير|الأخير|الاخيرة|الأحدث|الجديد))"
)
FRESH_START_RE = re.compile(
    r"(بدأت|بديت|بدات|بدأ|ابتدأت|ابتديت|رجعت اتابع|رجعت اشوف|اعادة|إعادة من|من جديد|من البداية)"
)
UNDO_RE = re.compile(
    r"(تراجع|ارجع|أرجع|ترجع|undo|الغي آخر|الغاء آخر|إلغاء آخر|عكس آخر|رجع لي|رجع آخر)"
)


def _detect_part_hint(text):
    """Return ('latest'|int|None) based on the message."""
    if LATEST_PART_KEYWORDS.search(text):
        return "latest"
    # explicit ordinal: "الجزء الرابع", "الموسم الرابع", "الجزء 4"
    m = re.search(r"(?:الجزء|الموسم|البارت|السيزون)\s*(\d+)", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(?:الجزء|الموسم|البارت|السيزون)\s*([أ-ي]+)", text)
    if m:
        w = m.group(1)
        for k, v in _AR_ORDINALS.items():
            if k in w:
                return v
    return None


def regex_parse(text):
    r = {
        "action": "TRACK",
        "media_type": "ANIME",
        "title": None,
        "status": None,
        "score": None,
        "progress_delta": None,
        "absolute_progress": None,
        "is_favorite": False,
        "genre": None,
        "friend_username": None,
        "original_text": text,
        "confidence": "low",
        "alternatives": [],
        "batch": None,
        "chat_response": None,
        "fresh_start": False,
        "part_hint": None,
        "corrected_progress": None,
    }
    lt = text.strip().lower()
    # Part hint
    r["part_hint"] = _detect_part_hint(text)
    # Fresh start
    r["fresh_start"] = bool(FRESH_START_RE.search(lt))
    # --- Action detection (priority order; Gemini overrides later) ---
    if UNDO_RE.search(lt):
        r["action"] = "UNDO"
    elif re.search(r"(احذف|شيل|امسح|ازل|حذف)", lt):
        r["action"] = "DELETE"
    elif re.search(r"(إحصائيات|احصائيات|احصائي|إحصائي)", lt):
        r["action"] = "STATS"
    elif re.search(r"(نشاطات|آخر نشاط|اخر شي سويت|اخر نشاط|نشاطي)", lt):
        r["action"] = "ACTIVITY"
    elif re.search(
        r"(اصدقائي|أصدقائي|اصحابي|أصحابي|متابعيني|اتابعهم|فرندز|من اتابع|قائمة اصدقائي|قائمة أصحابي)",
        lt,
    ):
        r["action"] = "MY_FOLLOWING"
    elif re.search(r"(قائمتي|انمياتي|وش اشوف|وش اتابع)", lt):
        r["action"] = "MY_LIST"
    elif re.search(r"(الترند|ترند|ترندنق|الشائع|شائع)", lt):
        r["action"] = "TRENDING"
    elif re.search(r"(هذا الموسم|موسم الحالي|انميات الموسم|موسم الآن|موسم الحين)", lt):
        r["action"] = "SEASONAL"
    elif re.search(r"(افضل|أفضل|اعلى|أعلى|توب|اعلى تقييم|افضل تقييم)", lt):
        r["action"] = "TOP_RATED"
    elif re.search(
        r"(فاجئني|فاجأني|اعمل اي شي|اي شي عشوائي|ملّيت|مليت|عشوائي|انمي عشوائي)", lt
    ):
        r["action"] = "SURPRISE"
    elif re.search(r"(خبر حلو|أخبار|اخبار|وش الجديد|اخباري|جديد انميات)", lt):
        r["action"] = "NEWS"
    elif re.search(r"(مفضلاتي|قائمة المفضلة|وش مفضلاتي|مفضلتي)", lt):
        r["action"] = "FAVORITES_LIST"
    elif re.search(r"(اقتر[حا]|ترشيح|رشح|أنمي حلو|انمي ممتاز|اقتراح)", lt):
        r["action"] = "RECOMMEND_GENRE"
    elif re.search(r"(مشابه|شبيه|يشبه|نفس نمط|على نمط)", lt):
        r["action"] = "RECOMMEND_SIMILAR"
    elif re.search(r"(مين مؤدي|مؤدي صوت|صوت شخصية|بحث عن شخصية)", lt):
        r["action"] = "CHARACTER_LOOKUP"
    elif re.search(r"(مؤدي|ممثل صوت|staff|طاقم)", lt):
        r["action"] = "STAFF_LOOKUP"
    elif re.search(r"(استديو|ستوديو|studio|شركة انتاج)", lt):
        r["action"] = "STUDIO_LOOKUP"
    elif re.search(
        r"(سيكويل|بريكويل|تتمة|الاجزاء|الأجزاء|اجزاء|علاقات|له علاقة|مرتبط)", lt
    ):
        r["action"] = "RELATIONS"
    elif re.search(
        r"(متى الحلقة|موعد|جدول البث|الحلقة الجاية|الحلقه الجايه|متى ينزل)", lt
    ):
        r["action"] = "AIRING_SCHEDULE"
    elif re.search(r"(بروفايل|حساب|ملف)\s+([\w\u0600-\u06FF]+)", lt):
        r["action"] = "FRIEND_PROFILE"
    elif re.search(
        r"(قائمة|انميات|قائمه)\s+([\w\u0600-\u06FF]+)", lt
    ) and not re.search(r"(انمياتي|قائمتي|اصدقائي|أصدقائي)", lt):
        r["action"] = "FRIEND_LIST"
    elif re.search(r"(قارن|مقارنة|قارنني|قارن مع)", lt):
        r["action"] = "COMPARE_FRIEND"
    elif re.match(r"^\s*(لا|لأ)\s+\S", lt) or CORRECTION_KEYWORDS.search(lt):
        r["action"] = "CORRECT"
    # favorite flag
    if re.search(r"(مفضلة|مفضلتي|فيفريت|favourite|favorite|فضلة)", lt):
        r["is_favorite"] = True
    # score
    sm = re.search(
        r"(?:اقيمه|أقيمه|قيمه|تقييم|تقييمه?|راي|رأي|score)\s*(\d+(?:\.\d+)?)", lt
    )
    if not sm:
        sm = re.search(r"(\d+(?:\.\d+)?)\s*(?:من\s*10|/10|من عشرة)", lt)
    if sm:
        v = float(sm.group(1))
        if 0 <= v <= 10:
            r["score"] = v
    # status
    if re.search(
        r"(كملت|أكملت|اكملت|كمّلت|خلصت|أنهيت|نهيت|ختمت|انتهيت|كل حلقاته|كل حلقات|شفت كامل|شفته كامل|كل الحلقات|وخلصته|وخلصتها)",
        lt,
    ):
        r["status"] = "COMPLETED"
    elif re.search(r"(سحبت|تركته|كنسلت|dropped|سحبت عليه)", lt):
        r["status"] = "DROPPED"
    elif re.search(
        r"(خطة|أفكر|اضف|أضف|بشوفه|بقراه|plan|ابي اشوفه|حابه اشوفه|نويت|بنوي)", lt
    ):
        r["status"] = "PLANNING"
    elif re.search(r"(وقفت|توقفت|paused|معلق|علقته)", lt):
        r["status"] = "PAUSED"
    elif re.search(
        r"(اعدت|أعدت|اعاده|إعادة|اشوفه من جديد|ريواتش|rewatch|re-watch)", lt
    ):
        r["status"] = "REPEATING"
    elif re.search(
        r"(شفت|تابع|تابعت|شاهدت|قريت|قرأت|وصلت|اكمل|بكمل|بشوف|اشوف|اتابع)", lt
    ):
        r["status"] = "CURRENT"
    # progress — number words first
    handled = False
    for w, n in _AR_NUM_WORDS.items():
        if w in lt:
            if r["fresh_start"]:
                r["absolute_progress"] = n
            else:
                r["progress_delta"] = n
            r["status"] = r["status"] or "CURRENT"
            handled = True
            break
    if not handled:
        # "حلقة/فصل 5" → absolute; "N حلقات/فصول" → delta (unless fresh start)
        abs_m = re.search(r"(?:حلقة|فصل|شابتر|ep|ch|episode|chapter)\s*(\d+)", lt)
        if abs_m:
            r["absolute_progress"] = int(abs_m.group(1))
            r["status"] = r["status"] or "CURRENT"
            handled = True
    if not handled:
        ep = re.search(r"(\d+)\s*(?:حلقة|حلقات|فصل|فصول|شابتر|ep|eps|episodes)", lt)
        if ep:
            val = int(ep.group(1))
            if r["fresh_start"]:
                r["absolute_progress"] = val
            else:
                r["progress_delta"] = val
            r["status"] = r["status"] or "CURRENT"
            handled = True
    # manga
    if re.search(r"(مانجا|مانها|مانهوا|فصل|فصول|شابتر|chapter|manga)", lt):
        r["media_type"] = "MANGA"
    # title from dictionary
    for nick, official in ANIME_DICT.items():
        if nick in lt:
            r["title"] = official
            r["confidence"] = "medium"
            break
    # smart title extraction if dict missed
    if not r["title"] and r["action"] in (
        "TRACK",
        "DELETE",
        "RECOMMEND_SIMILAR",
        "RELATIONS",
        "AIRING_SCHEDULE",
        "FAVORITES_ADD",
        "FAVORITES_REMOVE",
        "CHARACTER_LOOKUP",
        "STAFF_LOOKUP",
        "STUDIO_LOOKUP",
    ):
        ext = _extract_title(lt)
        if ext:
            r["title"] = ext
    # genre
    for g_ar, g_en in GENRE_MAP.items():
        if g_ar in lt:
            r["genre"] = g_en
            break
    return r


_STRIP_WORDS = re.compile(
    r"\b(?:شفت|كملت|اكملت|أكملت|خلصت|انهيت|نهيت|تابعت|شاهدت|قريت|قرأت|احذف|شيل|امسح|اضف|أضف|سحبت|تركت|وقفت|توقفت|بدأت|بدات|اقيمه|أقيمه|قيمه|واقيمه|واعمله|وكمان|ضيفه|ضيفها|اعمله|اعملها|حلقة|حلقات|حلقتين|فصل|فصول|فصلين|شابتر|انمي|أنمي|مانجا|مانها|كامل|كاملة|كل|حلقاته|حلقاتها|من|امس|اليوم|اخر|آخر|جزء|اجزاء|موسم|عاده|عادة|نزل|نزلت|الي|اللي|مفضلة|مفضل|فيفريت|التقييم|تقييم|واريد|اريد|وحطه|حطه|بعد|جديد|اول|ثاني|ثالث|رابع|خامس|ابي|حاب|حابة|بغيت|ودي|شرات|اثر|بس|لا|مو|مش|كنت|اقصد|قصدي|غلط|صح)\b",
    re.IGNORECASE,
)
_STRIP_NUMS = re.compile(r"\b\d+\b")


def _extract_title(text):
    cleaned = _STRIP_WORDS.sub(" ", text)
    cleaned = _STRIP_NUMS.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"^(?:و|ب|ل|ف|ال)\s+", "", cleaned)
    cleaned = cleaned.strip(" .,!?،")
    if len(cleaned) >= 2:
        return cleaned
    return None


def _extract_corrected_progress(text):
    """Pull a corrected episode number from a correction message: 'مش 2', 'مو 2', 'اقصد 2', 'الحلقة 2'."""
    for pat in (
        r"(?:مش|مو|ما)\s*(\d+)",
        r"(?:كنت\s*اقصد|اقصد|قصدي)\s*(?:الحلقة|حلقة)?\s*(\d+)",
        r"حلقة\s*(\d+)",
        r"(\d+)\s*حلقات?",
        r"(\d+)\s*مش",
        r"^\s*(\d+)\s*$",
    ):
        m = re.search(pat, text.strip().lower())
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass
    return None


# ============================================================

# ============================================================
# [8] GEMINI FUNCTION-CALLING AGENT (v4.0)
# ============================================================
# Replaces the old single-shot "parse intent -> route" flow.
# Gemini now drives a multi-step loop: it calls tools (functionDeclarations)
# to search, mutate and render, and we feed each tool result back until it
# emits a final natural-language reply. Pure-stdlib (urllib), single file.

AGENT_SYSTEM_PROMPT = """أنت «المخلافي»، مساعد AniList لأنمي/المانجا. تتكلم بالعربية (عامية خليجية/فصحى مبسطة)، ودود ومختصر جداً.

# القاعدة الذهبية: متى تستدعِ أداة؟
قبل أي استدعاء اسأل نفسك: هل يطلب المستخدم فعلاً إجراءً على AniList؟
- إذا يكلّمنك، يعطي رأي، يرد على سؤالك، أو يتكلم عن شخص/صديق → **لا تستدعِ search_anime/search_my_list**. ردّ مباشرة بالنص، أو استخدم الأداة الاجتماعية المناسبة (get_my_following لمتابعينك، get_profile لاسم مستخدم محدد).
- **search_anime و search_my_list للبحث عن أنمي/مانجا بالاسم فقط.** الـ query لازم يكون **اسم عنوان** (مثل One Piece)، **ولا مرة جملة كاملة ولا كلام عادي**. لو الرسالة ليست طلباً صريحاً لإيجاد أنمي بالاسم، لا تستدعِ search.

# كل أداة حسب الطلب (استدعِ واحدة فقط غالباً)
- «إحصائياتي/إحصائيات X» → get_my_stats.
- «أصدقائي/متابعيني/من أتابع» → get_my_following.
- «ابحث عن مستخدم X / مين X / بروفايل X» → get_profile(username=X). (X = اسم المستخدم بس، مو جملة)
- «قائمتي/انمياتي المكتملة» → get_my_list.
- «وش الترند/هذا الموسم/أفضل أنميات» → get_trending/get_seasonal/get_top_rated.
- «احذف X» → search_my_list(X) ثم delete_entry(entry_id, media_id, title).
- «تتبع/شفت/كملت X» → search_anime(X) ثم track_media(media_id,...).
- «أنمي مشابه/علاقات/متى الحلقة لـ X» → search_anime(X) ثم الأداة المناسبة.

# انضباط الأدوات (مهم جداً)
- **لا تكرّر نفس الأداة بنفس المعطيات أبداً.** نفّذها مرة، اقرأ النتيجة، وردّ.
- استدعِ أقل عدد ممكن (عادة 1، أقصى 2). كل استدعاء يثقل AniList.
- بعد ما تجيب نتيجة، **ردّ فوراً** — لا تستدعِ أدوات إضافية بدون داعٍ.

# الترقيم
حلقتين=2، ثلاث=3، أربع=4، خمس=5، ست=6، سبع=7، ثمان=8، تسع=9، عشر=10.
«بدأت X شفت N»=CURRENT,fresh_start=true,progress=N. «شفت N حلقات من X»=CURRENT,progress_delta=N. «كملت/أنهيت X»=COMPLETED.

# الأخطاء
لو رجعت أداة {error}: اعتذر بلطف. لا تختلق معلومات. لو الخطأ يدل على تعطّل AniList قل «AniList متعطّلة شوي، جرّب بعد دقايق 🙏».

# الأسلوب
ردود نهائية قصيرة وطبيعية. لا تذكر أسماء الدوال. التأكيدات («نعم/لا») من سياق المحادثة."""

MAX_STEPS = 8
GEMINI_TIMEOUT = 8
_RUNTIME = {"last_model": None}


def _gql_errors(res):
    """Return a joined AniList error string, or None if no errors."""
    if isinstance(res, dict) and res.get("errors"):
        msgs = [str((e or {}).get("message", "")) for e in res["errors"]]
        joined = "; ".join(m for m in msgs if m)
        return joined or "AniList error"
    return None


def _looks_disabled(err):
    """True ONLY for AniList's genuine global-disable message.
    Do NOT match rate-limits (429) or transient 503s — those are 'retry', not 'down'."""
    if not err:
        return False
    e = err.lower()
    return "temporarily disabled" in e or "severe stability" in e


def _viewer_me():
    if not ANILIST_TOKEN:
        return None
    try:
        return get_viewer(ANILIST_TOKEN)
    except Exception:
        return None


def _resolve_me_and_state():
    """One probe: returns (me_or_None, state) where state in {'ok','down','no_token','error'}.
    Distinguishes a globally-disabled AniList (HTTP 403) from a missing token or other error,
    so the agent can reply 'AniList متعطّلة' consistently instead of misleading 'not found'/'empty'."""
    if not ANILIST_TOKEN:
        return None, "no_token"
    res = _gql("query{Viewer{id name}}", token=ANILIST_TOKEN)
    err = _gql_errors(res)
    if err:
        return None, ("down" if _looks_disabled(err) else "error")
    return sget(res, "data", "Viewer"), "ok"


def _compact_media(m):
    if not m:
        return None
    return {
        "id": m.get("id"),
        "title": title_of(m),
        "year": sget(m, "seasonYear"),
        "score": sget(m, "averageScore"),
        "episodes": sget(m, "episodes") or sget(m, "chapters"),
        "cover_url": cover_of(m),
        "site_url": sget(m, "siteUrl"),
    }


# ---- search with explicit error surfacing (so we can detect AniList outages) ----
_SEARCH_MEDIA_Q = """query($s:String,$t:MediaType,$n:Int){Page(perPage:$n){media(search:$s,type:$t){
id type title{romaji english native} episodes chapters volumes season seasonYear status averageScore
coverImage{extraLarge large medium color} siteUrl trailer{id site} genres}}}"""


def _search_media_e(query, media_type="ANIME", per_page=5):
    res = _gql(_SEARCH_MEDIA_Q, {"s": query, "t": media_type, "n": per_page})
    err = _gql_errors(res)
    if err:
        return [], err
    return sget(res, "data", "Page", "media", default=[]) or [], None


# ============================================================
# TOOL IMPLEMENTATIONS — each renders to Telegram and returns a compact dict
# ============================================================
def _tool_search_my_list(cid, args, me):
    q = (args.get("query") or "").strip().lower()
    mt = args.get("type") or "ANIME"
    if not me:
        return {"error": "حساب AniList غير مربوط (ANILIST_ACCESS_TOKEN ناقص)"}
    results = []
    err = None
    for pg in (1, 2):
        res = _gql(
            """query($un:String,$t:MediaType,$p:Int,$n:Int){Page(page:$p,perPage:$n){mediaList(userName:$un,type:$t){
            id status score progress media{id title{romaji english native} episodes chapters seasonYear
            averageScore coverImage{large} siteUrl}}}}""",
            {"un": me["name"], "t": mt, "p": pg, "n": 50},
            token=ANILIST_TOKEN,
        )
        err = _gql_errors(res)
        if err:
            break
        entries = sget(res, "data", "Page", "mediaList", default=[]) or []
        if not entries:
            break
        for e in entries:
            media = e.get("media") or {}
            t = (title_of(media) or "").lower()
            if q in t or t in q:
                results.append(
                    {
                        "entry_id": e.get("id"),
                        "media_id": media.get("id"),
                        "title": title_of(media),
                        "year": sget(media, "seasonYear"),
                        "status": e.get("status"),
                        "progress": e.get("progress"),
                        "score": e.get("score"),
                    }
                )
        if len(entries) < 50:
            break
    if err:
        return {"error": ("AniList متعطّلة مؤقتاً، جرّب بعد دقايق" if _looks_disabled(err) else err)}
    if not results:
        return {"error": f"ما لقيت «{args.get('query','')}» في قائمتك. (ممكن يكون مو مسجّل عندك)"}
    results.sort(
        key=lambda r: (0 if r["title"].lower().startswith(q) else 1, -(r.get("year") or 0))
    )
    return {"count": len(results), "results": results[:15]}


def _tool_search_anime(cid, args, me):
    q = (args.get("query") or "").strip()
    mt = args.get("type") or "ANIME"
    n = 5
    try:
        n = int(args.get("per_page") or 5)
    except Exception:
        n = 5
    if not q:
        return {"error": "اكتب اسم الأنمي للبحث"}
    media_list, err = _search_media_e(q, mt, n)
    if err:
        return {"error": ("AniList متعطّلة مؤقتاً، جرّب بعد دقايق" if _looks_disabled(err) else err)}
    if not media_list:
        return {"error": f"ما لقيت نتائج لـ {q}. جرّب بالإنجليزي أو الياباني."}
    return {"count": len(media_list), "results": [_compact_media(m) for m in media_list]}


def _tool_get_media_info(cid, args, me):
    mid = args.get("media_id")
    if not mid:
        return {"error": "media_id مطلوب"}
    media = get_media_by_id(int(mid))
    if not media:
        return {"error": "ما لقيت هذا الأنمي"}
    my = None
    if ANILIST_TOKEN:
        ex = get_entry(int(mid), ANILIST_TOKEN)
        if ex:
            my = {"status": ex.get("status"), "progress": ex.get("progress"), "score": ex.get("score")}
    name = title_of(media)
    cap = f"📺 <b>{name}</b>"
    yr = sget(media, "seasonYear")
    if yr:
        cap += f"  ({yr})"
    cap += f"\n⭐ {sget(media,'averageScore') or 0}%"
    eps = sget(media, "episodes") or sget(media, "chapters")
    if eps:
        cap += f"  •  🎞️ {eps}"
    if my:
        cap += f"\n📌 عندك: {status_ar(my.get('status'))}"
        if my.get("progress"):
            cap += f" ({my['progress']})"
    kb = kb_make([[{"text": "🔗 AniList", "url": sget(media, "siteUrl") or "https://anilist.co"}]])
    tg_photo(cid, cover_of(media), cap, kb)
    out = _compact_media(media) or {}
    out["my_entry"] = my
    return {"ok": True, "info": out}


def _tool_track_media(cid, args, me):
    if not ANILIST_TOKEN:
        return {"error": "حساب AniList غير مربوط"}
    media_id = args.get("media_id")
    if not media_id:
        return {"error": "media_id مطلوب — استدعِ search_anime أولاً"}
    mt = args.get("type") or "ANIME"
    part = args.get("part")
    if part:
        try:
            parts_map = resolve_franchise_parts(int(media_id))
            if part == "latest":
                chosen = pick_latest_part(parts_map)
            else:
                chosen = pick_nth_part(parts_map, int(part)) or pick_latest_part(parts_map)
            media_id = (chosen or {}).get("id", media_id)
        except Exception:
            pass
    media = get_media_by_id(int(media_id))
    if not media:
        return {"error": "ما لقيت هذا الأنمي"}
    mid = media["id"]
    status = args.get("status") or "CURRENT"
    score = args.get("score")
    fresh = bool(args.get("fresh_start"))
    existing = get_entry(mid, ANILIST_TOKEN)
    cur_prog = (existing or {}).get("progress") or 0
    prev_status = (existing or {}).get("status")
    prev_score = (existing or {}).get("score")
    prev_existed = bool(existing)
    entry_id = (existing or {}).get("id")
    total = sget(media, "episodes") or sget(media, "chapters") or 0
    new_prog = cur_prog
    if args.get("progress") is not None:
        new_prog = int(args["progress"])
    elif args.get("progress_delta") is not None:
        delta = int(args["progress_delta"])
        if fresh and prev_existed and cur_prog > 0:
            new_prog = delta  # "بدأت ... شفت N" resets to N
        else:
            new_prog = cur_prog + delta
    elif status == "COMPLETED" and total > 0:
        new_prog = total
    elif status == "COMPLETED":
        new_prog = None
    if new_prog is not None and total and new_prog > total:
        new_prog = total
    if new_prog is not None and new_prog < 0:
        new_prog = 0
    res = save_entry(mid, ANILIST_TOKEN, status=status, score=score, progress=new_prog)
    if isinstance(res, dict) and res.get("error"):
        return {"error": f"خطأ من AniList: {res['error'][:150]}"}
    new_entry_id = sget(res, "id") or entry_id
    if args.get("favorite"):
        try:
            toggle_fav(mid, ANILIST_TOKEN, mt)
        except Exception:
            pass
    set_last_action(
        cid,
        {
            "type": "save",
            "media_id": mid,
            "entry_id": new_entry_id,
            "media_title": title_of(media),
            "media_type": mt,
            "prev_existed": prev_existed,
            "prev_progress": cur_prog,
            "prev_status": prev_status,
            "prev_score": prev_score,
        },
    )
    name = title_of(media)
    unit = "الفصل" if mt == "MANGA" else "الحلقة"
    cap = f"✅ <b>تم التحديث!</b>\n\n📺 <b>{name}</b>"
    yr = sget(media, "seasonYear")
    if yr:
        cap += f"  ({yr})"
    cap += f"\n📌 {status_ar(status)}"
    if new_prog is not None:
        cap += f"\n🔢 {unit}: <b>{new_prog}</b>"
        if total:
            cap += f"/{total}"
    if score is not None:
        cap += f"\n⭐ التقييم: {score}/10"
    if args.get("favorite"):
        cap += "\n❤️ أضيف للمفضلة!"
    kb = kb_make([[{"text": "🔗 AniList", "url": sget(media, "siteUrl") or "https://anilist.co"}]])
    tg_photo(cid, cover_of(media), cap, kb)
    return {"ok": True, "title": name, "status": status, "progress": new_prog, "total": total}


def _tool_delete_entry(cid, args, me):
    if not ANILIST_TOKEN:
        return {"error": "حساب AniList غير مربوط"}
    eid = args.get("entry_id")
    if not eid:
        return {"error": "entry_id مطلوب — استدعِ search_my_list أولاً"}
    media_id = args.get("media_id")
    title = args.get("title") or ""
    prev_status = prev_prog = prev_score = None
    if media_id:
        try:
            ex = get_entry(int(media_id), ANILIST_TOKEN) or {}
            prev_status = ex.get("status")
            prev_prog = ex.get("progress")
            prev_score = ex.get("score")
        except Exception:
            pass
    ok = delete_entry(int(eid), ANILIST_TOKEN)
    if not ok:
        return {"error": "فشل الحذف — ممكن الإدخال محذوف مسبقاً أو AniList متعطّلة"}
    set_last_action(
        cid,
        {
            "type": "delete",
            "media_id": media_id,
            "media_title": title,
            "prev_status": prev_status,
            "prev_progress": prev_prog,
            "prev_score": prev_score,
        },
    )
    tg_send(cid, f"🗑️ تم حذف <b>{title or 'الإدخال'}</b> من قائمتك.\n♻️ تقدر ترجّعه بـ <b>تراجع</b> أو /undo.")
    return {"ok": True, "title": title}


def _tool_undo_last(cid, args, me):
    last = get_last_action(cid)
    if not last:
        return {"error": "ما في إجراء سابق أقدر أرجّعه"}
    if not ANILIST_TOKEN:
        return {"error": "حساب AniList غير مربوط"}
    msg = ""
    if last.get("type") == "save":
        mid = last["media_id"]
        if not last.get("prev_existed"):
            ok = delete_entry(last.get("entry_id"), ANILIST_TOKEN)
            msg = "↩️ رجّعت الإضافة (حذفت الإدخال)." if ok else "❌ ما قدرت أرجّع."
        else:
            res = save_entry(
                mid,
                ANILIST_TOKEN,
                status=last.get("prev_status"),
                score=last.get("prev_score"),
                progress=last.get("prev_progress"),
            )
            msg = (
                f"↩️ رجّعت <b>{last.get('media_title')}</b> للتقدم السابق."
                if not (isinstance(res, dict) and res.get("error"))
                else f"❌ خطأ: {res.get('error', '')}"
            )
    elif last.get("type") == "delete":
        res = save_entry(
            last["media_id"],
            ANILIST_TOKEN,
            status=last.get("prev_status") or "CURRENT",
            score=last.get("prev_score"),
            progress=last.get("prev_progress"),
        )
        msg = (
            f"↩️ رجّعت <b>{last.get('media_title')}</b> لقائمتك."
            if not (isinstance(res, dict) and res.get("error"))
            else f"❌ خطأ: {res.get('error', '')}"
        )
    else:
        msg = "❌ ما أقدر أرجّع هذا الإجراء."
    store_del(f"last:{cid}")
    tg_send(cid, msg)
    return {"ok": True, "message": msg}


def _tool_toggle_favorite(cid, args, me):
    if not ANILIST_TOKEN:
        return {"error": "حساب AniList غير مربوط"}
    mid = args.get("media_id")
    mt = args.get("type") or "ANIME"
    if not mid:
        return {"error": "media_id مطلوب"}
    media = get_media_by_id(int(mid))
    if not media:
        return {"error": "ما لقيت هذا الأنمي"}
    try:
        toggle_fav(int(mid), ANILIST_TOKEN, mt)
    except Exception as e:
        return {"error": f"فشل: {e}"}
    tg_photo(cid, cover_of(media), f"❤️ بدّلت حالة المفضلة لـ <b>{title_of(media)}</b>")
    return {"ok": True, "title": title_of(media)}


# ---- browse / discovery tools (render albums) ----
def _album_from_list(cid, media_list, caption_fn, header=None):
    items = [
        {"url": cover_of(m), "caption": caption_fn(m)}
        for m in media_list
        if cover_of(m)
    ]
    if not items:
        return 0
    if header:
        tg_send(cid, header)
    tg_album(cid, items)
    return len(items)


def _tool_get_trending(cid, args, me):
    try:
        n = int(args.get("n") or 8)
    except Exception:
        n = 8
    r = get_trending(n)
    if not r:
        return {"error": "ما قدرت أجيب الترند (ممكن AniList متعطّلة، جرّب بعد شوي)"}
    c = _album_from_list(
        cid, r, lambda x: f"<b>{title_of(x)}</b>\n⭐ {x.get('averageScore', 0)}%"
    )
    return {"ok": True, "count": c}


def _tool_get_seasonal(cid, args, me):
    s, y = current_season()
    r = get_seasonal(s, y, 8)
    if not r:
        return {"error": "ما قدرت أجيب أنميات الموسم (ممكن AniList متعطّلة)"}
    c = _album_from_list(
        cid, r, lambda x: f"<b>{title_of(x)}</b>\n⭐ {x.get('averageScore', 0)}%"
    )
    return {"ok": True, "count": c, "season": s, "year": y}


def _tool_get_top_rated(cid, args, me):
    try:
        n = int(args.get("n") or 8)
    except Exception:
        n = 8
    res = _gql(
        """query($n:Int){Page(perPage:$n){media(type:ANIME,sort:SCORE_DESC){
        id title{romaji english} coverImage{large} averageScore siteUrl}}}""",
        {"n": n},
    )
    err = _gql_errors(res)
    if err:
        return {"error": ("AniList متعطّلة مؤقتاً" if _looks_disabled(err) else err)}
    media_list = sget(res, "data", "Page", "media", default=[]) or []
    if not media_list:
        return {"error": "ما قدرت أجلب الأعلى تقييماً"}
    c = _album_from_list(
        cid, media_list, lambda x: f"<b>{title_of(x)}</b>\n⭐ {x.get('averageScore', 0)}%"
    )
    return {"ok": True, "count": c}


def _tool_get_recommendations(cid, args, me):
    mid = args.get("media_id")
    if not mid:
        # fall back to a trending pick
        tr = get_trending(10)
        if not tr:
            return {"error": "ما قدرت أجيب توصيات الآن"}
        import random as _r

        mid = (_r.choice(tr)).get("id")
    recs = get_recommendations(int(mid), per_page=6)
    if not recs:
        return {"error": "ما في توصيات متاحة لهذا الأنمي"}
    c = _album_from_list(
        cid, recs, lambda x: f"<b>{title_of(x)}</b>\n⭐ {x.get('averageScore', 0)}%"
    )
    return {"ok": True, "count": c}


_REL_AR = {
    "SEQUEL": "تتمة",
    "PREQUEL": "جزء سابق",
    "SIDE_STORY": "قصة جانبية",
    "ADAPTATION": "اقتباس",
    "SPIN_OFF": "سبن أوف",
    "PARENT": "العمل الأصل",
    "CHARACTER": "شخصية مشتركة",
    "SUMMARY": "ملخص",
    "ALTERNATIVE": "بديل",
    "OTHER": "أخرى",
    "CONTAINS": "يحتوي",
    "COMPILATION": "تجميع",
}


def _tool_get_relations(cid, args, me):
    mid = args.get("media_id")
    if not mid:
        return {"error": "media_id مطلوب"}
    edges = get_relations(int(mid))
    if not edges:
        return {"error": "ما في علاقات مسجّلة لهذا الأنمي"}
    items = []
    for e in edges[:8]:
        node = e.get("node") or {}
        rel = _REL_AR.get(e.get("relationType", ""), e.get("relationType", ""))
        url = cover_of(node)
        if url:
            items.append({"url": url, "caption": f"<b>{title_of(node)}</b>\n🔗 {rel}"})
    if items:
        tg_album(cid, items)
    return {"ok": True, "count": len(items)}


def _tool_get_airing(cid, args, me):
    mid = args.get("media_id")
    if not mid:
        return {"error": "media_id مطلوب"}
    media = get_media_by_id(int(mid))
    if not media:
        return {"error": "ما لقيت هذا الأنمي"}
    schedule = get_airing(int(mid))
    if not schedule:
        return {"error": f"{title_of(media)} منتهي أو غير مجدول حالياً."}
    secs = schedule.get("timeUntilAiring", 0) or 0
    ep = schedule.get("episode", "?")
    days = secs // 86400
    hours = (secs % 86400) // 3600
    mins = (secs % 3600) // 60
    parts = []
    if days:
        parts.append(f"{days} يوم")
    if hours:
        parts.append(f"{hours} ساعة")
    if mins:
        parts.append(f"{mins} دقيقة")
    countdown = " و ".join(parts) or "أقل من دقيقة"
    cap = f"📺 <b>{title_of(media)}</b>\n\n⏱️ الحلقة {ep} بعد: <b>{countdown}</b>"
    tg_photo(cid, cover_of(media), cap)
    return {"ok": True, "episode": ep, "countdown": countdown}


def _tool_search_character(cid, args, me):
    name = args.get("name")
    if not name:
        return {"error": "اكتب اسم الشخصية"}
    ch = search_character_q(name)
    if not ch:
        return {"error": f"ما لقيت شخصية: {name}"}
    full = sget(ch, "name", "full") or name
    native = sget(ch, "name", "native") or ""
    nodes = sget(ch, "media", "nodes") or []
    anime = title_of(nodes[0]) if nodes else "غير محدد"
    cap = f"👤 <b>{full}</b>" + (f" ({native})" if native else "") + f"\n\n📺 من: <b>{anime}</b>"
    kb = kb_make([[{"text": "🔗 AniList", "url": ch.get("siteUrl") or "https://anilist.co"}]])
    tg_photo(cid, sget(ch, "image", "large"), cap, kb)
    return {"ok": True, "name": full}


def _tool_search_studio(cid, args, me):
    name = args.get("name")
    if not name:
        return {"error": "اكتب اسم الاستديو"}
    studio = search_studio_q(name)
    if not studio:
        return {"error": f"ما لقيت استديو: {name}"}
    works = sget(studio, "media", "nodes") or []
    if not works:
        return {"error": f"ما في أعمال مسجّلة لاستديو {studio.get('name')}"}
    c = _album_from_list(
        cid, works[:8], lambda w: f"<b>{title_of(w)}</b>\n⭐ {w.get('averageScore', 0)}%"
    )
    return {"ok": True, "studio": studio.get("name"), "count": c}


# ---- self / social tools ----
def _tool_get_my_stats(cid, args, me):
    username = args.get("username")
    if not username:
        if not me:
            return {"error": "ما قدرت أجيب حسابك"}
        username = me["name"]
    stats = get_stats(username)
    if not stats:
        return {"error": "ما لقيت إحصائيات (ممكن AniList متعطّلة أو اسم غلط)"}
    a = stats.get("anime") or {}
    m = stats.get("manga") or {}
    mins = a.get("minutesWatched", 0) or 0
    days = mins // 1440
    hours = (mins % 1440) // 60
    genres = ", ".join([g.get("genre", "") for g in (a.get("genres") or [])[:3]]) or "—"
    studios = ", ".join([sget(s, "studio", "name") for s in (a.get("studios") or [])[:3]]) or "—"
    cap = (
        f"📊 <b>إحصائيات {username}</b>\n\n"
        f"📺 <b>الأنمي:</b>\n"
        f"  • العدد: {a.get('count', 0)}\n"
        f"  • الحلقات: {a.get('episodesWatched', 0)}\n"
        f"  • وقت المشاهدة: {days} يوم و {hours} ساعة\n"
        f"  • متوسط التقييم: {a.get('meanScore', 0)}/100\n"
        f"  • الأنواع المفضلة: {genres}\n"
        f"  • الاستديوهات المفضلة: {studios}\n\n"
        f"📖 <b>المانجا:</b>\n"
        f"  • العدد: {m.get('count', 0)}\n"
        f"  • الفصول: {m.get('chaptersRead', 0)}\n"
        f"  • متوسط التقييم: {m.get('meanScore', 0)}/100"
    )
    tg_send(cid, cap)
    return {"ok": True}


def _tool_get_my_activity(cid, args, me):
    if not me:
        return {"error": "ما قدرت أجيب حسابك"}
    acts = get_activities(me["id"])
    if not acts:
        return {"error": "ما في نشاطات حديثة"}
    items = []
    for act in acts[:8]:
        media = act.get("media") or {}
        if not media:
            continue
        st = act.get("status", "") or ""
        prog = act.get("progress", "") or ""
        cap = f"<b>{title_of(media)}</b>\n{status_ar(st) if st in STATUS_AR else st}"
        if prog:
            cap += f" {prog}"
        url = cover_of(media)
        if url:
            items.append({"url": url, "caption": cap})
    if items:
        tg_album(cid, items)
        return {"ok": True, "count": len(items)}
    return {"error": "ما في نشاطات فيها صور"}


def _tool_get_my_list(cid, args, me):
    if not me:
        return {"error": "ما قدرت أجيب حسابك"}
    status = args.get("status")
    mt = args.get("type") or "ANIME"
    sort = ["SCORE_DESC", "UPDATED_TIME_DESC"] if status == "COMPLETED" else ["UPDATED_TIME_DESC"]
    entries = get_media_list(username=me["name"], media_type=mt, status=status, sort=sort, per_page=8)
    if not entries:
        return {"error": "قائمتك فاضية بهذا الفلتر"}
    items = []
    for e in entries[:8]:
        media = e.get("media") or {}
        cap = f"<b>{title_of(media)}</b>"
        if e.get("progress"):
            cap += f" — {e['progress']} حلقة"
        if e.get("score"):
            cap += f" | ⭐ {e['score']}"
        url = cover_of(media)
        if url:
            items.append({"url": url, "caption": cap})
    if items:
        tg_album(cid, items)
        return {"ok": True, "count": len(items)}
    return {"error": "ما في نتائج فيها صور"}


def _tool_get_my_following(cid, args, me):
    if not me:
        return {"error": "ما قدرت أجيب حسابك"}
    following = get_following(me["id"])
    if not following:
        return {"error": "ما تتابع أحد حالياً على AniList"}
    cap = f"👥 <b>قائمة متابعينك ({len(following)}):</b>\n\n"
    for u in following:
        a = (u.get("statistics") or {}).get("anime") or {}
        cap += f"• <b>{u.get('name','—')}</b> — {a.get('count', 0)} أنمي | ⭐ {a.get('meanScore', 0)}\n"
    tg_send(cid, cap)
    return {"ok": True, "count": len(following)}


def _tool_get_profile(cid, args, me):
    username = args.get("username")
    if not username:
        return {"error": "اكتب اسم المستخدم"}
    user = get_profile(username)
    if not user:
        return {"error": f"ما لقيت مستخدم: {username}"}
    a = sget(user, "statistics", "anime") or {}
    m = sget(user, "statistics", "manga") or {}
    cap = (
        f"👤 <b>{user.get('name')}</b>\n\n"
        f"📺 أنمي: {a.get('count', 0)} | حلقات: {a.get('episodesWatched', 0)}\n"
        f"📖 مانجا: {m.get('count', 0)} | فصول: {m.get('chaptersRead', 0)}\n"
        f"⭐ متوسط تقييم الأنمي: {a.get('meanScore', 0)}/100"
    )
    kb = kb_make([[{"text": "🔗 AniList", "url": user.get("siteUrl") or "https://anilist.co"}]])
    tg_photo(cid, sget(user, "avatar", "large"), cap, kb)
    return {"ok": True, "name": user.get("name")}


def _tool_get_friend_list(cid, args, me):
    username = args.get("username")
    if not username:
        return {"error": "اكتب اسم المستخدم"}
    entries = get_media_list(username=username, per_page=8)
    if not entries:
        return {"error": f"ما في نتائج لـ {username}"}
    items = []
    for e in entries[:8]:
        media = e.get("media") or {}
        cap = f"<b>{title_of(media)}</b>" + (f"\n⭐ {e['score']}" if e.get("score") else "")
        url = cover_of(media)
        if url:
            items.append({"url": url, "caption": cap})
    if items:
        tg_album(cid, items)
        return {"ok": True, "count": len(items)}
    return {"error": "ما في نتائج"}


def _tool_compare_with(cid, args, me):
    username = args.get("username")
    if not username:
        return {"error": "اكتب اسم الصديق للمقارنة"}
    if not me:
        return {"error": "ما قدرت أجيب حسابك"}
    my_list = get_media_list(username=me["name"], status="COMPLETED", per_page=50)
    fr_list = get_media_list(username=username, status="COMPLETED", per_page=50)
    my_ids = {e["media"]["id"]: e for e in my_list if sget(e, "media")}
    fr_ids = {e["media"]["id"]: e for e in fr_list if sget(e, "media")}
    shared = set(my_ids) & set(fr_ids)
    only_me = set(my_ids) - set(fr_ids)
    only_fr = set(fr_ids) - set(my_ids)
    cap = (
        f"🆚 <b>مقارنة: {me['name']} vs {username}</b>\n\n"
        f"🤝 مشترك: {len(shared)} أنمي\n"
        f"👤 عندك فقط: {len(only_me)} أنمي\n"
        f"👥 عند {username} فقط: {len(only_fr)} أنمي\n"
    )
    if shared:
        cap += "\n<b>تقييمات مشتركة:</b>\n"
        for sid in list(shared)[:6]:
            e = my_ids.get(sid) or {}
            name = title_of(e.get("media") or {})
            ms = e.get("score", 0)
            fs = (fr_ids.get(sid) or {}).get("score", 0)
            cap += f"• {name}: أنت {ms} | {username} {fs}\n"
    tg_send(cid, cap)
    return {"ok": True, "shared": len(shared), "only_me": len(only_me), "only_fr": len(only_fr)}


def _tool_surprise_me(cid, args, me):
    mode = random.choice(["rec", "flashback", "stat", "random"])
    if mode == "flashback" and me:
        entries = get_media_list(username=me["name"], status="COMPLETED", per_page=50)
        if entries:
            e = random.choice(entries)
            media = e.get("media") or {}
            cap = f"💭 <b>فلاش باك!</b>\n\n<b>{title_of(media)}</b>\nقيّمته: ⭐ {e.get('score', 0)}/10\n\nتتذكره؟ 🤔"
            tg_photo(cid, cover_of(media), cap)
            return {"ok": True, "mode": "flashback"}
    if mode == "stat" and me:
        stats = get_stats(me["name"])
        if stats:
            a = stats.get("anime") or {}
            mins = a.get("minutesWatched", 0) or 0
            eps = a.get("episodesWatched", 0) or 0
            days = mins // 1440
            tg_send(cid, f"🤯 <b>هل تعلم؟</b>\n\nشفت <b>{eps}</b> حلقة أنمي!\nيعادل <b>{days} يوم</b> مشاهدة متواصلة! 📺")
            return {"ok": True, "mode": "stat"}
    if mode == "rec":
        trending = get_trending(10)
        if trending:
            pick = random.choice(trending)
            cap = f"🔥 <b>جرّب هذا!</b>\n\n<b>{title_of(pick)}</b>\n⭐ {pick.get('averageScore', 0)}%\n\nأنمي ترند الحين!"
            kb = kb_make([[{"text": "🔗 AniList", "url": pick.get("siteUrl") or "https://anilist.co"}]])
            tg_photo(cid, cover_of(pick), cap, kb)
            return {"ok": True, "mode": "rec"}
    # random anime
    genre = random.choice(["Action", "Adventure", "Comedy", "Drama", "Fantasy", "Mystery", "Romance", "Sci-Fi"])
    page = random.randint(1, 10)
    res = _gql(
        """query($g:String,$p:Int){Page(page:$p,perPage:1){media(type:ANIME,genre:$g,sort:POPULARITY_DESC){
        id title{romaji english} coverImage{large} averageScore siteUrl episodes}}}""",
        {"g": genre, "p": page},
    )
    media_list = sget(res, "data", "Page", "media", default=[]) or []
    if not media_list:
        return {"error": "ما لقيت أنمي عشوائي، جرّب مرة ثانية"}
    m = media_list[0]
    cap = f"🎲 <b>أنمي عشوائي ({genre}):</b>\n\n<b>{title_of(m)}</b>\n⭐ {m.get('averageScore', 0)}%\n📺 {m.get('episodes', '?')} حلقة"
    tg_photo(cid, cover_of(m), cap)
    return {"ok": True, "mode": "random"}


def _tool_get_news(cid, args, me):
    news_items = []
    if me:
        current_list = get_media_list(username=me["name"], status="CURRENT", per_page=5)
        for e in current_list[:3]:
            media = e.get("media") or {}
            schedule = get_airing(media.get("id"))
            if schedule:
                ep = schedule.get("episode", "?")
                secs = schedule.get("timeUntilAiring", 0) or 0
                d = secs // 86400
                news_items.append(f"📺 <b>{title_of(media)}</b> — الحلقة {ep} بعد {d} يوم")
    trending = get_trending(5)
    for t in trending[:2]:
        news_items.append(f"🔥 ترند: <b>{title_of(t)}</b> — ⭐ {t.get('averageScore', 0)}%")
    if news_items:
        tg_send(cid, "📰 <b>أخبار أنمياتك:</b>\n\n" + "\n\n".join(news_items))
        return {"ok": True, "count": len(news_items)}
    return {"error": "ما في أخبار جديدة حالياً"}


# ============================================================
# TOOL DISPATCH TABLE + Gemini function declarations
# ============================================================
TOOL_DISPATCH = {
    "search_my_list": _tool_search_my_list,
    "search_anime": _tool_search_anime,
    "get_media_info": _tool_get_media_info,
    "track_media": _tool_track_media,
    "delete_entry": _tool_delete_entry,
    "undo_last": _tool_undo_last,
    "toggle_favorite": _tool_toggle_favorite,
    "get_trending": _tool_get_trending,
    "get_seasonal": _tool_get_seasonal,
    "get_top_rated": _tool_get_top_rated,
    "get_recommendations": _tool_get_recommendations,
    "get_relations": _tool_get_relations,
    "get_airing": _tool_get_airing,
    "search_character": _tool_search_character,
    "search_studio": _tool_search_studio,
    "get_my_stats": _tool_get_my_stats,
    "get_my_activity": _tool_get_my_activity,
    "get_my_list": _tool_get_my_list,
    "get_my_following": _tool_get_my_following,
    "get_profile": _tool_get_profile,
    "get_friend_list": _tool_get_friend_list,
    "compare_with": _tool_compare_with,
    "surprise_me": _tool_surprise_me,
    "get_news": _tool_get_news,
}


def _decl(name, description, properties, required=None):
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required or [],
        },
    }


GEMINI_TOOLS = [
    _decl(
        "search_my_list",
        "يبحث في قائمة المستخدم نفسه على AniList (وليس بحثاً عاماً). استخدمه دائماً قبل الحذف أو التعديل. يرجع entry_id و media_id و title و status و progress.",
        {
            "query": {"type": "string", "description": "نص البحث (جزء من العنوان)"},
            "type": {"type": "string", "enum": ["ANIME", "MANGA"], "description": "افتراضياً ANIME"},
        },
        ["query"],
    ),
    _decl(
        "search_anime",
        "بحث عام في قاعدة AniList لجلب media_id وعنوان وبيانات الأنمي/المانجا. استخدمه قبل track_media أو get_relations/get_airing.",
        {
            "query": {"type": "string"},
            "type": {"type": "string", "enum": ["ANIME", "MANGA"]},
            "per_page": {"type": "integer", "description": "عدد النتائج (افتراضي 5)"},
        },
        ["query"],
    ),
    _decl(
        "get_media_info",
        "يعرض بطاقة معلومات لأنمي معيّن (media_id) + حالته في قائمتك.",
        {"media_id": {"type": "integer"}},
        ["media_id"],
    ),
    _decl(
        "track_media",
        "يسجّل/يحدّث تتبّع أنمي في قائمتك (status, progress, score, مفضلة). pass media_id. للبداية الجديدة اجعل fresh_start=true. للجزء/الموسم مرّر part='latest' أو رقم.",
        {
            "media_id": {"type": "integer"},
            "status": {"type": "string", "enum": ["CURRENT", "COMPLETED", "PLANNING", "PAUSED", "DROPPED", "REPEATING"]},
            "progress": {"type": "integer", "description": "رقم الحلقة/الفصل المطلق النهائي"},
            "progress_delta": {"type": "integer", "description": "عدد حلقات للإضافة على التقدم الحالي"},
            "fresh_start": {"type": "boolean"},
            "score": {"type": "number", "description": "تقييم 0-10"},
            "favorite": {"type": "boolean"},
            "type": {"type": "string", "enum": ["ANIME", "MANGA"]},
            "part": {"type": "string", "description": "'latest' للأحدث أو رقم الجزء"},
        },
        ["media_id"],
    ),
    _decl(
        "delete_entry",
        "يحذف إدخالاً من قائمتك. entry_id و media_id و title تأتي من search_my_list. لا تستخدمه إلا بعد تأكيد المستخدم إذا كان هناك لبس.",
        {
            "entry_id": {"type": "integer"},
            "media_id": {"type": "integer"},
            "title": {"type": "string"},
        },
        ["entry_id"],
    ),
    _decl("undo_last", "يرجّع آخر إجراء (تتبّع أو حذف).", {}),
    _decl(
        "toggle_favorite",
        "يبدّل حالة المفضلة لأنمي/مانجا.",
        {"media_id": {"type": "integer"}, "type": {"type": "string", "enum": ["ANIME", "MANGA"]}},
        ["media_id"],
    ),
    _decl("get_trending", "يعرض الأكثر ترنداً الآن.", {"n": {"type": "integer"}}),
    _decl("get_seasonal", "يعرض أنميات الموسم الحالي.", {}),
    _decl("get_top_rated", "يعرض الأعلى تقييماً.", {"n": {"type": "integer"}}),
    _decl("get_recommendations", "توصيات مشابهة لأنمي (media_id).", {"media_id": {"type": "integer"}}),
    _decl("get_relations", "العلاقات/الأجزاء لأنمي (media_id).", {"media_id": {"type": "integer"}}, ["media_id"]),
    _decl("get_airing", "متى الحلقة القادمة لأنمي (media_id).", {"media_id": {"type": "integer"}}, ["media_id"]),
    _decl("search_character", "يبحث عن شخصية ويعرض بطاقتها.", {"name": {"type": "string"}}, ["name"]),
    _decl("search_studio", "يبحث عن استديو ويعرض أعماله.", {"name": {"type": "string"}}, ["name"]),
    _decl("get_my_stats", "إحصائيات المستخدم (أو صديق عبر username).", {"username": {"type": "string"}}),
    _decl("get_my_activity", "آخر نشاطاتك على AniList.", {}),
    _decl("get_my_list", "قائمتك حسب الحالة.", {"status": {"type": "string", "enum": ["CURRENT", "COMPLETED", "PLANNING", "PAUSED", "DROPPED", "REPEATING"]}, "type": {"type": "string", "enum": ["ANIME", "MANGA"]}}),
    _decl("get_my_following", "قائمة من تتابعهم على AniList.", {}),
    _decl("get_profile", "بروفايل مستخدم.", {"username": {"type": "string"}}, ["username"]),
    _decl("get_friend_list", "قائمة مستخدم على AniList.", {"username": {"type": "string"}}, ["username"]),
    _decl("compare_with", "يقارن قائمتك المكتملة مع صديق.", {"username": {"type": "string"}}, ["username"]),
    _decl("surprise_me", "يفاجئ المستخدم باقتراح/إحصائية/فلاش باك.", {}),
    _decl("get_news", "آخر أخبار أنمياتك الحالية + الترند.", {}),
]


def dispatch_tool(cid, name, args, me):
    fn = TOOL_DISPATCH.get(name)
    if not fn:
        return {"error": f"أداة غير معروفة: {name}"}
    try:
        return fn(cid, args or {}, me)
    except Exception as e:
        import traceback

        traceback.print_exc()
        return {"error": f"{type(e).__name__}: {e}"}


# ============================================================
# GEMINI GENERATE + AGENT LOOP
# ============================================================
def _gemini_generate(contents):
    """Call Gemini generateContent with tools. Returns parsed JSON or None on total failure."""
    models = []
    for m in ["gemini-3.5-flash", "gemini-3.6-flash", GEMINI_MODEL, "gemini-2.0-flash"]:
        if m and m not in models:
            models.append(m)
    payload = {
        "contents": contents,
        "tools": [{"functionDeclarations": GEMINI_TOOLS}],
        "systemInstruction": {"parts": [{"text": AGENT_SYSTEM_PROMPT}]},
        "generationConfig": {"temperature": 0.2},
    }
    data = json.dumps(payload).encode("utf-8")
    last_err = None
    for model in models:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={GEMINI_KEY}"
        )
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT) as resp:
                _RUNTIME["last_model"] = model
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                err = e.read().decode("utf-8")[:200]
            except Exception:
                err = ""
            print(f"[Gemini] {model} HTTP {e.code}: {err}")
            last_err = f"HTTP {e.code}"
        except Exception as e:
            print(f"[Gemini] {model} FAILED: {type(e).__name__}: {e}")
            last_err = f"{type(e).__name__}: {e}"
    print(f"[Gemini] all models failed: {last_err}")
    return None


def _build_contents(cid):
    """Build Gemini contents[] from stored conversation context (user/model turns)."""
    contents = []
    for m in get_context(cid):
        role = "user" if m.get("role") == "user" else "model"
        t = (m.get("text") or "").strip()
        if not t:
            continue
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"][0]["text"] += "\n" + t
        else:
            contents.append({"role": role, "parts": [{"text": t}]})
    # Gemini requires the first turn to be 'user'
    if contents and contents[0]["role"] != "user":
        contents.insert(0, {"role": "user", "parts": [{"text": "مرحبا"}]})
    return contents


def run_agent(cid, text):
    """Run the multi-step Gemini agent. Returns True if a reply was sent, False on failure (caller falls back)."""
    if not GEMINI_KEY:
        return False
    me = _viewer_me()
    contents = _build_contents(cid)
    if not contents:
        contents = [{"role": "user", "parts": [{"text": text}]}]
    seen_calls = set()

    for _step in range(MAX_STEPS):
        resp = _gemini_generate(contents)
        if resp is None:
            return False
        cands = resp.get("candidates") or []
        if not cands:
            fb = resp.get("promptFeedback") or {}
            if fb.get("blockReason"):
                print(f"[Gemini] blocked: {fb.get('blockReason')}")
            return False
        cand = cands[0]
        cparts = sget(cand, "content", "parts", default=[]) or []
        if not cparts:
            return False

        tool_calls = [p for p in cparts if "functionCall" in p]
        if tool_calls:
            for p in tool_calls:
                fc = p.get("functionCall") or {}
                name = fc.get("name")
                fargs = fc.get("args") or {}
                key = (name, json.dumps(fargs, sort_keys=True, ensure_ascii=False))
                if key in seen_calls:
                    # refuse to repeat the exact same call — force the agent to use the prior result
                    contents.append({"role": "user", "parts": [{"functionResponse": {"name": name, "response": {"error": "نُفّذت هذه الأداة بنفس المعطيات مسبقاً — استخدم النتيجة السابقة وردّ الآن، لا تكرّر."}}}]})
                    continue
                seen_calls.add(key)
                print(f"[Agent] tool: {name} args={fargs}")
                result = dispatch_tool(cid, name, fargs, me)
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": name,
                                    "response": result if isinstance(result, dict) else {"result": result},
                                }
                            }
                        ],
                    }
                )
            continue

        # final reply: concatenate text parts
        final = "".join(p.get("text", "") for p in cparts if "text" in p).strip()
        if not final:
            return False
        tg_send(cid, final)
        save_context(cid, "bot", final)
        return True

    # exhausted steps
    tg_send(cid, "⏳ تعذّر إكمال الطلب الآن، جرّب بصيغة أبسط أو /help.")
    return True

def _sender_allowed(update):
    """Private bot: only the configured owner (@KorosSama) may use it."""
    frm = update.get("from") or {}
    uname = (frm.get("username") or "").lower().lstrip("@")
    uid = str(frm.get("id") or "")
    if uname and uname == ALLOWED_TG_USERNAME:
        return True
    if uid and uid == ALLOWED_TG_ID:
        return True
    return False


# ============================================================
# [9] HANDLER CLASS (v4.0 — agent-driven)
# ============================================================
class handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # silence default stderr logging

    # ---------- HTTP entry ----------
    def do_GET(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if qs.get("diag"):
            self._json(200, self._diag())
            return
        if qs.get("models"):
            self._json(200, self._list_models())
            return
        if qs.get("modeltest"):
            self._json(200, self._model_test())
            return
        if qs.get("agenttest"):
            self._json(200, self._agent_test())
            return
        if qs.get("test"):
            self._json(200, self._test_agent())
            return
        self._json(
            200,
            {
                "status": "running",
                "bot": "المخلافي v4.0",
                "mode": "gemini-function-calling-agent",
                "tools": len(GEMINI_TOOLS),
                "telegram": "SET" if TELEGRAM_TOKEN else "MISSING",
                "anilist": "SET" if ANILIST_TOKEN else "MISSING",
                "gemini": "SET" if GEMINI_KEY else "MISSING",
                "gemini_model": GEMINI_MODEL,
                "storage": "redis" if KV_ENABLED else "memory",
                "test_url": "GET /api/webhook?test=1",
            },
        )

    def _json(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _agent_test(self):
        """Test the agent's tool-calling decisions on tricky sample messages (no Telegram side-effects)."""
        samples = [
            "هذا صديق متابعني ومتابعه مالك مدوخ",
            "اصدقائي",
            "وش الترند؟",
            "ابحث عن مستخدم laseel",
        ]
        out = {}
        for msg in samples:
            contents = [{"role": "user", "parts": [{"text": msg}]}]
            resp = _gemini_generate(contents)
            if resp is None:
                out[msg] = "GEMINI_FAILED"
                continue
            parts = sget(resp, "candidates", 0, "content", "parts", default=[]) or []
            calls = [p["functionCall"]["name"] for p in parts if "functionCall" in p]
            text = "".join(p.get("text", "") for p in parts if "text" in p).strip()[:100]
            out[msg] = ("TOOLS: " + ", ".join(calls)) if calls else ("TEXT: " + text)
        out["_model_used"] = _RUNTIME.get("last_model")
        return out

    def _model_test(self):
        """Try generateContent WITH the real tools against candidate models; report which work."""
        candidates = ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-3.5-flash", GEMINI_MODEL, "gemini-2.0-flash"]
        payload = {
            "contents": [{"role": "user", "parts": [{"text": "ردّ بكلمة واحدة: جاهز"}]}],
            "tools": [{"functionDeclarations": GEMINI_TOOLS}],
            "systemInstruction": {"parts": [{"text": "رد مختصر"}]},
            "generationConfig": {"temperature": 0.2},
        }
        data = json.dumps(payload).encode("utf-8")
        out = {}
        for model in dict.fromkeys([c for c in candidates if c]):
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}"
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
                out[model] = "ok" if res.get("candidates") else "no-candidates"
            except urllib.error.HTTPError as e:
                try:
                    msg = e.read().decode("utf-8")[:140]
                except Exception:
                    msg = str(e.code)
                out[model] = f"HTTP {e.code}: {msg}"
            except Exception as e:
                out[model] = f"{type(e).__name__}: {e}"
        return out

    def _list_models(self):
        """List Gemini models available to this key (to pick the best for function-calling)."""
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_KEY}&pageSize=200"
            req = urllib.request.Request(url, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            supported = [m for m in (data.get("models") or []) if "generateContent" in (m.get("supportedGenerationMethods") or [])]
            return {"count": len(supported), "models": [m.get("name", "").replace("models/", "") for m in supported]}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    def _diag(self):
        """Probe AniList from the bot's own server (Vercel) to see what it actually gets."""
        t0 = time.time()
        out = {
            "anilist_token": "SET" if ANILIST_TOKEN else "MISSING",
            "note": "probes AniList from the bot's server (Vercel), not your device",
        }
        try:
            res = _gql("query{Viewer{id name}}", token=ANILIST_TOKEN)
            out["probe_errors"] = res.get("errors")
            out["probe_viewer"] = sget(res, "data", "Viewer")
            out["looks_disabled_to_bot"] = bool(_looks_disabled(_gql_errors(res)))
        except Exception as e:
            out["probe_exception"] = f"{type(e).__name__}: {e}"
        try:
            _me, astate = _resolve_me_and_state()
            out["resolve_state"] = astate
        except Exception as e:
            out["resolve_exception"] = f"{type(e).__name__}: {e}"
        # stats via get_stats (now authenticated by default through _gql -> ANILIST_TOKEN)
        try:
            out["stats_ok"] = bool(get_stats("Koros1Sama"))
        except Exception as e:
            out["stats_exception"] = f"{type(e).__name__}: {e}"
        # following (auth) — verifies the 'friends' query returns the user's follows
        try:
            out["following_count"] = len(get_following(748233, token=ANILIST_TOKEN)) if ANILIST_TOKEN else -1
        except Exception as e:
            out["following_exception"] = f"{type(e).__name__}: {e}"
        out["elapsed_seconds"] = round(time.time() - t0, 2)
        return out

    def _test_agent(self):
        if not GEMINI_KEY:
            return {"error": "GEMINI_API_KEY not set"}
        t0 = time.time()
        try:
            resp = _gemini_generate(
                [{"role": "user", "parts": [{"text": "ردّ بكلمة واحدة: جاهز"}]}]
            )
            ok = bool(resp and resp.get("candidates"))
            txt = ""
            if ok:
                txt = "".join(
                    p.get("text", "")
                    for p in sget(resp, "candidates", 0, "content", "parts", default=[])
                    if "text" in p
                )[:60]
            return {
                "agent_works": ok,
                "model_configured": GEMINI_MODEL,
                "model_used": _RUNTIME.get("last_model"),
                "tools": len(GEMINI_TOOLS),
                "sample_reply": txt,
                "elapsed_seconds": round(time.time() - t0, 2),
            }
        except Exception as e:
            return {"error": str(e), "type": type(e).__name__}

    def do_POST(self):
        # Acknowledge Telegram immediately so it doesn't retry on slow processing.
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            update = json.loads(body.decode("utf-8"))
        except Exception:
            self.send_response(200)
            self.end_headers()
            return

        try:
            if "callback_query" in update:
                self._on_callback(update["callback_query"])
            elif "message" in update:
                self._on_message(update["message"])
        except Exception as e:
            import traceback

            print("[POST Error]", e)
            traceback.print_exc()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    # ---------- Message dispatch ----------
    def _on_message(self, msg):
        if not _sender_allowed(msg):
            return  # private bot — owner only
        chat_id = sget(msg, "chat", "id")
        text = (msg.get("text") or "").strip()
        if not text or not chat_id:
            return
        cid = str(chat_id)
        low = text.lower()

        # Commands handled before the agent (as designed)
        if text.startswith("/start"):
            self._welcome(cid)
            return
        if text.startswith("/help"):
            self._help(cid)
            return
        if text.startswith("/undo"):
            save_context(cid, "user", text)
            _tool_undo_last(cid, {}, _viewer_me())
            return
        if text.startswith("/reset"):
            clear_context(cid)
            clear_pending(cid)
            tg_send(cid, "🧹 تم مسح السياق.")
            return

        save_context(cid, "user", text)

        # Explicit undo words → undo tool directly (fast path)
        if UNDO_RE.search(low):
            _tool_undo_last(cid, {}, _viewer_me())
            return

        # Primary path: Gemini multi-step agent
        try:
            if run_agent(cid, text):
                return
        except Exception as e:
            import traceback

            print("[Agent Error]", e)
            traceback.print_exc()

        # Fallback: regex parser + minimal tool dispatcher (last resort)
        self._fallback(cid, text)

    # ---------- Fallback dispatcher (regex → tools) ----------
    def _fallback(self, cid, text):
        parsed = regex_parse(text)
        parsed["_parser"] = "regex_fallback"
        action = parsed.get("action", "CHAT")
        me = _viewer_me()

        if action == "UNDO":
            _tool_undo_last(cid, {}, me)
            return
        if action == "TRENDING":
            r = _tool_get_trending(cid, {}, me)
            if r.get("error"):
                tg_send(cid, "❌ " + r["error"])
            return
        if action == "SEASONAL":
            r = _tool_get_seasonal(cid, {}, me)
            if r.get("error"):
                tg_send(cid, "❌ " + r["error"])
            return
        if action == "TOP_RATED":
            r = _tool_get_top_rated(cid, {}, me)
            if r.get("error"):
                tg_send(cid, "❌ " + r["error"])
            return
        if action == "STATS":
            r = _tool_get_my_stats(cid, {"username": parsed.get("friend_username")}, me)
            if r.get("error"):
                tg_send(cid, "❌ " + r["error"])
            return
        if action == "DELETE":
            title = parsed.get("title") or _extract_title(text.lower())
            if title:
                res = _tool_search_my_list(cid, {"query": title, "type": parsed.get("media_type", "ANIME")}, me)
                hits = res.get("results") or []
                if hits:
                    h = hits[0]
                    _tool_delete_entry(cid, {"entry_id": h["entry_id"], "media_id": h.get("media_id"), "title": h.get("title")}, me)
                else:
                    tg_send(cid, "❌ " + res.get("error", "ما لقيته في قائمتك."))
            else:
                tg_send(cid, "❌ حدد الأنمي للحذف. مثال: <i>احذف ناروتو</i>")
            return
        if action == "TRACK":
            title = parsed.get("title") or _extract_title(text.lower())
            if not title:
                self._suggest(cid, text)
                return
            res = _tool_search_anime(cid, {"query": title, "type": parsed.get("media_type", "ANIME")}, me)
            hits = res.get("results") or []
            if not hits:
                tg_send(cid, "❌ " + res.get("error", "ما لقيت الأنمي."))
                return
            base = hits[0]
            _tool_track_media(
                cid,
                {
                    "media_id": base["id"],
                    "status": parsed.get("status") or "CURRENT",
                    "progress": parsed.get("absolute_progress"),
                    "progress_delta": parsed.get("progress_delta"),
                    "fresh_start": parsed.get("fresh_start", False),
                    "score": parsed.get("score"),
                    "favorite": parsed.get("is_favorite", False),
                    "type": parsed.get("media_type", "ANIME"),
                    "part": parsed.get("part_hint"),
                },
                me,
            )
            return

        # CHAT / unknown
        tg_send(
            cid,
            (
                "ما فهمت قصدك زين 🤔\nجرّب صيغة مثل:\n"
                "• <i>شفت 3 حلقات من ون بيس</i>\n"
                "• <i>كملت انمي فريرين واقيمه 9</i>\n"
                "• <i>احذف ناروتو من قائمتي</i>\n"
                "• <i>وش الترند؟</i>\n"
                "أو أرسل /help للدليل الكامل."
            ),
        )

    def _suggest(self, cid, raw):
        tg_send(
            cid,
            (
                "🤔 ما عرفت اسم الأنمي من رسالتك.\n"
                "جرّب تذكر الاسم بالإنجليزي أو الياباني.\n"
                "مثال: <i>شفت حلقتين من Lord of Mysteries</i>\n"
                "أو أرسل /help."
            ),
        )

    # ---------- Callbacks (v4 mostly uses URL links; legacy-safe stub) ----------
    def _on_callback(self, cb):
        if not _sender_allowed(cb):
            return  # private bot — owner only
        cbid = cb.get("id")
        cid = str(sget(cb, "message", "chat", "id") or "")
        mid = sget(cb, "message", "message_id")
        try:
            if cbid:
                tg_answer_cb(cbid, "تمام...")
        except Exception:
            pass
        if not cid:
            return
        data = cb.get("data", "") or ""
        if data.startswith("cancel"):
            try:
                tg_edit(cid, mid, "↩️ تم الإلغاء.", kb=None)
            except Exception:
                pass
        # v4.0 confirmations are textual; legacy del/pend/pick buttons are ignored gracefully.

    # ---------- Welcome & Help ----------
    def _welcome(self, cid):
        clear_context(cid)
        tg_send(
            cid,
            """🎬 <b>أهلاً بك في المخلافي — بوت AniList v4.0</b> 🤖

الحين صرت <b>agent ذكي متعدد الخطوات</b> — أفهم سياق محادثتك، وأبحث وأتنفّذ وأرجع لك بقرار.

<b>📌 أمثلة سريعة:</b>
• <i>بدأت أشوف لورد الغوامض، شفت حلقتين</i>
• <i>شفت 3 حلقات من ون بيس</i>
• <i>كملت انمي فريرين وأقيّمه 9</i>
• <i>شفت الجزء الجديد من ري زيرو</i>
• <i>احذف الكابتن تسوباسا من قائمتي</i> (يدوّر في قائمتك أنت!)
• <i>وش الترند؟</i> • <i>إحصائياتي</i> • <i>فاجئني!</i>

💡 غلطت في رقم؟ قول <i>صحّح التقدم إلى حلقة 2</i> أو <i>تراجع</i>.

أرسل /help للدليل الكامل 📖""",
        )

    def _help(self, cid):
        tg_send(
            cid,
            """📖 <b>دليل المخلافي v4.0</b>

<b>📺 تتبع الأنمي/المانجا:</b>
• <i>بدأت أشوف هجوم العمالقة شفت حلقتين</i> (بداية جديدة = الحلقة 2)
• <i>شفت 3 حلقات من بليتش</i> (يضيف 3 للتقدم الحالي)
• <i>كملت راجنا وأقيّمه 9 واعمله مفضلة</i>
• <i>شفت الجزء الجديد من ري زيرو</i> (يجيب آخر جزء تلقائياً)
• <i>قريت 10 فصول من سولو ليفلينق</i>

<b>♻️ التصحيح والتراجع:</b>
• <i>لا، كنت اقصد حلقة 2</i> / <i>كنت اقصد الجزء الرابع</i>
• <i>تراجع</i> أو <i>/undo</i> — يرجّع آخر تعديل/حذف

<b>🗑️ الحذف (يدوّر في قائمتك أنت):</b> <i>احذف ناروتو من قائمتي</i>

<b>📊 الإحصائيات:</b> <i>إحصائياتي</i> • <i>إحصائيات Ahmed</i>

<b>📋 القوائم والنشاط:</b>
• <i>آخر نشاطاتي</i> • <i>انمياتي المكتملة</i> • <i>قائمة أصدقائي</i>

<b>👥 الأصدقاء:</b> <i>بروفايل Ahmed</i> • <i>قارن قائمتي مع Ahmed</i>

<b>🧠 التوصيات:</b> <i>أنمي مشابه لهجوم العمالقة</i> • <i>فاجئني!</i>

<b>🔥 الترند والمواسم:</b>
• <i>وش الترند؟</i> • <i>أنميات هذا الموسم</i> • <i>أفضل 10 أنميات</i>

<b>🔍 البحث:</b>
• <i>مين شخصية لوفي؟</i> • <i>أنميات استديو MAPPA</i>
• <i>سيكويل هجوم العمالقة</i> • <i>متى الحلقة الجاية من ون بيس</i>

💡 تقدر تكلمني كلام عادي وبفهمك! 😊""",
        )
