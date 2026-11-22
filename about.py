"""'About this record' for the song that's playing: Discogs facts plus a few lines from Wikipedia.

No AI involved. Discogs gives the hard facts (artist, title, label, year, credits);
Wikipedia's public API gives a short bio of the artist and, when the record has its
own article, a few words about it. Each Wikipedia hit has to prove it's the right
page (same name, and the description is about music) before we show it.
"""
import re
import time
from urllib.parse import quote

import requests

WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"
USER_AGENT = "DustyDigger/1.1 (https://dusty-digger.vercel.app)"

MUSIC = re.compile(r"\b(musician|singer|songwriter|band|group|duo|trio|rapper|dj|deejay|producer|composer|"
                   r"guitarist|pianist|saxophonist|trumpeter|drummer|bassist|vocalist|toaster|ensemble|"
                   r"orchestra|artist|music|record label|organist|percussionist|keyboardist|violinist)\b", re.I)
RECORD = re.compile(r"\b(album|single|song|ep|record|lp|compilation|soundtrack|recording)\b", re.I)

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})
_cache = {}


def norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def bare(text):
    """Name without a leading 'The' or a trailing '(band)' / '(album)'."""
    t = re.sub(r"\s*\([^)]*\)\s*$", "", text or "")
    return re.sub(r"^the ", "", norm(t)).replace(" ", "")


GENERIC_QUALIFIER = re.compile(r"^\((?:musician|band|singer|rapper|group|duo|dj|producer|composer|"
                               r"artist|deejay|vocalist|guitarist|pianist|saxophonist|album|song|single|ep)\)$", re.I)


def qualifier(title):
    m = re.search(r"\([^)]*\)\s*$", title or "")
    return m.group(0) if m else ""


def hint_words(hints):
    """Genre, style and country words that a right page usually mentions ('reggae', 'jamaica')."""
    out = set()
    for h in hints or []:
        for w in norm(h).split():
            if len(w) > 3 and w not in ("music", "funk", "soul", "stage", "screen", "electronic", "folk", "world", "country", "pop"):
                out.add(w[:7])
    return out


def short(text, sentences=3, limit=480):
    """First few sentences, cut on a sentence end."""
    text = re.sub(r"\s+", " ", text or "").strip()
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", text)
    out = ""
    for p in parts[:sentences]:
        if out and len(out) + len(p) > limit:
            break
        out = (out + " " + p).strip()
    if len(out) > limit:
        out = out[:limit].rsplit(" ", 1)[0] + "..."
    return out


def clean_discogs(text):
    """Discogs profile markup -> plain text: [a=Name] -> Name, [url=..]x[/url] -> x, [b] etc. dropped."""
    text = re.sub(r"\[(?:a|l|m|r)=([^\]]+)\]", r"\1", text or "")
    text = re.sub(r"\[(?:a|l|m|r)\d+\]", "", text)
    text = re.sub(r"\[url=[^\]]*\]([^\[]*)\[/url\]", r"\1", text)
    text = re.sub(r"\[/?[a-z]+\]", "", text)
    return re.sub(r"\s*\(\d+\)", "", text).strip()


def _get(url, params=None, timeout=5):
    try:
        r = _session.get(url, params=params, timeout=timeout)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def wiki_search(query, limit=5):
    data = _get(WIKI_API, {"action": "query", "list": "search", "srsearch": query, "srlimit": limit,
                           "format": "json", "srprop": ""})
    return [h["title"] for h in ((data or {}).get("query") or {}).get("search") or []]


def wiki_summary(title):
    data = _get(WIKI_SUMMARY + quote(title.replace(" ", "_"), safe=""))
    if not data or data.get("type") == "disambiguation" or not data.get("extract"):
        return None
    return {"title": data.get("title") or title, "description": data.get("description") or "",
            "extract": data["extract"], "url": ((data.get("content_urls") or {}).get("desktop") or {}).get("page")
            or "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")}


