"""
AniList Telegram Bot - Vercel Serverless Webhook
All modules combined into a single file for reliable Vercel deployment.
"""
import os
import re
import json
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler

# ============================================================
# CONFIG
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ANILIST_TOKEN = os.environ.get("ANILIST_ACCESS_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

# ============================================================
# ANILIST API
# ============================================================
ANILIST_URL = "https://graphql.anilist.co"

def make_graphql_request(query, variables=None, access_token=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AniListBot/1.0"
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    data = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(ANILIST_URL, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_content = e.read().decode("utf-8")
        try:
            return json.loads(error_content)
        except Exception:
            return {"errors": [{"message": f"HTTP {e.code}: {error_content[:200]}"}]}
    except Exception as e:
        return {"errors": [{"message": str(e)}]}

def search_media_candidates(title, media_type="ANIME", per_page=4):
    query = """
    query ($search: String, $type: MediaType, $perPage: Int) {
      Page (perPage: $perPage) {
        media (search: $search, type: $type) {
          id type
          title { romaji english native }
          episodes chapters format seasonYear
        }
      }
    }
    """
    res = make_graphql_request(query, {"search": title, "type": media_type, "perPage": per_page})
    if "errors" in res:
        return []
    return res.get("data", {}).get("Page", {}).get("media", [])

def get_user_media_entry(media_id, access_token):
    query = """
    query ($mediaId: Int) {
      MediaList (mediaId: $mediaId) {
        id status score progress progressVolumes
      }
    }
    """
    res = make_graphql_request(query, {"mediaId": media_id}, access_token=access_token)
    if "errors" in res:
        return None
    return res.get("data", {}).get("MediaList")

def save_user_media_entry(media_id, access_token, status=None, score=None, progress=None):
    mutation = """
    mutation ($mediaId: Int, $status: MediaListStatus, $score: Float, $progress: Int) {
      SaveMediaListEntry (mediaId: $mediaId, status: $status, score: $score, progress: $progress) {
        id status score progress
        media { title { romaji english } }
      }
    }
    """
    variables = {"mediaId": media_id}
    if status:
        variables["status"] = status
    if score is not None:
        variables["score"] = score
    if progress is not None:
        variables["progress"] = progress
    res = make_graphql_request(mutation, variables, access_token=access_token)
    if "errors" in res:
        return {"error": res["errors"]}
    return res.get("data", {}).get("SaveMediaListEntry")

def toggle_favourite_anime(media_id, access_token):
    mutation = """
    mutation ($animeId: Int) {
      ToggleFavourite (animeId: $animeId) {
        anime { nodes { id } }
      }
    }
    """
    return make_graphql_request(mutation, {"animeId": media_id}, access_token=access_token)

def search_character(name):
    query = """
    query ($search: String) {
      Character (search: $search) {
        id
        name { full native }
        siteUrl
        media (perPage: 1, sort: POPULARITY_DESC) {
          nodes { title { romaji english } }
        }
      }
    }
    """
    res = make_graphql_request(query, {"search": name})
    if "errors" in res:
        return None
    return res.get("data", {}).get("Character")

def recommend_anime_by_genre(genre, limit=4):
    query = """
    query ($genre: String, $limit: Int) {
      Page (perPage: $limit) {
        media (genre: $genre, type: ANIME, sort: SCORE_DESC) {
          id
          title { romaji english }
          averageScore episodes siteUrl
        }
      }
    }
    """
    res = make_graphql_request(query, {"genre": genre, "limit": limit})
    if "errors" in res:
        return []
    return res.get("data", {}).get("Page", {}).get("media", [])

# ============================================================
# ARABIC PARSER (regex fallback)
# ============================================================
ARABIC_ANIME_DICTIONARY = {
    "\u0627\u0644\u0644\u0639\u0646\u0627\u062a": "Jujutsu Kaisen",
    "\u0627\u0646\u0645\u064a \u0627\u0644\u0644\u0639\u0646\u0627\u062a": "Jujutsu Kaisen",
    "\u062c\u062c\u062a\u0633\u0648 \u0643\u0627\u064a\u0633\u0646": "Jujutsu Kaisen",
    "\u062c\u0648\u062c\u0648\u062a\u0633\u0648 \u0643\u0627\u064a\u0633\u0646": "Jujutsu Kaisen",
    "\u062c\u0648\u062c\u0648\u062a\u0633\u0648": "Jujutsu Kaisen",
    "\u0647\u062c\u0648\u0645 \u0627\u0644\u0639\u0645\u0627\u0644\u0642\u0629": "Attack on Titan",
    "\u0627\u0644\u0639\u0645\u0627\u0644\u0642\u0629": "Attack on Titan",
    "\u0627\u062a\u0627\u0643 \u0627\u0648\u0646 \u062a\u0627\u064a\u062a\u0646": "Attack on Titan",
    "\u0627\u062a\u0627\u0643": "Attack on Titan",
    "\u0645\u0630\u0643\u0631\u0629 \u0627\u0644\u0645\u0648\u062a": "Death Note",
    "\u062f\u062b \u0646\u0648\u062a": "Death Note",
    "\u0642\u0627\u062a\u0644 \u0627\u0644\u0634\u064a\u0627\u0637\u064a\u0646": "Demon Slayer: Kimetsu no Yaiba",
    "\u062f\u064a\u0645\u0648\u0646 \u0633\u0644\u0627\u064a\u0631": "Demon Slayer",
    "\u0631\u062c\u0644 \u0627\u0644\u0645\u0646\u0634\u0627\u0631": "Chainsaw Man",
    "\u062a\u0634\u064a\u0646\u0633\u0648 \u0645\u0627\u0646": "Chainsaw Man",
    "\u0631\u0627\u062c\u0646\u0627": "Ragna Crimson",
    "\u0631\u0627\u063a\u0646\u0627": "Ragna Crimson",
    "\u0647\u0646\u062a\u0631": "Hunter x Hunter",
    "\u0627\u0644\u0642\u0646\u0627\u0635": "Hunter x Hunter",
    "\u0647\u0646\u062a\u0631 \u0647\u0627\u0646\u062a\u0631": "Hunter x Hunter",
    "\u0648\u0646 \u0628\u064a\u0633": "One Piece",
    "\u0646\u0627\u0631\u0648\u062a\u0648": "Naruto",
    "\u062f\u0631\u0627\u063a\u0648\u0646 \u0628\u0648\u0644": "Dragon Ball",
    "\u0643\u0648\u0646\u0627\u0646": "Detective Conan",
    "\u0627\u0644\u0645\u062d\u0642\u0642 \u0643\u0648\u0646\u0627\u0646": "Detective Conan",
    "\u0627\u0643\u0627\u062f\u064a\u0645\u064a\u062a\u064a \u0644\u0644\u0627\u0628\u0637\u0627\u0644": "My Hero Academia",
    "\u0623\u0643\u0627\u062f\u064a\u0645\u064a\u0629 \u0628\u0637\u0644": "My Hero Academia",
    "\u0637\u0648\u0643\u064a\u0648 \u063a\u0648\u0644": "Tokyo Ghoul",
    "\u0628\u0644\u064a\u062a\u0634": "Bleach",
    "\u0633\u0648\u0644\u0648 \u0644\u064a\u0641\u0644\u064a\u0646\u062c": "Solo Leveling",
    "\u0631\u0641\u0639 \u0627\u0644\u0645\u0633\u062a\u0648\u0649 \u0641\u0631\u062f\u064a\u0627": "Solo Leveling",
}

GENRE_MAP = {
    "\u0623\u0643\u0634\u0646": "Action", "\u0645\u063a\u0627\u0645\u0631\u0629": "Adventure", "\u0643\u0648\u0645\u064a\u062f\u064a": "Comedy",
    "\u062f\u0631\u0627\u0645\u0627": "Drama", "\u062e\u064a\u0627\u0644\u064a": "Fantasy", "\u063a\u0645\u0648\u0636": "Mystery",
    "\u0631\u0639\u0628": "Horror", "\u0631\u0648\u0645\u0627\u0646\u0633\u064a": "Romance", "\u062e\u064a\u0627\u0644 \u0639\u0644\u0645\u064a": "Sci-Fi",
    "\u0634\u0631\u064a\u062d\u0629 \u0645\u0646 \u0627\u0644\u062d\u064a\u0627\u0629": "Slice of Life", "\u0631\u064a\u0627\u0636\u064a": "Sports",
    "\u0646\u0641\u0633\u064a": "Psychological", "\u0625\u062b\u0627\u0631\u0629": "Thriller",
}

def regex_parse(text):
    text_clean = text.strip()
    result = {
        "action": "TRACK", "media_type": "ANIME", "title": None,
        "status": None, "score": None, "progress_delta": None,
        "absolute_progress": None, "is_favorite": False,
        "genre": None, "original_text": text,
    }
    lower_text = text_clean.lower()
    if any(k in lower_text for k in ["\u0645\u0641\u0636\u0644\u0629", "\u0645\u0641\u0636\u0644\u062a\u064a", "\u0627\u0641\u0636\u0644 \u0627\u0646\u0645\u064a", "favorite", "favourite"]):
        result["is_favorite"] = True
    if any(k in lower_text for k in ["\u0627\u0642\u062a\u0631\u062d", "\u0627\u0642\u062a\u0631\u0627\u062d", "\u062a\u0631\u0634\u064a\u062d", "\u0631\u0634\u062d", "\u0623\u0646\u0645\u064a \u062d\u0644\u0648", "\u0627\u0646\u0645\u064a \u0645\u0645\u062a\u0627\u0632"]):
        result["action"] = "RECOMMEND"
        for g_ar, g_en in GENRE_MAP.items():
            if g_ar in lower_text:
                result["genre"] = g_en
                break
        if not result["genre"]:
            result["genre"] = "Action"
        return result
    if any(k in lower_text for k in ["\u0634\u062e\u0635\u064a\u0629", "\u0645\u064a\u0646 \u0645\u0624\u062f\u064a", "\u0645\u0624\u062f\u064a \u0635\u0648\u062a", "\u0635\u0648\u062a \u0634\u062e\u0635\u064a\u0629"]):
        result["action"] = "CHARACTER_LOOKUP"
        cleaned_char = text_clean
        for filler in ["\u0634\u062e\u0635\u064a\u0629", "\u0645\u064a\u0646 \u0645\u0624\u062f\u064a \u0635\u0648\u062a", "\u0645\u0624\u062f\u064a \u0635\u0648\u062a", "\u0641\u064a \u0627\u0646\u0645\u064a", "\u0623\u0646\u0645\u064a"]:
            cleaned_char = re.sub(filler, '', cleaned_char, flags=re.IGNORECASE).strip()
        result["title"] = cleaned_char
        return result
    if any(k in lower_text for k in ["\u0645\u0627\u0646\u062c\u0627", "\u0645\u0627\u0646\u0647\u0627", "\u0645\u0627\u0646\u0647\u0648\u0627", "\u0641\u0635\u0644", "\u0641\u0635\u0648\u0644", "\u0634\u0627\u0628\u062a\u0631", "chapter", "read"]):
        result["media_type"] = "MANGA"
    score_match = re.search(r'(?:\u062a\u0642\u064a\u064a\u0645|\u0642\u064a\u0645\u062a\u0647|\u062a\u0642\u064a\u064a\u0645\u064a|\u0627\u0642\u064a\u0645\u0647|\u0623\u0642\u064a\u0645\u0647|score)?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:/10|\u0645\u0646 10)?', text_clean, re.IGNORECASE)
    if score_match:
        try:
            val = float(score_match.group(1))
            if 0 <= val <= 10:
                result["score"] = val
        except ValueError:
            pass
    if any(k in lower_text for k in ["\u0633\u062d\u0628\u062a", "\u062a\u0631\u0643\u062a\u0647", "\u0643\u0646\u0633\u0644\u062a", "dropped"]):
        result["status"] = "DROPPED"
    elif any(k in lower_text for k in ["plan", "\u062e\u0637\u0629", "\u0623\u0641\u0643\u0631", "\u0636\u064a\u0641", "\u0627\u0636\u0641", "\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u0627\u0646\u062a\u0638\u0627\u0631", "\u0628\u0634\u0648\u0641\u0647", "\u0628\u0642\u0631\u0627\u0647"]):
        result["status"] = "PLANNING"
    elif any(k in lower_text for k in ["\u062e\u0644\u0635\u062a", "\u0623\u0646\u0647\u064a\u062a", "\u0646\u0647\u064a\u062a", "\u062e\u062a\u0645\u062a", "completed", "\u0627\u0646\u062a\u0647\u064a\u062a"]):
        result["status"] = "COMPLETED"
    elif any(k in lower_text for k in ["\u0648\u0642\u0641\u062a", "\u062a\u0648\u0642\u0641\u062a", "paused", "\u0645\u0639\u0644\u0642"]):
        result["status"] = "PAUSED"
    elif any(k in lower_text for k in ["\u0634\u0641\u062a", "\u062a\u0627\u0628\u0639", "\u062a\u0627\u0628\u0639\u062a", "\u0634\u0627\u0647\u062f\u062a", "\u062d\u0644\u0642\u0629", "\u062d\u0644\u0642\u0627\u062a", "\u0642\u0631\u064a\u062a", "\u0642\u0631\u0623\u062a", "watching", "reading"]):
        result["status"] = "CURRENT"
    if "\u062d\u0644\u0642\u062a\u064a\u0646" in lower_text or "\u0641\u0635\u0644\u064a\u0646" in lower_text or "\u0634\u0627\u0628\u062a\u0631\u064a\u0646" in lower_text:
        result["progress_delta"] = 2
        if not result["status"]:
            result["status"] = "CURRENT"
    elif any(k in lower_text for k in ["\u062d\u0644\u0642\u0629", "\u062d\u0644\u0642\u0627\u062a", "\u0641\u0635\u0644", "\u0641\u0635\u0648\u0644", "\u0634\u0627\u0628\u062a\u0631", "\u0634\u0627\u0628\u062a\u0631\u0627\u062a"]):
        ep_match = re.search(r'(\d+)\s*(?:\u062d\u0644\u0642\u0629|\u062d\u0644\u0642\u0627\u062a|\u0641\u0635\u0644|\u0641\u0635\u0648\u0644|\u0634\u0627\u0628\u062a\u0631|eps|episodes|chapters)', lower_text)
        if ep_match:
            result["progress_delta"] = int(ep_match.group(1))
        else:
            result["progress_delta"] = 1
        if not result["status"]:
            result["status"] = "CURRENT"
    abs_match = re.search(r'(?:\u0644\u0644\u062d\u0644\u0642\u0629|\u062d\u0644\u0642\u0629|\u0648\u0627\u0635\u0644|\u0641\u0635\u0644|\u0634\u0627\u0628\u062a\u0631|ep|ch)\s*(\d+)', lower_text)
    if abs_match:
        result["absolute_progress"] = int(abs_match.group(1))
    cleaned_title = text_clean
    cleaned_title = re.sub(r'(?:\u0648\s*)?(?:\u0648\u0627\u0631\u064a\u062f\s*)?(?:\u062a\u0642\u064a\u064a\u0645|\u0642\u064a\u0645\u062a\u0647|\u062a\u0642\u064a\u064a\u0645\u064a|\u0627\u0642\u064a\u0645\u0647|\u0623\u0642\u064a\u0645\u0647|score)?\s*[0-9]+(?:\.[0-9]+)?\s*(?:/10|\u0645\u0646 10)?', '', cleaned_title, flags=re.IGNORECASE)
    cleaned_title = re.sub(r'(?:\u0644\u0644\u062d\u0644\u0642\u0629|\u062d\u0644\u0642\u0629|\u062d\u0644\u0642\u0627\u062a|\u062d\u0644\u0642\u062a\u064a\u0646|\u0641\u0635\u0644|\u0641\u0635\u0648\u0644|\u0634\u0627\u0628\u062a\u0631|eps|chapters)\s*\d*', '', cleaned_title, flags=re.IGNORECASE)
    cleaned_title = re.sub(r'(?:\u0648\s*)?(?:\u0627\u0639\u0645\u0644\u0647|\u0625\u0639\u0645\u0644\u0647|\u0627\u062c\u0639\u0644\u0647|\u0623\u062c\u0639\u0644\u0647)?\s*(?:\u0645\u0641\u0636\u0644\u0629|\u0645\u0641\u0636\u0644\u062a\u064a|favorite)?', '', cleaned_title, flags=re.IGNORECASE)
    for pattern in [r'^\s*\u0634\u0641\u062a\s+\u0645\u0646', r'^\s*\u0634\u0641\u062a', r'^\s*\u062a\u0627\u0628\u0639\u062a', r'^\s*\u0634\u0627\u0647\u062f\u062a', r'^\s*\u0642\u0631\u064a\u062a', r'^\s*\u0642\u0631\u0623\u062a', r'^\s*\u062e\u0644\u0635\u062a', r'^\s*\u0623\u0646\u0647\u064a\u062a', r'^\s*\u0646\u0647\u064a\u062a', r'^\s*\u062e\u062a\u0645\u062a', r'^\s*\u0636\u064a\u0641', r'^\s*\u0627\u0636\u0641', r'^\s*\u0623\u0641\u0643\u0631\s+\u0623\u0634\u0648\u0641', r'^\s*\u0633\u062d\u0628\u062a\s+\u0639\u0644\u0649', r'^\s*\u0648\u0642\u0641\u062a', r'^\s*\u0645\u0627\u0646\u062c\u0627', r'^\s*\u0627\u0646\u0645\u064a']:
        cleaned_title = re.sub(pattern, '', cleaned_title, flags=re.IGNORECASE).strip()
    for pattern in [r'\u0644\u0644\u0642\u0627\u0626\u0645\u0629\s*$', r'\u0641\u064a\s*\u0627\u0644\u0640\s*plan\s*$', r'\u062e\u0637\u0629\s*$', r'\u0645\u0646\s*$', r'\u0648\s*$', r'\u0645\u0627\u0646\u062c\u0627\s*$', r'\u0627\u0646\u0645\u064a\s*$']:
        cleaned_title = re.sub(pattern, '', cleaned_title, flags=re.IGNORECASE).strip()
    cleaned_title = re.sub(r'^\s*(?:\u0645\u0646|\u0639\u0644\u0649|\u0641\u064a|\u0648)\s+', '', cleaned_title).strip()
    cleaned_title = re.sub(r'\s+(?:\u0648|\u0645\u0646|\u0639\u0644\u0649|\u0641\u064a)\s*$', '', cleaned_title).strip()
    for nick, official in ARABIC_ANIME_DICTIONARY.items():
        if nick in cleaned_title.lower():
            cleaned_title = official
            break
    result["title"] = cleaned_title if cleaned_title else text_clean
    return result

# ============================================================
# GEMINI AI PARSER
# ============================================================
def call_gemini_api(text, model_name, key):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={key}"
    system_prompt = """You are an expert AniList anime & manga assistant.
Extract and return ONLY a JSON object with keys:
- "action": 'TRACK', 'CHARACTER_LOOKUP', or 'RECOMMEND'
- "media_type": 'ANIME' or 'MANGA'
- "title": Official English/Romaji title for AniList search (or character name)
- "status": 'COMPLETED', 'CURRENT', 'PLANNING', 'PAUSED', or 'DROPPED'
- "score": user score out of 10 (number or null)
- "progress_delta": episode/chapter count added (integer or null)
- "absolute_progress": absolute episode/chapter number (integer or null)
- "is_favorite": true if user wants to set as favorite
- "genre": e.g. 'Action', 'Comedy', 'Mystery', 'Romance', 'Psychological'
- "confidence": 'high', 'medium', or 'low'
- "alternatives": alternative matching titles if ambiguous
Respond ONLY with valid JSON."""
    payload = {
        "contents": [{"parts": [{"text": f"Analyze this user prompt: '{text}'"}]}],
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0.1},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as response:
        res_json = json.loads(response.read().decode("utf-8"))
        content_text = res_json["candidates"][0]["content"]["parts"][0]["text"]
        result = json.loads(content_text)
        result["original_text"] = text
        return result

def parse_with_gemini(text, api_key=None):
    key = api_key or GEMINI_KEY
    if not key:
        parsed = regex_parse(text)
        parsed["confidence"] = "high" if parsed["title"] or parsed.get("genre") else "low"
        parsed["alternatives"] = []
        return parsed
    models_to_try = [GEMINI_MODEL, "gemini-2.0-flash", "gemini-1.5-flash"]
    seen = set()
    for model in models_to_try:
        if model in seen:
            continue
        seen.add(model)
        try:
            return call_gemini_api(text, model, key)
        except Exception as e:
            print(f"[Gemini] Model {model} failed: {e}")
            continue
    parsed = regex_parse(text)
    parsed["confidence"] = "low"
    parsed["alternatives"] = []
    return parsed

# ============================================================
# TELEGRAM HELPERS
# ============================================================
def send_telegram_message(chat_id, text):
    if not TELEGRAM_TOKEN:
        print("[ERROR] TELEGRAM_BOT_TOKEN not set!")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"[Telegram] Sent OK to {chat_id}")
    except Exception as e:
        print(f"[Telegram] Send error: {e}")

# ============================================================
# MAIN HANDLER
# ============================================================
class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status = {
            "status": "running",
            "bot": "AniList Telegram Bot",
            "telegram_token": "SET" if TELEGRAM_TOKEN else "MISSING",
            "anilist_token": "SET" if ANILIST_TOKEN else "MISSING",
            "gemini_key": "SET" if GEMINI_KEY else "MISSING",
            "gemini_model": GEMINI_MODEL,
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(status, indent=2).encode("utf-8"))

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        chat_id = None
        try:
            update = json.loads(body.decode("utf-8"))
            message = update.get("message", {})
            chat_id = message.get("chat", {}).get("id")
            text = message.get("text", "")
            if not text or not chat_id:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"no_text"}')
                return
            if text.startswith("/start"):
                send_telegram_message(chat_id, "\u0645\u0631\u062d\u0628\u0627\u064b \u0628\u0643! \U0001f3ac\U0001f4d6\n\u0623\u0646\u0627 \u0628\u0648\u062a AniList \u0627\u0644\u0634\u0627\u0645\u0644.\n\n<b>\u0627\u0644\u0645\u0645\u064a\u0632\u0627\u062a:</b>\n1\ufe0f\u20e3 <b>\u062a\u062a\u0628\u0639 \u0627\u0644\u0623\u0646\u0645\u064a \u0648\u0627\u0644\u0645\u0627\u0646\u062c\u0627:</b>\n\u2022 <i>\u0634\u0641\u062a \u062d\u0644\u0642\u062a\u064a\u0646 \u0645\u0646 \u0627\u0646\u0645\u064a \u0627\u0644\u0644\u0639\u0646\u0627\u062a</i>\n\u2022 <i>\u0634\u0641\u062a \u0627\u0646\u0645\u064a \u0631\u0627\u062c\u0646\u0627 \u0648\u0627\u0642\u064a\u0645\u0647 10 \u0648\u0627\u0639\u0645\u0644\u0647 \u0645\u0641\u0636\u0644\u0629</i>\n\u2022 <i>\u0642\u0631\u064a\u062a 5 \u0641\u0635\u0648\u0644 \u0645\u0646 Solo Leveling</i>\n\n2\ufe0f\u20e3 <b>\u0627\u0644\u0628\u062d\u062b \u0639\u0646 \u0627\u0644\u0634\u062e\u0635\u064a\u0627\u062a:</b>\n\u2022 <i>\u0645\u064a\u0646 \u0634\u062e\u0635\u064a\u0629 \u0644\u0648\u0641\u0627\u064a\u061f</i>\n\n3\ufe0f\u20e3 <b>\u0627\u0642\u062a\u0631\u0627\u062d\u0627\u062a \u0627\u0644\u0623\u0646\u0645\u064a\u0627\u062a:</b>\n\u2022 <i>\u0627\u0642\u062a\u0631\u062d \u0644\u064a \u0623\u0646\u0645\u064a \u063a\u0645\u0648\u0636 \u0645\u0645\u062a\u0627\u0632</i>")
            else:
                self._handle_command(chat_id, text)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        except Exception as e:
            print(f"[WEBHOOK ERROR] {e}")
            self.send_response(200)
            self.end_headers()
            if chat_id:
                send_telegram_message(chat_id, f"\u274c \u062d\u062f\u062b \u062e\u0637\u0623: {str(e)[:200]}")
            self.wfile.write(json.dumps({"status": "error", "message": str(e)[:200]}).encode("utf-8"))

    def _handle_command(self, chat_id, text):
        parsed = parse_with_gemini(text, api_key=GEMINI_KEY)
        action = parsed.get("action", "TRACK")
        media_type = parsed.get("media_type", "ANIME")
        if action == "RECOMMEND":
            self._handle_recommend(chat_id, parsed)
        elif action == "CHARACTER_LOOKUP":
            self._handle_character(chat_id, parsed)
        else:
            self._handle_track(chat_id, parsed, media_type)

    def _handle_recommend(self, chat_id, parsed):
        genre = parsed.get("genre", "Action")
        recs = recommend_anime_by_genre(genre, limit=4)
        if not recs:
            send_telegram_message(chat_id, f"\u274c \u0644\u0645 \u0623\u0633\u062a\u0637\u0639 \u0627\u0644\u0639\u062b\u0648\u0631 \u0639\u0644\u0649 \u0627\u0642\u062a\u0631\u0627\u062d\u0627\u062a \u0644\u062a\u0635\u0646\u064a\u0641: <b>{genre}</b>")
            return
        msg = f"\U0001f31f <b>\u0623\u0641\u0636\u0644 \u0627\u0642\u062a\u0631\u0627\u062d\u0627\u062a \u0623\u0646\u0645\u064a \u0644\u062a\u0635\u0646\u064a\u0641 ({genre}):</b>\n\n"
        for idx, item in enumerate(recs, 1):
            name = item["title"].get("english") or item["title"]["romaji"]
            score = (item.get("averageScore") or 0) / 10.0
            msg += f"{idx}. <b>{name}</b> \u2014 \u2b50 {score}/10\n"
        send_telegram_message(chat_id, msg)

    def _handle_character(self, chat_id, parsed):
        char_query = parsed.get("title")
        char = search_character(char_query)
        if not char:
            send_telegram_message(chat_id, f"\u274c \u0644\u0645 \u0623\u062c\u062f \u0634\u062e\u0635\u064a\u0629 \u0628\u0627\u0633\u0645: <b>{char_query}</b>")
            return
        full_name = char["name"]["full"]
        native_name = char["name"].get("native") or ""
        anime_rel = "\u063a\u064a\u0631 \u0645\u062d\u062f\u062f"
        if char.get("media", {}).get("nodes"):
            node = char["media"]["nodes"][0]
            anime_rel = node["title"].get("english") or node["title"]["romaji"]
        reply = f"\U0001f464 <b>\u0645\u0639\u0644\u0648\u0645\u0627\u062a \u0627\u0644\u0634\u062e\u0635\u064a\u0629:</b>\n\n\u0627\u0633\u0645 \u0627\u0644\u0634\u062e\u0635\u064a\u0629: <b>{full_name}</b> ({native_name})\n\u0645\u0646 \u0623\u0646\u0645\u064a/\u0645\u0627\u0646\u062c\u0627: <b>{anime_rel}</b>\n\U0001f517 <a href='{char['siteUrl']}'>\u0631\u0627\u0628\u0637 AniList</a>"
        send_telegram_message(chat_id, reply)

    def _handle_track(self, chat_id, parsed, media_type):
        title = parsed.get("title")
        if not title:
            send_telegram_message(chat_id, "\u274c \u0644\u0645 \u0623\u0633\u062a\u0637\u0639 \u0641\u0647\u0645 \u0627\u0633\u0645 \u0627\u0644\u0623\u0646\u0645\u064a/\u0627\u0644\u0645\u0627\u0646\u062c\u0627. \u062d\u0627\u0648\u0644 \u0645\u0631\u0629 \u0623\u062e\u0631\u0649.")
            return
        candidates = search_media_candidates(title, media_type=media_type, per_page=4)
        if not candidates:
            send_telegram_message(chat_id, f"\u274c \u0644\u0645 \u0623\u0633\u062a\u0637\u0639 \u0627\u0644\u0639\u062b\u0648\u0631 \u0639\u0644\u0649 \u0627\u0644\u0640 {media_type.lower()} \u0628\u0627\u0633\u0645: <b>{title}</b>")
            return
        if len(candidates) > 1 and parsed.get("confidence") == "medium":
            msg = f"\U0001f914 <b>\u0647\u0644 \u062a\u0642\u0635\u062f \u0623\u062d\u062f \u0647\u0630\u0647 \u0627\u0644\u0640 {media_type} \u0644\u0640 '{title}'\u061f</b>\n\n"
            for idx, cand in enumerate(candidates, 1):
                cand_name = cand["title"].get("english") or cand["title"]["romaji"]
                year = f" ({cand['seasonYear']})" if cand.get("seasonYear") else ""
                msg += f"{idx}. <b>{cand_name}</b>{year}\n"
            msg += "\n\u064a\u0631\u062c\u0649 \u0625\u0639\u0627\u062f\u0629 \u0643\u062a\u0627\u0628\u0629 \u0627\u0644\u0627\u0633\u0645 \u0627\u0644\u062f\u0642\u064a\u0642."
            send_telegram_message(chat_id, msg)
            return
        media = candidates[0]
        title_display = media["title"].get("english") or media["title"]["romaji"]
        media_id = media["id"]
        status = parsed.get("status") or "COMPLETED"
        score = parsed.get("score")
        is_fav = parsed.get("is_favorite", False)
        current_entry = get_user_media_entry(media_id, ANILIST_TOKEN) if ANILIST_TOKEN else None
        current_progress = current_entry.get("progress", 0) if current_entry else 0
        new_progress = current_progress
        if parsed.get("absolute_progress") is not None:
            new_progress = parsed["absolute_progress"]
        elif parsed.get("progress_delta") is not None:
            new_progress += parsed["progress_delta"]
        unit_label = "\u0627\u0644\u0641\u0635\u0644" if media_type == "MANGA" else "\u0627\u0644\u062d\u0644\u0642\u0629"
        if ANILIST_TOKEN:
            save_result = save_user_media_entry(media_id, ANILIST_TOKEN, status=status, score=score, progress=new_progress)
            if is_fav:
                try:
                    toggle_favourite_anime(media_id, ANILIST_TOKEN)
                except Exception as e:
                    print(f"[Fav] Error: {e}")
            if isinstance(save_result, dict) and "error" in save_result:
                reply = f"\u26a0\ufe0f <b>\u062e\u0637\u0623 \u0645\u0646 AniList:</b> {str(save_result['error'])[:200]}"
            else:
                reply = f"\u2705 <b>\u062a\u0645 \u0627\u0644\u062a\u062d\u062f\u064a\u062b \u0641\u064a AniList \u0628\u0646\u062c\u0627\u062d!</b>\n\n\U0001f4fa <b>\u0627\u0644\u0640 {media_type}:</b> {title_display}\n\U0001f4cc <b>\u0627\u0644\u062d\u0627\u0644\u0629:</b> {status}\n\U0001f522 <b>{unit_label}:</b> {new_progress}"
                if score is not None:
                    reply += f"\n\u2b50 <b>\u0627\u0644\u062a\u0642\u064a\u064a\u0645:</b> {score}/10"
                if is_fav:
                    reply += f"\n\u2764\ufe0f <b>\u062a\u0645\u062a \u0625\u0636\u0627\u0641\u062a\u0647 \u0625\u0644\u0649 \u0627\u0644\u0645\u0641\u0636\u0644\u0629!</b>"
        else:
            reply = f"\U0001f50d <b>\u062a\u0645 \u0627\u0644\u0639\u062b\u0648\u0631 \u0639\u0644\u0649 \u0627\u0644\u0640 {media_type}:</b> {title_display}\n\U0001f4cc <b>\u0627\u0644\u062d\u0627\u0644\u0629:</b> {status}\n\U0001f522 <b>{unit_label}:</b> {new_progress}\n\u2b50 <b>\u0627\u0644\u062a\u0642\u064a\u064a\u0645:</b> {score or '\u063a\u064a\u0631 \u0645\u062d\u062f\u062f'}\n\n\u26a0\ufe0f <i>\u0645\u0644\u0627\u062d\u0638\u0629: \u064a\u0631\u062c\u0649 \u0625\u0636\u0627\u0641\u0629 ANILIST_ACCESS_TOKEN \u0644\u064a\u062a\u0645 \u0627\u0644\u062a\u062d\u062f\u064a\u062b \u0627\u0644\u0641\u0639\u0644\u064a.</i>"
            if is_fav:
                reply += "\n\u2764\ufe0f <b>\u0627\u0644\u062d\u0627\u0644\u0629: \u0645\u0641\u0636\u0644\u0629</b>"
        send_telegram_message(chat_id, reply)
