"""
AniList Telegram Bot v2.0 - Vercel Serverless Webhook
All modules combined into a single file for reliable Vercel deployment.
Features: 30+ actions, cover images, albums, inline keyboards, conversation memory, Gemini AI.
"""
import os
import re
import json
import time
import random
import datetime
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler

# ============================================================
# [1] CONFIG
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ANILIST_TOKEN = os.environ.get("ANILIST_ACCESS_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
ANILIST_URL = "https://graphql.anilist.co"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AniListBot/2.0"

# ============================================================
# [2] CONTEXT STORE (In-Memory Conversation History)
# ============================================================
_conversation_store = {}
CONTEXT_MAX_MESSAGES = 6
CONTEXT_TTL_SECONDS = 1800

def get_context(chat_id):
    chat_id = str(chat_id)
    if chat_id in _conversation_store:
        store = _conversation_store[chat_id]
        if time.time() - store["last_active"] > CONTEXT_TTL_SECONDS:
            del _conversation_store[chat_id]
            return []
        return store["messages"]
    return []

def save_context(chat_id, role, text, extra=None):
    chat_id = str(chat_id)
    if chat_id not in _conversation_store:
        _conversation_store[chat_id] = {"messages": [], "last_active": time.time()}
    store = _conversation_store[chat_id]
    msg = {"role": role, "text": text}
    if extra:
        msg["extra"] = extra
    store["messages"].append(msg)
    if len(store["messages"]) > CONTEXT_MAX_MESSAGES:
        store["messages"] = store["messages"][-CONTEXT_MAX_MESSAGES:]
    store["last_active"] = time.time()

def build_gemini_context(chat_id):
    messages = get_context(chat_id)
    if not messages:
        return ""
    lines = ["Recent Conversation Context:"]
    for msg in messages:
        role = "User" if msg["role"] == "user" else "Bot"
        extra = msg.get("extra", {})
        extra_str = ""
        if extra.get("media_title"):
            extra_str = f" [media: {extra['media_title']}, id: {extra.get('media_id','')}]"
        lines.append(f"{role}: {msg['text']}{extra_str}")
    return "\n".join(lines)

def clear_context(chat_id):
    chat_id = str(chat_id)
    _conversation_store.pop(chat_id, None)

# ============================================================
# [3] ANILIST GRAPHQL CLIENT
# ============================================================
def _gql(query, variables=None, token=None):
    """Base GraphQL request to AniList."""
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
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"errors": [{"message": f"HTTP {e.code}"}]}
    except Exception as e:
        return {"errors": [{"message": str(e)}]}

def search_media(title, media_type="ANIME", per_page=4):
    q = """query($s:String,$t:MediaType,$n:Int){Page(perPage:$n){media(search:$s,type:$t){
    id type title{romaji english native} episodes chapters format seasonYear
    coverImage{extraLarge large medium} siteUrl trailer{id site} averageScore}}}"""
    res = _gql(q, {"s": title, "t": media_type, "n": per_page})
    return res.get("data", {}).get("Page", {}).get("media", [])

def get_entry(media_id, token):
    q = "query($m:Int){MediaList(mediaId:$m){id status score progress}}"
    res = _gql(q, {"m": media_id}, token)
    if "errors" in res:
        return None
    return res.get("data", {}).get("MediaList")

def save_entry(media_id, token, status=None, score=None, progress=None):
    q = """mutation($m:Int,$st:MediaListStatus,$sc:Float,$p:Int){
    SaveMediaListEntry(mediaId:$m,status:$st,score:$sc,progress:$p){id status score progress
    media{title{romaji english}}}}"""
    v = {"m": media_id}
    if status: v["st"] = status
    if score is not None: v["sc"] = float(score)
    if progress is not None: v["p"] = int(progress)
    res = _gql(q, v, token)
    if "errors" in res:
        return {"error": res["errors"][0].get("message", "Unknown")}
    return res.get("data", {}).get("SaveMediaListEntry")

def delete_entry(entry_id, token):
    q = "mutation($id:Int){DeleteMediaListEntry(id:$id){deleted}}"
    res = _gql(q, {"id": entry_id}, token)
    return res.get("data", {}).get("DeleteMediaListEntry", {}).get("deleted", False)

def toggle_fav(media_id, token):
    q = "mutation($a:Int){ToggleFavourite(animeId:$a){anime{nodes{id}}}}"
    return _gql(q, {"a": media_id}, token)

def get_viewer(token):
    res = _gql("query{Viewer{id name}}", token=token)
    return res.get("data", {}).get("Viewer")

def get_activities(user_id, page=1, per_page=8):
    q = """query($u:Int,$p:Int,$n:Int){Page(page:$p,perPage:$n){
    activities(userId:$u,type:MEDIA_LIST,sort:ID_DESC){...on ListActivity{
    status progress createdAt media{id title{romaji english} coverImage{large} siteUrl}}}}}"""
    res = _gql(q, {"u": user_id, "p": page, "n": per_page})
    return res.get("data", {}).get("Page", {}).get("activities", [])

def get_stats(username):
    q = """query($n:String){User(name:$n){statistics{anime{count meanScore minutesWatched
    episodesWatched genres(limit:5,sort:COUNT_DESC){genre count meanScore}
    studios(limit:3,sort:COUNT_DESC){studio{name}count}} manga{count meanScore chaptersRead}}}}"""
    res = _gql(q, {"n": username})
    return res.get("data", {}).get("User", {}).get("statistics")

def get_profile(username):
    q = """query($n:String){User(name:$n){id name about avatar{large} bannerImage siteUrl
    statistics{anime{count meanScore episodesWatched} manga{count meanScore chaptersRead}}}}"""
    res = _gql(q, {"n": username})
    return res.get("data", {}).get("User")

def get_media_list(username=None, user_id=None, media_type="ANIME", status=None, sort=None, page=1, per_page=10):
    q = """query($un:String,$uid:Int,$t:MediaType,$st:MediaListStatus,$so:[MediaListSort],$p:Int,$n:Int){
    Page(page:$p,perPage:$n){mediaList(userName:$un,userId:$uid,type:$t,status:$st,sort:$so){
    id status score progress media{id title{romaji english} episodes chapters genres
    averageScore coverImage{large} siteUrl}}}}"""
    v = {"t": media_type, "p": page, "n": per_page}
    if username: v["un"] = username
    if user_id: v["uid"] = user_id
    if status: v["st"] = status
    if sort: v["so"] = sort
    res = _gql(q, v)
    return res.get("data", {}).get("Page", {}).get("mediaList", [])

def get_recommendations(media_id, per_page=6):
    q = """query($m:Int,$n:Int){Page(perPage:$n){recommendations(mediaId:$m,sort:RATING_DESC){
    mediaRecommendation{id title{romaji english} coverImage{large} averageScore siteUrl}}}}"""
    res = _gql(q, {"m": media_id, "n": per_page})
    recs = res.get("data", {}).get("Page", {}).get("recommendations", [])
    return [r["mediaRecommendation"] for r in recs if r.get("mediaRecommendation")]

