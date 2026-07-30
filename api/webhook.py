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

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ANILIST_TOKEN = os.environ.get("ANILIST_ACCESS_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
ANILIST_URL = "https://graphql.anilist.co"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AniListBot/2.0"

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
    if extra: msg["extra"] = extra
    store["messages"].append(msg)
    if len(store["messages"]) > CONTEXT_MAX_MESSAGES:
        store["messages"] = store["messages"][-CONTEXT_MAX_MESSAGES:]
    store["last_active"] = time.time()

def build_gemini_context(chat_id):
    messages = get_context(chat_id)
    if not messages: return ""
    lines = ["Recent Conversation Context:"]
    for msg in messages:
        role = "User" if msg["role"] == "user" else "Bot"
        extra = msg.get("extra", {})
        extra_str = f" [media: {extra['media_title']}, id: {extra.get('media_id','')}]" if extra.get("media_title") else ""
        lines.append(f"{role}: {msg['text']}{extra_str}")
    return "\n".join(lines)

def clear_context(chat_id):
    _conversation_store.pop(str(chat_id), None)

def _gql(query, variables=None, token=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}
    if token: headers["Authorization"] = f"Bearer {token}"
    data = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(ANILIST_URL, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try: return json.loads(e.read().decode("utf-8"))
        except: return {"errors": [{"message": f"HTTP {e.code}"}]}
    except Exception as e:
        return {"errors": [{"message": str(e)}]}

def search_media(title, media_type="ANIME", per_page=4):
    q = """query($s:String,$t:MediaType,$n:Int){Page(perPage:$n){media(search:$s,type:$t){
    id type title{romaji english native} episodes chapters format seasonYear
    coverImage{extraLarge large medium} siteUrl trailer{id site} averageScore genres}}}"""
    res = _gql(q, {"s": title, "t": media_type, "n": per_page})
    return res.get("data", {}).get("Page", {}).get("media", [])

def get_entry(media_id, token):
    q = "query($m:Int){MediaList(mediaId:$m){id status score progress}}"
    res = _gql(q, {"m": media_id}, token)
    if "errors" in res: return None
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
    if "errors" in res: return {"error": res["errors"][0].get("message", "Unknown")}
    return res.get("data", {}).get("SaveMediaListEntry")

def delete_entry(entry_id, token):
    q = "mutation($id:Int){DeleteMediaListEntry(id:$id){deleted}}"
    res = _gql(q, {"id": entry_id}, token)
    return res.get("data", {}).get("DeleteMediaListEntry", {}).get("deleted", False)

def toggle_fav(media_id, token):
    return _gql("mutation($a:Int){ToggleFavourite(animeId:$a){anime{nodes{id}}}}", {"a": media_id}, token)

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
    id title{romaji english} coverImage{large} averageScore siteUrl genres}}}"""
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

def get_following(user_id, page=1, per_page=10):
    q = """query($u:Int,$p:Int,$n:Int){Page(page:$p,perPage:$n){
    following(userId:$u){id name avatar{large} siteUrl
    statistics{anime{count episodesWatched meanScore}}}}}"""
    res = _gql(q, {"u": user_id, "p": page, "n": per_page})
    return res.get("data", {}).get("Page", {}).get("following", [])

def _tg(method, payload):
    if not TELEGRAM_TOKEN: return None
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
    if not url: return tg_send(chat_id, caption, kb)
    p = {"chat_id": chat_id, "photo": url, "caption": caption[:1024], "parse_mode": "HTML"}
    if kb: p["reply_markup"] = kb
    res = _tg("sendPhoto", p)
    if not res or not res.get("ok"): return tg_send(chat_id, caption, kb)
    return res

def tg_album(chat_id, items):
    if not items: return None
    if len(items) == 1: return tg_photo(chat_id, items[0].get("url"), items[0].get("caption", ""))
    media = []
    for item in items[:10]:
        m = {"type": "photo", "media": item["url"], "parse_mode": "HTML"}
        if item.get("caption"): m["caption"] = item["caption"][:1024]
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
    keyboard = []
    for row in rows:
        kr = []
        for b in row:
            btn = {"text": b["text"]}
            if "url" in b: btn["url"] = b["url"]
            elif "callback_data" in b: btn["callback_data"] = b["callback_data"]
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

_STRIP_WORDS = re.compile(r'\b(?:\u0634\u0641\u062a|\u0643\u0645\u0644\u062a|\u0627\u0643\u0645\u0644\u062a|\u0623\u0643\u0645\u0644\u062a|\u062e\u0644\u0635\u062a|\u0627\u0646\u0647\u064a\u062a|\u0646\u0647\u064a\u062a|\u062a\u0627\u0628\u0639\u062a|\u0634\u0627\u0647\u062f\u062a|\u0642\u0631\u064a\u062a|\u0642\u0631\u0623\u062a|\u0627\u062d\u0630\u0641|\u0634\u064a\u0644|\u0627\u0645\u0633\u062d|\u0627\u0636\u0641|\u0623\u0636\u0641|\u0633\u062d\u0628\u062a|\u062a\u0631\u0643\u062a|\u0648\u0642\u0641\u062a|\u062a\u0648\u0642\u0641\u062a|\u0628\u062f\u0623\u062a|\u0628\u062f\u0627\u062a|\u0627\u0642\u064a\u0645\u0647|\u0623\u0642\u064a\u0645\u0647|\u0642\u064a\u0645\u0647|\u0648\u0627\u0642\u064a\u0645\u0647|\u0648\u0627\u0639\u0645\u0644\u0647|\u0648\u0643\u0645\u0627\u0646|\u0636\u064a\u0641\u0647|\u0636\u064a\u0641\u0647\u0627|\u0627\u0639\u0645\u0644\u0647|\u0627\u0639\u0645\u0644\u0647\u0627|\u062d\u0644\u0642\u0629|\u062d\u0644\u0642\u0627\u062a|\u062d\u0644\u0642\u062a\u064a\u0646|\u0641\u0635\u0644|\u0641\u0635\u0648\u0644|\u0641\u0635\u0644\u064a\u0646|\u0634\u0627\u0628\u062a\u0631|\u0627\u0646\u0645\u064a|\u0623\u0646\u0645\u064a|\u0645\u0627\u0646\u062c\u0627|\u0645\u0627\u0646\u0647\u0627|\u0643\u0627\u0645\u0644|\u0643\u0627\u0645\u0644\u0629|\u0643\u0644|\u062d\u0644\u0642\u0627\u062a\u0647|\u062d\u0644\u0642\u0627\u062a\u0647\u0627|\u0645\u0646|\u0627\u0645\u0633|\u0627\u0644\u064a\u0648\u0645|\u0627\u062e\u0631|\u0622\u062e\u0631|\u062c\u0632\u0621|\u0639\u0627\u062f\u0647|\u0639\u0627\u062f\u0629|\u0646\u0632\u0644|\u0646\u0632\u0644\u062a|\u0627\u0644\u064a|\u0627\u0644\u0644\u064a|\u0645\u0641\u0636\u0644\u0629|\u0645\u0641\u0636\u0644|\u0641\u064a\u0641\u0631\u064a\u062a|\u0627\u0644\u062a\u0642\u064a\u064a\u0645|\u062a\u0642\u064a\u064a\u0645|\u0648\u0627\u0631\u064a\u062f|\u0627\u0631\u064a\u062f|\u0648\u062d\u0637\u0647|\u062d\u0637\u0647|\u0628\u0639\u062f|\u062c\u062f\u064a\u062f|\u0627\u0648\u0644|\u062b\u0627\u0646\u064a|\u062b\u0627\u0644\u062b|\u0631\u0627\u0628\u0639)\b')
_STRIP_NUMS = re.compile(r'\b\d+\b')

def _extract_title(text):
    cleaned = _STRIP_WORDS.sub(' ', text)
    cleaned = _STRIP_NUMS.sub(' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    cleaned = re.sub(r'^(\u0648|\u0628|\u0644|\u0641)\s', '', cleaned)
    if len(cleaned) >= 2: return cleaned
    return None

ANIME_DICT = {
    "\u0648\u0646 \u0628\u064a\u0633": "One Piece", "\u0648\u0627\u0646 \u0628\u064a\u0633": "One Piece",
    "\u0646\u0627\u0631\u0648\u062a\u0648": "Naruto", "\u0628\u0648\u0631\u0648\u062a\u0648": "Boruto",
    "\u0628\u0644\u064a\u062a\u0634": "Bleach", "\u062f\u0631\u0627\u063a\u0648\u0646 \u0628\u0648\u0644": "Dragon Ball",
    "\u0647\u0646\u062a\u0631": "Hunter x Hunter", "\u0627\u0644\u0642\u0646\u0627\u0635": "Hunter x Hunter",
    "\u0647\u062c\u0648\u0645 \u0627\u0644\u0639\u0645\u0627\u0644\u0642\u0629": "Attack on Titan", "\u0627\u0644\u0639\u0645\u0627\u0644\u0642\u0629": "Attack on Titan", "\u0627\u062a\u0627\u0643": "Attack on Titan",
    "\u0642\u0627\u062a\u0644 \u0627\u0644\u0634\u064a\u0627\u0637\u064a\u0646": "Demon Slayer", "\u062f\u064a\u0645\u0648\u0646 \u0633\u0644\u0627\u064a\u0631": "Demon Slayer", "\u0643\u064a\u0645\u064a\u062a\u0633\u0648": "Demon Slayer",
    "\u0627\u0644\u0644\u0639\u0646\u0627\u062a": "Jujutsu Kaisen", "\u062c\u0648\u062c\u0648\u062a\u0633\u0648": "Jujutsu Kaisen", "\u062c\u0648\u062c\u0648": "Jujutsu Kaisen",
    "\u0631\u064a \u0632\u064a\u0631\u0648": "Re:Zero kara Hajimeru Isekai Seikatsu", "\u0631\u064a\u0632\u064a\u0631\u0648": "Re:Zero kara Hajimeru Isekai Seikatsu",
    "\u0627\u0644\u0633\u0644\u0627\u064a\u0645": "Tensei shitara Slime Datta Ken", "\u0633\u0644\u0627\u064a\u0645": "Tensei shitara Slime Datta Ken",
    "\u0633\u0648\u0644\u0648 \u0644\u064a\u0641\u0644\u064a\u0646\u062c": "Solo Leveling", "\u0641\u0631\u064a\u0631\u0646": "Sousou no Frieren",
    "\u0631\u062c\u0644 \u0627\u0644\u0645\u0646\u0634\u0627\u0631": "Chainsaw Man", "\u062a\u0634\u064a\u0646\u0633\u0648 \u0645\u0627\u0646": "Chainsaw Man",
    "\u0631\u0627\u062c\u0646\u0627": "Ragna Crimson", "\u0631\u0627\u063a\u0646\u0627": "Ragna Crimson",
    "\u0643\u0648\u0646\u0627\u0646": "Detective Conan", "\u0637\u0648\u0643\u064a\u0648 \u063a\u0648\u0644": "Tokyo Ghoul",
    "\u0645\u0630\u0643\u0631\u0629 \u0627\u0644\u0645\u0648\u062a": "Death Note", "\u062f\u064a\u062b \u0646\u0648\u062a": "Death Note",
    "\u0633\u0628\u0627\u064a \u0641\u0627\u0645\u0644\u064a": "Spy x Family", "\u0628\u0644\u0648 \u0644\u0648\u0643": "Blue Lock",
    "\u0641\u0648\u0644\u0645\u064a\u062a\u0627\u0644": "Fullmetal Alchemist: Brotherhood",
    "\u0633\u0648\u0631\u062f \u0627\u0631\u062a": "Sword Art Online", "\u0643\u0648\u0646\u0648\u0633\u0648\u0628\u0627": "KonoSuba",
    "\u0627\u0648\u0641\u0631\u0644\u0648\u0631\u062f": "Overlord", "\u0645\u0648\u0634\u0648\u0643\u0648": "Mushoku Tensei",
}

def regex_parse(text):
    r = {"action": "TRACK", "media_type": "ANIME", "title": None, "status": None,
         "score": None, "progress_delta": None, "absolute_progress": None,
         "is_favorite": False, "genre": None, "friend_username": None,
         "original_text": text, "confidence": "low", "alternatives": [],
         "batch": None, "chat_response": None}
    lt = text.strip().lower()
    if re.search(r'(\u0627\u062d\u0630\u0641|\u0634\u064a\u0644|\u0627\u0645\u0633\u062d)', lt): r["action"] = "DELETE"
    elif re.search(r'(\u0625\u062d\u0635\u0627\u0626\u064a\u0627\u062a|\u0627\u062d\u0635\u0627\u0626\u064a\u0627\u062a)', lt): r["action"] = "STATS"
    elif re.search(r'(\u0646\u0634\u0627\u0637\u0627\u062a|\u0622\u062e\u0631 \u0646\u0634\u0627\u0637)', lt): r["action"] = "ACTIVITY"
    elif re.search(r'(\u0627\u0644\u062a\u0631\u0646\u062f|\u062a\u0631\u0646\u062f)', lt): r["action"] = "TRENDING"
    elif re.search(r'(\u0647\u0630\u0627 \u0627\u0644\u0645\u0648\u0633\u0645|\u0627\u0646\u0645\u064a\u0627\u062a \u0627\u0644\u0645\u0648\u0633\u0645)', lt): r["action"] = "SEASONAL"
    elif re.search(r'(\u0641\u0627\u062c\u0626\u0646\u064a|\u0641\u0627\u062c\u0623\u0646\u064a)', lt): r["action"] = "SURPRISE"
    elif re.search(r'(\u0623\u062e\u0628\u0627\u0631|\u0627\u062e\u0628\u0627\u0631|\u062e\u0628\u0631 \u062d\u0644\u0648)', lt): r["action"] = "NEWS"
    elif re.search(r'(\u0645\u0641\u0636\u0644\u0627\u062a\u064a|\u0648\u0634 \u0645\u0641\u0636\u0644\u0627\u062a\u064a)', lt): r["action"] = "FAVORITES_LIST"
    elif re.search(r'(\u0627\u0642\u062a\u0631\u062d|\u0631\u0634\u062d)', lt): r["action"] = "RECOMMEND_GENRE"
    elif re.search(r'(\u0645\u0634\u0627\u0628\u0647|\u0634\u0628\u064a\u0647|\u064a\u0634\u0628\u0647)', lt): r["action"] = "RECOMMEND_SIMILAR"
    elif re.search(r'(\u0634\u062e\u0635\u064a\u0629|\u0645\u0624\u062f\u064a \u0635\u0648\u062a)', lt): r["action"] = "CHARACTER_LOOKUP"
    elif re.search(r'(\u0627\u0633\u062a\u062f\u064a\u0648|\u0633\u062a\u0648\u062f\u064a\u0648|studio)', lt): r["action"] = "STUDIO_LOOKUP"
    elif re.search(r'(\u0633\u064a\u0643\u0648\u064a\u0644|\u062a\u062a\u0645\u0629|\u0627\u0644\u062c\u0632\u0621 \u0627\u0644\u062b\u0627\u0646\u064a)', lt): r["action"] = "RELATIONS"
    elif re.search(r'(\u0645\u062a\u0649 \u0627\u0644\u062d\u0644\u0642\u0629|\u0627\u0644\u062d\u0644\u0642\u0629 \u0627\u0644\u062c\u0627\u064a\u0629)', lt): r["action"] = "AIRING_SCHEDULE"
    elif re.search(r'(\u0627\u0635\u062f\u0642\u0627\u0626\u064a|\u0627\u0635\u062d\u0627\u0628\u064a|\u0645\u062a\u0627\u0628\u0639\u064a\u0646\u064a|\u0641\u0631\u0646\u062f\u0632)', lt): r["action"] = "MY_FOLLOWING"
    elif re.search(r'(\u0628\u0631\u0648\u0641\u0627\u064a\u0644|\u062d\u0633\u0627\u0628)\s+\w+', lt): r["action"] = "FRIEND_PROFILE"
    elif re.search(r'(\u0627\u0646\u0645\u064a\u0627\u062a|\u0642\u0627\u0626\u0645\u0629)\s+\w+', lt) and not re.search(r'(\u0627\u0646\u0645\u064a\u0627\u062a\u064a|\u0642\u0627\u0626\u0645\u062a\u064a)', lt): r["action"] = "FRIEND_LIST"
    elif re.search(r'(\u0642\u0627\u0631\u0646|\u0645\u0642\u0627\u0631\u0646\u0629)', lt): r["action"] = "COMPARE_FRIEND"
    if re.search(r'(\u0645\u0641\u0636\u0644\u0629|\u0641\u064a\u0641\u0631\u064a\u062a|favourite|favorite)', lt): r["is_favorite"] = True
    sm = re.search(r'(?:\u0627\u0642\u064a\u0645\u0647|\u0623\u0642\u064a\u0645\u0647|\u0642\u064a\u0645\u0647|\u062a\u0642\u064a\u064a\u0645|score)\s*(\d+(?:\.\d+)?)', lt)
    if not sm: sm = re.search(r'(\d+(?:\.\d+)?)\s*(?:\u0645\u0646\s*10|/10)', lt)
    if sm:
        v = float(sm.group(1))
        if 0 <= v <= 10: r["score"] = v
    if re.search(r'(\u0643\u0645\u0644\u062a|\u0623\u0643\u0645\u0644\u062a|\u0627\u0643\u0645\u0644\u062a|\u062e\u0644\u0635\u062a|\u0627\u0646\u0647\u064a\u062a|\u0634\u0641\u062a \u0643\u0627\u0645\u0644|\u0643\u0644 \u0627\u0644\u062d\u0644\u0642\u0627\u062a)', lt):
        r["status"] = "COMPLETED"
    elif re.search(r'(\u0633\u062d\u0628\u062a|\u062a\u0631\u0643\u062a|dropped)', lt): r["status"] = "DROPPED"
    elif re.search(r'(\u062e\u0637\u0629|\u0627\u0636\u0641|\u0623\u0636\u0641|plan)', lt): r["status"] = "PLANNING"
    elif re.search(r'(\u0648\u0642\u0641\u062a|\u062a\u0648\u0642\u0641\u062a|paused)', lt): r["status"] = "PAUSED"
    elif re.search(r'(\u0634\u0641\u062a|\u062a\u0627\u0628\u0639|\u0634\u0627\u0647\u062f\u062a)', lt): r["status"] = "CURRENT"
    if re.search(r'(\u0645\u0627\u0646\u062c\u0627|\u0641\u0635\u0644|\u0634\u0627\u0628\u062a\u0631|manga)', lt): r["media_type"] = "MANGA"
    ep = re.search(r'(\d+)\s*(?:\u062d\u0644\u0642\u0629|\u062d\u0644\u0642\u0627\u062a|\u0641\u0635\u0644|ep)', lt)
    if ep: r["progress_delta"] = int(ep.group(1)); r["status"] = r["status"] or "CURRENT"
    for nick, official in ANIME_DICT.items():
        if nick in lt: r["title"] = official; break
    if not r["title"] and r["action"] in ("TRACK","DELETE","RECOMMEND_SIMILAR","RELATIONS","AIRING_SCHEDULE","FAVORITES_ADD","CHARACTER_LOOKUP","STAFF_LOOKUP","STUDIO_LOOKUP"):
        r["title"] = _extract_title(lt)
    return r

GEMINI_SYSTEM_PROMPT = """You are an expert AniList anime & manga assistant understanding Arabic, English, Arabizi, and typos.

