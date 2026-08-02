import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from anilist_api import search_media, search_character, recommend_anime_by_genre
from ai_parser import parse_user_prompt

def test():
    print("--- Testing Manga Tracking ---")
    p1 = parse_user_prompt("قريت 5 فصول من Solo Leveling")
    print("Parsed:", p1)
    manga = search_media(p1["title"], media_type="MANGA")
    if manga:
        print("Manga Found:", manga['title']['english'] or manga['title']['romaji'])

    print("\n--- Testing Character Lookup ---")
    char = search_character("Luffy")
    if char:
        print("Character Found:", char['name']['full'], "from AniList!")

    print("\n--- Testing Recommendations ---")
    recs = recommend_anime_by_genre("Mystery", limit=3)
    for r in recs:
        print("Recommended:", r['title']['english'] or r['title']['romaji'], f"⭐ {r['averageScore']/10.0}")

if __name__ == "__main__":
    test()