def get_trending(per_page=8):
    q = """query($n:Int){Page(perPage:$n){media(type:ANIME,sort:TRENDING_DESC){
    id title{romaji english} coverImage{large} averageScore siteUrl}}}"""
    res = _gql(q, {"n": per_page})
    return res.get("data", {}).get("Page", {}).get("media", [])

def get_seasonal(season, year, per_page=8):
    q = """query($s:MediaSeason,$y:Int,$n:Int){Page(perPage:$n){
    media(type:ANIME,season:$s,seasonYear:$y,sort:POPULARITY_DESC){
    id title{romaji english} coverImage{large} averageScore siteUrl}}}"""
    res = _gql(q, {"s": season, "y": year, "n": per_page})
    return res.get("data", {}).get("Page", {}).get("media", [])

def get_airing(media_id):
    q = """query($m:Int){AiringSchedule(mediaId:$m,notYetAired:true){airingAt timeUntilAiring episode}}"""
    res = _gql(q, {"m": media_id})
    return res.get("data", {}).get("AiringSchedule")

def get_relations(media_id):
    q = """query($m:Int){Media(id:$m){relations{edges{relationType(version:2)
    node{id title{romaji english} type format status coverImage{large} siteUrl}}}}}"""
    res = _gql(q, {"m": media_id})
    return res.get("data", {}).get("Media", {}).get("relations", {}).get("edges", [])

def get_favorites(username):
    q = """query($n:String){User(name:$n){favourites{anime{nodes{id title{romaji english}
    coverImage{large} siteUrl}} manga{nodes{id title{romaji english} coverImage{large}}}}}}"""
    res = _gql(q, {"n": username})
    fav = res.get("data", {}).get("User", {}).get("favourites", {})
    return fav.get("anime", {}).get("nodes", []), fav.get("manga", {}).get("nodes", [])

def search_character_q(name):
    q = """query($s:String){Character(search:$s){name{full native} siteUrl image{large}
    media(perPage:3,sort:POPULARITY_DESC){nodes{title{romaji english}}}}}"""
    res = _gql(q, {"s": name})
    return res.get("data", {}).get("Character")

def search_staff_q(name):
    q = """query($s:String){Staff(search:$s){name{full native} image{large} primaryOccupations
    characters(perPage:5,sort:FAVOURITES_DESC){nodes{name{full}}}
    staffMedia(perPage:5,sort:POPULARITY_DESC){nodes{title{romaji}type}}}}"""
    res = _gql(q, {"s": name})
    return res.get("data", {}).get("Staff")

def search_studio_q(name):
    q = """query($s:String){Studio(search:$s){name isAnimationStudio siteUrl
    media(perPage:8,sort:POPULARITY_DESC){nodes{id title{romaji english} averageScore coverImage{large}}}}}"""
    res = _gql(q, {"s": name})
    return res.get("data", {}).get("Studio")

def current_season():
    m = datetime.datetime.now().month
    y = datetime.datetime.now().year
    if m <= 3: return "WINTER", y
    elif m <= 6: return "SPRING", y
    elif m <= 9: return "SUMMER", y
    else: return "FALL", y

# ============================================================
# [4] TELEGRAM CLIENT
# ============================================================
def _tg(method, payload):
    if not TELEGRAM_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[TG Error] {method}: {e}")
        return None

def tg_send(chat_id, text, kb=None):
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if kb: p["reply_markup"] = kb
    return _tg("sendMessage", p)

def tg_photo(chat_id, url, caption, kb=None):
    if not url:
        return tg_send(chat_id, caption, kb)
    p = {"chat_id": chat_id, "photo": url, "caption": caption[:1024], "parse_mode": "HTML"}
    if kb: p["reply_markup"] = kb
    res = _tg("sendPhoto", p)
    if not res or not res.get("ok"):
        return tg_send(chat_id, caption, kb)
    return res

def tg_album(chat_id, items):
    """items: list of {"url": ..., "caption": ...}"""
    if not items:
        return None
    if len(items) == 1:
        return tg_photo(chat_id, items[0].get("url"), items[0].get("caption", ""))
    media = []
    for i, item in enumerate(items[:10]):
        m = {"type": "photo", "media": item["url"], "parse_mode": "HTML"}
        if item.get("caption"):
            m["caption"] = item["caption"][:1024]
        media.append(m)
    return _tg("sendMediaGroup", {"chat_id": chat_id, "media": media})

def tg_answer_cb(cb_id, text=None):
    p = {"callback_query_id": cb_id}
    if text: p["text"] = text
    return _tg("answerCallbackQuery", p)

def tg_edit(chat_id, msg_id, text, kb=None):
    p = {"chat_id": chat_id, "message_id": msg_id, "text": text, "parse_mode": "HTML"}
    if kb: p["reply_markup"] = kb
    return _tg("editMessageText", p)

def kb_make(rows):
    """rows: [[{"text":..,"callback_data":..,"style":..},...],...]"""
    keyboard = []
    for row in rows:
        kr = []
        for b in row:
            btn = {"text": b["text"]}
            if "url" in b: btn["url"] = b["url"]
            elif "callback_data" in b: btn["callback_data"] = b["callback_data"]
            if "style" in b: btn["style"] = b["style"]
            kr.append(btn)
        keyboard.append(kr)
    return {"inline_keyboard": keyboard}

def title_of(media):
    if not media or "title" not in media: return "Unknown"
    t = media["title"]
    return t.get("english") or t.get("romaji") or "Unknown"

def cover_of(media):
    if not media: return None
    ci = media.get("coverImage", {})
    return ci.get("extraLarge") or ci.get("large") or ci.get("medium")

# ============================================================
# [5] ARABIC PARSER (regex fallback)
# ============================================================
ANIME_DICT = {
    "اللعنات": "Jujutsu Kaisen", "جوجوتسو": "Jujutsu Kaisen", "جوجو": "Jujutsu Kaisen",
    "هجوم العمالقة": "Attack on Titan", "العمالقة": "Attack on Titan", "اتاك": "Attack on Titan",
    "مذكرة الموت": "Death Note", "ديث نوت": "Death Note",
    "قاتل الشياطين": "Demon Slayer", "ديمون سلاير": "Demon Slayer", "كيميتسو": "Demon Slayer",
    "رجل المنشار": "Chainsaw Man", "تشينسو مان": "Chainsaw Man",
    "راجنا": "Ragna Crimson", "راغنا": "Ragna Crimson",
    "هنتر": "Hunter x Hunter", "القناص": "Hunter x Hunter",
    "ون بيس": "One Piece", "ناروتو": "Naruto", "دراغون بول": "Dragon Ball",
    "كونان": "Detective Conan", "المحقق كونان": "Detective Conan",
    "اكاديميتي للابطال": "My Hero Academia", "بوكو نو هيرو": "My Hero Academia",
    "طوكيو غول": "Tokyo Ghoul", "بليتش": "Bleach",
    "سولو ليفلينج": "Solo Leveling", "رفع المستوى فرديا": "Solo Leveling",
    "بلاك كلوفر": "Black Clover", "فيري تيل": "Fairy Tail",
}
GENRE_MAP = {
    "أكشن": "Action", "اكشن": "Action", "مغامرة": "Adventure", "كوميدي": "Comedy",
    "دراما": "Drama", "خيالي": "Fantasy", "خيال": "Fantasy", "غموض": "Mystery",
    "رعب": "Horror", "رومانسي": "Romance", "رومانسية": "Romance",
    "خيال علمي": "Sci-Fi", "رياضي": "Sports", "نفسي": "Psychological",
    "شريحة من الحياة": "Slice of Life", "إثارة": "Thriller",
}

