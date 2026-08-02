import os
import json
import urllib.request
import urllib.error
from ai_parser import parse_user_prompt as regex_parse

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

def call_gemini_api(text: str, model_name: str, key: str) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={key}"
    
    system_prompt = """
You are an expert AniList anime & manga assistant.
Extract and return ONLY a JSON object with keys:
- "action": (string) 'TRACK' (for watching/reading updates), 'CHARACTER_LOOKUP' (for character/voice actor questions), or 'RECOMMEND' (for recommendation requests)
- "media_type": (string) 'ANIME' or 'MANGA'
- "title": (string or null) Official English/Romaji title for AniList or character name
- "status": (string or null) 'COMPLETED', 'CURRENT', 'PLANNING', 'PAUSED', or 'DROPPED'
- "score": (number or null) user score out of 10
- "progress_delta": (integer or null) episode/chapter count added (e.g. +2)
- "absolute_progress": (integer or null) absolute episode/chapter number (e.g. 15)
- "is_favorite": (boolean) true if user wants to set as favorite (e.g. 'اعمله مفضلة')
- "genre": (string or null) e.g. 'Action', 'Comedy', 'Mystery', 'Romance', 'Psychological'
- "confidence": (string) 'high' if confident, 'medium' or 'low' if ambiguous
- "alternatives": (list of strings) alternative matching titles or seasons if ambiguous

Respond ONLY with valid JSON, no markdown code blocks.
"""

    payload = {
        "contents": [{
            "parts": [{"text": f"Analyze this user prompt: '{text}'"}]
        }],
        "system_instruction": {
            "parts": [{"text": system_prompt}]
        },
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.1
        }
    }

    headers = {"Content-Type": "application/json"}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers)

    with urllib.request.urlopen(req) as response:
        res_body = response.read().decode("utf-8")
        res_json = json.loads(res_body)
        content_text = res_json["candidates"][0]["content"]["parts"][0]["text"]
        result = json.loads(content_text)
        result["original_text"] = text
        return result

def parse_with_gemini(text: str, api_key: str = None) -> dict:
    key = api_key or GEMINI_API_KEY
    if not key:
        parsed = regex_parse(text)
        parsed["confidence"] = "high" if parsed["title"] or parsed.get("genre") else "low"
        parsed["alternatives"] = []
        return parsed

    models_to_try = [DEFAULT_MODEL, "gemini-2.0-flash", "gemini-1.5-flash"]
    for model in models_to_try:
        try:
            return call_gemini_api(text, model, key)
        except Exception as e:
            print(f"Model {model} failed, trying fallback:", e)
            continue

    parsed = regex_parse(text)
    parsed["confidence"] = "low"
    parsed["alternatives"] = []
    return parsed
