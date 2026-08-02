import json
import urllib.request
import urllib.error

ANILIST_URL = "https://graphql.anilist.co"

def make_graphql_request(query: str, variables: dict = None, access_token: str = None) -> dict:
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
        with urllib.request.urlopen(req) as response:
            res_body = response.read().decode("utf-8")
            return json.loads(res_body)
    except urllib.error.HTTPError as e:
        error_content = e.read().decode("utf-8")
        try:
            return json.loads(error_content)
        except Exception:
            raise Exception(f"HTTP Error {e.code}: {error_content}")

def search_media(title: str, media_type: str = "ANIME") -> dict:
    query = """
    query ($search: String, $type: MediaType) {
      Media (search: $search, type: $type) {
        id
        type
        title {
          romaji
          english
          native
        }
        episodes
        chapters
        volumes
        status
        coverImage {
          medium
        }
        siteUrl
      }
    }
    """
    res = make_graphql_request(query, {"search": title, "type": media_type})
    if "errors" in res:
        return None
    return res.get("data", {}).get("Media")

def search_media_candidates(title: str, media_type: str = "ANIME", per_page: int = 4) -> list:
    query = """
    query ($search: String, $type: MediaType, $perPage: Int) {
      Page (perPage: $perPage) {
        media (search: $search, type: $type) {
          id
          type
          title {
            romaji
            english
            native
          }
          episodes
          chapters
          format
          seasonYear
        }
      }
    }
    """
    res = make_graphql_request(query, {"search": title, "type": media_type, "perPage": per_page})
    if "errors" in res:
        return []
    return res.get("data", {}).get("Page", {}).get("media", [])

def get_user_media_entry(media_id: int, access_token: str) -> dict:
    query = """
    query ($mediaId: Int) {
      MediaList (mediaId: $mediaId) {
        id
        status
        score
        progress
        progressVolumes
      }
    }
    """
    res = make_graphql_request(query, {"mediaId": media_id}, access_token=access_token)
    if "errors" in res:
        return None
    return res.get("data", {}).get("MediaList")

def save_user_media_entry(media_id: int, access_token: str, status: str = None, score: float = None, progress: int = None) -> dict:
    mutation = """
    mutation ($mediaId: Int, $status: MediaListStatus, $score: Float, $progress: Int) {
      SaveMediaListEntry (mediaId: $mediaId, status: $status, score: $score, progress: $progress) {
        id
        status
        score
        progress
        media {
          title {
            romaji
            english
          }
        }
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
        raise Exception(f"AniList API Error: {res['errors']}")
    return res.get("data", {}).get("SaveMediaListEntry")

def toggle_favourite_anime(media_id: int, access_token: str):
    mutation = """
    mutation ($animeId: Int) {
      ToggleFavourite (animeId: $animeId) {
        anime {
          nodes {
            id
          }
        }
      }
    }
    """
    res = make_graphql_request(mutation, {"animeId": media_id}, access_token=access_token)
    return res

def search_character(name: str) -> dict:
    query = """
    query ($search: String) {
      Character (search: $search) {
        id
        name {
          full
          native
        }
        siteUrl
        media (perPage: 1, sort: POPULARITY_DESC) {
          nodes {
            title {
              romaji
              english
            }
          }
        }
      }
    }
    """
    res = make_graphql_request(query, {"search": name})
    if "errors" in res:
        return None
    return res.get("data", {}).get("Character")

def recommend_anime_by_genre(genre: str, limit: int = 4) -> list:
    query = """
    query ($genre: String, $limit: Int) {
      Page (perPage: $limit) {
        media (genre: $genre, type: ANIME, sort: SCORE_DESC) {
          id
          title {
            romaji
            english
          }
          averageScore
          episodes
          siteUrl
        }
      }
    }
    """
    res = make_graphql_request(query, {"genre": genre, "limit": limit})
    if "errors" in res:
        return []
    return res.get("data", {}).get("Page", {}).get("media", [])
