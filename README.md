# playlist-sync

Watches your Apple Music playlists and mirrors every change (songs added or
removed) to matching playlists on **Spotify** and **YouTube Music**.

## How it works
1. Reads each Apple Music playlist listed in `config.yaml`.
2. Compares it to the snapshot saved last run in `state.json`.
3. Finds each new song on Spotify (by ISRC code when available, otherwise
   fuzzy title/artist/duration matching) and on YouTube Music.
4. Adds new songs / removes deleted songs on both services. The first run
   creates private playlists with the same names.

Songs it can't confidently match are logged and listed under `missing` in
`state.json` so you can add them by hand.

Safety: if a read returns 0 songs, or more than half the playlist would be
deleted at once, it skips that playlist instead of wiping your copies
(override with `--force`). Use `--dry-run` to preview.

## Requirements
- Python 3.10+
- **Spotify Premium** on the account that owns the developer app (Spotify's
  rule since Feb 2026 for personal/dev-mode apps).
- Apple playlists must be **shared** (Share > Copy Link). Edits you make to a
  shared playlist show up in the shared version.
- Optional: Apple Developer account ($99/yr) for `mode: api`.

## Setup
```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml   # then add your playlist links
```

**Spotify**
1. Go to https://developer.spotify.com/dashboard > Create app.
2. Redirect URI: `http://127.0.0.1:8888/callback`, API: Web API.
3. Run:
   `SPOTIFY_CLIENT_ID=... SPOTIFY_CLIENT_SECRET=... python setup_spotify.py`
4. Save the printed refresh token.

**YouTube Music**
Run `ytmusicapi browser` and follow its prompts (you paste request headers
copied from music.youtube.com in your browser's dev tools). It creates
`browser.json`. This login lasts until you sign out of that browser session.

**Apple (only for `mode: api`)**
In your Apple Developer account: Certificates, IDs & Profiles > Keys > create a
key with **Media Services (MusicKit)** enabled. Download the `.p8`, note the
Key ID and your Team ID, and put them in `config.yaml`.

## Run it
```bash
export SPOTIFY_CLIENT_ID=... SPOTIFY_CLIENT_SECRET=... SPOTIFY_REFRESH_TOKEN=...
python sync.py --dry-run   # preview
python sync.py
```

## Put it on autopilot

**Option A: GitHub Actions (free, runs even when your PC is off)**
1. Push this folder to a **private** GitHub repo (with `config.yaml`, without
   `browser.json` or the `.p8` key; `.gitignore` handles that).
2. Repo > Settings > Secrets and variables > Actions, add:
   `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REFRESH_TOKEN`,
   `YTMUSIC_BROWSER_JSON` (paste the whole contents of browser.json), and for
   api mode `APPLE_TEAM_ID`, `APPLE_KEY_ID`, `APPLE_PRIVATE_KEY` (contents of the .p8).
3. Actions tab > Playlist sync > Run workflow to test. After that it runs every
   6 hours; change the cron line in `.github/workflows/sync.yml` to adjust.

**Option B: your own computer**
- Windows: Task Scheduler > Create Basic Task > run `python C:\path\sync.py`.
- Mac/Linux: `crontab -e` and add
  `0 */6 * * * cd /path/to/playlist-sync && /usr/bin/python3 sync.py >> sync.log 2>&1`

## Notes
- Song order isn't mirrored; new songs are appended to the end.
- YouTube Music has no official API; this uses the community `ytmusicapi`
  library, which occasionally needs a `pip install -U ytmusicapi` after
  YouTube changes things.
- If a match is wrong, edit that entry under `matches` in `state.json`
  (or delete it to force a re-search next time the song is added).
