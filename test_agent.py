"""Standalone test for the v4.0 agent loop (no network). Mocks _gemini_generate + _gql."""

import sys, os, importlib.util, json

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "webhook", os.path.join(HERE, "api", "webhook.py")
)
wb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wb)

# --- force in-memory storage + dummy key so run_agent proceeds ---
wb.KV_ENABLED = False
wb.GEMINI_KEY = "dummy-key"
wb.ANILIST_TOKEN = None  # so _viewer_me() short-circuits without network

failures = []


def check(cond, msg):
    print(("  OK  " if cond else " FAIL ") + msg)
    if not cond:
        failures.append(msg)


print("\n=== [A] TOOL DISPATCH + RETURN SHAPES (mocked _gql) ===")


# Mock _gql to return canned AniList data based on query content
def fake_gql(query, variables=None, token=None, timeout=None):
    q = query
    if "Page(perPage:$n){media(search" in q:  # search_media
        return {
            "data": {
                "Page": {
                    "media": [
                        {
                            "id": 21,
                            "type": "ANIME",
                            "title": {"romaji": "One Piece", "english": "One Piece"},
                            "episodes": 1100,
                            "seasonYear": 1999,
                            "averageScore": 87,
                            "coverImage": {"large": "http://x/21.jpg"},
                            "siteUrl": "http://anilist/21",
                        },
                    ]
                }
            }
        }
    if "mediaList(userName" in q:  # get_media_list (used by search_my_list)
        return {
            "data": {
                "Page": {
                    "mediaList": [
                        {
                            "id": 9901,
                            "status": "CURRENT",
                            "progress": 100,
                            "score": 9,
                            "media": {
                                "id": 21,
                                "title": {"romaji": "One Piece"},
                                "seasonYear": 1999,
                                "averageScore": 87,
                                "coverImage": {"large": "http://x/21.jpg"},
                            },
                        },
                        {
                            "id": 9902,
                            "status": "COMPLETED",
                            "progress": 50,
                            "score": 8,
                            "media": {
                                "id": 302,
                                "title": {"romaji": "Captain Tsubasa (2018)"},
                                "seasonYear": 2018,
                                "coverImage": {"large": "http://x/302.jpg"},
                            },
                        },
                    ]
                }
            }
        }
    if "media(type:ANIME,sort:TRENDING" in q:  # trending
        return {
            "data": {
                "Page": {
                    "media": [
                        {
                            "id": 1,
                            "title": {"romaji": "TrendA"},
                            "averageScore": 80,
                            "coverImage": {"large": "http://x/1.jpg"},
                        },
                    ]
                }
            }
        }
    return {"data": {}}


wb._gql = fake_gql

# stub rendering (no Telegram)
sent = []
wb.tg_send = lambda cid, text, kb=None: sent.append(("send", text))
wb.tg_photo = lambda cid, url, caption, kb=None: sent.append(("photo", caption))
wb.tg_album = lambda cid, items: sent.append(("album", len(items)))

# A) search_my_list returns the delete-critical shape (entry_id + media_id + title)
me = {"id": 748233, "name": "Koros1Sama"}
r = wb._tool_search_my_list("123", {"query": "tsubasa", "type": "ANIME"}, me)
check(
    isinstance(r, dict) and "results" in r, "search_my_list returns dict with results"
)
hit = (r.get("results") or [{}])[0]
check(
    {"entry_id", "media_id", "title"} <= set(hit.keys()),
    "search_my_list result has entry_id+media_id+title (delete fix)",
)
check(
    hit.get("title") == "Captain Tsubasa (2018)",
    "search_my_list found the user's OWN Captain Tsubasa entry",
)

# B) search_anime returns compact media list
r2 = wb._tool_search_anime("123", {"query": "one piece"}, None)
check(isinstance(r2, dict) and r2.get("count") == 1, "search_anime returns count=1")
cm = r2["results"][0]
check(
    {"id", "title", "year", "score", "episodes"} <= set(cm.keys()),
    "search_anime media has id/title/year/score/episodes",
)

# C) trending returns ok+count and rendered album
r3 = wb._tool_get_trending("123", {"n": 5}, me)
check(r3.get("ok") is True and r3.get("count", 0) >= 1, "get_trending ok+count")
check(any(t[0] == "album" for t in sent), "get_trending rendered an album")

print("\n=== [B] AGENT LOOP WIRING (2-step: functionCall -> response -> final) ===")
sent.clear()
# override one tool to prove dispatch + functionResponse round-trip without network
wb.TOOL_DISPATCH["search_anime"] = lambda cid, args, me: {
    "count": 1,
    "results": [{"id": 21, "title": "One Piece"}],
}
calls = {"n": 0}
seq = [
    {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "functionCall": {
                                "name": "search_anime",
                                "args": {"query": "one piece"},
                            }
                        }
                    ]
                }
            }
        ]
    },
    {"candidates": [{"content": {"parts": [{"text": "لقيت ون بيس 🔥"}]}}]},
]


def fake_generate(contents):
    calls["n"] += 1
    # on step 2, prove the functionResponse from step 1 was appended
    if calls["n"] == 2:
        last = contents[-1]
        fr = (last.get("parts", [{}])[0]).get("functionResponse", {})
        check(
            fr.get("name") == "search_anime" and isinstance(fr.get("response"), dict),
            "step2 contents include functionResponse{name,response} from step1 tool",
        )
    if calls["n"] <= len(seq):
        return seq[calls["n"] - 1]
    raise RuntimeError("agent looped past final")


wb._gemini_generate = fake_generate

ok = wb.run_agent("999", "شفت ون بيس")
check(ok is True, "run_agent returned True (reply sent)")
check(calls["n"] == 2, f"loop ran exactly 2 steps (got {calls['n']})")
check(
    any(t[0] == "send" and "ون بيس" in t[1] for t in sent),
    "final text delivered via tg_send",
)

print("\n=== [C] FAILURE PATH (_gemini_generate None -> return False) ===")
wb._gemini_generate = lambda contents: None
sent.clear()
ok2 = wb.run_agent("999", "anything")
check(
    ok2 is False, "run_agent returns False when gemini unavailable (enables fallback)"
)

print("\n=== [D] NO-KEY PATH (run_agent returns False without GEMINI_KEY) ===")
wb.GEMINI_KEY = ""
ok3 = wb.run_agent("999", "x")
check(ok3 is False, "run_agent returns False when GEMINI_KEY missing")

print("\n=== [E] OUTAGE INJECTION (AniList down -> agent told up-front, replies gracefully) ===")
sent.clear()
wb.GEMINI_KEY = "dummy-key"
wb.ANILIST_TOKEN = "dummy"  # so _resolve_me_and_state proceeds to the probe
wb._resolve_me_and_state = lambda: (None, "down")
injected = {"v": False}
def fake_gen_down(contents):
    blob = json.dumps(contents, ensure_ascii=False)
    if "AniList API متعطّلة" in blob:
        injected["v"] = True
    return {"candidates": [{"content": {"parts": [{"text": "AniList متعطّلة شوي، جرّب بعد دقايق 🙏"}]}}]}
wb._gemini_generate = fake_gen_down
ok = wb.run_agent("777", "هات قائمة اصدقائي")
check(ok is True, "run_agent returned True during outage (reply sent)")
check(injected["v"], "outage note injected into Gemini contents (agent told AniList is down)")
check(any("متعطّلة" in t[1] for t in sent), "agent replied with the graceful outage message")

print("\n" + ("=" * 50))
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print("   -", f)
    sys.exit(1)
print("ALL CHECKS PASSED — agent loop + tools verified")
