import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from anilist_api import search_anime
from ai_parser import parse_user_prompt

def test_prompt(prompt_text):
    print(f"\n--- Prompt: '{prompt_text}' ---")
    parsed = parse_user_prompt(prompt_text)
    print("Parsed Intent:", parsed)

    anime = search_anime(parsed["title"])
    if anime:
        title_eng = anime['title']['english'] or anime['title']['romaji']
        print(f"Found on AniList: ID={anime['id']}, Title='{title_eng}'")
        print(f"Status to set: {parsed['status'] or 'COMPLETED'}")
        print(f"Score to set: {parsed['score']}")
        print(f"Progress: Delta={parsed['progress_delta']}, Absolute={parsed['absolute_progress']}")
    else:
        print(f"Could not find anime matching title: '{parsed['title']}'")

if __name__ == "__main__":
    prompts = [
        "شفت حلقتين من انمي اللعنات",
        "خلصت هجوم العمالقة وقيمته 10",
        "وقفت دث نوت بالحلقة 12",
        "سحبت على رجل المنشار",
        "شفت ججتسو كايسن وقيمته 9"
    ]
    for p in prompts:
        test_prompt(p)
