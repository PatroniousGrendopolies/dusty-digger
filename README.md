# Dusty Digger

This was my first app. I built it in 2023.

Type in a song and an artist. Dusty Digger finds the record label that first put the song out, then digs up other top-rated releases from that label in the same genre, with links to their tracks.

**The player:** every track it digs up goes into a playlist you can play right on the results page (YouTube embeds). Skip what you don't like, hit ♥ Keep on the good ones, and your keepers collect below the player (saved in your browser). Keys: → skip, ← back, K keep, space play/pause.

**Try it:** https://dusty-digger.vercel.app

## Run it locally

Built with Flask and the [Discogs API](https://www.discogs.com/developers).

```bash
pip install -r requirements.txt
export DISCOGS_TOKEN=your-discogs-token   # get one at discogs.com/settings/developers
python app.py
```

Then open http://localhost:5008.


**Two ways to dig:** *by label* (the record label that first released the song, same era and style, via Discogs) or *by uploader* (everything else posted by the YouTube channel that uploaded the song, usually a crate-digging collector, ranked by genre, year and artist, with genre checkboxes). The uploader mode reads YouTube's public web pages, so it needs no API key.
