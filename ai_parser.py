import re

ARABIC_ANIME_DICTIONARY = {
    "اللعنات": "Jujutsu Kaisen",
    "انمي اللعنات": "Jujutsu Kaisen",
    "ججتسو كايسن": "Jujutsu Kaisen",
    "جوجوتسو كايسن": "Jujutsu Kaisen",
    "جوجوتسو": "Jujutsu Kaisen",
    "هجوم العمالقة": "Attack on Titan",
    "العمالقة": "Attack on Titan",
    "اتاك اون تايتن": "Attack on Titan",
    "اتاك": "Attack on Titan",
    "مذكرة الموت": "Death Note",
    "دث نوت": "Death Note",
    "قاتل الشياطين": "Demon Slayer: Kimetsu no Yaiba",
    "ديمون سلاير": "Demon Slayer",
    "رجل المنشار": "Chainsaw Man",
    "تشينسو مان": "Chainsaw Man",
    "هنتر": "Hunter x Hunter",
    "القناص": "Hunter x Hunter",
    "هنتر هانتر": "Hunter x Hunter",
    "ون بيس": "One Piece",
    "ناروتو": "Naruto",
    "دراغون بول": "Dragon Ball",
    "كونان": "Detective Conan",
    "المحقق كونان": "Detective Conan",
    "اكاديميتي للابطال": "My Hero Academia",
    "أكاديمية بطل": "My Hero Academia",
    "طوكيو غول": "Tokyo Ghoul",
    "بليتش": "Bleach",
    "سولو ليفلينج": "Solo Leveling",
    "رفع المستوى فرديا": "Solo Leveling"
}

GENRE_MAP = {
    "أكشن": "Action",
    "مغامرة": "Adventure",
    "كوميدي": "Comedy",
    "دراما": "Drama",
    "خيالي": "Fantasy",
    "غموض": "Mystery",
    "رعب": "Horror",
    "رومانسي": "Romance",
    "خيال علمي": "Sci-Fi",
    "شريحة من الحياة": "Slice of Life",
    "رياضي": "Sports",
    "نفسي": "Psychological",
    "إثارة": "Thriller"
}

