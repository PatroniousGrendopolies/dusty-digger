"""Dig by uploader: the YouTube channel that posted the original track, ranked by relevance.

Obscure records are usually posted by crate-digging collectors, so the rest of their
uploads tend to be good. We read the channel's uploads through YouTube's own web
endpoints (no API key), tag each upload with a genre and year from its title,
description and tags, and rank them against the original song.
"""
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

YT = "https://www.youtube.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
FALLBACK_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"   # YouTube web client's public key
FALLBACK_VERSION = "2.20250101.00.00"

# Display genre -> words that point to it in titles, descriptions and tags.
GENRES = {
    "Reggae": r"reggae|roots|rocksteady|rock steady|\bska\b|dancehall|lovers rock|\bdub\b|nyabinghi|\bdeejay\b|\bjamaica",
    "Funk / Soul": r"\bfunk|\bsoul\b|r&b|\brnb\b|rhythm (?:and|&) blues|northern soul|modern soul|boogie|gospel|sweet soul",
    "Jazz": r"\bjazz|bebop|hard bop|fusion|spiritual jazz|\bswing\b",
    "Latin": r"\blatin|salsa|cumbia|bossa|samba|\bmpb\b|tropicalia|boogaloo|guaracha|son cubano|merengue|baião",
    "Afro": r"\bafro|highlife|\bjuju\b|ethio|soukous|makossa|\bafrican\b",
    "Disco": r"\bdisco\b|\bitalo\b",
    "Electronic": r"electronic|\bhouse\b|techno|electro\b|\bsynth|ambient|\bacid\b|balearic|new beat|minimal wave|cold wave|coldwave",
    "Hip Hop": r"hip[ -]?hop|\brap\b|boom bap|breakbeat",
    "Rock": r"\brock\b(?! ?steady)|psych|garage|\bprog|kraut|\bpunk|new wave|\bbeat group|freakbeat|mod\b",
    "Folk / Blues / Country": r"\bfolk|\bblues|country|bluegrass|americana",
    "Pop": r"\bpop\b|city pop|baroque|sunshine pop|chanson|yé-yé|ye-ye",
    "Library / Soundtrack": r"\blibrary\b|soundtrack|\bost\b|film score|bande originale",
}
GENRE_RE = {g: re.compile(p, re.I) for g, p in GENRES.items()}

# Discogs genres -> our display genres.
DISCOGS_GENRES = {
    "Reggae": "Reggae", "Funk / Soul": "Funk / Soul", "Jazz": "Jazz", "Latin": "Latin",
    "Electronic": "Electronic", "Hip Hop": "Hip Hop", "Rock": "Rock", "Pop": "Pop",
    "Blues": "Folk / Blues / Country", "Folk, World, & Country": "Folk / Blues / Country",
    "Stage & Screen": "Library / Soundtrack",
}

YEAR_RE = re.compile(r"(?<![\d/.-])(19[3-9]\d|20[0-2]\d)(?![\d/.-])")
DESC_NOISE = re.compile(r"https?://\S+|©.*|℗.*|provided to youtube.*|auto-generated.*|follow .*|subscribe.*|instagram.*|facebook.*",
                        re.I)


def genres_in(text):
    return [g for g, rx in GENRE_RE.items() if rx.search(text or "")]


def target_genres(discogs_genres, discogs_styles, extra_text=""):
    """The original song's genres, in our display names."""
    out = {DISCOGS_GENRES[g] for g in discogs_genres or [] if g in DISCOGS_GENRES}
    for s in discogs_styles or []:
        out.update(genres_in(s))
    if not out:
        out.update(genres_in(extra_text))
    return out


def is_robot_channel(name):
    return bool(re.search(r"\s-\sTopic$|vevo", name or "", re.I))


def norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def artist_of(title):
    m = re.match(r"\s*(.+?)\s+[-–—]\s+.+", title or "")
    return m.group(1).strip() if m else ""


