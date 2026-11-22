import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, jsonify, render_template, request

import about as about_mod
import uploader

app = Flask(__name__)

API = "https://api.discogs.com"
USER_AGENT = "DustyDigger/1.1 +https://dusty-digger.vercel.app"

# How much digging one search may do. Discogs allows about 60 requests a minute,
# and each album's track list is one request, so we look up a handful of albums
# instead of 50, and stop early if the clock runs out.
MAX_LABELS = 2
MAX_ALBUMS = 40          # releases listed; tracks for the ones not fetched up front load in the browser
LABEL_PAGES = 3          # up to 300 releases looked at per label
ERA_YEARS = 4            # keep releases within this many years of the original
RESERVE = 8  # requests per minute kept free so the next search's lookups always get through
TIME_BUDGET = 20  # seconds per search

NO_MATCH = "We've hit a rock! No match found for the song"
BUSY = "Discogs is busy right now. Give it a minute and dig again."
NO_TOKEN = "Dusty Digger isn't connected to Discogs yet (DISCOGS_TOKEN is missing)."


class DiscogsError(Exception):
    pass


class Digger:
    """One search's worth of Discogs calls, sharing a session, a token and a deadline."""

    def __init__(self, token):
        self.deadline = time.monotonic() + TIME_BUDGET
        self.remaining = None  # Discogs tells us how many requests are left this minute
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Discogs token={token}",
            "User-Agent": USER_AGENT,
        })

    def time_left(self):
        return self.deadline - time.monotonic()

    def get(self, path, **params):
        """GET a Discogs endpoint. Returns parsed JSON, or None when Discogs says no.

        Discogs answers "too many requests" (429) with a plain-text page, which is
        what used to crash the app. Wait once if there's time, otherwise give up
        on this call instead of falling over.
        """
        for attempt in range(2):
            if self.time_left() <= 1:
                return None
            try:
                r = self.session.get(f"{API}{path}", params=params or None, timeout=min(8, self.time_left()))
            except requests.RequestException:
                return None
            try:
                self.remaining = int(r.headers.get("X-Discogs-Ratelimit-Remaining"))
            except (TypeError, ValueError):
                pass
            if r.status_code == 429 and attempt == 0:
                # Discogs counts a rolling minute, so a short pause usually frees a slot.
                if self.time_left() > 6:
                    time.sleep(3)
                    continue
                return None
            if r.status_code != 200:
                return None
            try:
                return r.json()
            except ValueError:
                return None
        return None


def _num_year(y):
    y = str(y or "")[:4]
    return int(y) if y.isdigit() and int(y) > 0 else None


def versions_of(dig, master_id):
    """Every pressing of the record, oldest first: [{id, label, year, country}], one per label+year."""
    data = dig.get(f"/masters/{master_id}/versions", per_page=100, sort="released", sort_order="asc")
    best = {}
    for v in (data or {}).get("versions") or []:
        fmt = (v.get("format") or "").lower()
        if "unofficial" in fmt or "compilation" in fmt or not v.get("id"):
            continue
        label = clean_label((v.get("label") or "").split(",")[0]) or "no label"
        year = _num_year(v.get("released"))
        community = ((v.get("stats") or {}).get("community") or {})
        have = (community.get("in_collection") or 0) + (community.get("in_wantlist") or 0)
        key = (label.lower(), year)
        if key not in best or have > best[key]["have"]:
            best[key] = {"id": v["id"], "label": label, "year": year, "country": v.get("country") or "", "have": have}
    # Oldest first; within a year, the most collected pressing (the home-country original,
    # e.g. Factory's Blue Monday rather than the German licence the same year).
    out = sorted(best.values(), key=lambda v: (v["year"] is None, v["year"] or 0, -v["have"]))
    return out


