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
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(ANILIST_URL, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout or HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"errors": [{"message": f"HTTP {e.code}"}]}
    except Exception as e:
        return {"errors": [{"message": str(e)}]}


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


def get_activities(user_id, page=1, per_page=8):
    q = """query($u:Int,$p:Int,$n:Int){Page(page:$p,perPage:$n){
    activities(userId:$u,type:MEDIA_LIST,sort:ID_DESC){...on ListActivity{
    status progress createdAt media{id title{romaji english} coverImage{large} siteUrl}}}}}"""
    res = _gql(q, {"u": user_id, "p": page, "n": per_page})
    return sget(res, "data", "Page", "activities", default=[]) or []


def get_stats(username):
    q = """query($n:String){User(name:$n){statistics{anime{count meanScore minutesWatched
    episodesWatched chaptersRead genres(limit:5,sort:COUNT_DESC){genre count meanScore}
    studios(limit:3,sort:COUNT_DESC){studio{name}count}} manga{count meanScore chaptersRead}}}}"""
    res = _gql(q, {"n": username})
    return sget(res, "data", "User", "statistics")


def get_profile(username):
    q = """query($n:String){User(name:$n){id name about avatar{large} bannerImage siteUrl
    statistics{anime{count meanScore episodesWatched} manga{count meanScore chaptersRead}}}}"""
    res = _gql(q, {"n": username})
    return sget(res, "data", "User")


def get_media_list(
    username=None,
    user_id=None,
    media_type="ANIME",
    status=None,
    sort=None,
    page=1,
    per_page=10,
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
    res = _gql(q, v)
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


def get_favorites(username):
    q = """query($n:String){User(name:$n){favourites{anime{nodes{id title{romaji english}
    coverImage{large} siteUrl}} manga{nodes{id title{romaji english} coverImage{large}}}}}}"""
    res = _gql(q, {"n": username})
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


def get_following(user_id, page=1, per_page=10):
    q = """query($u:Int,$p:Int,$n:Int){Page(page:$p,perPage:$n){
    following(userId:$u){id name avatar{large} siteUrl
    statistics{anime{count episodesWatched meanScore} manga{count chaptersRead}}}}}"""
    res = _gql(q, {"u": user_id, "p": page, "n": per_page})
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
# [8] GEMINI AI PARSER
# ============================================================
GEMINI_SYSTEM_PROMPT = """أنت مساعد خبير لأنمي/مانجا AniList اسمه «المخلافي». تفهم العربية العامية والفصحى والإنجليزية والأرابيزي والأخطاء الإملائية وأوصاف الحبكة والشخصيات.

قواعد الحالة (STATUS):
- 'كملت'/'أكملت'/'خلصت'/'شفت كامل'/'شفت كل حلقاته'/'انهيت'/'وخلصته' = COMPLETED (وليس CURRENT)
- 'شفت الأنمي' بدون ذكر عدد حلقات = COMPLETED
- 'شفت حلقتين من X' أو 'بدأت اشوف X شفت حلقتين' = CURRENT مع absolute_progress=2 (وليس progress_delta!)
- 'شفت 5 حلقات من X' (بدون كلمة بدأت) = CURRENT مع progress_delta=5
- 'بدأت/بديت/بدات اشوف X شفت N حلقات' أو 'رجعت اتابع X' أو 'من جديد/من البداية' = CURRENT مع absolute_progress=N (بداية/إعادة، absolute وليس delta). واجعل fresh_start=true.
- عند COMPLETED ولا يوجد عدد حلقات: اجعل progress_delta و absolute_progress بقيمة null (النظام يملأه تلقائياً حسب إجمالي الحلقات).
- لا تحدد تواريخ أبداً إلا إذا ذكرها المستخدم صراحة.
- إذا قال المستخدم 'ه'/'ها'/'هذا'/'ذاك'/'نفسه' دون عنوان، اترك title=null (النظام يحلّه من السياق).

الأرقام العربية (انتبه جداً للصيغة المثنى):
- حلقتين = 2 (اثنان بالضبط، وليس ثلاثة عشر!)
- ثلاث حلقات = 3، أربع = 4، خمس = 5، ست = 6، سبع = 7، ثمان = 8، تسع = 9، عشر = 10.

الأجزاء والمواسم:
- 'الجزء الجديد/الأخير/الأخيرة/الي نزل قريب/الأحدث/الموسم الجديد' → اجعل part_hint = "latest" (النظام يبحث في AniList عن أحدث جزء فعلياً).
- 'الجزء الرابع'/'الموسم 4' → part_hint = رقم (مثال 4).
- لا تعتمد على معرفتك بمواسم الأنمي؛ دائماً اتركها كـ part_hint ودع النظام يحلّها من AniList.

التصحيحات (مهم جداً):
- إذا بدأت رسالة المستخدم بـ 'لا'/'لأ' أو احتوت 'كنت اقصد'/'مش'/'مو'/'غلط'/'صحح'/'مو صح'/'غيرت بياناته'، فهي تصحيح لآخر إجراء. اجعل action="CORRECT" وضع: corrected_progress (الرقم المصحح إن وجد)، part_hint (إن كان يصحّح الجزء)، title (العنوان الصحيح أو null ليُؤخذ من السياق).
- إذا قال 'ليش سجلت 13؟ انا قلت حلقتين' → CORRECT مع corrected_progress=2.
- إذا قال 'كنت اقصد الرابع' → CORRECT مع part_hint=4.

التأكيد والإلغاء:
- 'نعم/اي/اكيد/صح/تمام/اوك/ok/yes/ايوه/اه' بعد سؤال من البوت = تأكيد الإجراء المعلّق. اجعل action="CHAT" مع chat_response="__CONFIRM__".
- 'لا/no/الغي/cancel' بعد سؤال = إلغاء. action="CHAT" مع chat_response="__CANCEL__".
- 'تراجع/ارجع/undo/الغي آخر' = action="UNDO".

تحديد الأنمي بالحبكة/الوصف: أنت خبير أنمي حقيقي. عند وصف حبكة أو شخصيات أو قدرات، حدد الأنمي.
أمثلة: 'البطل يتحكم بالناس ويسرق القدرات' = Charlotte، 'عدنان ولينا' = Future Boy Conan، 'تشارلوت' = Charlotte.

أرجع JSON صالحاً فقط بمفاتيح:
- action: TRACK, DELETE, STATS, ACTIVITY, MY_LIST, FRIEND_PROFILE, FRIEND_LIST, FRIEND_ACTIVITY, COMPARE_FRIEND, RECOMMEND_GENRE, RECOMMEND_SIMILAR, RECOMMEND_FROM_LIST, TRENDING, SEASONAL, TOP_RATED, RANDOM_ANIME, CHARACTER_LOOKUP, STAFF_LOOKUP, STUDIO_LOOKUP, RELATIONS, AIRING_SCHEDULE, FAVORITES_LIST, FAVORITES_ADD, FAVORITES_REMOVE, BATCH_TRACK, SURPRISE, NEWS, CHAT, MY_FOLLOWING, UNDO, CORRECT
- media_type: ANIME أو MANGA
- title: العنوان الرسمي (إنجليزي/روماجي) أو null
- status: COMPLETED, CURRENT, PLANNING, PAUSED, DROPPED, REPEATING أو null
- score: رقم 0-10 أو null
- progress_delta: عدد صحيح (حلقات للإضافة) أو null
- absolute_progress: رقم الحلقة بالضبط (للبدايات الجديدة والتصحيحات) أو null
- fresh_start: boolean (true عند البداية/الإعادة)
- part_hint: "latest" أو رقم صحيح أو null
- corrected_progress: رقم صحيح أو null (للتصحيحات)
- is_favorite: boolean
- genre: نص أو null
- friend_username: اسم مستخدم AniList أو null
- confidence: high/medium/low
- alternatives: [] قائمة عناوين بديلة
- batch: [{title,status,score,progress_delta},...] أو null
- chat_response: نص عربي (لـ CHAT) أو null"""


def parse_with_gemini(text, chat_id=None):
    if not GEMINI_KEY:
        result = regex_parse(text)
        result["_parser"] = "regex_no_key"
        return result
    context = build_gemini_context(chat_id) if chat_id else ""
    models_to_try = []
    for m in [
        GEMINI_MODEL,
        "gemini-2.0-flash",
        "gemini-2.5-flash",
        "gemini-2.0-flash-lite",
    ]:
        if m and m not in models_to_try:
            models_to_try.append(m)
    last_error = None
    for model_name in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_KEY}"
        user_part = (
            f"السياق:\n{context}\n\nرسالة المستخدم: {text}"
            if context
            else f"رسالة المستخدم: {text}"
        )
        payload = {
            "contents": [{"parts": [{"text": user_part}]}],
            "systemInstruction": {"parts": [{"text": GEMINI_SYSTEM_PROMPT}]},
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.1,
            },
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                content = res["candidates"][0]["content"]["parts"][0]["text"]
                parsed = json.loads(content)
                # merge defaults so missing keys don't crash handlers
                base = regex_parse(text)
                base.update(parsed)
                base["original_text"] = text
                base["_parser"] = f"gemini:{model_name}"
                return base
        except urllib.error.HTTPError as e:
            err = ""
            try:
                err = e.read().decode("utf-8")[:200]
            except Exception:
                pass
            print(f"[Gemini] {model_name} HTTP {e.code}: {err}")
            last_error = f"HTTP {e.code}"
            if e.code in (429, 503):
                continue
        except Exception as e:
            print(f"[Gemini] {model_name} FAILED: {type(e).__name__}: {e}")
            last_error = f"{type(e).__name__}: {e}"
    result = regex_parse(text)
    result["_parser"] = f"regex_fallback:{last_error}"
    return result


