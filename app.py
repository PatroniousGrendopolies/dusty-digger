import os
import requests
from flask import Flask, render_template, request

app = Flask(__name__)

# Discogs personal token, read from the environment (never commit it)
user_token = os.environ["DISCOGS_TOKEN"]

def search_song(user_token, song_name, artist_name):
    headers = {
        "Authorization": f"Discogs token={user_token}",
        "User-Agent": "DiscogsAPIExample/1.0 +http://example.com",
    }
    url = "https://api.discogs.com/database/search"
    response = requests.get(url, headers=headers, params={"q": f"{song_name} {artist_name}", "type": "release"})
    data = response.json()

    if data['pagination']['items'] == 0:
        return None, None
    else:
        genre = data['results'][0]['genre'][0] if data['results'][0]['genre'] else None
        return data['results'][0]['id'], genre

def get_label_of_song(user_token, song_id):
    headers = {
        "Authorization": f"Discogs token={user_token}",
        "User-Agent": "DiscogsAPIExample/1.0 +http://example.com",
    }
    url = f"https://api.discogs.com/releases/{song_id}"
    response = requests.get(url, headers=headers)
    data = response.json()
    labels = [label['name'] for label in data['labels']] if data['labels'] else ["No label found"]
    return labels

def get_top_albums(user_token, label_names, genre, num_albums=50):
    headers = {
        "Authorization": f"Discogs token={user_token}",
        "User-Agent": "DiscogsAPIExample/1.0 +http://example.com",
    }
    top_albums = []
    for label_name in label_names:
        page = 1
        while len(top_albums) < num_albums:
            url = "https://api.discogs.com/database/search"
            response = requests.get(url, headers=headers, params={"label": label_name, "type": "release", "sort": "rating", "sort_order": "desc", "per_page": 100, "page": page})
            data = response.json()
            filtered_albums = [album for album in data['results'] if genre in album.get('genre', [])]
            top_albums.extend(filtered_albums)
            if len(data['results']) < 100:  # less than 100 results, no more pages
                break
            page += 1
    return [(album['id'], album['title'], album.get('rating', 'No rating available')) for album in top_albums[:num_albums]]

'''
#get album videos with an embedded video link (legacy); now retrieves a thumbnail.
def get_album_videos(user_token, album_id):
    headers = {
        "Authorization": f"Discogs token={user_token}",
        "User-Agent": "DiscogsAPIExample/1.0 +http://example.com",
    }
    url = f"https://api.discogs.com/releases/{album_id}"
    response = requests.get(url, headers=headers)
    data = response.json()
    videos = [video['uri'].replace("https://www.youtube.com/watch?v=", "https://www.youtube.com/embed/") for video in data.get('videos', [])]
    return videos
'''
def get_album_videos(user_token, album_id):
    headers = {
        "Authorization": f"Discogs token={user_token}",
        "User-Agent": "DiscogsAPIExample/1.0 +http://example.com",
    }
    url = f"https://api.discogs.com/releases/{album_id}"
    response = requests.get(url, headers=headers)
    data = response.json()
    videos = [{"link": video['uri'],
               "thumb": data['thumb'], # use the album thumbnail
               "artist": data['artists'][0]['name'], # assuming the first artist is the main one
               "title": video['title']}
              for video in data.get('videos', [])
              if video['embed'] == True]
    return videos





@app.route('/', methods=['GET', 'POST'])
def home():
    if request.method == 'POST':
        song_name = request.form.get('song_name')
        artist_name = request.form.get('artist_name')
        song_id, genre = search_song(user_token, song_name, artist_name)
        if song_id is None:
            return render_template("results.html", result="We've hit a rock! No match found for the song")
        else:
            label_names = get_label_of_song(user_token, song_id)
            albums_with_videos = []
            for label_name in label_names:
                top_albums = get_top_albums(user_token, [label_name], genre)
                for album in top_albums:
                    videos = get_album_videos(user_token, album[0])
                    albums_with_videos.append((album[1], album[2], videos))
            return render_template("results.html", result={'label_names': label_names, 'albums_with_videos': albums_with_videos})

    return render_template('home.html')

if __name__ == '__main__':
    app.run(debug=True, port=5008)