def search_song(dig, song_name, artist_name, release_id=None):
    """Find the song, then step back to its ORIGINAL release: the oldest pressing, so the
    label and the era are the ones it first came out on (not a later reissue).
    release_id picks a specific pressing instead (the 'dig a different version' menu)."""
    data = dig.get("/database/search", q=f"{song_name} {artist_name}".strip(), type="release", per_page=50)
    if data is None:
        raise DiscogsError(BUSY)
    results = data.get("results") or []
    if not results:
        return None
    first = pick_original(results, artist_name)
    master_id = first.get("master_id")
    options = versions_of(dig, master_id) if master_id else []
    if not options:
        # No master on Discogs: the other pressings are the search hits with the same title.
        title = norm(first.get("title"))
        seen = set()
        for r in sorted(results, key=lambda r: _num_year(r.get("year")) or 9999):
            if norm(r.get("title")) != title:
                continue
            label = clean_label((r.get("label") or ["no label"])[0])
            key = (label.lower(), _num_year(r.get("year")))
            if key not in seen:
                seen.add(key)
                options.append({"id": r["id"], "label": label, "year": key[1], "country": r.get("country") or ""})
    chosen = None
    if release_id:
        chosen = int(release_id)
    elif options and options[0]["year"]:
        chosen = options[0]["id"]  # the oldest pressing is the original
    else:
        chosen = first.get("id")
    release = dig.get(f"/releases/{chosen}")
    if release is None and chosen != first.get("id"):
        chosen = first.get("id")
        release = dig.get(f"/releases/{chosen}")
    if release is None:
        raise DiscogsError(BUSY)
    year = _num_year(release.get("year"))
    if not year:
        year = next((o["year"] for o in options if o["id"] == chosen and o["year"]), None)
    if not year and not release_id:
        year = next((o["year"] for o in options if o["year"]), None)
    labels = label_names(release)
    return {
        "id": chosen,
        "name": display_title(artist_names(release.get("artists")), release.get("title") or ""),
        "url": release.get("uri") or f"https://www.discogs.com/release/{chosen}",
        "master_id": master_id,
        "year": year,
        "label": labels[0] if labels else None,
        "country": release.get("country") or "",
        "options": options[:40],
        "genres": release.get("genres") or first.get("genre") or [],
        "styles": release.get("styles") or first.get("style") or [],
        "labels": labels,
        "label_refs": label_list(release),
        "videos": [{"yt": youtube_id(v.get("uri")), "title": v.get("title") or ""}
                   for v in release.get("videos") or [] if youtube_id(v.get("uri"))],
    }


def disc_info(song):
    """What the results page shows about the record being dug on."""
    if not song:
        return None
    return {k: song.get(k) for k in ("id", "name", "url", "year", "label", "country", "options")}


def pick_original(results, artist_name):
    """Prefer the artist's own single/album over later compilations and reissues:
    among results that aren't compilations (and name the artist, when given), take the earliest."""
    def year(r):
        y = str(r.get("year") or "")
        return int(y) if y.isdigit() and int(y) > 0 else 9999

    def is_comp(r):
        return any("compilation" in (f or "").lower() for f in r.get("format") or []) or \
            (r.get("title") or "").lower().startswith("various")

    want = norm(artist_name)
    pool = [r for r in results if not is_comp(r)]
    if want:
        named = [r for r in pool if want in norm(r.get("title"))]
        pool = named or pool
    if not pool:
        return results[0]
    best = min(year(r) for r in pool)
    return next(r for r in pool if year(r) == best)


def clean_label(name):
    """Discogs numbers labels that share a name: 'Crystal Records (3)'. Searches and people want 'Crystal Records'."""
    return re.sub(r"\s*\(\d+\)$", "", name or "").strip()


def label_list(release):
    labels, seen = [], set()
    for label in release.get("labels") or []:
        name = (label.get("name") or "").strip()
        if not name or name.lower() == "not on label" or name in seen:
            continue
        seen.add(name)
        labels.append({"name": name, "clean": clean_label(name), "id": label.get("id")})
    return labels[:MAX_LABELS]