def parse_user_prompt(text: str) -> dict:
    text_clean = text.strip()

    result = {
        "action": "TRACK",  # 'TRACK', 'CHARACTER_LOOKUP', 'RECOMMEND'
        "media_type": "ANIME",  # 'ANIME' or 'MANGA'
        "title": None,
        "status": None,
        "score": None,
        "progress_delta": None,
        "absolute_progress": None,
        "genre": None,
        "original_text": text
    }

    lower_text = text_clean.lower()

    if any(k in lower_text for k in ["اقترح", "اقتراح", "ترشيح", "رشح", "أنمي حلو", "انمي ممتاز"]):
        result["action"] = "RECOMMEND"
        for g_ar, g_en in GENRE_MAP.items():
            if g_ar in lower_text:
                result["genre"] = g_en
                break
        if not result["genre"]:
            result["genre"] = "Action"
        return result

    if any(k in lower_text for k in ["شخصية", "مين مؤدي", "مؤدي صوت", "صوت شخصية"]):
        result["action"] = "CHARACTER_LOOKUP"
        cleaned_char = text_clean
        for filler in ["شخصية", "مين مؤدي صوت", "مؤدي صوت", "في انمي", "أنمي"]:
            cleaned_char = re.sub(filler, '', cleaned_char, flags=re.IGNORECASE).strip()
        result["title"] = cleaned_char
        return result

    if any(k in lower_text for k in ["مانجا", "مانها", "مانهوا", "فصل", "فصول", "شابتر", "chapter", "read"]):
        result["media_type"] = "MANGA"

    score_match = re.search(r'(?:تقييم|قيمته|تقييمي|score)?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:/10|من 10)?', text_clean, re.IGNORECASE)
    if score_match:
        try:
            val = float(score_match.group(1))
            if 0 <= val <= 10:
                result["score"] = val
        except ValueError:
            pass

    if any(k in lower_text for k in ["سحبت", "تركته", "كنسلت", "dropped"]):
        result["status"] = "DROPPED"
    elif any(k in lower_text for k in ["plan", "خطة", "أفكر", "ضيف", "اضف", "قائمة الانتظار", "بشوفه", "بقراه"]):
        result["status"] = "PLANNING"
    elif any(k in lower_text for k in ["خلصت", "أنهيت", "نهيت", "ختمت", "completed", "انتهيت"]):
        result["status"] = "COMPLETED"
    elif any(k in lower_text for k in ["وقفت", "توقفت", "paused", "معلق"]):
        result["status"] = "PAUSED"
    elif any(k in lower_text for k in ["شفت", "تابع", "تابعت", "شاهدت", "حلقة", "حلقات", "قريت", "قرأت", "watching", "reading"]):
        result["status"] = "CURRENT"

    if "حلقتين" in lower_text or "فصلين" in lower_text or "شابترين" in lower_text:
        result["progress_delta"] = 2
        if not result["status"]:
            result["status"] = "CURRENT"
    elif any(k in lower_text for k in ["حلقة", "حلقات", "فصل", "فصول", "شابتر", "شابترات"]):
        ep_match = re.search(r'(\d+)\s*(?:حلقة|حلقات|فصل|فصول|شابتر|eps|episodes|chapters)', lower_text)
        if ep_match:
            result["progress_delta"] = int(ep_match.group(1))
        else:
            result["progress_delta"] = 1
        if not result["status"]:
            result["status"] = "CURRENT"

    abs_match = re.search(r'(?:للحلقة|حلقة|واصل|فصل|شابتر|ep|ch)\s*(\d+)', lower_text)
    if abs_match:
        result["absolute_progress"] = int(abs_match.group(1))

    cleaned_title = text_clean
    cleaned_title = re.sub(r'(?:و\s*)?(?:تقييم|قيمته|تقييمي|score)?\s*[0-9]+(?:\.[0-9]+)?\s*(?:/10|من 10)?', '', cleaned_title, flags=re.IGNORECASE)
    cleaned_title = re.sub(r'(?:للحلقة|حلقة|حلقات|حلقتين|فصل|فصول|شابتر|eps|chapters)\s*\d*', '', cleaned_title, flags=re.IGNORECASE)

    prefix_patterns = [
        r'^\s*شفت\s+من', r'^\s*شفت', r'^\s*تابعت', r'^\s*شاهدت', r'^\s*قريت', r'^\s*قرأت',
        r'^\s*خلصت', r'^\s*أنهيت', r'^\s*نهيت', r'^\s*ختمت',
        r'^\s*ضيف', r'^\s*اضف', r'^\s*أفكر\s+أشوف', r'^\s*سحبت\s+على',
        r'^\s*وقفت', r'^\s*مانجا', r'^\s*انمي'
    ]
    for pattern in prefix_patterns:
        cleaned_title = re.sub(pattern, '', cleaned_title, flags=re.IGNORECASE).strip()

    suffix_patterns = [
        r'للقائمة\s*$', r'في\s*الـ\s*plan\s*$', r'خطة\s*$', r'من\s*$', r'و\s*$', r'مانجا\s*$', r'انمي\s*$'
    ]
    for pattern in suffix_patterns:
        cleaned_title = re.sub(pattern, '', cleaned_title, flags=re.IGNORECASE).strip()

    cleaned_title = re.sub(r'^\s*(?:من|على|في|و)\s+', '', cleaned_title).strip()
    cleaned_title = re.sub(r'\s+(?:و|من|على|في)\s*$', '', cleaned_title).strip()

    for nick, official in ARABIC_ANIME_DICTIONARY.items():
        if nick in cleaned_title.lower():
            cleaned_title = official
            break

    result["title"] = cleaned_title if cleaned_title else text_clean
    return result