class YouTube:
    def __init__(self, deadline):
        self.deadline = deadline
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        self.s.cookies.set("CONSENT", "YES+1", domain=".youtube.com")
        self.key, self.version = FALLBACK_KEY, FALLBACK_VERSION

    def time_left(self):
        return self.deadline - time.monotonic()

    def _post(self, endpoint, body, timeout=8):
        if self.time_left() < 1:
            return None
        body = dict(body, context={"client": {"clientName": "WEB", "clientVersion": self.version, "hl": "en", "gl": "US"}})
        try:
            r = self.s.post(f"{YT}/youtubei/v1/{endpoint}", params={"key": self.key, "prettyPrint": "false"},
                            json=body, timeout=min(timeout, max(1, self.time_left())))
            return r.json() if r.status_code == 200 else None
        except (requests.RequestException, ValueError):
            return None

    def video(self, video_id):
        """Title, channel, description and tags of one video."""
        d = self._post("player", {"videoId": video_id}, timeout=5)
        vd = (d or {}).get("videoDetails") or {}
        if not vd.get("channelId"):
            return None
        return {
            "id": video_id,
            "title": vd.get("title") or "",
            "channel_id": vd["channelId"],
            "channel": vd.get("author") or "",
            "text": " ".join([vd.get("title") or "", vd.get("shortDescription") or "", " ".join(vd.get("keywords") or [])]),
            "description": vd.get("shortDescription") or "",
            "tags": vd.get("keywords") or [],
        }

    def search(self, query):
        """First result posted by a person, not an auto-generated 'Topic' or VEVO channel."""
        d = self._post("search", {"query": query})
        for v in walk(d, "videoRenderer"):
            owner = " ".join(r.get("text", "") for r in ((v.get("ownerText") or {}).get("runs") or []))
            if v.get("videoId") and not is_robot_channel(owner):
                return v["videoId"]
        return None

    def _first_page(self, channel_id):
        """The channel's Videos tab (newest first), read from the page itself."""
        if self.time_left() < 2:
            return None
        try:
            r = self.s.get(f"{YT}/channel/{channel_id}/videos", timeout=min(8, self.time_left()))
        except requests.RequestException:
            return None
        h = r.text
        k = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', h)
        ver = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', h)
        if k:
            self.key = k.group(1)
        if ver:
            self.version = ver.group(1)
        i = h.find("var ytInitialData = ")
        if i < 0:
            return None
        try:
            data, _ = json.JSONDecoder().raw_decode(h[i + len("var ytInitialData = "):])
        except ValueError:
            return None
        return data

    def uploads(self, channel_id=None, token=None):
        """One page (~30) of a channel's uploads, newest first, and the token for the next page."""
        d = self._post("browse", {"continuation": token}) if token else self._first_page(channel_id)
        if d is None:
            return None, None
        videos, seen = [], set()
        for lv in walk(d, "lockupViewModel"):
            vid = lv.get("contentId")
            meta = ((lv.get("metadata") or {}).get("lockupMetadataViewModel") or {})
            title = ((meta.get("title") or {}).get("content")) or ""
            if vid and len(vid) == 11 and vid not in seen:
                seen.add(vid)
                videos.append({"id": vid, "title": title})
        for vr in walk(d, "videoRenderer"):
            vid = vr.get("videoId")
            if vid and vid not in seen:
                seen.add(vid)
                runs = ((vr.get("title") or {}).get("runs") or [{}])
                videos.append({"id": vid, "title": runs[0].get("text", "")})
        # Several "load more" tokens come back; the video grid's is by far the longest
        # (the short ones are for side shelves and return nothing).
        toks = [(((c.get("continuationEndpoint") or {}).get("continuationCommand") or {}).get("token"))
                for c in walk(d, "continuationItemRenderer")]
        toks = [t for t in toks if t]
        nxt = max(toks, key=len) if toks and videos else None
        return videos, nxt


def walk(o, key):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == key:
                yield v
            yield from walk(v, key)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v, key)