def label_names(release):
    return [l["clean"] for l in label_list(release)]


# Later remixes and bootleg mixes ride on a label's name but aren't its sound or era.
# Dub/version/extended/vocal mixes are kept: they're part of the original releases.
REMIX = re.compile(
    r"\bre-?mix(es|ed)?\b|\brmx\b|\bbootleg\b|\bmash-?up\b|"
    r"\b(hip[ -]?hop|house|club|radio|garage|dnb|drum (?:&|and) bass|techno|trap|edm|lazy dog|deep)\s+(mix|edit|version|rework)\b",
    re.I)


def fits(song, album, check_genre=True):
    """Same era, same sound. Drops e.g. 2010s house remixes from a 90s reggae label."""
    if REMIX.search(album.get("title") or ""):
        return False
    year = album.get("year")
    year = int(year) if str(year or "").isdigit() and int(year) > 0 else None
    if song["year"]:
        if year is None or abs(year - song["year"]) > ERA_YEARS:
            return False
    if not check_genre:
        return True  # straight from the label's own catalogue, which doesn't list genres
    genres, styles = album.get("genre") or [], album.get("style") or []
    if song["genres"] and not set(genres) & set(song["genres"]):
        return False
    # Styles that just repeat a genre name ("Reggae") say nothing about the sound.
    generic = {g.lower() for g in list(genres) + list(song["genres"])}
    mine = {x for x in song["styles"] if x.lower() not in generic}
    theirs = {x for x in styles if x.lower() not in generic}
    shared = mine & theirs
    if mine and theirs and not shared:
        return False
    # Bringing in another genre (Sade singles on a roots label tagged Hip Hop or Funk / Soul)
    # is only OK when a real style is shared (e.g. Synth-pop on Electronic + Rock records).
    if song["genres"] and set(genres) - set(song["genres"]) and not shared:
        return False
    return True


def label_search(dig, label_name):
    """Discogs search by label name: has genres, styles and collector counts, but mixes
    together every label that shares the name."""
    found = {}
    for page in range(1, LABEL_PAGES + 1):
        data = dig.get("/database/search", label=label_name, type="release", per_page=100, page=page)
        if not data:
            break
        for r in data.get("results") or []:
            if r.get("id"):
                found[r["id"]] = r
        if page >= ((data.get("pagination") or {}).get("pages") or 1):
            break
    return found


def _year(r):
    y = str(r.get("year") or "")
    return int(y) if y.isdigit() else 0


def label_catalogue(dig, label_id, start=None, end=None, max_pages=4):
    """Exactly this label's releases (by id). Sorted by year, so jump to the pages that
    cover start..end instead of reading from 1956 onwards. No genres in here."""
    def page(n):
        return dig.get(f"/labels/{label_id}/releases", per_page=100, page=n, sort="year", sort_order="asc")

    first = page(1)
    if not first:
        return []
    pages = (first.get("pagination") or {}).get("pages") or 1
    if not start:
        return first.get("releases") or []

    def last_year(data):
        rs = data.get("releases") or []
        return _year(rs[-1]) if rs else 0

    # Binary search for the first page whose last release reaches the start year.
    lo, hi, cache = 1, pages, {1: first}
    while lo < hi:
        mid = (lo + hi) // 2
        cache[mid] = cache.get(mid) or page(mid)
        if not cache[mid]:
            break
        if last_year(cache[mid]) < start:
            lo = mid + 1
        else:
            hi = mid
    out, n = [], lo
    while n <= pages and len([1 for _ in out]) < max_pages * 100:
        data = cache.get(n) or page(n)
        if not data:
            break
        rs = data.get("releases") or []
        out += [r for r in rs if start <= _year(r) <= (end or 9999)]
        if not rs or _year(rs[-1]) > (end or 9999) or n - lo + 1 >= max_pages:
            break
        n += 1
    return out