def regex_parse(text):
    r = {"action": "TRACK", "media_type": "ANIME", "title": None, "status": None,
         "score": None, "progress_delta": None, "absolute_progress": None,
         "is_favorite": False, "genre": None, "friend_username": None,
         "original_text": text, "confidence": "low", "alternatives": [],
         "batch": None, "chat_response": None}
    lt = text.strip().lower()
    # Detect special actions first
    if re.search(r'(احذف|شيل|امسح|ازل)', lt): r["action"] = "DELETE"
    elif re.search(r'(إحصائيات|احصائيات)', lt): r["action"] = "STATS"
    elif re.search(r'(نشاطات|آخر نشاط|اخر شي سويت|اخر نشاط)', lt): r["action"] = "ACTIVITY"
    elif re.search(r'(الترند|ترند|ترندنق|الشائع)', lt): r["action"] = "TRENDING"
    elif re.search(r'(هذا الموسم|موسم الحالي|انميات الموسم)', lt): r["action"] = "SEASONAL"
    elif re.search(r'(فاجئني|فاجأني|اعمل اي شي|اي شي عشوائي|ملّيت|مليت)', lt): r["action"] = "SURPRISE"
    elif re.search(r'(خبر حلو|أخبار|اخبار|وش الجديد)', lt): r["action"] = "NEWS"
    elif re.search(r'(مفضلاتي|قائمة المفضلة|وش مفضلاتي)', lt): r["action"] = "FAVORITES_LIST"
    elif re.search(r'(اقتر[حا]|ترشيح|رشح|أنمي حلو|انمي ممتاز)', lt): r["action"] = "RECOMMEND_GENRE"
    elif re.search(r'(مشابه|شبيه|زي |يشبه)', lt): r["action"] = "RECOMMEND_SIMILAR"
    elif re.search(r'(شخصية|مين مؤدي|مؤدي صوت)', lt): r["action"] = "CHARACTER_LOOKUP"
    elif re.search(r'(استديو|ستوديو|studio)', lt): r["action"] = "STUDIO_LOOKUP"
    elif re.search(r'(سيكويل|بريكويل|تتمة|الجزء الثاني)', lt): r["action"] = "RELATIONS"
    elif re.search(r'(متى الحلقة|موعد|جدول البث|الحلقة الجاية|الحلقه الجايه)', lt): r["action"] = "AIRING_SCHEDULE"
    elif re.search(r'(بروفايل|حساب)\s+\w+', lt): r["action"] = "FRIEND_PROFILE"
    elif re.search(r'(انميات|قائمة)\s+\w+', lt) and not re.search(r'(انمياتي|قائمتي)', lt): r["action"] = "FRIEND_LIST"
    elif re.search(r'(قارن|مقارنة)', lt): r["action"] = "COMPARE_FRIEND"
    # Favorite
    if re.search(r'(مفضلة|مفضلتي|فيفريت|favourite|favorite)', lt):
        r["is_favorite"] = True
    # Score
    sm = re.search(r'(?:اقيمه|أقيمه|قيمه|تقييم|تقييمه?|score)\s*(\d+(?:\.\d+)?)', lt)
    if not sm: sm = re.search(r'(\d+(?:\.\d+)?)\s*(?:من\s*10|/10)', lt)
    if sm:
        v = float(sm.group(1))
        if 0 <= v <= 10: r["score"] = v
    # Status — COMPLETED first!
    if re.search(r'(كملت|أكملت|اكملت|كمّلت|خلصت|أنهيت|نهيت|ختمت|انتهيت|كل حلقاته|كل حلقات|شفت كامل|شفته كامل|كل الحلقات)', lt):
        r["status"] = "COMPLETED"
    elif re.search(r'(سحبت|تركته|كنسلت|dropped)', lt): r["status"] = "DROPPED"
    elif re.search(r'(خطة|أفكر|اضف|أضف|بشوفه|بقراه|plan)', lt): r["status"] = "PLANNING"
    elif re.search(r'(وقفت|توقفت|paused|معلق)', lt): r["status"] = "PAUSED"
    elif re.search(r'(شفت|تابع|تابعت|شاهدت|قريت|قرأت)', lt): r["status"] = "CURRENT"
    # Progress
    if "حلقتين" in lt: r["progress_delta"] = 2; r["status"] = r["status"] or "CURRENT"
    elif "فصلين" in lt: r["progress_delta"] = 2; r["status"] = r["status"] or "CURRENT"
    else:
        ep = re.search(r'(\d+)\s*(?:حلقة|حلقات|فصل|فصول|شابتر|ep)', lt)
        if ep: r["progress_delta"] = int(ep.group(1)); r["status"] = r["status"] or "CURRENT"
    abs_m = re.search(r'(?:حلقة|فصل|شابتر|ep|ch)\s*(\d+)', lt)
    if abs_m: r["absolute_progress"] = int(abs_m.group(1))
    # Manga
    if re.search(r'(مانجا|مانها|فصل|فصول|شابتر|chapter|manga)', lt): r["media_type"] = "MANGA"
    # Title from dictionary
    for nick, official in ANIME_DICT.items():
        if nick in lt: r["title"] = official; break
    # Genre
    for g_ar, g_en in GENRE_MAP.items():
        if g_ar in lt: r["genre"] = g_en; break
    return r

# ============================================================
# [6] GEMINI AI PARSER v2
# ============================================================
GEMINI_SYSTEM_PROMPT = """You are an expert AniList anime & manga assistant understanding Arabic, English, Arabizi, and typos.

CRITICAL STATUS RULES:
- 'كملت'/'أكملت'/'خلصت'/'شفت كامل'/'شفت كل حلقاته'/'انهيت' = COMPLETED (NOT CURRENT!)
- 'شفت الأنمي' without 'حلقة/حلقات' = COMPLETED
- 'شفت حلقتين من X' = CURRENT with progress_delta=2
- 'شفت 5 حلقات' = CURRENT with progress_delta=5
- When COMPLETED and no episode count: set progress_delta to null (system auto-fills)
- NEVER set dates unless user explicitly mentions them
- If user says 'ه'/'ها'/'هذا' without a title, set title to null (system resolves from context)

Return ONLY valid JSON with these keys:
- action: TRACK, DELETE, STATS, ACTIVITY, MY_LIST, FRIEND_PROFILE, FRIEND_LIST, FRIEND_ACTIVITY, COMPARE_FRIEND, RECOMMEND_GENRE, RECOMMEND_SIMILAR, RECOMMEND_FROM_LIST, TRENDING, SEASONAL, TOP_RATED, RANDOM_ANIME, CHARACTER_LOOKUP, STAFF_LOOKUP, STUDIO_LOOKUP, RELATIONS, AIRING_SCHEDULE, FAVORITES_LIST, FAVORITES_ADD, FAVORITES_REMOVE, BATCH_TRACK, SURPRISE, NEWS, CHAT
- media_type: ANIME or MANGA
- title: official English/Romaji title or null
- status: COMPLETED, CURRENT, PLANNING, PAUSED, DROPPED or null
- score: number 0-10 or null
- progress_delta: integer or null
- absolute_progress: integer or null
- is_favorite: boolean
- genre: genre string or null
- friend_username: AniList username or null
- confidence: high, medium, or low
- alternatives: [] list of alternative titles
- batch: [{title,status,score,progress_delta},...] for BATCH_TRACK or null
- chat_response: Arabic response string for CHAT action or null"""

