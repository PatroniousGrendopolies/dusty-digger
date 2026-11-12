# Dusty Digger

This was my first app. I built it in 2023.

Type in a song and an artist. Dusty Digger finds the record label that first put the song out, then digs up other top-rated releases from that label in the same genre, with links to their tracks.

**Try it:** https://dusty-digger.vercel.app

## Run it locally

Built with Flask and the [Discogs API](https://www.discogs.com/developers).

```bash
pip install -r requirements.txt
export DISCOGS_TOKEN=your-discogs-token   # get one at discogs.com/settings/developers
python app.py
```

Then open http://localhost:5008.