def get_top_albums(dig, song, num_albums=MAX_ALBUMS):
    """Other releases on the original label(s), from the same era and style, most-collected first."""
    seen, albums = set(), []

    def add(album, check_genre=True):
        if album.get("id") == song["id"] or (song["master_id"] and album.get("master_id") == song["master_id"]):
            return
        if norm(album.get("title")) == norm(song.get("name")):  # another pressing of the song itself
            return
        if not fits(song, album, check_genre=check_genre):
            return
        key = album.get("master_id") or (album.get("title") or "").lower()
        if key in seen:  # one entry per record, not every pressing
            return
        seen.add(key)
        community = album.get("community") or {}
        albums.append({
            "id": album.get("id"),
            "title": album.get("title") or "Untitled",
            "year": album.get("year"),
            "want": community.get("want", 0) or 0,
            "have": community.get("have", 0) or 0,
        })

    refs = song.get("label_refs") or [{"name": n, "clean": n, "id": None} for n in song["labels"]]
    start = song["year"] - ERA_YEARS if song.get("year") else None
    end = song["year"] + ERA_YEARS if song.get("year") else None
    for ref in refs:
        searched = label_search(dig, ref["clean"])
        catalogue = label_catalogue(dig, ref["id"], start, end) if ref.get("id") else []
        if not catalogue:
            for r in searched.values():
                add(r)
            continue
        # The label's own list decides what's on it; the search adds genres where it has them.
        # If the search knows most of this era's releases, skip the ones it can't vouch for
        # (genre unknown); if it barely knows them (small label), go on era alone.
        known = sum(1 for r in catalogue if r.get("id") in searched)
        trust_unknown = known < 0.3 * len(catalogue)
        for r in catalogue:
            info = searched.get(r.get("id"))
            if info:
                add(info)
            elif trust_unknown:
                stats = ((r.get("stats") or {}).get("community") or {})
                title = " - ".join(x for x in (re.sub(r"\s*\(\d+\)$", "", (r.get("artist") or "")).rstrip("*"), r.get("title")) if x)
                add({"id": r.get("id"), "title": title, "year": r.get("year"),
                     "community": {"want": stats.get("in_wantlist", 0), "have": stats.get("in_collection", 0)}},
                    check_genre=False)
    albums.sort(key=lambda a: a["want"] + a["have"], reverse=True)
    return albums[:num_albums]


YOUTUBE_ID = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})")


def youtube_id(url):
    m = YOUTUBE_ID.search(url or "")
    return m.group(1) if m else None


def artist_names(artists):
    """Discogs artist credits -> 'A & B'; drops the '(2)' disambiguation numbers and '*'."""
    out = []
    for a in artists or []:
        name = re.sub(r"\s*\(\d+\)$", "", (a.get("anv") or a.get("name") or "").strip()).rstrip("*")
        if name:
            out.append(name + ((" " + a["join"].strip()) if a.get("join", "").strip() and a["join"].strip() != "," else ""))
    return " ".join(out).strip(" &,") or None


def norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def track_artist_for(video_title, tracklist):
    """Compilations credit 'Various': find the track this video is, and use its own artist."""
    vt = norm(video_title)
    best = None
    for t in tracklist:
        tt = norm(t.get("title"))
        if len(tt) >= 3 and tt in vt and (best is None or len(tt) > len(norm(best.get("title")))):
            best = t
    return artist_names(best.get("artists")) if best else None


def display_title(artist, title):
    """'Artist - Title', without repeating the artist when the video title already has it."""
    if not artist or artist.lower() in ("various", "unknown artist"):
        return title
    if norm(title).startswith(norm(artist)):
        return title
    return f"{artist} - {title}"


