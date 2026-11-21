import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, render_template, request

app = Flask(__name__)

API = "https://api.discogs.com"
USER_AGENT = "DustyDigger/1.1 +https://dusty-digger.vercel.app"

# How much digging one search may do. Discogs allows about 60 requests a minute,
# and each album's track list is one request, so we look up a handful of albums
# instead of 50, and stop early if the clock runs out.
MAX_LABELS = 2
MAX_ALBUMS = 12
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
            if r.status_code == 429 and attempt == 0:
                try:
                    wait = float(r.headers.get("Retry-After") or 2)
                except ValueError:
                    wait = 2
                if wait < self.time_left() - 2:
                    time.sleep(min(wait, 5))
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
    data = dig.get("/database/search", q=f"{song_name} {artist_name}".strip(), type="release")
    if data is None:
        raise DiscogsError(BUSY)
    results = data.get("results") or []
    if not results:
        return None, None
    first = results[0]
    genres = first.get("genre") or []
    return first.get("id"), (genres[0] if genres else None)


def get_label_of_song(dig, song_id):
    data = dig.get(f"/releases/{song_id}")
    if data is None:
        raise DiscogsError(BUSY)
    names = []
    for label in data.get("labels") or []:
        name = (label.get("name") or "").strip()
        if name and name not in names and name.lower() != "not on label":
            names.append(name)
    return names[:MAX_LABELS]


def get_top_albums(dig, label_names, genre, exclude_id=None, num_albums=MAX_ALBUMS):
    """Other releases on the same label(s), same genre, most-collected first."""
    seen_titles, albums = set(), []
    for label_name in label_names:
        data = dig.get("/database/search", label=label_name, type="release", per_page=100)
        if not data:
            continue
        for album in data.get("results") or []:
            if album.get("id") == exclude_id:
                continue
            if genre and genre not in (album.get("genre") or []):
                continue
            title = album.get("title") or "Untitled"
            if title.lower() in seen_titles:  # skip reissues/duplicates of the same record
                continue
            seen_titles.add(title.lower())
            community = album.get("community") or {}
            albums.append({
                "id": album.get("id"),
                "title": title,
                "year": album.get("year"),
                "want": community.get("want", 0) or 0,
                "have": community.get("have", 0) or 0,
            })
    albums.sort(key=lambda a: a["want"] + a["have"], reverse=True)
    return albums[:num_albums]


def get_album_videos(dig, album_id):
    data = dig.get(f"/releases/{album_id}")
    if not data:
        return []
    artists = data.get("artists") or [{}]
    artist = artists[0].get("name", "Unknown artist")
    thumb = data.get("thumb") or ""
    return [
        {"link": v.get("uri"), "thumb": thumb, "artist": artist, "title": v.get("title", "Untitled")}
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
    song_id, genre = search_song(dig, song_name, artist_name)
    if song_id is None:
        return NO_MATCH
    label_names = get_label_of_song(dig, song_id)
    if not label_names:
        return {"label_names": ["an unknown label"], "albums_with_videos": []}

    albums = get_top_albums(dig, label_names, genre, exclude_id=song_id)

    # Fetch track lists a few at a time; whatever misses the deadline shows without tracks.
    videos = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(get_album_videos, dig, a["id"]): a["id"] for a in albums}
        for fut in as_completed(futures):
            try:
                videos[futures[fut]] = fut.result()
            except Exception:
                videos[futures[fut]] = []

    return {
        "label_names": label_names,
        "albums_with_videos": [(album_title(a), popularity(a), videos.get(a["id"], [])) for a in albums],
    }


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