def parse_with_gemini(text, chat_id=None):
    key = GEMINI_KEY
    if not key:
        return regex_parse(text)
    context = build_gemini_context(chat_id) if chat_id else ""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={key}"
    payload = {
        "contents": [{"parts": [{"text": f"Context:\n{context}\n\nUser message: {text}"}]}],
        "systemInstruction": {"parts": [{"text": GEMINI_SYSTEM_PROMPT}]},
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            content = res["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(content)
            parsed["original_text"] = text
            return parsed
    except Exception as e:
        print(f"[Gemini] Error: {e}")
        return regex_parse(text)

# ============================================================
# [7] HANDLER CLASS
# ============================================================
class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        status = {"status": "running", "bot": "AniList Bot v2.0",
                  "telegram": "SET" if TELEGRAM_TOKEN else "MISSING",
                  "anilist": "SET" if ANILIST_TOKEN else "MISSING",
                  "gemini": "SET" if GEMINI_KEY else "MISSING"}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(status, indent=2).encode("utf-8"))

    def do_POST(self):
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            update = json.loads(body.decode("utf-8"))
        except Exception:
            self.send_response(200); self.end_headers(); return

        try:
            if "callback_query" in update:
                self._on_callback(update["callback_query"])
            elif "message" in update:
                msg = update["message"]
                chat_id = msg.get("chat", {}).get("id")
                text = (msg.get("text") or "").strip()
                if not text or not chat_id:
                    pass
                elif text.startswith("/start"):
                    self._welcome(chat_id)
                elif text.startswith("/help"):
                    self._help(chat_id)
                else:
                    save_context(chat_id, "user", text)
                    parsed = parse_with_gemini(text, chat_id)
                    self._route(chat_id, parsed)
        except Exception as e:
            print(f"[POST Error] {e}")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    # ── Routing ──
    def _route(self, cid, p):
        a = p.get("action", "TRACK")
        # Resolve title from context if missing
        if not p.get("title") and a in ("TRACK","DELETE","RECOMMEND_SIMILAR","RELATIONS","AIRING_SCHEDULE","FAVORITES_ADD","FAVORITES_REMOVE"):
            p["title"] = self._ctx_title(cid)
        try:
            m = {
                "TRACK": self._track, "DELETE": self._delete, "STATS": self._stats,
                "ACTIVITY": self._activity, "MY_LIST": self._mylist,
                "FRIEND_PROFILE": self._friend_profile, "FRIEND_LIST": self._friend_list,
                "FRIEND_ACTIVITY": self._friend_activity, "COMPARE_FRIEND": self._compare,
                "RECOMMEND_GENRE": self._rec_genre, "RECOMMEND_SIMILAR": self._rec_similar,
                "RECOMMEND_FROM_LIST": self._rec_from_list, "TRENDING": self._trending,
                "SEASONAL": self._seasonal, "TOP_RATED": self._top_rated,
                "RANDOM_ANIME": self._random_anime,
                "CHARACTER_LOOKUP": self._character, "STAFF_LOOKUP": self._staff,
                "STUDIO_LOOKUP": self._studio, "RELATIONS": self._relations,
                "AIRING_SCHEDULE": self._airing, "FAVORITES_LIST": self._fav_list,
                "FAVORITES_ADD": self._fav_add, "FAVORITES_REMOVE": self._fav_remove,
                "BATCH_TRACK": self._batch, "SURPRISE": self._surprise,
                "NEWS": self._news, "CHAT": self._chat,
            }
            handler_fn = m.get(a, self._chat)
            handler_fn(cid, p)
        except Exception as e:
            tg_send(cid, f"❌ حدث خطأ: {str(e)[:200]}")

    def _ctx_title(self, cid):
        for msg in reversed(get_context(cid)):
            if msg.get("extra", {}).get("media_title"):
                return msg["extra"]["media_title"]
        return None

    # ── TRACK ──
    def _track(self, cid, p):
        title = p.get("title")
        if not title:
            tg_send(cid, "❌ لم أستطع تحديد اسم الأنمي. حاول مرة أخرى.")
            return
        mt = p.get("media_type", "ANIME")
        candidates = search_media(title, mt)
        if not candidates:
            tg_send(cid, f"❌ لم أجد نتائج لـ: <b>{title}</b>")
            return
        # Disambiguation
        if len(candidates) > 1 and p.get("confidence") == "medium":
            rows = []
            for i, c in enumerate(candidates[:4]):
                n = title_of(c)
                yr = f" ({c.get('seasonYear','')})" if c.get("seasonYear") else ""
                rows.append([{"text": f"{n}{yr}", "callback_data": f"pick:{c['id']}:{p.get('status','COMPLETED')}:{p.get('score','') }:{p.get('is_favorite',False)}"}])
            tg_send(cid, f"🤔 وجدت عدة نتائج لـ <b>{title}</b>، اختر:", kb_make(rows))
            return
        media = candidates[0]
        self._do_track(cid, media, p)

    def _do_track(self, cid, media, p):
        mid = media["id"]
        status = p.get("status") or "COMPLETED"
        score = p.get("score")
        mt = p.get("media_type", "ANIME")
        # Calculate progress
        existing = get_entry(mid, ANILIST_TOKEN) if ANILIST_TOKEN else None
        cur_prog = (existing or {}).get("progress", 0) or 0
        total = media.get("episodes") or media.get("chapters") or 0
        new_prog = cur_prog
        if p.get("absolute_progress") is not None:
            new_prog = p["absolute_progress"]
        elif p.get("progress_delta") is not None:
            new_prog = cur_prog + p["progress_delta"]
        elif status == "COMPLETED" and total > 0:
            new_prog = total
        # Save
        if ANILIST_TOKEN:
            res = save_entry(mid, ANILIST_TOKEN, status=status, score=score, progress=new_prog)
            if isinstance(res, dict) and "error" in res:
                tg_send(cid, f"⚠️ خطأ AniList: {res['error'][:200]}")
                return
            if p.get("is_favorite"):
                toggle_fav(mid, ANILIST_TOKEN)
        name = title_of(media)
        unit = "الفصل" if mt == "MANGA" else "الحلقة"
        cap = f"✅ <b>تم التحديث بنجاح!</b>\n\n📺 <b>{name}</b>\n📌 الحالة: {status}\n🔢 {unit}: {new_prog}"
        if total: cap += f"/{total}"
        if score is not None: cap += f"\n⭐ التقييم: {score}/10"
        if p.get("is_favorite"): cap += "\n❤️ تمت إضافته للمفضلة!"
        trailer = media.get("trailer")
        if trailer and trailer.get("site") == "youtube":
            cap += f"\n🎬 <a href='https://youtube.com/watch?v={trailer['id']}'>تريلر</a>"
        kb = kb_make([[{"text": "🔗 AniList", "url": media.get("siteUrl", "https://anilist.co")}]])
        tg_photo(cid, cover_of(media), cap, kb)
        save_context(cid, "bot", f"تم تحديث {name}", extra={"media_title": name, "media_id": mid})

    # ── DELETE ──
    def _delete(self, cid, p):
        title = p.get("title")
        if not title: tg_send(cid, "❌ حدد الأنمي للحذف."); return
        candidates = search_media(title, p.get("media_type", "ANIME"))
        if not candidates: tg_send(cid, f"❌ لم أجد: <b>{title}</b>"); return
        media = candidates[0]
        mid = media["id"]
        entry = get_entry(mid, ANILIST_TOKEN) if ANILIST_TOKEN else None
        if not entry:
            tg_send(cid, f"❌ <b>{title_of(media)}</b> غير موجود في قائمتك.")
            return
        eid = entry["id"]
        name = title_of(media)
        cap = f"🗑️ هل تريد حذف <b>{name}</b> من قائمتك؟"
        kb = kb_make([
            [{"text": "✅ تأكيد الحذف", "callback_data": f"del:{eid}", "style": "danger"},
             {"text": "❌ إلغاء", "callback_data": "cancel", "style": "success"}]
        ])
        tg_photo(cid, cover_of(media), cap, kb)

    # ── STATS ──
    def _stats(self, cid, p):
        fn = p.get("friend_username")
        if fn:
            username = fn
        else:
            viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
            if not viewer: tg_send(cid, "❌ لم أستطع جلب حسابك. تأكد من ربط AniList."); return
            username = viewer["name"]
        stats = get_stats(username)
        if not stats: tg_send(cid, "❌ لم أجد إحصائيات."); return
        a = stats.get("anime", {})
        m = stats.get("manga", {})
        mins = a.get("minutesWatched", 0)
        days = mins // 1440
        hours = (mins % 1440) // 60
        genres = a.get("genres", [])
        studios = a.get("studios", [])
        top_genres = ", ".join([g["genre"] for g in genres[:3]]) or "—"
        top_studios = ", ".join([s["studio"]["name"] for s in studios[:3]]) or "—"
        cap = (f"📊 <b>إحصائيات {username}</b>\n\n"
               f"📺 <b>الأنمي:</b>\n"
               f"  • العدد: {a.get('count',0)}\n"
               f"  • الحلقات: {a.get('episodesWatched',0)}\n"
               f"  • وقت المشاهدة: {days} يوم و {hours} ساعة\n"
               f"  • متوسط التقييم: {a.get('meanScore',0)}/100\n"
               f"  • الأنواع المفضلة: {top_genres}\n"
               f"  • الاستديوهات المفضلة: {top_studios}\n\n"
               f"📖 <b>المانجا:</b>\n"
               f"  • العدد: {m.get('count',0)}\n"
               f"  • الفصول: {m.get('chaptersRead',0)}\n"
               f"  • متوسط التقييم: {m.get('meanScore',0)}/100")
        tg_send(cid, cap)
        save_context(cid, "bot", f"عرض إحصائيات {username}")

    # ── ACTIVITY ──
    def _activity(self, cid, p):
        viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
        if not viewer: tg_send(cid, "❌ لم أستطع جلب حسابك."); return
        acts = get_activities(viewer["id"])
        if not acts: tg_send(cid, "لا توجد نشاطات حديثة."); return
        items = []
        for act in acts[:8]:
            media = act.get("media")
            if not media: continue
            name = title_of(media)
            st = act.get("status", "")
            prog = act.get("progress", "")
            cap = f"<b>{name}</b>\n{st}"
            if prog: cap += f" {prog}"
            url = cover_of(media)
            if url: items.append({"url": url, "caption": cap})
        if items: tg_album(cid, items)
        else: tg_send(cid, "لا توجد نشاطات بصور.")
        save_context(cid, "bot", "عرض آخر النشاطات")

    # ── MY LIST ──
    def _mylist(self, cid, p):
        viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
        if not viewer: tg_send(cid, "❌ لم أستطع جلب حسابك."); return
        status = p.get("status")
        mt = p.get("media_type", "ANIME")
        entries = get_media_list(username=viewer["name"], media_type=mt, status=status, per_page=8)
        if not entries: tg_send(cid, "قائمتك فارغة بهذا الفلتر."); return
        items = []
        for e in entries[:8]:
            media = e.get("media", {})
            name = title_of(media)
            sc = e.get("score", 0)
            cap = f"<b>{name}</b>\n⭐ {sc}/10" if sc else f"<b>{name}</b>"
            url = cover_of(media)
            if url: items.append({"url": url, "caption": cap})
        if items: tg_album(cid, items)
        else: tg_send(cid, "لا توجد نتائج بصور.")

    # ── FRIEND ──
    def _friend_profile(self, cid, p):
        fn = p.get("friend_username")
        if not fn: tg_send(cid, "❌ حدد اسم المستخدم."); return
        user = get_profile(fn)
        if not user: tg_send(cid, f"❌ لم أجد مستخدم باسم: {fn}"); return
        a = user.get("statistics", {}).get("anime", {})
        m = user.get("statistics", {}).get("manga", {})
        cap = (f"👤 <b>{user['name']}</b>\n\n"
               f"📺 أنمي: {a.get('count',0)} | حلقات: {a.get('episodesWatched',0)}\n"
               f"📖 مانجا: {m.get('count',0)} | فصول: {m.get('chaptersRead',0)}\n"
               f"⭐ متوسط تقييم الأنمي: {a.get('meanScore',0)}/100")
        kb = kb_make([[{"text": "🔗 AniList", "url": user.get("siteUrl", "https://anilist.co")}]])
        avatar = user.get("avatar", {}).get("large")
        tg_photo(cid, avatar, cap, kb)

    def _friend_list(self, cid, p):
        fn = p.get("friend_username")
        if not fn: tg_send(cid, "❌ حدد اسم المستخدم."); return
        status = p.get("status", "COMPLETED")
        entries = get_media_list(username=fn, status=status, per_page=8)
        if not entries: tg_send(cid, f"لا توجد نتائج لـ {fn}."); return
        items = []
        for e in entries[:8]:
            media = e.get("media", {})
            name = title_of(media)
            sc = e.get("score", 0)
            cap = f"<b>{name}</b>" + (f"\n⭐ {sc}" if sc else "")
            url = cover_of(media)
            if url: items.append({"url": url, "caption": cap})
        if items: tg_album(cid, items)

    def _friend_activity(self, cid, p):
        fn = p.get("friend_username")
        if not fn: tg_send(cid, "❌ حدد اسم المستخدم."); return
        user = get_profile(fn)
        if not user: tg_send(cid, f"❌ لم أجد: {fn}"); return
        acts = get_activities(user["id"])
        if not acts: tg_send(cid, f"لا نشاطات حديثة لـ {fn}."); return
        items = []
        for act in acts[:8]:
            media = act.get("media")
            if not media: continue
            cap = f"<b>{title_of(media)}</b>\n{act.get('status','')} {act.get('progress','')}"
            url = cover_of(media)
            if url: items.append({"url": url, "caption": cap})
        if items: tg_album(cid, items)

    # ── COMPARE ──
    def _compare(self, cid, p):
        fn = p.get("friend_username")
        if not fn: tg_send(cid, "❌ حدد اسم صديقك للمقارنة."); return
        viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
        if not viewer: tg_send(cid, "❌ لم أستطع جلب حسابك."); return
        my_list = get_media_list(username=viewer["name"], status="COMPLETED", per_page=50)
        fr_list = get_media_list(username=fn, status="COMPLETED", per_page=50)
        my_ids = {e["media"]["id"]: e for e in my_list if e.get("media")}
        fr_ids = {e["media"]["id"]: e for e in fr_list if e.get("media")}
        shared = set(my_ids) & set(fr_ids)
        only_me = set(my_ids) - set(fr_ids)
        only_fr = set(fr_ids) - set(my_ids)
        cap = (f"🆚 <b>مقارنة: {viewer['name']} vs {fn}</b>\n\n"
               f"🤝 مشترك: {len(shared)} أنمي\n"
               f"👤 عندك فقط: {len(only_me)} أنمي\n"
               f"👥 عند {fn} فقط: {len(only_fr)} أنمي\n")
        if shared:
            cap += "\n<b>تقييمات مشتركة:</b>\n"
            for sid in list(shared)[:5]:
                name = title_of(my_ids[sid]["media"])
                ms = my_ids[sid].get("score", 0)
                fs = fr_ids[sid].get("score", 0)
                cap += f"• {name}: أنت {ms} | {fn} {fs}\n"
        tg_send(cid, cap)

    # ── RECOMMENDATIONS ──
    def _rec_genre(self, cid, p):
        genre = p.get("genre", "Action")
        trending = get_trending(8)
        filtered = [t for t in trending if genre.lower() in [g.lower() for g in (t.get("genres") or [])]] if trending else []
        results = filtered if filtered else trending[:5]
        if not results: tg_send(cid, f"❌ لا توجد اقتراحات لتصنيف {genre}"); return
        items = [{"url": cover_of(r), "caption": f"<b>{title_of(r)}</b>\n⭐ {r.get('averageScore',0)}%"} for r in results[:6] if cover_of(r)]
        if items: tg_album(cid, items)
        else: tg_send(cid, "لا توجد نتائج بصور.")

    def _rec_similar(self, cid, p):
        title = p.get("title")
        if not title: tg_send(cid, "❌ حدد الأنمي لإيجاد أنمي مشابه."); return
        candidates = search_media(title)
        if not candidates: tg_send(cid, f"❌ لم أجد: {title}"); return
        recs = get_recommendations(candidates[0]["id"])
        if not recs: tg_send(cid, f"❌ لا توجد توصيات مشابهة لـ <b>{title}</b>"); return
        items = [{"url": cover_of(r), "caption": f"<b>{title_of(r)}</b>\n⭐ {r.get('averageScore',0)}%"} for r in recs[:6] if cover_of(r)]
        if items: tg_album(cid, items)

    def _rec_from_list(self, cid, p):
        viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
        if not viewer: tg_send(cid, "❌ لم أستطع جلب حسابك."); return
        entries = get_media_list(username=viewer["name"], status="COMPLETED", sort=["SCORE_DESC"], per_page=8)
        if not entries: tg_send(cid, "قائمتك المكتملة فارغة."); return
        items = [{"url": cover_of(e["media"]), "caption": f"<b>{title_of(e['media'])}</b>\n⭐ {e.get('score',0)}"} for e in entries if e.get("media") and cover_of(e["media"])]
        if items: tg_album(cid, items[:8])

    # ── TRENDING / SEASONAL / TOP ──
    def _trending(self, cid, p):
        results = get_trending(8)
        if not results: tg_send(cid, "❌ لم أستطع جلب الترند."); return
        items = [{"url": cover_of(r), "caption": f"<b>{title_of(r)}</b>\n⭐ {r.get('averageScore',0)}%"} for r in results if cover_of(r)]
        if items: tg_album(cid, items)
        save_context(cid, "bot", "عرض الترند")

    def _seasonal(self, cid, p):
        s, y = current_season()
        results = get_seasonal(s, y, 8)
        if not results: tg_send(cid, f"❌ لا أنميات لموسم {s} {y}"); return
        items = [{"url": cover_of(r), "caption": f"<b>{title_of(r)}</b>\n⭐ {r.get('averageScore',0)}%"} for r in results if cover_of(r)]
        if items: tg_album(cid, items)

    def _top_rated(self, cid, p):
        q = """query{Page(perPage:8){media(type:ANIME,sort:SCORE_DESC){
        id title{romaji english} coverImage{large} averageScore siteUrl}}}"""
        res = _gql(q)
        media_list = res.get("data", {}).get("Page", {}).get("media", [])
        items = [{"url": cover_of(m), "caption": f"<b>{title_of(m)}</b>\n⭐ {m.get('averageScore',0)}%"} for m in media_list if cover_of(m)]
        if items: tg_album(cid, items)
        else: tg_send(cid, "❌ فشل في جلب الأعلى تقييماً.")

    def _random_anime(self, cid, p):
        genre = p.get("genre", random.choice(["Action","Adventure","Comedy","Drama","Fantasy","Mystery","Romance","Sci-Fi"]))
        page = random.randint(1, 10)
        q = """query($g:String,$p:Int){Page(page:$p,perPage:1){media(type:ANIME,genre:$g,sort:POPULARITY_DESC){
        id title{romaji english} coverImage{large} averageScore siteUrl episodes}}}"""
        res = _gql(q, {"g": genre, "p": page})
        media_list = res.get("data", {}).get("Page", {}).get("media", [])
        if not media_list: tg_send(cid, "❌ لم أجد أنمي عشوائي. جرب مرة ثانية!"); return
        m = media_list[0]
        cap = f"🎲 <b>أنمي عشوائي ({genre}):</b>\n\n<b>{title_of(m)}</b>\n⭐ {m.get('averageScore',0)}%\n📺 {m.get('episodes','?')} حلقة"
        kb = kb_make([[{"text": "🔗 AniList", "url": m.get("siteUrl", "https://anilist.co")}]])
        tg_photo(cid, cover_of(m), cap, kb)

    # ── CHARACTER / STAFF / STUDIO ──
    def _character(self, cid, p):
        name = p.get("title")
        if not name: tg_send(cid, "❌ حدد اسم الشخصية."); return
        ch = search_character_q(name)
        if not ch: tg_send(cid, f"❌ لم أجد شخصية: {name}"); return
        full = ch["name"]["full"]
        native = ch["name"].get("native", "")
        anime = "غير محدد"
        if ch.get("media", {}).get("nodes"):
            anime = title_of(ch["media"]["nodes"][0])
        cap = f"👤 <b>{full}</b> ({native})\n\n📺 من: <b>{anime}</b>"
        kb = kb_make([[{"text": "🔗 AniList", "url": ch.get("siteUrl", "")}]])
        tg_photo(cid, ch.get("image", {}).get("large"), cap, kb)

    def _staff(self, cid, p):
        name = p.get("title")
        if not name: tg_send(cid, "❌ حدد اسم الشخص."); return
        st = search_staff_q(name)
        if not st: tg_send(cid, f"❌ لم أجد: {name}"); return
        full = st["name"]["full"]
        native = st["name"].get("native", "")
        jobs = ", ".join(st.get("primaryOccupations", [])[:3]) or "—"
        chars = ", ".join([c["name"]["full"] for c in (st.get("characters", {}).get("nodes", []))[:3]]) or "—"
        cap = f"🎙️ <b>{full}</b> ({native})\n\n💼 المهنة: {jobs}\n🎭 شخصيات: {chars}"
        tg_photo(cid, st.get("image", {}).get("large"), cap)

    def _studio(self, cid, p):
        name = p.get("title")
        if not name: tg_send(cid, "❌ حدد اسم الاستديو."); return
        studio = search_studio_q(name)
        if not studio: tg_send(cid, f"❌ لم أجد استديو: {name}"); return
        works = studio.get("media", {}).get("nodes", [])
        if not works: tg_send(cid, f"لا أعمال لاستديو {studio['name']}"); return
        items = [{"url": cover_of(w), "caption": f"<b>{title_of(w)}</b>\n⭐ {w.get('averageScore',0)}%"} for w in works[:8] if cover_of(w)]
        if items:
            tg_send(cid, f"🏢 <b>أعمال {studio['name']}:</b>")
            tg_album(cid, items)

    # ── RELATIONS ──
    def _relations(self, cid, p):
        title = p.get("title")
        if not title: tg_send(cid, "❌ حدد الأنمي."); return
        candidates = search_media(title)
        if not candidates: tg_send(cid, f"❌ لم أجد: {title}"); return
        edges = get_relations(candidates[0]["id"])
        if not edges: tg_send(cid, f"❌ لا علاقات لـ <b>{title}</b>"); return
        TYPE_AR = {"SEQUEL":"تتمة","PREQUEL":"جزء سابق","SIDE_STORY":"قصة جانبية",
                   "ADAPTATION":"اقتباس","SPIN_OFF":"سبن أوف","PARENT":"العمل الأصل",
                   "CHARACTER":"شخصية مشتركة","SUMMARY":"ملخص","ALTERNATIVE":"بديل"}
        items = []
        for e in edges[:8]:
            node = e.get("node", {})
            rel = TYPE_AR.get(e.get("relationType", ""), e.get("relationType", ""))
            cap = f"<b>{title_of(node)}</b>\n🔗 {rel}"
            url = cover_of(node)
            if url: items.append({"url": url, "caption": cap})
        if items: tg_album(cid, items)

    # ── AIRING ──
    def _airing(self, cid, p):
        title = p.get("title")
        if not title: tg_send(cid, "❌ حدد الأنمي."); return
        candidates = search_media(title)
        if not candidates: tg_send(cid, f"❌ لم أجد: {title}"); return
        media = candidates[0]
        schedule = get_airing(media["id"])
        if not schedule:
            tg_send(cid, f"📺 <b>{title_of(media)}</b> منتهي أو غير مجدول حالياً.")
            return
        secs = schedule.get("timeUntilAiring", 0)
        ep = schedule.get("episode", "?")
        days = secs // 86400; hours = (secs % 86400) // 3600; mins = (secs % 3600) // 60
        parts = []
        if days: parts.append(f"{days} يوم")
        if hours: parts.append(f"{hours} ساعة")
        if mins: parts.append(f"{mins} دقيقة")
        countdown = " و ".join(parts) or "أقل من دقيقة"
        cap = f"📺 <b>{title_of(media)}</b>\n\n⏱️ الحلقة {ep} بعد: <b>{countdown}</b>"
        tg_photo(cid, cover_of(media), cap)

    # ── FAVORITES ──
    def _fav_list(self, cid, p):
        viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
        if not viewer: tg_send(cid, "❌ لم أستطع جلب حسابك."); return
        anime_favs, manga_favs = get_favorites(viewer["name"])
        all_favs = anime_favs + manga_favs
        if not all_favs: tg_send(cid, "قائمة مفضلاتك فارغة."); return
        items = [{"url": cover_of(f), "caption": f"<b>{title_of(f)}</b> ❤️"} for f in all_favs[:8] if cover_of(f)]
        if items: tg_album(cid, items)

    def _fav_add(self, cid, p):
        title = p.get("title")
        if not title: tg_send(cid, "❌ حدد الأنمي."); return
        candidates = search_media(title)
        if not candidates: tg_send(cid, f"❌ لم أجد: {title}"); return
        media = candidates[0]
        if ANILIST_TOKEN: toggle_fav(media["id"], ANILIST_TOKEN)
        tg_photo(cid, cover_of(media), f"❤️ تم تبديل حالة المفضلة لـ <b>{title_of(media)}</b>")

    def _fav_remove(self, cid, p):
        self._fav_add(cid, p)  # Toggle works both ways

    # ── BATCH ──
    def _batch(self, cid, p):
        batch = p.get("batch", [])
        if not batch: tg_send(cid, "❌ لم أجد عناصر لتحديثها."); return
        items = []
        for entry in batch[:5]:
            title = entry.get("title")
            if not title: continue
            candidates = search_media(title)
            if not candidates: continue
            media = candidates[0]
            mid = media["id"]
            status = entry.get("status", "COMPLETED")
            score = entry.get("score")
            total = media.get("episodes") or media.get("chapters") or 0
            prog = total if status == "COMPLETED" and total > 0 else entry.get("progress_delta")
            if ANILIST_TOKEN:
                save_entry(mid, ANILIST_TOKEN, status=status, score=score, progress=prog)
            name = title_of(media)
            cap = f"<b>{name}</b> ✅\n{status}"
            if score: cap += f" | ⭐ {score}"
            url = cover_of(media)
            if url: items.append({"url": url, "caption": cap})
        if items:
            tg_send(cid, f"✅ تم تحديث {len(items)} عنصر:")
            tg_album(cid, items)
        else:
            tg_send(cid, "❌ فشل في تحديث العناصر.")

    # ── SURPRISE ──
    def _surprise(self, cid, p):
        mode = random.choice(["rec", "flashback", "stat", "random"])
        viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
        if mode == "flashback" and viewer:
            entries = get_media_list(username=viewer["name"], status="COMPLETED", per_page=50)
            if entries:
                e = random.choice(entries)
                media = e.get("media", {})
                sc = e.get("score", 0)
                cap = f"💭 <b>فلاش باك!</b>\n\n<b>{title_of(media)}</b>\nقيّمته: ⭐ {sc}/10\n\nتتذكر هذا الأنمي؟ 🤔"
                tg_photo(cid, cover_of(media), cap)
                return
        if mode == "stat" and viewer:
            stats = get_stats(viewer["name"])
            if stats:
                a = stats.get("anime", {})
                mins = a.get("minutesWatched", 0)
                eps = a.get("episodesWatched", 0)
                days = mins // 1440
                tg_send(cid, f"🤯 <b>هل تعلم؟</b>\n\nشاهدت <b>{eps}</b> حلقة أنمي!\nهذا يعادل <b>{days} يوم</b> متواصل من المشاهدة! 📺")
                return
        if mode == "rec":
            trending = get_trending(10)
            if trending:
                pick = random.choice(trending)
                cap = f"🔥 <b>جرب هذا!</b>\n\n<b>{title_of(pick)}</b>\n⭐ {pick.get('averageScore',0)}%\n\nأنمي ترند الحين!"
                kb = kb_make([[{"text": "🔗 AniList", "url": pick.get("siteUrl", "")}]])
                tg_photo(cid, cover_of(pick), cap, kb)
                return
        self._random_anime(cid, p)

    # ── NEWS ──
    def _news(self, cid, p):
        viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
        news_items = []
        if viewer:
            current_list = get_media_list(username=viewer["name"], status="CURRENT", per_page=5)
            for e in current_list[:3]:
                media = e.get("media", {})
                schedule = get_airing(media.get("id"))
                if schedule:
                    ep = schedule.get("episode", "?")
                    secs = schedule.get("timeUntilAiring", 0)
                    d = secs // 86400
                    name = title_of(media)
                    news_items.append(f"📺 <b>{name}</b> — الحلقة {ep} بعد {d} يوم")
        trending = get_trending(5)
        for t in trending[:2]:
            news_items.append(f"🔥 ترند: <b>{title_of(t)}</b> — ⭐ {t.get('averageScore',0)}%")
        if news_items:
            tg_send(cid, "📰 <b>أخبار أنمياتك:</b>\n\n" + "\n\n".join(news_items))
        else:
            tg_send(cid, "لا أخبار جديدة حالياً.")

    # ── CHAT ──
    def _chat(self, cid, p):
        resp = p.get("chat_response") or "عذراً، لم أفهم طلبك. جرب تسألني بطريقة ثانية! 😊"
        tg_send(cid, resp)
        save_context(cid, "bot", resp[:100])

    # ── CALLBACK ──
    def _on_callback(self, cb):
        data = cb.get("data", "")
        cid = cb.get("message", {}).get("chat", {}).get("id")
        mid = cb.get("message", {}).get("message_id")
        cbid = cb.get("id")
        if not cid: return
        tg_answer_cb(cbid, "جاري التنفيذ...")
        parts = data.split(":")
        if parts[0] == "del" and len(parts) >= 2:
            eid = int(parts[1])
            ok = delete_entry(eid, ANILIST_TOKEN) if ANILIST_TOKEN else False
            if ok:
                tg_edit(cid, mid, "✅ تم الحذف بنجاح!")
            else:
                tg_edit(cid, mid, "❌ فشل الحذف.")
        elif parts[0] == "cancel":
            tg_edit(cid, mid, "↩️ تم الإلغاء.")
        elif parts[0] == "pick" and len(parts) >= 3:
            media_id = int(parts[1])
            status = parts[2] if len(parts) > 2 else "COMPLETED"
            score_str = parts[3] if len(parts) > 3 else ""
            is_fav = parts[4] == "True" if len(parts) > 4 else False
            score = float(score_str) if score_str else None
            candidates = search_media(str(media_id))
            # Direct lookup by searching with ID
            q = """query($id:Int){Media(id:$id){id type title{romaji english} episodes chapters
            coverImage{extraLarge large} siteUrl trailer{id site} averageScore}}"""
            res = _gql(q, {"id": media_id})
            media = res.get("data", {}).get("Media")
            if media:
                self._do_track(cid, media, {"status": status, "score": score, "is_favorite": is_fav, "media_type": media.get("type","ANIME")})

    # ── WELCOME & HELP ──
    def _welcome(self, cid):
        clear_context(cid)
        tg_send(cid, """🎬 <b>مرحباً بك في بوت AniList v2.0!</b>

أنا مساعدك الشامل لإدارة قوائم الأنمي والمانجا.

<b>📌 أمثلة سريعة:</b>
• <i>كملت انمي راجنا واقيمه 10</i>
• <i>شفت 3 حلقات من ون بيس</i>
• <i>احذف ناروتو من قائمتي</i>
• <i>وش الترند؟</i>
• <i>إحصائياتي</i>
• <i>فاجئني!</i>

أرسل /help للدليل الكامل 📖""")

    def _help(self, cid):
        tg_send(cid, """📖 <b>الدليل الشامل</b>

<b>📺 تتبع الأنمي/المانجا:</b>
• <i>شفت انمي هجوم العمالقة كامل</i>
• <i>شفت حلقتين من بليتش</i>
• <i>كملت راجنا واقيمه 9 واعمله مفضلة</i>
• <i>قريت 10 فصول من سولو ليفلينج</i>

<b>🗑️ الحذف:</b>
• <i>احذف ناروتو من قائمتي</i>

<b>📊 الإحصائيات:</b>
• <i>إحصائياتي</i>
• <i>إحصائيات Ahmed</i>

<b>📋 النشاطات والقوائم:</b>
• <i>آخر نشاطاتي</i>
• <i>انمياتي المكتملة</i>
• <i>مفضلاتي</i>

<b>👥 الأصدقاء:</b>
• <i>بروفايل Ahmed</i>
• <i>قارن قائمتي مع Ahmed</i>

<b>🧠 التوصيات:</b>
• <i>أنمي مشابه لهجوم العمالقة</i>
• <i>اقترح لي أنمي أكشن</i>
• <i>أنمي عشوائي</i>

<b>📺 الترند والمواسم:</b>
• <i>وش الترند؟</i>
• <i>أنميات هذا الموسم</i>
• <i>أفضل 10 أنميات</i>

<b>🔍 البحث:</b>
• <i>مين شخصية لوفي؟</i>
• <i>أنميات استديو MAPPA</i>
• <i>سيكويل هجوم العمالقة</i>
• <i>متى الحلقة الجاية من ون بيس</i>

<b>🎲 المرح:</b>
• <i>فاجئني!</i>
• <i>خبر حلو</i>

💡 تقدر تكلمني كلام عادي وبفهمك!""")