def get_album_videos(dig, album_id):
    data = dig.get(f"/releases/{album_id}")
    if not data:
        return None
    album_artist = artist_names(data.get("artists")) or "Unknown artist"
    various = album_artist.lower().startswith("various")
    tracklist = data.get("tracklist") or []
    thumb = data.get("thumb") or ""
    out = []
    for v in data.get("videos") or []:
        if not (v.get("embed") and v.get("uri")):
            continue
        title = v.get("title", "Untitled")
        artist = track_artist_for(title, tracklist) or (None if various else album_artist)
        out.append({"link": v.get("uri"), "thumb": thumb, "artist": artist or "",
                    "title": title, "name": display_title(artist, title), "yt": youtube_id(v.get("uri")),
                    "secs": v.get("duration") or None})
    return out


def popularity(album):
    bits = []
    if album["want"]:
        bits.append(f"{album['want']:,} want it")
    if album["have"]:
        bits.append(f"{album['have']:,} have it")
    return " · ".join(bits) or "No rating available"


def album_title(album):
    return f"{album['title']} ({album['year']})" if album.get("year") else album["title"]


def dig_song(token, song_name, artist_name, release_id=None):
    dig = Digger(token)
    song = search_song(dig, song_name, artist_name, release_id)
    if song is None:
        return NO_MATCH
    if not song["labels"]:
        return {"label_names": ["an unknown label"], "albums": [], "playlist": [], "year": song["year"], "song": song["name"], "disc": disc_info(song)}

    albums = get_top_albums(dig, song)

    # Track lists cost one request per album. Fetch what Discogs' per-minute allowance
    # leaves room for now; the browser fetches the rest a few seconds apart (/tracks).
    budget = len(albums) if dig.remaining is None else max(0, min(len(albums), dig.remaining - RESERVE))
    videos = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(get_album_videos, dig, a["id"]): a["id"] for a in albums[:budget]}
        for fut in as_completed(futures):
            try:
                result = fut.result()
            except Exception:
                result = None
            if result is not None:
                videos[futures[fut]] = result

    out, playlist, seen = [], [], set()
    for a in albums:
        tracks = videos.get(a["id"])
        out.append({"id": a["id"], "title": album_title(a), "rating": popularity(a),
                    "tracks": tracks or [], "pending": tracks is None})
        for v in tracks or []:
            if v.get("yt") and v["yt"] not in seen:
                seen.add(v["yt"])
                playlist.append({"id": v["yt"], "name": v["name"], "rid": a["id"],
                                 "album": album_title(a), "link": v["link"], "secs": v.get("secs")})

    return {"label_names": song["labels"], "year": song["year"], "albums": out, "playlist": playlist, "song": song["name"],
            "disc": disc_info(song)}


def dig_by_uploader(token, song_name, artist_name, release_id=None):
    song = None
    if token:
        try:
            song = search_song(Digger(token), song_name, artist_name, release_id)
        except DiscogsError:
            song = None  # Discogs only helps rank; YouTube can still do the digging
    result = uploader.dig_uploader(song, song_name, artist_name)
    if isinstance(result, dict):
        result["disc"] = disc_info(song)
    return result


_PROFILES = {}


def various_id(aid):
    return aid in (194, 355)   # Discogs' "Various" and "Unknown Artist"


def _artist_profile(dig, aid):
    """Discogs artist page (bio, band members), remembered for a few hours."""
    hit = _PROFILES.get(aid)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    prof = dig.get(f"/artists/{aid}") if dig.time_left() > 2 else None
    if prof is not None:
        _PROFILES[aid] = (time.time(), {k: prof.get(k) for k in ("profile", "uri", "members")})
        if len(_PROFILES) > 500:
            _PROFILES.pop(next(iter(_PROFILES)))
    return prof