def describe(yt, videos, workers=12):
    """Fill in description/tags for a batch of uploads, in parallel."""
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for v, info in zip(videos, pool.map(lambda v: yt.video(v["id"]), videos)):
            out[v["id"]] = info
    return out


def tag_and_score(videos, details, target):
    """Genre, year and a relevance score for each upload."""
    t_genres = set(target.get("genres") or [])
    t_year = target.get("year")
    t_artist = norm(target.get("artist"))
    out = []
    for v in videos:
        info = details.get(v["id"]) or {}
        title = v["title"] or info.get("title") or ""
        desc = DESC_NOISE.sub(" ", info.get("description") or "")
        text = " ".join([title, desc, " ".join(info.get("tags") or [])])
        genres = genres_in(text)
        m = YEAR_RE.search(title) or YEAR_RE.search(desc)
        year = int(m.group(1)) if m else None
        artist = artist_of(title)
        score = 0
        if t_genres and set(genres) & t_genres:
            score += 3
        if t_year and year:
            gap = abs(year - t_year)
            score += 3 if gap <= 3 else 2 if gap <= 7 else 1 if gap <= 12 else 0
        if t_artist and artist and (norm(artist) == t_artist or t_artist in norm(title)):
            score += 2
        out.append({"id": v["id"], "name": title, "genres": genres, "year": year, "score": score,
                    "link": f"https://www.youtube.com/watch?v={v['id']}"})
    return out


def dig_uploads_page(token, target, budget=20):
    """Next page of a channel for the browser: tagged, scored uploads and the following token."""
    yt = YouTube(time.monotonic() + budget)
    videos, nxt = yt.uploads(token=token)
    if videos is None:
        return None, token
    return tag_and_score(videos, describe(yt, videos), target), nxt


def dig_uploader(song, song_name, artist_name, budget=22, first_pages=3):
    """song: the Discogs match (may be None). Returns the result dict for the page, or a message."""
    yt = YouTube(time.monotonic() + budget)

    # 1. The original track's YouTube video: the one Discogs links for this song, else a search.
    original_id = None
    if song and song.get("videos"):
        want = norm(song_name)
        for v in song["videos"]:
            if want and want in norm(v.get("title")):
                original_id = v["yt"]
                break
        original_id = original_id or song["videos"][0]["yt"]
    if not original_id:
        original_id = yt.search(" ".join(x for x in (artist_name, song_name) if x))
    if not original_id:
        return "Couldn't find that song on YouTube."
    original = yt.video(original_id)
    if original and is_robot_channel(original["channel"]):
        other = yt.search(" ".join(x for x in (artist_name, song_name) if x))
        if other and other != original_id:
            original_id, original = other, yt.video(other) or original
    if not original:
        return "YouTube didn't answer. Give it a minute and dig again."

    target = {
        "genres": sorted(target_genres((song or {}).get("genres"), (song or {}).get("styles"), original["text"])),
        "year": (song or {}).get("year"),
        "artist": artist_name or artist_of(original["title"]),
    }

    # 2. The uploader's channel, a few pages now; the page asks for the rest.
    tagged, token, pages = [], None, 0
    videos, token = yt.uploads(channel_id=original["channel_id"])
    while videos:
        tagged += tag_and_score(videos, describe(yt, videos), target)
        pages += 1
        if not token or pages >= first_pages or yt.time_left() < 6:
            break
        videos, token = yt.uploads(token=token)

    first = {"id": original_id, "name": original["title"], "genres": genres_in(original["text"]),
             "year": target["year"], "score": 99, "link": f"https://www.youtube.com/watch?v={original_id}",
             "original": True}
    tagged = [first] + [t for t in tagged if t["id"] != original_id]

    return {
        "mode": "uploader",
        "channel": original["channel"],
        "channel_url": f"{YT}/channel/{original['channel_id']}",
        "original": first,
        "target": target,
        "uploads": tagged,
        "next": token,
        "song": (song or {}).get("name"),
        "year": target["year"],
    }
