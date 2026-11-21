import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, jsonify, render_template, request

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


def search_song(dig, song_name, artist_name):
    """Find the song, then step back to its ORIGINAL release (not a later reissue),
    so the label and the era are the ones it first came out on."""
    data = dig.get("/database/search", q=f"{song_name} {artist_name}".strip(), type="release")
    if data is None:
        raise DiscogsError(BUSY)
    results = data.get("results") or []
    if not results:
        return None
    first = results[0]
    release_id = first.get("id")
    master_id = first.get("master_id")
    if master_id:
        master = dig.get(f"/masters/{master_id}")
        if master and master.get("main_release"):
            release_id = master["main_release"]
    release = dig.get(f"/releases/{release_id}")
    if release is None:
        raise DiscogsError(BUSY)
    years = [y for y in [release.get("year")] + [r.get("year") for r in results[:10]
                                                   if r.get("master_id") == master_id and master_id]
             if str(y or "").isdigit() and int(y) > 0]
    return {
        "id": release_id,
        "master_id": master_id,
        "year": min(int(y) for y in years) if years else None,
        "genres": release.get("genres") or first.get("genre") or [],
        "styles": release.get("styles") or first.get("style") or [],
        "labels": label_names(release),
    }


def label_names(release):
    names = []
    for label in release.get("labels") or []:
        name = (label.get("name") or "").strip()
        if name and name not in names and name.lower() != "not on label":
            names.append(name)
    return names[:MAX_LABELS]


def fits(song, album):
    """Same era, same sound. Drops e.g. 2010s house remixes from a 90s reggae label."""
    year = album.get("year")
    year = int(year) if str(year or "").isdigit() and int(year) > 0 else None
    if song["year"]:
        if year is None or abs(year - song["year"]) > ERA_YEARS:
            return False
    genres, styles = album.get("genre") or [], album.get("style") or []
    if song["genres"] and not set(genres) & set(song["genres"]):
        return False
    if song["styles"] and styles and not set(styles) & set(song["styles"]):
        return False
    return True


def get_top_albums(dig, song, num_albums=MAX_ALBUMS):
    """Other releases on the original label(s), from the same era and style, most-collected first."""
    seen, albums = set(), []
    for label_name in song["labels"]:
        for page in range(1, LABEL_PAGES + 1):
            data = dig.get("/database/search", label=label_name, type="release", per_page=100, page=page)
            if not data:
                break
            for album in data.get("results") or []:
                if album.get("id") == song["id"] or (song["master_id"] and album.get("master_id") == song["master_id"]):
                    continue
                if not fits(song, album):
                    continue
                key = album.get("master_id") or (album.get("title") or "").lower()
                if key in seen:  # one entry per record, not every pressing
                    continue
                seen.add(key)
                community = album.get("community") or {}
                albums.append({
                    "id": album.get("id"),
                    "title": album.get("title") or "Untitled",
                    "year": album.get("year"),
                    "want": community.get("want", 0) or 0,
                    "have": community.get("have", 0) or 0,
                })
            if page >= ((data.get("pagination") or {}).get("pages") or 1):
                break
    albums.sort(key=lambda a: a["want"] + a["have"], reverse=True)
    return albums[:num_albums]


YOUTUBE_ID = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})")


def youtube_id(url):
    m = YOUTUBE_ID.search(url or "")
    return m.group(1) if m else None


def get_album_videos(dig, album_id):
    data = dig.get(f"/releases/{album_id}")
    if not data:
        return None
    artists = data.get("artists") or [{}]
    artist = artists[0].get("name", "Unknown artist")
    thumb = data.get("thumb") or ""
    return [
        {"link": v.get("uri"), "thumb": thumb, "artist": artist, "title": v.get("title", "Untitled"),
         "yt": youtube_id(v.get("uri"))}
        for v in data.get("videos") or []
        if v.get("embed") and v.get("uri")
    ]


def popularity(album):
    bits = []
    if album["want"]:
        bits.append(f"{album['want']:,} want it")
    if album["have"]:
        bits.append(f"{album['have']:,} have it")
    return " · ".join(bits) or "No rating available"


def album_title(album):
    return f"{album['title']} ({album['year']})" if album.get("year") else album["title"]


def dig_song(token, song_name, artist_name):
    dig = Digger(token)
    song = search_song(dig, song_name, artist_name)
    if song is None:
        return NO_MATCH
    if not song["labels"]:
        return {"label_names": ["an unknown label"], "albums": [], "playlist": [], "year": song["year"]}

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
                playlist.append({"id": v["yt"], "title": v["title"], "artist": v["artist"],
                                 "album": album_title(a), "link": v["link"]})

    return {"label_names": song["labels"], "year": song["year"], "albums": out, "playlist": playlist}


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


@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        song_name = (request.form.get("song_name") or "").strip()
        artist_name = (request.form.get("artist_name") or "").strip()
        if not song_name and not artist_name:
            return render_template("home.html")
        token = os.environ.get("DISCOGS_TOKEN")
        if not token:
            return render_template("results.html", result=NO_TOKEN)
        try:
            result = dig_song(token, song_name, artist_name)
        except DiscogsError as e:
            result = str(e)
        except Exception:
            app.logger.exception("dig failed")
            result = BUSY
        return render_template("results.html", result=result)

    return render_template("home.html")


if __name__ == "__main__":
    app.run(debug=True, port=5008)