def _cached(key, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    if len(_cache) > 500:
        _cache.pop(next(iter(_cache)))
    return val


def _mentions(s, words):
    head = norm(s["description"] + " " + s["extract"][:500]).split()
    return sum(any(w.startswith(h) for w in head) for h in words)


def artist_bio(artist, hints=None):
    """Wikipedia's opening lines about this artist, or None if no page clearly is them.

    Names repeat (there's a Thai boy band called Trinity), so a page whose title needs a
    qualifier like '(Thai band)' must also mention the record's genre or country.
    """
    if not artist or bare(artist) in ("various", "variousartists", "unknownartist", ""):
        return None
    words = hint_words(hints)

    def find():
        want = bare(artist)
        titles = wiki_search(f"{artist} music") + wiki_search(artist, 5)
        if hints:
            titles += wiki_search(f"{artist} {hints[0]}", 5)
        best, seen = None, set()
        for t in titles:
            if t in seen or bare(t) != want:
                continue
            seen.add(t)
            s = wiki_summary(t)
            if not s or not (MUSIC.search(s["description"]) or MUSIC.search(s["extract"][:300])):
                continue
            hits = _mentions(s, words)
            q = qualifier(t)
            if q and not GENERIC_QUALIFIER.match(q) and not hits:
                continue
            score = hits * 2 + (0 if q else 1)
            if best is None or score > best[0]:
                best = (score, s)
        if not best:
            return None
        return {"text": short(best[1]["extract"]), "url": best[1]["url"], "source": "Wikipedia"}
    return _cached("artist|" + norm(artist) + "|" + "|".join(sorted(words)), find)


def record_blurb(title, artist):
    """A few words about the record from its own Wikipedia article, if it has one."""
    if not title or not artist:
        return None

    def find():
        want, who = bare(title), bare(artist)
        for t in dict.fromkeys(wiki_search(f"{title} {artist}") + wiki_search(f"{title} {artist} album", 3)):
            if bare(t) != want:
                continue
            s = wiki_summary(t)
            if not s:
                continue
            head = norm(s["description"] + " " + s["extract"][:400])
            if who in head.replace(" ", "") and (RECORD.search(s["description"]) or RECORD.search(s["extract"][:300])):
                return {"text": short(s["extract"]), "url": s["url"], "source": "Wikipedia"}
        return None
    return _cached("record|" + norm(title) + "|" + norm(artist), find)


def credits(release, video_title=None, limit=14):
    """Who played on it: release credits plus the playing track's own, merged by person."""
    people = {}
    rows = list(release.get("extraartists") or [])
    vt = norm(video_title)
    best = None
    for t in release.get("tracklist") or []:
        tt = norm(t.get("title"))
        if vt and len(tt) >= 3 and tt in vt and (best is None or len(tt) > len(norm(best.get("title")))):
            best = t
    if best:
        rows = list(best.get("extraartists") or []) + rows
    for c in rows:
        name = re.sub(r"\s*\(\d+\)$", "", (c.get("anv") or c.get("name") or "").strip()).rstrip("*")
        role = re.sub(r"\s*\[[^\]]*\]", "", c.get("role") or "").strip()
        if not name:
            continue
        roles = people.setdefault(name, [])
        for r in [x.strip() for x in role.split(",") if x.strip()]:
            if r not in roles:
                roles.append(r)
    return [{"name": n, "roles": ", ".join(r)} for n, r in list(people.items())[:limit]]


SEARCH_NOISE = re.compile(r"\((?:official|full album|hq|hd|audio|video|lyrics|remaster)[^)]*\)|\[[^\]]*\]|"
                          r"\b(official (?:video|audio)|full album|hq|hd)\b", re.I)


BUSY = object()


def _plain(text):
    """Drop decorations YouTube titles carry (ᴴᴰ, ★, emoji) but keep accented letters."""
    import unicodedata
    return "".join(c for c in text if unicodedata.category(c)[0] in "LNZ" and unicodedata.category(c) != "Lm"
                   or c in "-'&")


def find_release(dig, video_title):
    """Uploader mode: turn a YouTube title into the Discogs release it most likely is.

    Returns a release id, None when Discogs has nothing that matches, or BUSY when
    Discogs didn't answer (so the page retries instead of saying 'not found').
    """
    q = SEARCH_NOISE.sub(" ", video_title or "")
    q = re.sub(r"#\S+", " ", q)                       # hashtags
    q = re.sub(r"\([^)]*\)", " ", q)                  # (UK, 1983), (South Africa 1982), (excerpt)
    q = re.sub(r"\s+[\u2013\u2014|:~]\s+", " - ", q)     # other dashes and separators
    q = re.sub(r"\s+", " ", _plain(q)).strip(" -")
    words = [w for w in norm(q).split() if len(w) > 1]
    if not words:
        return None
    tries = [{"q": q}]
    if " - " in q:
        left, right = q.split(" - ", 1)
        tries.append({"artist": left.strip(), "track": right.strip()})
    answered = False
    for params in tries:
        data = dig.get("/database/search", type="release", per_page=10, **params)
        if data is None:
            continue
        answered = True
        for r in data.get("results") or []:
            have = set(norm(r.get("title")).split())
            if sum(w in have for w in words) >= max(2, round(len(words) * 0.6)):
                return r.get("id")
            if "track" in params and norm(params["artist"]) in norm(r.get("title")):
                return r.get("id")   # matched on the track list, so the record's own title differs
    return None if answered else BUSY