CRITICAL RULES:
- 'كملت'/'أكملت'/'خلصت'/'شفت كامل'/'كل حلقاته' = COMPLETED (NOT CURRENT!)
- 'شفت الأنمي' without 'حلقة/حلقات' = COMPLETED
- 'شفت حلقتين من X' = CURRENT with progress_delta=2
- When COMPLETED: set progress_delta to null (system auto-fills total episodes)
- NEVER set dates unless user explicitly mentions them
- If user says 'ه'/'ها'/'هذا' without title, set title to null (system resolves from context)
- You MUST identify anime titles even if user uses Arabic names, descriptions, or nicknames
- If user describes an anime by plot/characters, try to identify it and set title
- For conversational messages not about anime, use action=CHAT with chat_response in Arabic

Return ONLY valid JSON:
- action: TRACK, DELETE, STATS, ACTIVITY, MY_LIST, FRIEND_PROFILE, FRIEND_LIST, FRIEND_ACTIVITY, COMPARE_FRIEND, RECOMMEND_GENRE, RECOMMEND_SIMILAR, RECOMMEND_FROM_LIST, TRENDING, SEASONAL, TOP_RATED, RANDOM_ANIME, CHARACTER_LOOKUP, STAFF_LOOKUP, STUDIO_LOOKUP, RELATIONS, AIRING_SCHEDULE, FAVORITES_LIST, FAVORITES_ADD, FAVORITES_REMOVE, BATCH_TRACK, SURPRISE, NEWS, MY_FOLLOWING, CHAT
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
- alternatives: [] list of alternative titles if unsure
- batch: [{title,status,score,progress_delta},...] for BATCH_TRACK or null
- chat_response: Arabic response string for CHAT action or null"""

def parse_with_gemini(text, chat_id=None):
    key = GEMINI_KEY
    if not key:
        print("[Gemini] NO API KEY")
        result = regex_parse(text)
        result["_parser"] = "regex_no_key"
        return result
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
        print(f"[Gemini] Calling {GEMINI_MODEL}...")
        with urllib.request.urlopen(req, timeout=9) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            content = res["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(content)
            parsed["original_text"] = text
            parsed["_parser"] = "gemini"
            print(f"[Gemini] OK: action={parsed.get('action')}, title={parsed.get('title')}")
            return parsed
    except Exception as e:
        print(f"[Gemini] FAIL: {type(e).__name__}: {e}")
        result = regex_parse(text)
        result["_parser"] = f"regex_fallback:{type(e).__name__}"
        return result

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        if qs.get("test"):
            result = self._test_gemini()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result, indent=2, ensure_ascii=False).encode("utf-8"))
            return
        status = {"status": "running", "bot": "AniList Bot v2.1",
                  "telegram": "SET" if TELEGRAM_TOKEN else "MISSING",
                  "anilist": "SET" if ANILIST_TOKEN else "MISSING",
                  "gemini": "SET" if GEMINI_KEY else "MISSING",
                  "model": GEMINI_MODEL,
                  "test": "add ?test=1 to diagnose Gemini"}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(status, indent=2).encode("utf-8"))

    def _test_gemini(self):
        if not GEMINI_KEY:
            return {"error": "GEMINI_API_KEY not set"}
        test_text = "\u0643\u0645\u0644\u062a \u0627\u0646\u0645\u064a \u0648\u0646 \u0628\u064a\u0633"
        try:
            t0 = time.time()
            result = parse_with_gemini(test_text)
            elapsed = round(time.time() - t0, 2)
            return {"gemini_works": result.get("_parser") == "gemini", "parser": result.get("_parser"),
                    "seconds": elapsed, "action": result.get("action"), "title": result.get("title"),
                    "key_prefix": GEMINI_KEY[:6] + "...", "model": GEMINI_MODEL}
        except Exception as e:
            return {"error": str(e)}

    def do_POST(self):
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            update = json.loads(body.decode("utf-8"))
        except: self.send_response(200); self.end_headers(); return
        try:
            if "callback_query" in update: self._on_callback(update["callback_query"])
            elif "message" in update:
                msg = update["message"]
                cid = msg.get("chat", {}).get("id")
                text = (msg.get("text") or "").strip()
                if not text or not cid: pass
                elif text.startswith("/start"): self._welcome(cid)
                elif text.startswith("/help"): self._help(cid)
                else:
                    save_context(cid, "user", text)
                    parsed = parse_with_gemini(text, cid)
                    self._route(cid, parsed)
        except Exception as e: print(f"[POST Error] {e}")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def _route(self, cid, p):
        a = p.get("action", "TRACK")
        if not p.get("title") and a in ("TRACK","DELETE","RECOMMEND_SIMILAR","RELATIONS","AIRING_SCHEDULE","FAVORITES_ADD","FAVORITES_REMOVE"):
            p["title"] = self._ctx_title(cid)
        try:
            m = {"TRACK": self._track, "DELETE": self._delete, "STATS": self._stats,
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
                "NEWS": self._news, "CHAT": self._chat, "MY_FOLLOWING": self._my_following}
            m.get(a, self._chat)(cid, p)
        except Exception as e: tg_send(cid, f"\u274c {str(e)[:200]}")

    def _ctx_title(self, cid):
        for msg in reversed(get_context(cid)):
            if msg.get("extra", {}).get("media_title"): return msg["extra"]["media_title"]
        return None

    def _track(self, cid, p):
        title = p.get("title")
        if not title:
            raw = p.get("original_text", "")
            extracted = _extract_title(raw.lower()) if raw else None
            if extracted: title = extracted; p["title"] = title
            else: tg_send(cid, "\u274c \u0644\u0645 \u0623\u0633\u062a\u0637\u0639 \u062a\u062d\u062f\u064a\u062f \u0627\u0633\u0645 \u0627\u0644\u0623\u0646\u0645\u064a. \u062c\u0631\u0628 \u0627\u0644\u0627\u0633\u0645 \u0628\u0627\u0644\u0625\u0646\u062c\u0644\u064a\u0632\u064a."); return
        mt = p.get("media_type", "ANIME")
        candidates = search_media(title, mt)
        if not candidates: tg_send(cid, f"\u274c \u0644\u0645 \u0623\u062c\u062f: <b>{title}</b>"); return
        if len(candidates) > 1 and p.get("confidence") == "medium":
            rows = [[{"text": f"{title_of(c)} ({c.get('seasonYear','')})", "callback_data": f"pick:{c['id']}:{p.get('status','COMPLETED')}:{p.get('score','')}:{p.get('is_favorite',False)}"}] for c in candidates[:4]]
            tg_send(cid, f"\ud83e\udd14 \u0648\u062c\u062f\u062a \u0639\u062f\u0629 \u0646\u062a\u0627\u0626\u062c \u0644\u0640 <b>{title}</b>\u060c \u0627\u062e\u062a\u0631:", kb_make(rows)); return
        self._do_track(cid, candidates[0], p)

    def _do_track(self, cid, media, p):
        mid = media["id"]; status = p.get("status") or "COMPLETED"; score = p.get("score")
        mt = p.get("media_type", "ANIME")
        existing = get_entry(mid, ANILIST_TOKEN) if ANILIST_TOKEN else None
        cur_prog = (existing or {}).get("progress", 0) or 0
        total = media.get("episodes") or media.get("chapters") or 0
        new_prog = cur_prog
        if p.get("absolute_progress") is not None: new_prog = p["absolute_progress"]
        elif p.get("progress_delta") is not None: new_prog = cur_prog + p["progress_delta"]
        elif status == "COMPLETED" and total > 0: new_prog = total
        if ANILIST_TOKEN:
            res = save_entry(mid, ANILIST_TOKEN, status=status, score=score, progress=new_prog)
            if isinstance(res, dict) and "error" in res: tg_send(cid, f"\u26a0\ufe0f {res['error'][:200]}"); return
            if p.get("is_favorite"): toggle_fav(mid, ANILIST_TOKEN)
        name = title_of(media); unit = "\u0627\u0644\u0641\u0635\u0644" if mt == "MANGA" else "\u0627\u0644\u062d\u0644\u0642\u0629"
        cap = f"\u2705 <b>\u062a\u0645 \u0627\u0644\u062a\u062d\u062f\u064a\u062b!</b>\n\n\ud83d\udcfa <b>{name}</b>\n\ud83d\udccc {status}\n\ud83d\udd22 {unit}: {new_prog}"
        if total: cap += f"/{total}"
        if score is not None: cap += f"\n\u2b50 {score}/10"
        if p.get("is_favorite"): cap += "\n\u2764\ufe0f \u0645\u0641\u0636\u0644\u0629!"
        trailer = media.get("trailer")
        if trailer and trailer.get("site") == "youtube": cap += f"\n\ud83c\udfac <a href='https://youtube.com/watch?v={trailer['id']}'>Trailer</a>"
        kb = kb_make([[{"text": "\ud83d\udd17 AniList", "url": media.get("siteUrl", "https://anilist.co")}]])
        tg_photo(cid, cover_of(media), cap, kb)
        save_context(cid, "bot", f"\u062a\u0645 {name}", extra={"media_title": name, "media_id": mid})

    def _delete(self, cid, p):
        title = p.get("title")
        if not title: tg_send(cid, "\u274c \u062d\u062f\u062f \u0627\u0644\u0623\u0646\u0645\u064a."); return
        candidates = search_media(title, p.get("media_type", "ANIME"))
        if not candidates: tg_send(cid, f"\u274c \u0644\u0645 \u0623\u062c\u062f: <b>{title}</b>"); return
        media = candidates[0]; entry = get_entry(media["id"], ANILIST_TOKEN) if ANILIST_TOKEN else None
        if not entry: tg_send(cid, f"\u274c <b>{title_of(media)}</b> \u063a\u064a\u0631 \u0645\u0648\u062c\u0648\u062f \u0628\u0642\u0627\u0626\u0645\u062a\u0643."); return
        kb = kb_make([[{"text": "\u2705 \u062a\u0623\u0643\u064a\u062f", "callback_data": f"del:{entry['id']}"}, {"text": "\u274c \u0625\u0644\u063a\u0627\u0621", "callback_data": "cancel"}]])
        tg_photo(cid, cover_of(media), f"\ud83d\uddd1\ufe0f \u062d\u0630\u0641 <b>{title_of(media)}</b>?", kb)

    def _stats(self, cid, p):
        fn = p.get("friend_username")
        if fn: username = fn
        else:
            viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
            if not viewer: tg_send(cid, "\u274c \u0644\u0645 \u0623\u062c\u062f \u062d\u0633\u0627\u0628\u0643."); return
            username = viewer["name"]
        stats = get_stats(username)
        if not stats: tg_send(cid, "\u274c \u0644\u0627 \u0625\u062d\u0635\u0627\u0626\u064a\u0627\u062a."); return
        a = stats.get("anime", {}); m = stats.get("manga", {})
        mins = a.get("minutesWatched", 0); days = mins // 1440; hours = (mins % 1440) // 60
        genres = ", ".join([g["genre"] for g in a.get("genres", [])[:3]]) or "\u2014"
        studios = ", ".join([s["studio"]["name"] for s in a.get("studios", [])[:3]]) or "\u2014"
        cap = f"\ud83d\udcca <b>{username}</b>\n\n\ud83d\udcfa <b>Anime:</b>\n  \u2022 {a.get('count',0)} titles\n  \u2022 {a.get('episodesWatched',0)} eps\n  \u2022 {days}d {hours}h watched\n  \u2022 Avg: {a.get('meanScore',0)}/100\n  \u2022 Genres: {genres}\n  \u2022 Studios: {studios}\n\n\ud83d\udcd6 <b>Manga:</b> {m.get('count',0)} | {m.get('chaptersRead',0)} ch | {m.get('meanScore',0)}/100"
        tg_send(cid, cap)
        save_context(cid, "bot", f"Stats {username}")

    def _activity(self, cid, p):
        viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
        if not viewer: tg_send(cid, "\u274c"); return
        acts = get_activities(viewer["id"])
        if not acts: tg_send(cid, "\u0644\u0627 \u0646\u0634\u0627\u0637\u0627\u062a."); return
        items = []
        for act in acts[:8]:
            media = act.get("media")
            if not media: continue
            cap = f"<b>{title_of(media)}</b>\n{act.get('status','')} {act.get('progress','')}"
            url = cover_of(media)
            if url: items.append({"url": url, "caption": cap})
        if items: tg_album(cid, items)
        else: tg_send(cid, "\u0644\u0627 \u0646\u0634\u0627\u0637\u0627\u062a.")
        save_context(cid, "bot", "Activities")

    def _mylist(self, cid, p):
        viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
        if not viewer: tg_send(cid, "\u274c"); return
        entries = get_media_list(username=viewer["name"], media_type=p.get("media_type","ANIME"), status=p.get("status"), per_page=8)
        if not entries: tg_send(cid, "\u0642\u0627\u0626\u0645\u062a\u0643 \u0641\u0627\u0631\u063a\u0629."); return
        items = [{"url": cover_of(e.get("media",{})), "caption": f"<b>{title_of(e.get('media',{}))}</b>\n\u2b50 {e.get('score',0)}"} for e in entries if cover_of(e.get("media",{}))]
        if items: tg_album(cid, items)

    def _friend_profile(self, cid, p):
        fn = p.get("friend_username")
        if not fn: self._my_following(cid, p); return
        user = get_profile(fn)
        if not user: tg_send(cid, f"\u274c {fn}"); return
        a = user.get("statistics",{}).get("anime",{}); m = user.get("statistics",{}).get("manga",{})
        cap = f"\ud83d\udc64 <b>{user['name']}</b>\n\n\ud83d\udcfa {a.get('count',0)} anime | {a.get('episodesWatched',0)} eps\n\ud83d\udcd6 {m.get('count',0)} manga\n\u2b50 {a.get('meanScore',0)}/100"
        kb = kb_make([[{"text": "\ud83d\udd17 AniList", "url": user.get("siteUrl", "https://anilist.co")}]])
        tg_photo(cid, user.get("avatar",{}).get("large"), cap, kb)

    def _my_following(self, cid, p):
        viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
        if not viewer: tg_send(cid, "\u274c"); return
        following = get_following(viewer["id"])
        if not following: tg_send(cid, "\ud83d\udc65 \u0644\u0627 \u062a\u062a\u0627\u0628\u0639 \u0623\u062d\u062f."); return
        cap = f"\ud83d\udc65 <b>\u0645\u062a\u0627\u0628\u0639\u064a\u0646\u0643 ({len(following)}):</b>\n\n"
        for u in following:
            a = u.get("statistics",{}).get("anime",{})
            cap += f"\u2022 <b>{u['name']}</b> \u2014 {a.get('count',0)} anime | \u2b50 {a.get('meanScore',0)}\n"
        tg_send(cid, cap)

    def _friend_list(self, cid, p):
        fn = p.get("friend_username")
        if not fn: tg_send(cid, "\u274c \u062d\u062f\u062f \u0627\u0633\u0645 \u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645."); return
        entries = get_media_list(username=fn, status=p.get("status","COMPLETED"), per_page=8)
        if not entries: tg_send(cid, f"\u274c {fn}"); return
        items = [{"url": cover_of(e.get("media",{})), "caption": f"<b>{title_of(e.get('media',{}))}</b>"} for e in entries if cover_of(e.get("media",{}))]
        if items: tg_album(cid, items)

    def _friend_activity(self, cid, p):
        fn = p.get("friend_username")
        if not fn: tg_send(cid, "\u274c"); return
        user = get_profile(fn)
        if not user: tg_send(cid, f"\u274c {fn}"); return
        acts = get_activities(user["id"])
        if not acts: tg_send(cid, f"\u274c {fn}"); return
        items = [{"url": cover_of(act.get("media",{})), "caption": f"<b>{title_of(act.get('media',{}))}</b>\n{act.get('status','')}"} for act in acts[:8] if act.get("media") and cover_of(act.get("media",{}))]
        if items: tg_album(cid, items)

    def _compare(self, cid, p):
        fn = p.get("friend_username")
        if not fn: tg_send(cid, "\u274c \u062d\u062f\u062f \u0635\u062f\u064a\u0642\u0643."); return
        viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
        if not viewer: tg_send(cid, "\u274c"); return
        my_list = get_media_list(username=viewer["name"], status="COMPLETED", per_page=50)
        fr_list = get_media_list(username=fn, status="COMPLETED", per_page=50)
        my_ids = {e["media"]["id"]: e for e in my_list if e.get("media")}
        fr_ids = {e["media"]["id"]: e for e in fr_list if e.get("media")}
        shared = set(my_ids) & set(fr_ids)
        cap = f"\ud83c\udd9a <b>{viewer['name']} vs {fn}</b>\n\n\ud83e\udd1d {len(shared)} shared\n\ud83d\udc64 {len(set(my_ids)-set(fr_ids))} only you\n\ud83d\udc65 {len(set(fr_ids)-set(my_ids))} only {fn}"
        if shared:
            cap += "\n\n<b>Shared:</b>\n"
            for sid in list(shared)[:5]:
                cap += f"\u2022 {title_of(my_ids[sid]['media'])}: {my_ids[sid].get('score',0)} vs {fr_ids[sid].get('score',0)}\n"
        tg_send(cid, cap)

    def _rec_genre(self, cid, p):
        genre = p.get("genre", "Action")
        results = get_trending(8)
        if not results: tg_send(cid, "\u274c"); return
        filtered = [t for t in results if genre.lower() in [g.lower() for g in (t.get("genres") or [])]]
        show = filtered[:6] if filtered else results[:5]
        items = [{"url": cover_of(r), "caption": f"<b>{title_of(r)}</b>\n\u2b50 {r.get('averageScore',0)}%"} for r in show if cover_of(r)]
        if items: tg_album(cid, items)

    def _rec_similar(self, cid, p):
        title = p.get("title")
        if not title: tg_send(cid, "\u274c"); return
        candidates = search_media(title)
        if not candidates: tg_send(cid, f"\u274c {title}"); return
        recs = get_recommendations(candidates[0]["id"])
        if not recs: tg_send(cid, f"\u274c \u0644\u0627 \u062a\u0648\u0635\u064a\u0627\u062a \u0644\u0640 <b>{title}</b>"); return
        items = [{"url": cover_of(r), "caption": f"<b>{title_of(r)}</b>\n\u2b50 {r.get('averageScore',0)}%"} for r in recs[:6] if cover_of(r)]
        if items: tg_album(cid, items)

    def _rec_from_list(self, cid, p):
        viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
        if not viewer: tg_send(cid, "\u274c"); return
        entries = get_media_list(username=viewer["name"], status="COMPLETED", sort=["SCORE_DESC"], per_page=8)
        if not entries: tg_send(cid, "\u274c"); return
        items = [{"url": cover_of(e["media"]), "caption": f"<b>{title_of(e['media'])}</b> \u2b50{e.get('score',0)}"} for e in entries if e.get("media") and cover_of(e["media"])]
        if items: tg_album(cid, items[:8])

    def _trending(self, cid, p):
        results = get_trending(8)
        if not results: tg_send(cid, "\u274c"); return
        items = [{"url": cover_of(r), "caption": f"<b>{title_of(r)}</b>\n\u2b50 {r.get('averageScore',0)}%"} for r in results if cover_of(r)]
        if items: tg_album(cid, items)

    def _seasonal(self, cid, p):
        s, y = current_season()
        results = get_seasonal(s, y, 8)
        if not results: tg_send(cid, f"\u274c {s} {y}"); return
        items = [{"url": cover_of(r), "caption": f"<b>{title_of(r)}</b>\n\u2b50 {r.get('averageScore',0)}%"} for r in results if cover_of(r)]
        if items: tg_album(cid, items)

    def _top_rated(self, cid, p):
        q = """query{Page(perPage:8){media(type:ANIME,sort:SCORE_DESC){
        id title{romaji english} coverImage{large} averageScore siteUrl}}}"""
        res = _gql(q)
        ml = res.get("data",{}).get("Page",{}).get("media",[])
        items = [{"url": cover_of(m), "caption": f"<b>{title_of(m)}</b>\n\u2b50 {m.get('averageScore',0)}%"} for m in ml if cover_of(m)]
        if items: tg_album(cid, items)

    def _random_anime(self, cid, p):
        genre = p.get("genre", random.choice(["Action","Adventure","Comedy","Drama","Fantasy","Romance"]))
        page = random.randint(1, 10)
        q = """query($g:String,$p:Int){Page(page:$p,perPage:1){media(type:ANIME,genre:$g,sort:POPULARITY_DESC){
        id title{romaji english} coverImage{large} averageScore siteUrl episodes}}}"""
        res = _gql(q, {"g": genre, "p": page})
        ml = res.get("data",{}).get("Page",{}).get("media",[])
        if not ml: tg_send(cid, "\u274c"); return
        m = ml[0]
        cap = f"\ud83c\udfb2 <b>Random ({genre}):</b>\n\n<b>{title_of(m)}</b>\n\u2b50 {m.get('averageScore',0)}%\n\ud83d\udcfa {m.get('episodes','?')} eps"
        kb = kb_make([[{"text": "\ud83d\udd17 AniList", "url": m.get("siteUrl","")}]])
        tg_photo(cid, cover_of(m), cap, kb)

    def _character(self, cid, p):
        name = p.get("title")
        if not name: tg_send(cid, "\u274c"); return
        ch = search_character_q(name)
        if not ch: tg_send(cid, f"\u274c {name}"); return
        anime = title_of(ch["media"]["nodes"][0]) if ch.get("media",{}).get("nodes") else "?"
        cap = f"\ud83d\udc64 <b>{ch['name']['full']}</b> ({ch['name'].get('native','')})\n\u0645\u0646: <b>{anime}</b>"
        kb = kb_make([[{"text": "\ud83d\udd17 AniList", "url": ch.get("siteUrl","")}]])
        tg_photo(cid, ch.get("image",{}).get("large"), cap, kb)

    def _staff(self, cid, p):
        name = p.get("title")
        if not name: tg_send(cid, "\u274c"); return
        st = search_staff_q(name)
        if not st: tg_send(cid, f"\u274c {name}"); return
        jobs = ", ".join(st.get("primaryOccupations",[])[:3]) or "\u2014"
        chars = ", ".join([c["name"]["full"] for c in (st.get("characters",{}).get("nodes",[]))[:3]]) or "\u2014"
        cap = f"\ud83c\udf99\ufe0f <b>{st['name']['full']}</b>\n\ud83d\udcbc {jobs}\n\ud83c\udfad {chars}"
        tg_photo(cid, st.get("image",{}).get("large"), cap)

    def _studio(self, cid, p):
        name = p.get("title")
        if not name: tg_send(cid, "\u274c"); return
        studio = search_studio_q(name)
        if not studio: tg_send(cid, f"\u274c {name}"); return
        works = studio.get("media",{}).get("nodes",[])
        if not works: return
        items = [{"url": cover_of(w), "caption": f"<b>{title_of(w)}</b>\n\u2b50 {w.get('averageScore',0)}%"} for w in works[:8] if cover_of(w)]
        if items:
            tg_send(cid, f"\ud83c\udfe2 <b>{studio['name']}:</b>")
            tg_album(cid, items)

    def _relations(self, cid, p):
        title = p.get("title")
        if not title: tg_send(cid, "\u274c"); return
        candidates = search_media(title)
        if not candidates: tg_send(cid, f"\u274c {title}"); return
        edges = get_relations(candidates[0]["id"])
        if not edges: tg_send(cid, f"\u274c \u0644\u0627 \u0639\u0644\u0627\u0642\u0627\u062a \u0644\u0640 <b>{title}</b>"); return
        TYPE_AR = {"SEQUEL":"\u062a\u062a\u0645\u0629","PREQUEL":"\u0633\u0627\u0628\u0642","SIDE_STORY":"\u062c\u0627\u0646\u0628\u064a\u0629","ADAPTATION":"\u0627\u0642\u062a\u0628\u0627\u0633","SPIN_OFF":"Spin-off","PARENT":"\u0627\u0644\u0623\u0635\u0644"}
        items = [{"url": cover_of(e.get("node",{})), "caption": f"<b>{title_of(e.get('node',{}))}</b>\n\ud83d\udd17 {TYPE_AR.get(e.get('relationType',''),e.get('relationType',''))}"} for e in edges[:8] if cover_of(e.get("node",{}))]
        if items: tg_album(cid, items)

    def _airing(self, cid, p):
        title = p.get("title")
        if not title: tg_send(cid, "\u274c"); return
        candidates = search_media(title)
        if not candidates: tg_send(cid, f"\u274c {title}"); return
        media = candidates[0]; schedule = get_airing(media["id"])
        if not schedule: tg_send(cid, f"\ud83d\udcfa <b>{title_of(media)}</b> \u0645\u0646\u062a\u0647\u064a."); return
        secs = schedule.get("timeUntilAiring",0); ep = schedule.get("episode","?")
        d=secs//86400; h=(secs%86400)//3600; m=(secs%3600)//60
        parts = []
        if d: parts.append(f"{d}d")
        if h: parts.append(f"{h}h")
        if m: parts.append(f"{m}m")
        tg_photo(cid, cover_of(media), f"\ud83d\udcfa <b>{title_of(media)}</b>\n\n\u23f1\ufe0f Ep {ep} in: <b>{' '.join(parts)}</b>")

    def _fav_list(self, cid, p):
        viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
        if not viewer: tg_send(cid, "\u274c"); return
        af, mf = get_favorites(viewer["name"])
        all_f = af + mf
        if not all_f: tg_send(cid, "\u274c \u0641\u0627\u0631\u063a\u0629."); return
        items = [{"url": cover_of(f), "caption": f"<b>{title_of(f)}</b> \u2764\ufe0f"} for f in all_f[:8] if cover_of(f)]
        if items: tg_album(cid, items)

    def _fav_add(self, cid, p):
        title = p.get("title")
        if not title: tg_send(cid, "\u274c"); return
        candidates = search_media(title)
        if not candidates: tg_send(cid, f"\u274c {title}"); return
        media = candidates[0]
        if ANILIST_TOKEN: toggle_fav(media["id"], ANILIST_TOKEN)
        tg_photo(cid, cover_of(media), f"\u2764\ufe0f <b>{title_of(media)}</b>")

    def _fav_remove(self, cid, p): self._fav_add(cid, p)

    def _batch(self, cid, p):
        batch = p.get("batch", [])
        if not batch: tg_send(cid, "\u274c"); return
        items = []
        for entry in batch[:5]:
            title = entry.get("title")
            if not title: continue
            candidates = search_media(title)
            if not candidates: continue
            media = candidates[0]; status = entry.get("status","COMPLETED")
            total = media.get("episodes") or media.get("chapters") or 0
            prog = total if status=="COMPLETED" and total>0 else entry.get("progress_delta")
            if ANILIST_TOKEN: save_entry(media["id"], ANILIST_TOKEN, status=status, score=entry.get("score"), progress=prog)
            url = cover_of(media)
            if url: items.append({"url": url, "caption": f"<b>{title_of(media)}</b> \u2705"})
        if items: tg_send(cid, f"\u2705 {len(items)} updated:"); tg_album(cid, items)

    def _surprise(self, cid, p):
        mode = random.choice(["rec","flashback","stat","random"])
        viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
        if mode=="flashback" and viewer:
            entries = get_media_list(username=viewer["name"], status="COMPLETED", per_page=50)
            if entries:
                e = random.choice(entries); media = e.get("media",{})
                tg_photo(cid, cover_of(media), f"\ud83d\udcad <b>Flashback!</b>\n\n<b>{title_of(media)}</b>\n\u2b50 {e.get('score',0)}/10"); return
        if mode=="stat" and viewer:
            stats = get_stats(viewer["name"])
            if stats:
                a = stats.get("anime",{})
                tg_send(cid, f"\ud83e\udd2f <b>\u0647\u0644 \u062a\u0639\u0644\u0645\u061f</b>\n\n\u0634\u0627\u0647\u062f\u062a <b>{a.get('episodesWatched',0)}</b> \u062d\u0644\u0642\u0629!\n= <b>{a.get('minutesWatched',0)//1440} \u064a\u0648\u0645</b> \ud83d\udcfa"); return
        if mode=="rec":
            trending = get_trending(10)
            if trending:
                pick = random.choice(trending)
                kb = kb_make([[{"text": "\ud83d\udd17 AniList", "url": pick.get("siteUrl","")}]])
                tg_photo(cid, cover_of(pick), f"\ud83d\udd25 <b>{title_of(pick)}</b>\n\u2b50 {pick.get('averageScore',0)}%", kb); return
        self._random_anime(cid, p)

    def _news(self, cid, p):
        viewer = get_viewer(ANILIST_TOKEN) if ANILIST_TOKEN else None
        news = []
        if viewer:
            for e in get_media_list(username=viewer["name"], status="CURRENT", per_page=3):
                media = e.get("media",{})
                sch = get_airing(media.get("id"))
                if sch: news.append(f"\ud83d\udcfa <b>{title_of(media)}</b> \u2014 Ep {sch.get('episode','?')} in {sch.get('timeUntilAiring',0)//86400}d")
        for t in get_trending(3)[:2]:
            news.append(f"\ud83d\udd25 <b>{title_of(t)}</b> \u2b50 {t.get('averageScore',0)}%")
        tg_send(cid, "\ud83d\udcf0 <b>News:</b>\n\n" + "\n".join(news) if news else "\u274c")

    def _chat(self, cid, p):
        resp = p.get("chat_response") or "\u0639\u0630\u0631\u0627\u064b\u060c \u0644\u0645 \u0623\u0641\u0647\u0645. \u062c\u0631\u0628 \u0628\u0637\u0631\u064a\u0642\u0629 \u062b\u0627\u0646\u064a\u0629! \ud83d\ude0a"
        tg_send(cid, resp)
        save_context(cid, "bot", resp[:100])

    def _on_callback(self, cb):
        data = cb.get("data",""); cid = cb.get("message",{}).get("chat",{}).get("id")
        mid = cb.get("message",{}).get("message_id"); cbid = cb.get("id")
        if not cid: return
        tg_answer_cb(cbid, "...")
        parts = data.split(":")
        if parts[0]=="del" and len(parts)>=2:
            ok = delete_entry(int(parts[1]), ANILIST_TOKEN) if ANILIST_TOKEN else False
            tg_edit(cid, mid, "\u2705 \u062a\u0645!" if ok else "\u274c")
        elif parts[0]=="cancel": tg_edit(cid, mid, "\u21a9\ufe0f")
        elif parts[0]=="pick" and len(parts)>=3:
            media_id = int(parts[1]); status = parts[2]
            score = float(parts[3]) if len(parts)>3 and parts[3] else None
            is_fav = parts[4]=="True" if len(parts)>4 else False
            q = """query($id:Int){Media(id:$id){id type title{romaji english} episodes chapters
            coverImage{extraLarge large} siteUrl trailer{id site} averageScore}}"""
            res = _gql(q, {"id": media_id})
            media = res.get("data",{}).get("Media")
            if media: self._do_track(cid, media, {"status": status, "score": score, "is_favorite": is_fav, "media_type": media.get("type","ANIME")})

    def _welcome(self, cid):
        clear_context(cid)
        tg_send(cid, "\ud83c\udfac <b>AniList Bot v2.1</b>\n\n\u0623\u0646\u0627 \u0645\u0633\u0627\u0639\u062f\u0643 \u0627\u0644\u0630\u0643\u064a \u0644\u0625\u062f\u0627\u0631\u0629 \u0627\u0644\u0623\u0646\u0645\u064a. \u0643\u0644\u0645\u0646\u064a \u0628\u0627\u0644\u0639\u0631\u0628\u064a!\n\n/help \u0644\u0644\u062f\u0644\u064a\u0644 \ud83d\udcd6")

    def _help(self, cid):
        tg_send(cid, """\ud83d\udcd6 <b>\u0627\u0644\u062f\u0644\u064a\u0644</b>\n\n<b>\u062a\u062a\u0628\u0639:</b> \u0643\u0645\u0644\u062a X \u0648\u0627\u0642\u064a\u0645\u0647 9\n<b>\u062d\u0630\u0641:</b> \u0627\u062d\u0630\u0641 X\n<b>\u0625\u062d\u0635\u0627\u0626\u064a\u0627\u062a:</b> \u0625\u062d\u0635\u0627\u0626\u064a\u0627\u062a\u064a / \u0625\u062d\u0635\u0627\u0626\u064a\u0627\u062a Ahmed\n<b>\u0646\u0634\u0627\u0637\u0627\u062a:</b> \u0622\u062e\u0631 \u0646\u0634\u0627\u0637\u0627\u062a\u064a\n<b>\u0623\u0635\u062f\u0642\u0627\u0621:</b> \u0627\u0635\u062f\u0642\u0627\u0626\u064a / \u0628\u0631\u0648\u0641\u0627\u064a\u0644 Ahmed / \u0642\u0627\u0631\u0646 \u0645\u0639 Ahmed\n<b>\u062a\u0648\u0635\u064a\u0627\u062a:</b> \u0645\u0634\u0627\u0628\u0647 \u0644X / \u0627\u0642\u062a\u0631\u062d \u0623\u0643\u0634\u0646\n<b>\u062a\u0631\u0646\u062f:</b> \u0648\u0634 \u0627\u0644\u062a\u0631\u0646\u062f / \u0627\u0646\u0645\u064a\u0627\u062a \u0627\u0644\u0645\u0648\u0633\u0645\n<b>\u0628\u062d\u062b:</b> \u0634\u062e\u0635\u064a\u0629 X / \u0627\u0633\u062a\u062f\u064a\u0648 MAPPA / \u0645\u062a\u0649 \u0627\u0644\u062d\u0644\u0642\u0629 \u0627\u0644\u062c\u0627\u064a\u0629\n<b>\u0645\u0631\u062d:</b> \u0641\u0627\u062c\u0626\u0646\u064a! / \u0623\u062e\u0628\u0627\u0631\n\n\ud83e\udde0 \u0643\u0644\u0645\u0646\u064a \u0637\u0628\u064a\u0639\u064a \u0648\u0628\u0641\u0647\u0645\u0643!""")