# ============================================================
# [9] HANDLER CLASS
# ============================================================
class handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # silence default stderr logging

    # ---------- HTTP entry ----------
    def do_GET(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if qs.get("test"):
            self._json(200, self._test_gemini())
            return
        self._json(
            200,
            {
                "status": "running",
                "bot": "المخلافي v3.0",
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

    def _test_gemini(self):
        if not GEMINI_KEY:
            return {"error": "GEMINI_API_KEY not set"}
        t0 = time.time()
        try:
            result = parse_with_gemini("كملت انمي ون بيس واقيمه 9", chat_id=None)
            return {
                "gemini_works": str(result.get("_parser", "")).startswith("gemini"),
                "parser_used": result.get("_parser"),
                "elapsed_seconds": round(time.time() - t0, 2),
                "parsed_action": result.get("action"),
                "parsed_title": result.get("title"),
                "model": GEMINI_MODEL,
            }
        except Exception as e:
            return {"error": str(e), "type": type(e).__name__}

    def do_POST(self):
        # Always acknowledge Telegram first-thing to avoid retries on slow processing.
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            update = json.loads(body.decode("utf-8"))
        except Exception:
            self.send_response(200)
            self.end_headers()
            return

        # process in try/except so we never crash without 200
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
        chat_id = sget(msg, "chat", "id")
        text = (msg.get("text") or "").strip()
        if not text or not chat_id:
            return
        cid = str(chat_id)
        low = text.lower()

        if text.startswith("/start"):
            self._welcome(cid)
            return
        if text.startswith("/help"):
            self._help(cid)
            return
        if text.startswith("/undo"):
            save_context(cid, "user", text)
            self._undo(cid, {})
            return
        if text.startswith("/reset"):
            clear_context(cid)
            clear_pending(cid)
            tg_send(cid, "🧹 تم مسح السياق.")
            return

        save_context(cid, "user", text)

        # 1) pending confirmation flow (نعم / لا)
        pending = get_pending(cid)
        if pending:
            if low in (
                "نعم",
                "اي",
                "أي",
                "اكيد",
                "أكيد",
                "صح",
                "تمام",
                "اوك",
                "ok",
                "yes",
                "ايوه",
                "اه",
                "آه",
                "يب",
                "ايوا",
                "نعم اعمل",
                "اي سو",
            ):
                clear_pending(cid)
                self._route(cid, pending)
                return
            if low in (
                "لا",
                "لأ",
                "no",
                "الغي",
                "إلغاء",
                "cancel",
                "خلاص",
                "لا ما ابي",
            ):
                clear_pending(cid)
                tg_send(cid, "👌 تم الإلغاء.")
                save_context(cid, "bot", "تم الإلغاء")
                return

        # 2) undo
        if UNDO_RE.search(low):
            self._undo(cid, {})
            return

        # 3) parse (Gemini, with regex fallback)
        parsed = parse_with_gemini(text, cid)

        # 4) confirmation tokens emitted by Gemini
        if parsed.get("action") == "CHAT":
            cr = parsed.get("chat_response") or ""
            if cr.strip() == "__CONFIRM__" and pending:
                clear_pending(cid)
                self._route(cid, pending)
                return
            if cr.strip() == "__CANCEL__":
                clear_pending(cid)
                tg_send(cid, "👌 تم الإلغاء.")
                save_context(cid, "bot", "تم الإلغاء")
                return

        # 5) route
        self._route(cid, parsed)

    # ---------- Routing ----------
    def _route(self, cid, p):
        a = p.get("action", "TRACK")
        # resolve pronoun title from context
        if not p.get("title") and a in (
            "TRACK",
            "DELETE",
            "RECOMMEND_SIMILAR",
            "RELATIONS",
            "AIRING_SCHEDULE",
            "FAVORITES_ADD",
            "FAVORITES_REMOVE",
        ):
            p["title"] = self._ctx_title(cid)
        m = {
            "TRACK": self._track,
            "DELETE": self._delete,
            "STATS": self._stats,
            "ACTIVITY": self._activity,
            "MY_LIST": self._mylist,
            "UNDO": self._undo,
            "FRIEND_PROFILE": self._friend_profile,
            "FRIEND_LIST": self._friend_list,
            "FRIEND_ACTIVITY": self._friend_activity,
            "COMPARE_FRIEND": self._compare,
            "RECOMMEND_GENRE": self._rec_genre,
            "RECOMMEND_SIMILAR": self._rec_similar,
            "RECOMMEND_FROM_LIST": self._rec_from_list,
            "TRENDING": self._trending,
            "SEASONAL": self._seasonal,
            "TOP_RATED": self._top_rated,
            "RANDOM_ANIME": self._random_anime,
            "CHARACTER_LOOKUP": self._character,
            "STAFF_LOOKUP": self._staff,
            "STUDIO_LOOKUP": self._studio,
            "RELATIONS": self._relations,
            "AIRING_SCHEDULE": self._airing,
            "FAVORITES_LIST": self._fav_list,
            "FAVORITES_ADD": self._fav_add,
            "FAVORITES_REMOVE": self._fav_remove,
            "BATCH_TRACK": self._batch,
            "SURPRISE": self._surprise,
            "NEWS": self._news,
            "CHAT": self._chat,
            "MY_FOLLOWING": self._my_following,
            "CORRECT": self._correct,
        }
        fn = m.get(a, self._chat)
        try:
            fn(cid, p)
        except Exception as e:
            import traceback

            traceback.print_exc()
            tg_send(
                cid,
                f"⚠️ صار خطأ أثناء التنفيذ: {str(e)[:150]}\nجرّب بصيغة ثانية أو /help",
            )

    def _ctx_title(self, cid):
        for msg in reversed(get_context(cid)):
            t = sget(msg, "extra", "media_title")
            if t:
                return t
        return None

    def _last_track_extra(self, cid):
        for msg in reversed(get_context(cid)):
            ex = msg.get("extra") or {}
            if ex.get("action") in ("TRACK", "DELETE") and ex.get("media_id"):
                return ex
        return None

    # ---------- TRACK ----------
    def _track(self, cid, p):
        title = p.get("title")
        if not title:
            raw = p.get("original_text", "") or ""
            ext = _extract_title(raw.lower()) if raw else None
            if ext:
                title = ext
                p["title"] = title
            else:
                self._suggest(cid, raw)
                return
        mt = p.get("media_type", "ANIME")
        candidates = search_media(title, mt)
        if not candidates:
            tg_send(
                cid,
                f"❌ ما لقيت نتائج لـ: <b>{title}</b>\nجرّب الاسم بالإنجليزي أو الياباني.",
            )
            return
        part_hint = p.get("part_hint")
        # season resolution
        if part_hint and not p.get("media_id"):
            base = candidates[0]
            parts = resolve_franchise_parts(base["id"])
            if part_hint == "latest":
                chosen = pick_latest_part(parts) or base
            else:
                try:
                    chosen = (
                        pick_nth_part(parts, int(part_hint))
                        or pick_latest_part(parts)
                        or base
                    )
                except Exception:
                    chosen = pick_latest_part(parts) or base
            self._do_track(cid, chosen, p)
            return
        # disambiguation on low confidence + multiple
        if (
            len(candidates) > 1
            and p.get("confidence") in ("low", "medium")
            and not part_hint
        ):
            self._disambiguate_track(cid, title, candidates, p)
            return
        self._do_track(cid, candidates[0], p)

    def _disambiguate_track(self, cid, title, candidates, p):
        rows = []
        status = p.get("status") or ""
        score = p.get("score", "")
        fav = p.get("is_favorite", False)
        mt = p.get("media_type", "ANIME")
        ph = p.get("part_hint", "")
        for c in candidates[:5]:
            n = media_label(c)
            cd = f"pick:{c['id']}:{status}:{score}:{fav}:{mt}:{ph}"
            rows.append([{"text": n, "callback_data": cd}])
        tg_send(cid, f"🤔 لقيت عدة نتائج لـ <b>{title}</b>، اختر الصح:", kb_make(rows))

    def _do_track(self, cid, media, p):
        mid = media["id"]
        status = p.get("status") or "CURRENT"
        score = p.get("score")
        mt = p.get("media_type") or sget(media, "type") or "ANIME"
        fresh = bool(p.get("fresh_start"))
        existing = get_entry(mid, ANILIST_TOKEN) if ANILIST_TOKEN else None
        cur_prog = (existing or {}).get("progress") or 0
        prev_status = (existing or {}).get("status")
        prev_score = (existing or {}).get("score")
        prev_existed = bool(existing)
        entry_id = (existing or {}).get("id")
        total = sget(media, "episodes") or sget(media, "chapters") or 0
        # compute new progress — fresh_start forces absolute semantics on deltas too
        new_prog = cur_prog
        if p.get("absolute_progress") is not None:
            new_prog = int(p["absolute_progress"])
        elif p.get("progress_delta") is not None:
            if fresh and prev_existed and cur_prog > 0:
                # "بدأت ... شفت N" should reset to N, not add to existing
                new_prog = int(p["progress_delta"])
            else:
                new_prog = cur_prog + int(p["progress_delta"])
        elif status == "COMPLETED" and total > 0:
            new_prog = total
        elif status == "COMPLETED":
            new_prog = None  # leave untouched, AniList keeps existing
        if new_prog is not None and total and new_prog > total:
            new_prog = total
        if new_prog is not None and new_prog < 0:
            new_prog = 0
        # save
        if ANILIST_TOKEN:
            res = save_entry(
                mid, ANILIST_TOKEN, status=status, score=score, progress=new_prog
            )
            if isinstance(res, dict) and res.get("error"):
                tg_send(cid, f"⚠️ خطأ من AniList: {res['error'][:200]}")
                return
            new_entry_id = sget(res, "id") or entry_id
            if p.get("is_favorite"):
                try:
                    toggle_fav(mid, ANILIST_TOKEN, mt)
                except Exception:
                    pass
            # store last action for UNDO
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
        if p.get("is_favorite"):
            cap += "\n❤️ أضيف للمفضلة!"
        trailer = sget(media, "trailer")
        if trailer and sget(trailer, "site") == "youtube" and sget(trailer, "id"):
            cap += f"\n🎬 <a href='https://youtube.com/watch?v={trailer['id']}'>العرض الدعائي</a>"
        kb = kb_make(
            [
                [
                    {
                        "text": "🔗 AniList",
                        "url": sget(media, "siteUrl") or "https://anilist.co",
                    }
                ]
            ]
        )
        tg_photo(cid, cover_of(media), cap, kb)
        save_context(
            cid,
            "bot",
            f"حدثّت {name} - {unit} {new_prog}",
            extra={
                "media_title": name,
                "media_id": mid,
                "action": "TRACK",
                "progress": new_prog,
            },
        )

    # ---------- CORRECT (correction of last track) ----------
    def _correct(self, cid, p):
        last = self._last_track_extra(cid)
        corrected = p.get("corrected_progress")
        if corrected is None:
            corrected = p.get("absolute_progress")
        if corrected is None:
            corrected = _extract_corrected_progress(p.get("original_text", "") or "")
        part_hint = p.get("part_hint")
        # derive a part number from a bare ordinal (الرابع/الثالث...) if none detected
        if part_hint is None:
            low_txt = (p.get("original_text", "") or "").lower()
            for _ord, _n in _AR_ORDINALS.items():
                if _ord in low_txt:
                    part_hint = _n
                    break
        # part correction
        if (part_hint or p.get("title")) and last:
            base_title = p.get("title") or last.get("media_title")
            cands = search_media(base_title, last.get("media_type", "ANIME"))
            if cands:
                base = cands[0]
                parts = resolve_franchise_parts(base["id"])
                if part_hint == "latest":
                    chosen = pick_latest_part(parts) or base
                elif part_hint:
                    try:
                        chosen = (
                            pick_nth_part(parts, int(part_hint))
                            or pick_latest_part(parts)
                            or base
                        )
                    except Exception:
                        chosen = pick_latest_part(parts) or base
                else:
                    chosen = base
                # if picking a different part, re-track fresh
                if chosen.get("id") != last.get("media_id"):
                    tg_send(
                        cid, f"👌 فهمت، أتبع الجزء الصحيح: <b>{title_of(chosen)}</b>"
                    )
                    self._do_track(
                        cid,
                        chosen,
                        {
                            "status": "CURRENT",
                            "media_type": sget(chosen, "type", "ANIME"),
                            "absolute_progress": corrected,
                            "fresh_start": True,
                        },
                    )
                    return
        # progress correction
        if corrected is not None and last:
            name = last.get("media_title")
            preview = {
                "title": name,
                "absolute_progress": int(corrected),
                "status": "CURRENT",
                "media_type": last.get("media_type", "ANIME"),
                "fresh_start": True,
            }
            cap = (
                f"👌 سأعدّل <b>{name}</b> وأخلي التقدم <b>{int(corrected)}</b> بدل اللي سجلته.\n"
                f"أكّد بكلمة <b>نعم</b> (خلال ٥ دقايق)."
            )
            set_pending(cid, preview)
            kb = kb_make(
                [
                    [
                        {"text": "✅ تأكيد", "callback_data": "pend:yes"},
                        {"text": "❌ إلغاء", "callback_data": "pend:no"},
                    ],
                ]
            )
            tg_send(cid, cap, kb)
            return
        # correction without resolvable target
        tg_send(
            cid,
            "🤔 ما قدرت أفهم التصحيح زين. اكتب لي المطلوب بوضوح، مثلاً:\n"
            "<i>صحّح التقدم إلى حلقة 2</i> أو <i>كنت اقصد الجزء الرابع من ري زيرو</i>.",
        )

    # ---------- UNDO ----------
    def _undo(self, cid, p):
        last = get_last_action(cid)
        if not last:
            tg_send(cid, "📭 ما في إجراء سابق أقدر أرجّعه.")
            return
        if not ANILIST_TOKEN:
            tg_send(cid, "❌ ما عندي صلاحية AniList.")
            return
        if last.get("type") == "save":
            mid = last["media_id"]
            if not last.get("prev_existed"):
                # it was newly added → delete it
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
        save_context(cid, "bot", "تراجع عن آخر إجراء")

    # ---------- DELETE ----------
    def _delete(self, cid, p):
        title = p.get("title")
        if not title:
            tg_send(cid, "❌ حدد الأنمي للحذف. مثال: <i>احذف ناروتو</i>")
            return
        cands = search_media(title, p.get("media_type", "ANIME"))
        if not cands:
            tg_send(cid, f"❌ ما لقيت: <b>{title}</b>")
            return
        media = cands[0]
        mid = media["id"]
        entry = get_entry(mid, ANILIST_TOKEN) if ANILIST_TOKEN else None
        if not entry:
            tg_send(cid, f"❌ <b>{title_of(media)}</b> أصلاً مو بقائمتك.")
            return
        eid = entry["id"]
        name = title_of(media)
        cap = f"🗑️ تأكد تبي تحذف <b>{name}</b> من قائمتك؟"
        kb = kb_make(
            [
                [
                    {"text": "✅ تأكيد الحذف", "callback_data": f"del:{eid}:{mid}"},
                    {"text": "❌ إلغاء", "callback_data": "cancel"},
                ],
            ]
        )
        tg_photo(cid, cover_of(media), cap, kb)
        save_context(
            cid,
            "bot",
            f"طلب حذف {name}",
            extra={"media_title": name, "media_id": mid, "action": "DELETE"},
        )

    # ---------- STATS ----------
    def _stats(self, cid, p):
        fn = p.get("friend_username")
        if not fn:
            viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
            if not viewer:
                tg_send(cid, "❌ ما قدرت أجيب حسابك. تأكد من ربط AniList.")
                return
            fn = viewer["name"]
        stats = get_stats(fn)
        if not stats:
            tg_send(cid, "❌ ما لقيت إحصائيات.")
            return
        a = stats.get("anime") or {}
        m = stats.get("manga") or {}
        mins = a.get("minutesWatched", 0) or 0
        days = mins // 1440
        hours = (mins % 1440) // 60
        genres = a.get("genres") or []
        studios = a.get("studios") or []
        top_genres = ", ".join([g.get("genre", "") for g in genres[:3]]) or "—"
        top_studios = ", ".join([sget(s, "studio", "name") for s in studios[:3]]) or "—"
        cap = (
            f"📊 <b>إحصائيات {fn}</b>\n\n"
            f"📺 <b>الأنمي:</b>\n"
            f"  • العدد: {a.get('count', 0)}\n"
            f"  • الحلقات: {a.get('episodesWatched', 0)}\n"
            f"  • وقت المشاهدة: {days} يوم و {hours} ساعة\n"
            f"  • متوسط التقييم: {a.get('meanScore', 0)}/100\n"
            f"  • الأنواع المفضلة: {top_genres}\n"
            f"  • الاستديوهات المفضلة: {top_studios}\n\n"
            f"📖 <b>المانجا:</b>\n"
            f"  • العدد: {m.get('count', 0)}\n"
            f"  • الفصول: {m.get('chaptersRead', 0)}\n"
            f"  • متوسط التقييم: {m.get('meanScore', 0)}/100"
        )
        tg_send(cid, cap)
        save_context(cid, "bot", f"عرضت إحصائيات {fn}")

    # ---------- ACTIVITY ----------
    def _activity(self, cid, p):
        viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
        if not viewer:
            tg_send(cid, "❌ ما قدرت أجيب حسابك.")
            return
        acts = get_activities(viewer["id"])
        if not acts:
            tg_send(cid, "ما في نشاطات حديثة.")
            return
        items = []
        for act in acts[:8]:
            media = act.get("media") or {}
            if not media:
                continue
            name = title_of(media)
            st = act.get("status", "") or ""
            prog = act.get("progress", "") or ""
            cap = f"<b>{name}</b>\n{status_ar(st) if st in STATUS_AR else st}"
            if prog:
                cap += f" {prog}"
            url = cover_of(media)
            if url:
                items.append({"url": url, "caption": cap})
        if items:
            tg_album(cid, items)
        else:
            tg_send(cid, "ما في نشاطات فيها صور.")
        save_context(cid, "bot", "عرضت آخر النشاطات")

    # ---------- MY LIST ----------
    def _mylist(self, cid, p):
        viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
        if not viewer:
            tg_send(cid, "❌ ما قدرت أجيب حسابك.")
            return
        status = p.get("status")
        mt = p.get("media_type", "ANIME")
        sort = ["UPDATED_TIME_DESC"]
        if status == "COMPLETED":
            sort = ["SCORE_DESC", "UPDATED_TIME_DESC"]
        entries = get_media_list(
            username=viewer["name"], media_type=mt, status=status, sort=sort, per_page=8
        )
        if not entries:
            tg_send(cid, "قائمتك فاضية بهذا الفلتر.")
            return
        items = []
        for e in entries[:8]:
            media = e.get("media") or {}
            name = title_of(media)
            sc = e.get("score", 0)
            prog = e.get("progress")
            cap = f"<b>{name}</b>"
            if prog:
                cap += f" — {prog} حلقة"
            if sc:
                cap += f" | ⭐ {sc}"
            url = cover_of(media)
            if url:
                items.append({"url": url, "caption": cap})
        if items:
            tg_album(cid, items)
        else:
            tg_send(cid, "ما في نتائج فيها صور.")

    # ---------- FRIEND ----------
    def _friend_profile(self, cid, p):
        fn = p.get("friend_username")
        if not fn:
            self._my_following(cid, p)
            return
        user = get_profile(fn)
        if not user:
            tg_send(cid, f"❌ ما لقيت مستخدم: {fn}")
            return
        a = sget(user, "statistics", "anime") or {}
        m = sget(user, "statistics", "manga") or {}
        cap = (
            f"👤 <b>{user.get('name')}</b>\n\n"
            f"📺 أنمي: {a.get('count', 0)} | حلقات: {a.get('episodesWatched', 0)}\n"
            f"📖 مانجا: {m.get('count', 0)} | فصول: {m.get('chaptersRead', 0)}\n"
            f"⭐ متوسط تقييم الأنمي: {a.get('meanScore', 0)}/100"
        )
        kb = kb_make(
            [
                [
                    {
                        "text": "🔗 AniList",
                        "url": user.get("siteUrl") or "https://anilist.co",
                    }
                ]
            ]
        )
        tg_photo(cid, sget(user, "avatar", "large"), cap, kb)

    def _my_following(self, cid, p):
        viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
        if not viewer:
            tg_send(cid, "❌ ما قدرت أجيب حسابك.")
            return
        following = get_following(viewer["id"])
        if not following:
            tg_send(cid, "👥 ما تتابع أحد حالياً على AniList.")
            return
        cap = f"👥 <b>قائمة متابعينك ({len(following)}):</b>\n\n"
        for u in following:
            name = u.get("name", "—")
            a = (u.get("statistics") or {}).get("anime") or {}
            cap += f"• <b>{name}</b> — {a.get('count', 0)} أنمي | ⭐ {a.get('meanScore', 0)}\n"
        tg_send(cid, cap)
        save_context(cid, "bot", "عرضت قائمة المتابعين")

    def _friend_list(self, cid, p):
        fn = p.get("friend_username")
        if not fn:
            # "قائمة أصدقائي" → show following
            self._my_following(cid, p)
            return
        entries = get_media_list(username=fn, per_page=8)
        if not entries:
            tg_send(cid, f"ما في نتائج لـ {fn}.")
            return
        items = []
        for e in entries[:8]:
            media = e.get("media") or {}
            name = title_of(media)
            sc = e.get("score", 0)
            cap = f"<b>{name}</b>" + (f"\n⭐ {sc}" if sc else "")
            url = cover_of(media)
            if url:
                items.append({"url": url, "caption": cap})
        if items:
            tg_album(cid, items)

    def _friend_activity(self, cid, p):
        fn = p.get("friend_username")
        if not fn:
            tg_send(cid, "❌ حدد اسم المستخدم.")
            return
        user = get_profile(fn)
        if not user:
            tg_send(cid, f"❌ ما لقيت: {fn}")
            return
        acts = get_activities(user["id"])
        if not acts:
            tg_send(cid, f"ما في نشاطات حديثة لـ {fn}.")
            return
        items = []
        for act in acts[:8]:
            media = act.get("media") or {}
            if not media:
                continue
            cap = f"<b>{title_of(media)}</b>\n{act.get('status', '')} {act.get('progress', '')}"
            url = cover_of(media)
            if url:
                items.append({"url": url, "caption": cap})
        if items:
            tg_album(cid, items)

    # ---------- COMPARE ----------
    def _compare(self, cid, p):
        fn = p.get("friend_username")
        if not fn:
            tg_send(cid, "❌ حدد اسم صديقك للمقارنة.")
            return
        viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
        if not viewer:
            tg_send(cid, "❌ ما قدرت أجيب حسابك.")
            return
        my_list = get_media_list(
            username=viewer["name"], status="COMPLETED", per_page=50
        )
        fr_list = get_media_list(username=fn, status="COMPLETED", per_page=50)
        my_ids = {e["media"]["id"]: e for e in my_list if sget(e, "media")}
        fr_ids = {e["media"]["id"]: e for e in fr_list if sget(e, "media")}
        shared = set(my_ids) & set(fr_ids)
        only_me = set(my_ids) - set(fr_ids)
        only_fr = set(fr_ids) - set(my_ids)
        cap = (
            f"🆚 <b>مقارنة: {viewer['name']} vs {fn}</b>\n\n"
            f"🤝 مشترك: {len(shared)} أنمي\n"
            f"👤 عندك فقط: {len(only_me)} أنمي\n"
            f"👥 عند {fn} فقط: {len(only_fr)} أنمي\n"
        )
        if shared:
            cap += "\n<b>تقييمات مشتركة:</b>\n"
            for sid in list(shared)[:6]:
                e = my_ids.get(sid) or {}
                name = title_of(e.get("media") or {})
                ms = e.get("score", 0)
                fs = (fr_ids.get(sid) or {}).get("score", 0)
                cap += f"• {name}: أنت {ms} | {fn} {fs}\n"
        tg_send(cid, cap)

    # ---------- RECOMMENDATIONS ----------
    def _rec_genre(self, cid, p):
        genre = p.get("genre", "Action")
        trending = get_trending(10)
        filtered = (
            [
                t
                for t in trending
                if genre.lower() in [g.lower() for g in (t.get("genres") or [])]
            ]
            if trending
            else []
        )
        results = filtered if filtered else trending[:6]
        if not results:
            tg_send(cid, f"❌ ما في اقتراحات لتصنيف {genre}")
            return
        items = [
            {
                "url": cover_of(r),
                "caption": f"<b>{title_of(r)}</b>\n⭐ {r.get('averageScore', 0)}%",
            }
            for r in results[:6]
            if cover_of(r)
        ]
        if items:
            tg_album(cid, items)
        else:
            tg_send(cid, "ما في نتائج فيها صور.")

    def _rec_similar(self, cid, p):
        title = p.get("title")
        if not title:
            tg_send(cid, "❌ حدد الأنمي لإيجاد أنمي مشابه.")
            return
        cands = search_media(title)
        if not cands:
            tg_send(cid, f"❌ ما لقيت: {title}")
            return
        recs = get_recommendations(cands[0]["id"])
        if not recs:
            tg_send(cid, f"❌ ما في توصيات مشابهة لـ <b>{title}</b>")
            return
        items = [
            {
                "url": cover_of(r),
                "caption": f"<b>{title_of(r)}</b>\n⭐ {r.get('averageScore', 0)}%",
            }
            for r in recs[:6]
            if cover_of(r)
        ]
        if items:
            tg_album(cid, items)

    def _rec_from_list(self, cid, p):
        viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
        if not viewer:
            tg_send(cid, "❌ ما قدرت أجيب حسابك.")
            return
        entries = get_media_list(
            username=viewer["name"], status="COMPLETED", sort=["SCORE_DESC"], per_page=8
        )
        if not entries:
            tg_send(cid, "قائمتك المكتملة فاضية.")
            return
        items = [
            {
                "url": cover_of(e.get("media")),
                "caption": f"<b>{title_of(e.get('media'))}</b>\n⭐ {e.get('score', 0)}",
            }
            for e in entries
            if sget(e, "media") and cover_of(e["media"])
        ]
        if items:
            tg_album(cid, items[:8])

    # ---------- TRENDING / SEASONAL / TOP ----------
    def _trending(self, cid, p):
        results = get_trending(8)
        if not results:
            tg_send(cid, "❌ ما قدرت أجيب الترند.")
            return
        items = [
            {
                "url": cover_of(r),
                "caption": f"<b>{title_of(r)}</b>\n⭐ {r.get('averageScore', 0)}%",
            }
            for r in results
            if cover_of(r)
        ]
        if items:
            tg_album(cid, items)
        save_context(cid, "bot", "عرضت الترند")

    def _seasonal(self, cid, p):
        s, y = current_season()
        results = get_seasonal(s, y, 8)
        if not results:
            tg_send(cid, f"❌ ما في أنميات لموسم {s} {y}")
            return
        items = [
            {
                "url": cover_of(r),
                "caption": f"<b>{title_of(r)}</b>\n⭐ {r.get('averageScore', 0)}%",
            }
            for r in results
            if cover_of(r)
        ]
        if items:
            tg_album(cid, items)

    def _top_rated(self, cid, p):
        q = """query{Page(perPage:8){media(type:ANIME,sort:SCORE_DESC){
        id title{romaji english} coverImage{large} averageScore siteUrl}}}"""
        res = _gql(q)
        media_list = sget(res, "data", "Page", "media", default=[]) or []
        items = [
            {
                "url": cover_of(m),
                "caption": f"<b>{title_of(m)}</b>\n⭐ {m.get('averageScore', 0)}%",
            }
            for m in media_list
            if cover_of(m)
        ]
        if items:
            tg_album(cid, items)
        else:
            tg_send(cid, "❌ فشل جلب الأعلى تقييماً.")

    def _random_anime(self, cid, p):
        genre = p.get("genre") or random.choice(
            [
                "Action",
                "Adventure",
                "Comedy",
                "Drama",
                "Fantasy",
                "Mystery",
                "Romance",
                "Sci-Fi",
            ]
        )
        page = random.randint(1, 10)
        q = """query($g:String,$p:Int){Page(page:$p,perPage:1){media(type:ANIME,genre:$g,sort:POPULARITY_DESC){
        id title{romaji english} coverImage{large} averageScore siteUrl episodes}}}"""
        res = _gql(q, {"g": genre, "p": page})
        media_list = sget(res, "data", "Page", "media", default=[]) or []
        if not media_list:
            tg_send(cid, "❌ ما لقيت أنمي عشوائي. جرّب مرة ثانية!")
            return
        m = media_list[0]
        cap = f"🎲 <b>أنمي عشوائي ({genre}):</b>\n\n<b>{title_of(m)}</b>\n⭐ {m.get('averageScore', 0)}%\n📺 {m.get('episodes', '?')} حلقة"
        kb = kb_make(
            [
                [
                    {
                        "text": "🔗 AniList",
                        "url": m.get("siteUrl") or "https://anilist.co",
                    },
                    {
                        "text": "➕ تابع",
                        "callback_data": f"pick:{m['id']}:CURRENT::False:ANIME:",
                    },
                ]
            ]
        )
        tg_photo(cid, cover_of(m), cap, kb)

    # ---------- CHARACTER / STAFF / STUDIO ----------
    def _character(self, cid, p):
        name = p.get("title")
        if not name:
            tg_send(cid, "❌ حدد اسم الشخصية.")
            return
        ch = search_character_q(name)
        if not ch:
            tg_send(cid, f"❌ ما لقيت شخصية: {name}")
            return
        full = sget(ch, "name", "full") or name
        native = sget(ch, "name", "native") or ""
        anime = "غير محدد"
        nodes = sget(ch, "media", "nodes") or []
        if nodes:
            anime = title_of(nodes[0])
        cap = (
            f"👤 <b>{full}</b>"
            + (f" ({native})" if native else "")
            + f"\n\n📺 من: <b>{anime}</b>"
        )
        kb = kb_make(
            [[{"text": "🔗 AniList", "url": ch.get("siteUrl") or "https://anilist.co"}]]
        )
        tg_photo(cid, sget(ch, "image", "large"), cap, kb)

    def _staff(self, cid, p):
        name = p.get("title")
        if not name:
            tg_send(cid, "❌ حدد اسم الشخص.")
            return
        st = search_staff_q(name)
        if not st:
            tg_send(cid, f"❌ ما لقيت: {name}")
            return
        full = sget(st, "name", "full") or name
        native = sget(st, "name", "native") or ""
        jobs = ", ".join((st.get("primaryOccupations") or [])[:3]) or "—"
        chars = (
            ", ".join(
                [
                    sget(c, "name", "full")
                    for c in (sget(st, "characters", "nodes") or [])[:3]
                ]
            )
            or "—"
        )
        cap = (
            f"🎙️ <b>{full}</b>"
            + (f" ({native})" if native else "")
            + f"\n\n💼 المهنة: {jobs}\n🎭 شخصيات: {chars}"
        )
        tg_photo(cid, sget(st, "image", "large"), cap)

    def _studio(self, cid, p):
        name = p.get("title")
        if not name:
            tg_send(cid, "❌ حدد اسم الاستديو.")
            return
        studio = search_studio_q(name)
        if not studio:
            tg_send(cid, f"❌ ما لقيت استديو: {name}")
            return
        works = sget(studio, "media", "nodes") or []
        if not works:
            tg_send(cid, f"ما في أعمال لاستديو {studio.get('name')}")
            return
        items = [
            {
                "url": cover_of(w),
                "caption": f"<b>{title_of(w)}</b>\n⭐ {w.get('averageScore', 0)}%",
            }
            for w in works[:8]
            if cover_of(w)
        ]
        if items:
            tg_send(cid, f"🏢 <b>أعمال {studio.get('name')}:</b>")
            tg_album(cid, items)

    # ---------- RELATIONS ----------
    def _relations(self, cid, p):
        title = p.get("title")
        if not title:
            tg_send(cid, "❌ حدد الأنمي.")
            return
        cands = search_media(title)
        if not cands:
            tg_send(cid, f"❌ ما لقيت: {title}")
            return
        edges = get_relations(cands[0]["id"])
        if not edges:
            tg_send(cid, f"❌ ما في علاقات لـ <b>{title}</b>")
            return
        TYPE_AR = {
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
        items = []
        for e in edges[:8]:
            node = e.get("node") or {}
            rel = TYPE_AR.get(e.get("relationType", ""), e.get("relationType", ""))
            cap = f"<b>{title_of(node)}</b>\n🔗 {rel}"
            url = cover_of(node)
            if url:
                items.append({"url": url, "caption": cap})
        if items:
            tg_album(cid, items)

    # ---------- AIRING ----------
    def _airing(self, cid, p):
        title = p.get("title")
        if not title:
            tg_send(cid, "❌ حدد الأنمي.")
            return
        cands = search_media(title)
        if not cands:
            tg_send(cid, f"❌ ما لقيت: {title}")
            return
        media = cands[0]
        schedule = get_airing(media["id"])
        if not schedule:
            tg_send(cid, f"📺 <b>{title_of(media)}</b> منتهي أو غير مجدول حالياً.")
            return
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

    # ---------- FAVORITES ----------
    def _fav_list(self, cid, p):
        viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
        if not viewer:
            tg_send(cid, "❌ ما قدرت أجيب حسابك.")
            return
        anime_favs, manga_favs = get_favorites(viewer["name"])
        all_favs = (anime_favs or []) + (manga_favs or [])
        if not all_favs:
            tg_send(cid, "قائمة مفضلاتك فاضية.")
            return
        items = [
            {"url": cover_of(f), "caption": f"<b>{title_of(f)}</b> ❤️"}
            for f in all_favs[:8]
            if cover_of(f)
        ]
        if items:
            tg_album(cid, items)

    def _fav_add(self, cid, p):
        title = p.get("title")
        if not title:
            tg_send(cid, "❌ حدد الأنمي.")
            return
        cands = search_media(title)
        if not cands:
            tg_send(cid, f"❌ ما لقيت: {title}")
            return
        media = cands[0]
        if ANILIST_TOKEN:
            try:
                toggle_fav(media["id"], ANILIST_TOKEN, p.get("media_type", "ANIME"))
            except Exception:
                pass
        tg_photo(
            cid, cover_of(media), f"❤️ بدّلت حالة المفضلة لـ <b>{title_of(media)}</b>"
        )

    def _fav_remove(self, cid, p):
        self._fav_add(cid, p)

    # ---------- BATCH ----------
    def _batch(self, cid, p):
        batch = p.get("batch") or []
        if not batch:
            tg_send(cid, "❌ ما لقيت عناصر للتحديث.")
            return
        items = []
        for entry in batch[:5]:
            title = entry.get("title")
            if not title:
                continue
            cands = search_media(title)
            if not cands:
                continue
            media = cands[0]
            mid = media["id"]
            status = entry.get("status", "COMPLETED")
            score = entry.get("score")
            total = sget(media, "episodes") or sget(media, "chapters") or 0
            prog = (
                total
                if status == "COMPLETED" and total > 0
                else entry.get("progress_delta")
            )
            if ANILIST_TOKEN:
                save_entry(
                    mid, ANILIST_TOKEN, status=status, score=score, progress=prog
                )
            name = title_of(media)
            cap = f"<b>{name}</b> ✅\n{status_ar(status)}"
            if score:
                cap += f" | ⭐ {score}"
            url = cover_of(media)
            if url:
                items.append({"url": url, "caption": cap})
        if items:
            tg_send(cid, f"✅ حدّثت {len(items)} عنصر:")
            tg_album(cid, items)
        else:
            tg_send(cid, "❌ فشل تحديث العناصر.")

    # ---------- SURPRISE ----------
    def _surprise(self, cid, p):
        mode = random.choice(["rec", "flashback", "stat", "random"])
        viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
        if mode == "flashback" and viewer:
            entries = get_media_list(
                username=viewer["name"], status="COMPLETED", per_page=50
            )
            if entries:
                e = random.choice(entries)
                media = e.get("media") or {}
                sc = e.get("score", 0)
                cap = f"💭 <b>فلاش باك!</b>\n\n<b>{title_of(media)}</b>\nقيّمته: ⭐ {sc}/10\n\nتتذكره؟ 🤔"
                tg_photo(cid, cover_of(media), cap)
                return
        if mode == "stat" and viewer:
            stats = get_stats(viewer["name"])
            if stats:
                a = stats.get("anime") or {}
                mins = a.get("minutesWatched", 0) or 0
                eps = a.get("episodesWatched", 0) or 0
                days = mins // 1440
                tg_send(
                    cid,
                    f"🤯 <b>هل تعلم؟</b>\n\nشفت <b>{eps}</b> حلقة أنمي!\nهذا يعادل <b>{days} يوم</b> متواصل مشاهدة! 📺",
                )
                return
        if mode == "rec":
            trending = get_trending(10)
            if trending:
                pick = random.choice(trending)
                cap = f"🔥 <b>جرّب هذا!</b>\n\n<b>{title_of(pick)}</b>\n⭐ {pick.get('averageScore', 0)}%\n\nأنمي ترند الحين!"
                kb = kb_make(
                    [
                        [
                            {
                                "text": "🔗 AniList",
                                "url": pick.get("siteUrl") or "https://anilist.co",
                            }
                        ]
                    ]
                )
                tg_photo(cid, cover_of(pick), cap, kb)
                return
        self._random_anime(cid, p)

    # ---------- NEWS ----------
    def _news(self, cid, p):
        viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
        news_items = []
        if viewer:
            current_list = get_media_list(
                username=viewer["name"], status="CURRENT", per_page=5
            )
            for e in current_list[:3]:
                media = e.get("media") or {}
                schedule = get_airing(media.get("id"))
                if schedule:
                    ep = schedule.get("episode", "?")
                    secs = schedule.get("timeUntilAiring", 0) or 0
                    d = secs // 86400
                    name = title_of(media)
                    news_items.append(f"📺 <b>{name}</b> — الحلقة {ep} بعد {d} يوم")
        trending = get_trending(5)
        for t in trending[:2]:
            news_items.append(
                f"🔥 ترند: <b>{title_of(t)}</b> — ⭐ {t.get('averageScore', 0)}%"
            )
        if news_items:
            tg_send(cid, "📰 <b>أخبار أنمياتك:</b>\n\n" + "\n\n".join(news_items))
        else:
            tg_send(cid, "ما في أخبار جديدة حالياً.")

    # ---------- CHAT / fallback ----------
    def _chat(self, cid, p):
        resp = p.get("chat_response")
        if not resp or resp.strip() in ("__CONFIRM__", "__CANCEL__"):
            resp = (
                "ما فهمت قصدك زين 🤔\nجرّب صيغة مثل:\n"
                "• <i>شفت 3 حلقات من ون بيس</i>\n"
                "• <i>كملت انمي فريرين واقيمه 9</i>\n"
                "• <i>وش الترند؟</i>\n"
                "أو أرسل /help للدليل الكامل."
            )
        tg_send(cid, resp)
        save_context(cid, "bot", resp[:120])

    def _suggest(self, cid, raw):
        """Friendly fallback when no title could be resolved."""
        tg_send(
            cid,
            (
                "🤔 ما عرفت اسم الأنمي من رسالتك.\n"
                "جرّب تذكر الاسم بالإنجليزي أو الياباني.\n"
                "مثال: <i>شفت حلقتين من Lord of Mysteries</i>\n"
                "أو أرسل /help."
            ),
        )

    # ---------- CALLBACKS ----------
    def _on_callback(self, cb):
        data = cb.get("data", "") or ""
        cid = str(sget(cb, "message", "chat", "id") or "")
        mid = sget(cb, "message", "message_id")
        cbid = cb.get("id")
        try:
            if cid and cbid:
                tg_answer_cb(cbid, "تمام...")
        except Exception:
            pass
        if not cid:
            return
        parts = data.split(":")
        cmd = parts[0] if parts else ""
        try:
            if cmd == "del" and len(parts) >= 2:
                self._cb_delete(cid, mid, parts)
            elif cmd == "pend":
                self._cb_pending(cid, mid, parts)
            elif cmd == "pick" and len(parts) >= 2:
                self._cb_pick(cid, parts)
            elif cmd == "cancel":
                try:
                    tg_edit(cid, mid, "↩️ تم الإلغاء.", kb=None)
                except Exception:
                    pass
        except Exception as e:
            import traceback

            traceback.print_exc()
            try:
                tg_send(cid, f"⚠️ خطأ في الزر: {str(e)[:120]}")
            except Exception:
                pass

    def _cb_delete(self, cid, mid, parts):
        eid = int(parts[1])
        media_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
        # capture prev for undo
        prev_status = prev_prog = prev_score = None
        if media_id and ANILIST_TOKEN:
            ex = get_entry(media_id, ANILIST_TOKEN) or {}
            prev_status = ex.get("status")
            prev_prog = ex.get("progress")
            prev_score = ex.get("score")
        ok = delete_entry(eid, ANILIST_TOKEN) if ANILIST_TOKEN else False
        if ok:
            try:
                tg_edit(cid, mid, "✅ تم الحذف بنجاح!", kb=None)
            except Exception:
                pass
            if media_id:
                set_last_action(
                    cid,
                    {
                        "type": "delete",
                        "media_id": media_id,
                        "media_title": "",
                        "prev_status": prev_status,
                        "prev_progress": prev_prog,
                        "prev_score": prev_score,
                    },
                )
            tg_send(cid, "♻️ تقدر ترجّعه بكلمة <b>تراجع</b> أو /undo.")
        else:
            try:
                tg_edit(cid, mid, "❌ فشل الحذف. جرّب مرة ثانية.", kb=None)
            except Exception:
                tg_send(cid, "❌ فشل الحذف.")

    def _cb_pending(self, cid, mid, parts):
        pending = get_pending(cid)
        if not pending:
            try:
                tg_edit(cid, mid, "⏳ انتهت صلاحية الطلب، أعد المحاولة.", kb=None)
            except Exception:
                pass
            return
        clear_pending(cid)
        if len(parts) > 1 and parts[1] == "no":
            try:
                tg_edit(cid, mid, "↩️ تم الإلغاء.", kb=None)
            except Exception:
                pass
            save_context(cid, "bot", "تم الإلغاء")
            return
        # confirm
        try:
            tg_edit(cid, mid, "⏳ جاري التنفيذ...", kb=None)
        except Exception:
            pass
        self._route(cid, pending)

    def _cb_pick(self, cid, parts):
        media_id = int(parts[1])
        status = parts[2] if len(parts) > 2 and parts[2] else "CURRENT"
        score_str = parts[3] if len(parts) > 3 else ""
        is_fav = (parts[4] == "True") if len(parts) > 4 else False
        mt = parts[5] if len(parts) > 5 else "ANIME"
        ph = parts[6] if len(parts) > 6 else ""
        score = None
        try:
            score = float(score_str) if score_str not in ("", "None") else None
        except Exception:
            score = None
        media = get_media_by_id(media_id)
        if not media:
            tg_send(cid, "❌ ما لقيت الأنمي المختار.")
            return
        p = {"status": status, "score": score, "is_favorite": is_fav, "media_type": mt}
        if ph == "latest":
            parts_map = resolve_franchise_parts(media_id)
            chosen = pick_latest_part(parts_map) or media
            self._do_track(cid, chosen, p)
            return
        if ph and ph.isdigit():
            try:
                parts_map = resolve_franchise_parts(media_id)
                chosen = (
                    pick_nth_part(parts_map, int(ph))
                    or pick_latest_part(parts_map)
                    or media
                )
                self._do_track(cid, chosen, p)
                return
            except Exception:
                pass
        self._do_track(cid, media, p)

    # ---------- WELCOME & HELP ----------
    def _welcome(self, cid):
        clear_context(cid)
        tg_send(
            cid,
            """🎬 <b>أهلاً بك في المخلافي — بوت AniList v3.0</b> 🤖

مساعدك الشامل لإدارة قوائم الأنمي والمانجا. أفهم كلامك العادي بالعربي!

<b>📌 أمثلة سريعة:</b>
• <i>بدأت أشوف لورد الغوامض، شفت حلقتين</i>
• <i>شفت 3 حلقات من ون بيس</i>
• <i>كملت انمي فريرين وأقيّمه 9</i>
• <i>شفت الجزء الجديد من ري زيرو</i>
• <i>احذف ناروتو من قائمتي</i>
• <i>وش الترند؟</i> • <i>إحصائياتي</i> • <i>فاجئني!</i>

💡 غلطت في رقم؟ قول <i>صحّح التقدم إلى حلقة 2</i> أو <i>تراجع</i>.

أرسل /help للدليل الكامل 📖""",
        )

    def _help(self, cid):
        tg_send(
            cid,
            """📖 <b>دليل المخلافي الشامل</b>

<b>📺 تتبع الأنمي/المانجا:</b>
• <i>بدأت أشوف هجوم العمالقة شفت حلقتين</i> (بداية جديدة = الحلقة 2)
• <i>شفت 3 حلقات من بليتش</i> (يضيف 3 للتقدم الحالي)
• <i>كملت راجنا وأقيّمه 9 واعمله مفضلة</i>
• <i>شفت الجزء الجديد من ري زيرو</i> (يجيب آخر جزء تلقائياً)
• <i>قريت 10 فصول من سولو ليفلينق</i>

<b>♻️ التصحيح والتراجع:</b>
• <i>صحّح التقدم إلى حلقة 2</i> / <i>كنت اقصد الجزء الرابع</i>
• <i>تراجع</i> أو <i>/undo</i> — يرجّع آخر تعديل/حذف

<b>🗑️ الحذف:</b> <i>احذف ناروتو من قائمتي</i>

<b>📊 الإحصائيات:</b> <i>إحصائياتي</i> • <i>إحصائيات Ahmed</i>

<b>📋 القوائم والنشاط:</b>
• <i>آخر نشاطاتي</i> • <i>انمياتي المكتملة</i> • <i>مفضلاتي</i> • <i>قائمة أصدقائي</i>

<b>👥 الأصدقاء:</b> <i>بروفايل Ahmed</i> • <i>قارن قائمتي مع Ahmed</i>

<b>🧠 التوصيات:</b>
• <i>أنمي مشابه لهجوم العمالقة</i>
• <i>اقترح لي أنمي أكشن</i> • <i>أنمي عشوائي</i>

<b>🔥 الترند والمواسم:</b>
• <i>وش الترند؟</i> • <i>أنميات هذا الموسم</i> • <i>أفضل 10 أنميات</i>

<b>🔍 البحث:</b>
• <i>مين شخصية لوفي؟</i> • <i>مؤدي صوت إيتشيغو</i>
• <i>أنميات استديو MAPPA</i> • <i>سيكويل هجوم العمالقة</i>
• <i>متى الحلقة الجاية من ون بيس</i>

<b>🎲 المرح:</b> <i>فاجئني!</i> • <i>خبر حلو</i>

💡 تقدر تكلمني كلام عادي وبفهمك! 😊""",
        )
