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

**Third way: *by lists*** (records on the same Discogs lists). Dusty Digger steps to the song's ORIGINAL release, asks the *list finder* which Discogs user lists it is on, reads up to 12 of those lists from the public API (`GET /lists/{id}`, lists over 500 records skipped), drops the searched record, and ranks the rest by how many of the lists they share (ties: the record on the smaller list first). Only release items are used (master items on a list are skipped). Results show `Digging on:` and `Lists used:`. If the finder is down or blocked, the page says so and offers a box to paste Discogs list links (or numbers); the same box on the home page works directly.

The list finder is needed because Discogs has no API for "lists containing this release" and the www page is behind a Cloudflare challenge. It is `listfinder/finder.js`, a small Node service on the Mac mini that drives one ordinary Google Chrome (its own profile, port 9333 on 127.0.0.1) at human pace: one page load at a time, at least 8s apart, results cached 7 days. It is exposed with Tailscale Funnel (`tailscale funnel --bg --https=8443 8787`) and requires header `X-DD-Key`. Vercel env: `LISTFINDER_URL`, `LISTFINDER_KEY` (the key lives in `~/.config/dd-listfinder/key` on the Mini). LaunchAgent: `listfinder/ai.hermes.dd-listfinder.plist` (copy to `~/Library/LaunchAgents`, `launchctl bootstrap gui/$(id -u) ...`); log `~/Library/Logs/dd-listfinder.log`.


**Kept songs:** pressing Keep saves the song in your browser straight away. If you're not signed in, it also opens Google sign-in in a new tab (once per page, the music keeps playing) so your keepers can follow you between devices. The `/kept` page ("kept songs" link at the top of every page) lists your kept songs with artist, title, record, year and date kept, a `remove` button, a small player ("play these", shuffle, skip) and a "cast to tv / open in YouTube" link for the first 50. Signed out, it shows a sign-in link and whatever is kept in this browser. Keepers live in a private Vercel Blob store; the Google redirect URI is `https://dusty-digger.vercel.app/auth/callback`.
