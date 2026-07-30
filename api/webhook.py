import os
import json
import urllib.request
from http.server import BaseHTTPRequestHandler
from anilist_api import (
    search_media, search_media_candidates, get_user_media_entry, 
    save_user_media_entry, search_character, recommend_anime_by_genre
)
from gemini_parser import parse_with_gemini

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ANILIST_TOKEN = os.environ.get("ANILIST_ACCESS_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

def send_telegram_message(chat_id: int, text: str):
    if not TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req)
    except Exception as e:
        print("Telegram Send Error:", e)

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        
        try:
            update = json.loads(body.decode('utf-8'))
            message = update.get('message', {})
            chat_id = message.get('chat', {}).get('id')
            text = message.get('text', '')

            if text and chat_id:
                if text.startswith('/start'):
                    send_telegram_message(
                        chat_id, 
                        "مرحباً بك! 🎬📖\nأنا بوت AniList الشامل للأجهزة والأنمي والمانجا.\n\n"
                        "<b>المميزات:</b>\n"
                        "1️⃣ <b>تتبع الأنمي والمانجا:</b>\n"
                        "• <i>شفت حلقتين من انمي اللعنات</i>\n"
                        "• <i>قريت 5 فصول من Solo Leveling</i>\n"
                        "• <i>خلصت هجوم العمالقة وقيمته 10</i>\n\n"
                        "2️⃣ <b>البحث عن الشخصيات:</b>\n"
                        "• <i>مين شخصية لوفاي؟</i>\n\n"
                        "3️⃣ <b>اقتراحات الأنميات:</b>\n"
                        "• <i>اقترح لي أنمي غموض ممتاز</i>"
                    )
                else:
                    parsed = parse_with_gemini(text, api_key=GEMINI_KEY)
                    action = parsed.get("action", "TRACK")
                    media_type = parsed.get("media_type", "ANIME")

                    if action == "RECOMMEND":
                        genre = parsed.get("genre", "Action")
                        recs = recommend_anime_by_genre(genre, limit=4)
                        if not recs:
                            send_telegram_message(chat_id, f"❌ لم أستطع العثور على اقتراحات لتصنيف: <b>{genre}</b>")
                        else:
                            msg = f"🌟 <b>أفضل اقتراحات أنمي لتصنيف ({genre}):</b>\n\n"
                            for idx, item in enumerate(recs, 1):
                                name = item['title']['english'] or item['title']['romaji']
                                score = item.get('averageScore', 0) / 10.0
                                msg += f"{idx}. <b>{name}</b> — ⭐ {score}/10\n"
                            send_telegram_message(chat_id, msg)

                    elif action == "CHARACTER_LOOKUP":
                        char_query = parsed.get("title")
                        char = search_character(char_query)
                        if not char:
                            send_telegram_message(chat_id, f"❌ لم أجد شخصية باسم: <b>{char_query}</b>")
                        else:
                            full_name = char['name']['full']
                            native_name = char['name']['native'] or ""
                            anime_rel = "غير محدد"
                            if char.get('media', {}).get('nodes'):
                                node = char['media']['nodes'][0]
                                anime_rel = node['title']['english'] or node['title']['romaji']
                            
                            reply = f"👤 <b>معلومات الشخصية:</b>\n\n" \
                                    f"اسم الشخصية: <b>{full_name}</b> ({native_name})\n" \
                                    f"من أنمي/مانجا: <b>{anime_rel}</b>\n" \
                                    f"🔗 <a href='{char['siteUrl']}'>رابط الصفحة بـ AniList</a>"
                            send_telegram_message(chat_id, reply)

                    else:
                        title = parsed.get("title")
                        candidates = search_media_candidates(title, media_type=media_type, per_page=4)

                        if not candidates:
                            send_telegram_message(chat_id, f"❌ لم أستطع العثور على {media_type.lower()} باسم: <b>{title}</b>")
                        elif len(candidates) > 1 and parsed.get("confidence") == "medium":
                            msg = f"🤔 <b>هل تقصد أحد هذه الـ {media_type} لـ '{title}'؟</b>\n\n"
                            for idx, cand in enumerate(candidates, 1):
                                cand_name = cand['title']['english'] or cand['title']['romaji']
                                year = f" ({cand['seasonYear']})" if cand.get('seasonYear') else ""
                                msg += f"{idx}. <b>{cand_name}</b>{year}\n"
                            msg += "\nيرجى إعادة كتابة الاسم الدقيق."
                            send_telegram_message(chat_id, msg)
                        else:
                            media = candidates[0]
                            title_display = media['title']['english'] or media['title']['romaji']
                            media_id = media['id']
                            status = parsed.get('status') or 'COMPLETED'
                            score = parsed.get('score')
                            
                            current_entry = get_user_media_entry(media_id, ANILIST_TOKEN) if ANILIST_TOKEN else None
                            current_progress = current_entry.get('progress', 0) if current_entry else 0

                            new_progress = current_progress
                            if parsed.get('absolute_progress') is not None:
                                new_progress = parsed['absolute_progress']
                            elif parsed.get('progress_delta') is not None:
                                new_progress += parsed['progress_delta']

                            unit_label = "الفصل" if media_type == "MANGA" else "الحلقة"

                            if ANILIST_TOKEN:
                                save_user_media_entry(media_id, ANILIST_TOKEN, status=status, score=score, progress=new_progress)
                                reply = f"✅ <b>تم التحديث في AniList بنجاح!</b>\n\n📺 <b>الـ {media_type}:</b> {title_display}\n📌 <b>الحالة:</b> {status}\n🔢 <b>{unit_label}:</b> {new_progress}"
                                if score is not None:
                                    reply += f"\n⭐ <b>التقييم:</b> {score}/10"
                            else:
                                reply = f"🔍 <b>تم العثور على الـ {media_type}:</b> {title_display}\n📌 <b>الحالة المفترضة:</b> {status}\n🔢 <b>{unit_label}:</b> {new_progress}\n⭐ <b>التقييم:</b> {score or 'غير محدد'}\n\n⚠️ <i>ملاحظة: يرجى إضافة ANILIST_ACCESS_TOKEN.</i>"
                            
                            send_telegram_message(chat_id, reply)

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode('utf-8'))
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode('utf-8'))