def _release_facts(release):
    labels = []
    for l in release.get("labels") or []:
        name = clean_label(l.get("name"))
        cat = (l.get("catno") or "").strip()
        bit = name + (f" ({cat})" if cat and cat.lower() != "none" else "")
        if name and not any(x == name or x.startswith(name + " (") for x in labels):
            labels.append(bit)
    fmt = []
    for f in release.get("formats") or []:
        for d in [f.get("name")] + (f.get("descriptions") or []):
            if d and d not in fmt:
                fmt.append(d)
    rid = release.get("id")
    return {"artist": artist_names(release.get("artists")) or "", "title": release.get("title") or "",
            "labels": labels[:3], "year": release.get("year") or None, "country": release.get("country") or "",
            "genres": (release.get("genres") or []) + (release.get("styles") or []), "format": ", ".join(fmt[:5]),
            "discogs_url": release.get("uri") or (f"https://www.discogs.com/release/{rid}" if rid else "")}


@app.route("/about")
def about():
    """What the page shows beside the player: Discogs facts, credits, and a few lines from Wikipedia."""
    token = os.environ.get("DISCOGS_TOKEN")
    video_title = request.args.get("title", "")[:300]
    rid = request.args.get("release", "")
    if not token:
        return jsonify({"error": "no token"}), 503
    dig = Digger(token)
    dig.deadline = time.monotonic() + 9
    if not rid.isdigit():
        rid = about_mod.find_release(dig, video_title)
        if rid is about_mod.BUSY:
            return jsonify({"retry": 6}), 429
        if not rid:
            return jsonify({"found": False})
    release = dig.get(f"/releases/{rid}")
    if not release:
        return jsonify({"retry": 5}), 429
    out = _release_facts(release)
    out["found"] = True
    out["credits"] = about_mod.credits(release, video_title)

    various = norm(out["artist"]).startswith("various")
    track_artist = track_artist_for(video_title, release.get("tracklist") or [])
    who = track_artist or ("" if various else out["artist"])
    out["track_artist"] = who if who and who != out["artist"] else ""
    first = re.split(r"\s+(?:&|and|feat\.?|featuring|with|vs\.?)\s+|,\s*", who or "", maxsplit=1)[0].strip()
    hints = out["genres"] + ([out["country"]] if out["country"] else [])

    song = None
    vt = norm(video_title)
    for t in release.get("tracklist") or []:
        tt = norm(t.get("title"))
        if len(tt) >= 3 and tt in vt and (song is None or len(tt) > len(norm(song))):
            song = t.get("title")

    with ThreadPoolExecutor(max_workers=3) as pool:
        bio_f = pool.submit(about_mod.artist_bio, first, hints) if first else None
        rec_f = pool.submit(about_mod.record_blurb, out["title"], first or out["artist"])
        song_f = (pool.submit(about_mod.record_blurb, song, first or out["artist"])
                  if song and norm(song) != norm(out["title"]) else None)
        bio = bio_f.result() if bio_f else None
        rec = rec_f.result() or (song_f.result() if song_f else None)

    aid = next((x.get("id") for x in (release.get("artists") or []) if x.get("id")), None)
    if track_artist:
        for t in release.get("tracklist") or []:
            if t.get("title") == song and t.get("artists"):
                aid = t["artists"][0].get("id") or aid
    prof = _artist_profile(dig, aid) if aid and not various_id(aid) else None
    if prof:
        out["members"] = [re.sub(r"\s*\(\d+\)$", "", m.get("name") or "") for m in prof.get("members") or []
                          if m.get("name")][:12]
    if not bio and prof:
        # No Wikipedia page that's clearly them: Discogs' own profile, if anyone wrote one.
        text = about_mod.short(about_mod.clean_discogs(prof.get("profile")))
        if text:
            bio = {"text": text, "url": prof.get("uri") or "", "source": "Discogs"}
    out["artist_bio"] = bio
    out["record"] = rec
    return jsonify(out)


@app.route("/uploads")
def uploads():
    """Next page of the uploader's channel, tagged and scored, for the page to append."""
    token = request.args.get("token") or ""
    year = request.args.get("year", "")
    target = {"genres": [g for g in request.args.get("genres", "").split("|") if g],
              "year": int(year) if year.isdigit() else None,
              "artist": request.args.get("artist", "")}
    items, nxt = uploader.dig_uploads_page(token, target)
    if items is None:
        return jsonify({"retry": 3}), 503
    return jsonify({"uploads": items, "next": nxt})


@app.route("/tracks/<int:album_id>")
def tracks(album_id):
    """One album's playable tracks, fetched by the page after it loads."""
    token = os.environ.get("DISCOGS_TOKEN")
    if not token:
        return jsonify({"error": "no token"}), 503
    dig = Digger(token)
    dig.deadline = time.monotonic() + 8
    videos = get_album_videos(dig, album_id)
    if videos is None:
        return jsonify({"retry": 5}), 429
    return jsonify({"tracks": videos})


_ARTISTS = {}


@app.route("/artists")
def artists():
    """Artist names for the home page's autofill, from Discogs."""
    q = (request.args.get("q") or "").strip()[:60]
    if len(q) < 2:
        return jsonify([])
    key = q.lower()
    if key not in _ARTISTS:
        token = os.environ.get("DISCOGS_TOKEN")
        if not token:
            return jsonify([])
        dig = Digger(token)
        dig.deadline = time.monotonic() + 5
        data = dig.get("/database/search", q=q, type="artist", per_page=15)
        if data is None:
            return jsonify([])
        names, seen = [], set()
        for r in data.get("results") or []:
            name = clean_label(r.get("title"))  # drops Discogs' "(2)" numbering
            if name and name.lower() not in seen:
                seen.add(name.lower())
                names.append(name)
        if len(_ARTISTS) > 2000:
            _ARTISTS.clear()
        _ARTISTS[key] = names[:8]
    resp = jsonify(_ARTISTS[key])
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/playlist")
def playlist():
    """Turn up to 50 YouTube ids into a real YouTube playlist link, so the songs can be
    opened in the YouTube app and cast to a TV from there."""
    ids = [i for i in (request.args.get("ids") or "").split(",") if re.fullmatch(r"[\w-]{11}", i)][:50]
    if not ids:
        return jsonify({"url": None}), 400
    direct = "https://www.youtube.com/watch_videos?video_ids=" + ",".join(ids)
    try:
        r = requests.get(direct, allow_redirects=False, timeout=6,
                         headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"})
        loc = r.headers.get("Location") or ""
        if "list=" in loc:
            return jsonify({"url": loc if loc.startswith("http") else "https://www.youtube.com" + loc})
    except requests.RequestException:
        pass
    return jsonify({"url": direct})


@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        song_name = (request.form.get("song_name") or "").strip()
        artist_name = (request.form.get("artist_name") or "").strip()
        if not song_name and not artist_name:
            return render_template("home.html")
        token = os.environ.get("DISCOGS_TOKEN")
        query = " by ".join(x for x in (song_name, artist_name) if x)
        release_id = request.form.get("release_id") or ""
        release_id = release_id if release_id.isdigit() else None
        form = {"song_name": song_name, "artist_name": artist_name, "mode": request.form.get("mode") or "label"}
        if request.form.get("mode") == "uploader":
            try:
                result = dig_by_uploader(token, song_name, artist_name, release_id)
            except Exception:
                app.logger.exception("uploader dig failed")
                result = "YouTube didn't answer. Give it a minute and dig again."
            return render_template("uploader.html", result=result, query=query, form=form)
        if not token:
            return render_template("results.html", result=NO_TOKEN, query="")
        try:
            result = dig_song(token, song_name, artist_name, release_id)
        except DiscogsError as e:
            result = str(e)
        except Exception:
            app.logger.exception("dig failed")
            result = BUSY
        return render_template("results.html", result=result, query=query, form=form)

    return render_template("home.html")


if __name__ == "__main__":
    app.run(debug=True, port=5008)



