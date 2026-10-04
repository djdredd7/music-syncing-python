#!/usr/bin/env python3
"""
Apple Music -> Spotify + YouTube Music playlist sync.

Each run: pull the current track list of every Apple Music playlist in
config.yaml, compare it to the last snapshot in state.json, and push only the
differences (adds/removes) to matching playlists on Spotify and YouTube Music.

Usage:
  python sync.py              # normal run
  python sync.py --dry-run    # show what would change, touch nothing
  python sync.py --force      # bypass the mass-removal safety check
"""
import difflib
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
STATE_PATH = ROOT / "state.json"
CONFIG_PATH = ROOT / "config.yaml"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) playlist-sync/1.0"}
MATCH_THRESHOLD = 0.62

DRY_RUN = "--dry-run" in sys.argv
FORCE = "--force" in sys.argv


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# --------------------------------------------------------------------------
# Text normalization + fuzzy matching
# --------------------------------------------------------------------------
def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\s*[\(\[](feat|ft|with)\.?[^\)\]]*[\)\]]", "", s)
    s = re.sub(r"\s*-\s*(\d{4}\s*)?(remaster(ed)?|single version|radio edit|album version).*$", "", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_title(t):
    return re.sub(r"\s*[\(\[].*?[\)\]]", "", t or "").strip() or t


def primary_artist(a):
    return re.split(r"\s*(?:,|&| x | feat\.?| ft\.?| and )\s*", a or "", flags=re.I)[0].strip()


def track_key(t):
    return t.get("isrc") or f"{norm(t['artist'])}|{norm(t['title'])}"


def _sim(a, b):
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def match_score(src, title, artist, duration_ms=None):
    a1, a2 = norm(src["artist"]), norm(artist)
    artist_sim = max(_sim(src["artist"], artist),
                     1.0 if a1 and a2 and (a1 in a2 or a2 in a1 or norm(primary_artist(src["artist"])) in a2) else 0.0)
    score = 0.6 * _sim(src["title"], title) + 0.4 * artist_sim
    if duration_ms and src.get("duration_ms") and abs(duration_ms - src["duration_ms"]) > 10_000:
        score -= 0.15
    return score


# --------------------------------------------------------------------------
# Apple Music (source)
# --------------------------------------------------------------------------
def apple_playlist_id(url_or_id):
    m = re.search(r"(pl\.[A-Za-z0-9\-_]+)", url_or_id)
    if not m:
        raise ValueError(f"Can't find a playlist id (pl.xxxx) in: {url_or_id}")
    return m.group(1)


def apple_dev_token(acfg):
    if os.getenv("APPLE_DEVELOPER_TOKEN"):
        return os.environ["APPLE_DEVELOPER_TOKEN"]
    import jwt  # PyJWT
    team_id = os.getenv("APPLE_TEAM_ID") or acfg.get("team_id")
    key_id = os.getenv("APPLE_KEY_ID") or acfg.get("key_id")
    key = os.getenv("APPLE_PRIVATE_KEY") or (ROOT / acfg["key_file"]).read_text()
    now = int(time.time())
    return jwt.encode({"iss": team_id, "iat": now, "exp": now + 12 * 3600},
                      key, algorithm="ES256", headers={"kid": key_id})


def fetch_apple_api(pid, storefront, token):
    """Official Apple Music catalog API. Works for any playlist shared by link."""
    base = "https://api.music.apple.com"
    h = {"Authorization": f"Bearer {token}"}
    r = requests.get(f"{base}/v1/catalog/{storefront}/playlists/{pid}", headers=h, timeout=30)
    r.raise_for_status()
    name = r.json()["data"][0]["attributes"]["name"]
    tracks, url = [], f"{base}/v1/catalog/{storefront}/playlists/{pid}/tracks?limit=100"
    while url:
        r = requests.get(url, headers=h, timeout=30)
        r.raise_for_status()
        j = r.json()
        for d in j.get("data", []):
            a = d.get("attributes", {})
            tracks.append({"title": a.get("name", ""), "artist": a.get("artistName", ""),
                           "album": a.get("albumName"), "isrc": a.get("isrc"),
                           "duration_ms": a.get("durationInMillis")})
        url = base + j["next"] if j.get("next") else None
    return name, tracks


def fetch_apple_scrape(url):
    """No developer account needed: reads the public playlist web page.
    Best-effort -- Apple can change their page layout at any time."""
    html = requests.get(url, headers=UA, timeout=30).text
    m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
    name = (m.group(1).split(" by ")[0] if m else "Apple Music Playlist").replace("&amp;", "&")
    tracks, seen = [], set()

    m = re.search(r'<script[^>]*id="serialized-server-data"[^>]*>(.*?)</script>', html, re.S)
    if m:
        def walk(o):
            if isinstance(o, dict):
                if isinstance(o.get("title"), str) and isinstance(o.get("artistName"), str) and "duration" in o:
                    k = (o["title"], o["artistName"])
                    if k not in seen:
                        seen.add(k)
                        dur = o.get("duration")
                        tracks.append({"title": o["title"], "artist": o["artistName"], "isrc": None,
                                       "duration_ms": dur if isinstance(dur, int) else None})
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        try:
            walk(json.loads(m.group(1)))
        except json.JSONDecodeError:
            pass

    if not tracks:  # fallback: schema.org JSON-LD
        for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
            try:
                data = json.loads(block)
            except json.JSONDecodeError:
                continue
            for t in data.get("track", []) if isinstance(data, dict) else []:
                artist = t.get("byArtist")
                if isinstance(artist, list):
                    artist = ", ".join(x.get("name", "") for x in artist)
                elif isinstance(artist, dict):
                    artist = artist.get("name", "")
                tracks.append({"title": t.get("name", ""), "artist": artist or "", "isrc": None,
                               "duration_ms": None})
    return name, tracks


# --------------------------------------------------------------------------
# Spotify (target) -- uses the post-Feb-2026 Dev Mode endpoints
# --------------------------------------------------------------------------
class Spotify:
    API = "https://api.spotify.com/v1"
    name = "spotify"

    def __init__(self):
        cid, secret = os.environ["SPOTIFY_CLIENT_ID"], os.environ["SPOTIFY_CLIENT_SECRET"]
        refresh = os.environ["SPOTIFY_REFRESH_TOKEN"]
        r = requests.post("https://accounts.spotify.com/api/token",
                          data={"grant_type": "refresh_token", "refresh_token": refresh},
                          auth=(cid, secret), timeout=30)
        r.raise_for_status()
        j = r.json()
        self.token = j["access_token"]
        if j.get("refresh_token") and j["refresh_token"] != refresh:
            (ROOT / ".spotify_refresh_token.new").write_text(j["refresh_token"])
            log("NOTE: Spotify issued a new refresh token -> saved to .spotify_refresh_token.new")

    def _req(self, method, path, **kw):
        for _ in range(6):
            r = requests.request(method, self.API + path, timeout=30,
                                 headers={"Authorization": f"Bearer {self.token}"}, **kw)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "5")) + 1)
                continue
            r.raise_for_status()
            return r.json() if r.content else {}
        raise RuntimeError(f"Spotify rate-limited too many times on {path}")

    def create_playlist(self, name, desc):
        return self._req("POST", "/me/playlists",
                         json={"name": name, "description": desc, "public": False})["id"]

    def find(self, t):
        if t.get("isrc"):
            items = self._req("GET", "/search", params={"q": f"isrc:{t['isrc']}", "type": "track",
                                                        "limit": 1})["tracks"]["items"]
            if items:
                return items[0]["uri"]
        for q in (f"track:{clean_title(t['title'])} artist:{primary_artist(t['artist'])}",
                  f"{clean_title(t['title'])} {primary_artist(t['artist'])}"):
            items = self._req("GET", "/search", params={"q": q, "type": "track", "limit": 10})["tracks"]["items"]
            scored = [(match_score(t, i["name"], ", ".join(a["name"] for a in i["artists"]),
                                   i.get("duration_ms")), i["uri"]) for i in items]
            if scored:
                best = max(scored)
                if best[0] >= MATCH_THRESHOLD:
                    return best[1]
        return None

    def add(self, pid, ids):
        for i in range(0, len(ids), 100):
            self._req("POST", f"/playlists/{pid}/items", json={"uris": ids[i:i + 100]})

    def remove(self, pid, ids):
        for i in range(0, len(ids), 100):
            self._req("DELETE", f"/playlists/{pid}/items",
                      json={"items": [{"uri": u} for u in ids[i:i + 100]]})


# --------------------------------------------------------------------------
# YouTube Music (target) -- via the unofficial ytmusicapi library
# --------------------------------------------------------------------------
class YTMusicTarget:
    name = "youtube_music"

    def __init__(self, auth_file):
        from ytmusicapi import YTMusic
        self.yt = YTMusic(str(ROOT / auth_file))

    def create_playlist(self, name, desc):
        res = self.yt.create_playlist(name, desc, privacy_status="PRIVATE")
        if not isinstance(res, str):
            raise RuntimeError(f"YouTube Music create_playlist failed: {res}")
        return res

    def find(self, t):
        results = self.yt.search(f"{clean_title(t['title'])} {primary_artist(t['artist'])}",
                                 filter="songs", limit=5)
        scored = []
        for r in results:
            if not r.get("videoId"):
                continue
            artists = ", ".join(a.get("name", "") for a in r.get("artists") or [])
            dur = r.get("duration_seconds")
            scored.append((match_score(t, r.get("title", ""), artists, dur * 1000 if dur else None),
                           r["videoId"]))
        if scored:
            best = max(scored)
            if best[0] >= MATCH_THRESHOLD:
                return best[1]
        return None

    def add(self, pid, ids):
        for i in range(0, len(ids), 50):
            self.yt.add_playlist_items(pid, ids[i:i + 50], duplicates=True)

    def remove(self, pid, ids):
        wanted = set(ids)
        pl = self.yt.get_playlist(pid, limit=None)
        items = [t for t in pl.get("tracks", []) if t.get("videoId") in wanted]
        if items:
            self.yt.remove_playlist_items(pid, items)


# --------------------------------------------------------------------------
# Main sync loop
# --------------------------------------------------------------------------
def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"playlists": {}, "matches": {}}


def save_state(state):
    if not DRY_RUN:
        STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def main():
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    acfg = cfg.get("apple", {})
    mode = acfg.get("mode", "scrape")
    state = load_state()

    targets = []
    if cfg.get("targets", {}).get("spotify"):
        targets.append(Spotify())
    if cfg.get("targets", {}).get("youtube_music"):
        targets.append(YTMusicTarget(cfg.get("youtube_music_auth", "browser.json")))
    if not targets:
        sys.exit("No targets enabled in config.yaml")

    token = apple_dev_token(acfg) if mode == "api" else None
    errors = 0

    for entry in cfg["playlists"]:
        entry = entry if isinstance(entry, dict) else {"apple": entry}
        url = entry["apple"]
        try:
            pid = apple_playlist_id(url)
            if mode == "api":
                apple_name, tracks = fetch_apple_api(pid, acfg.get("storefront", "us"), token)
            else:
                apple_name, tracks = fetch_apple_scrape(url)
        except Exception as e:
            log(f"ERROR reading Apple playlist {url}: {e}")
            errors += 1
            continue

        name = entry.get("name") or apple_name
        ps = state["playlists"].setdefault(pid, {"tracks": [], "targets": {}, "missing": {}})
        if not tracks:
            log(f"[{name}] Apple returned 0 tracks -- skipping (won't wipe targets on a bad read)")
            continue

        by_key = {}
        for t in tracks:
            by_key.setdefault(track_key(t), t)
        new_keys = list(by_key)
        old = set(ps["tracks"])
        added = [k for k in new_keys if k not in old]
        removed = [k for k in ps["tracks"] if k not in by_key]
        fresh_targets = [tg for tg in targets if tg.name not in ps["targets"]]

        if not added and not removed and not fresh_targets:
            log(f"[{name}] no changes ({len(new_keys)} tracks)")
            continue
        if old and len(removed) > max(5, len(old) // 2) and not FORCE:
            log(f"[{name}] {len(removed)} of {len(old)} tracks would be removed -- looks like a bad "
                f"read, skipping. Re-run with --force if that's really what you did.")
            continue

        log(f"[{name}] +{len(added)} / -{len(removed)}")
        ok = True
        for tg in targets:
            try:
                is_new = tg.name not in ps["targets"]
                to_add = new_keys if is_new else added
                to_remove = [] if is_new else removed

                ids, missing = [], []
                for k in to_add:
                    m = state["matches"].setdefault(k, {})
                    if tg.name not in m:
                        found = tg.find(by_key[k])
                        if found:
                            m[tg.name] = found
                    if m.get(tg.name):
                        ids.append(m[tg.name])
                    else:
                        t = by_key[k]
                        missing.append(f"{t['artist']} - {t['title']}")
                ids = list(dict.fromkeys(ids))
                rm_ids = [state["matches"].get(k, {}).get(tg.name) for k in to_remove]
                rm_ids = [i for i in rm_ids if i]

                if DRY_RUN:
                    log(f"  {tg.name}: would {'create playlist, ' if is_new else ''}"
                        f"add {len(ids)}, remove {len(rm_ids)}, unmatched {len(missing)}")
                else:
                    if is_new:
                        ps["targets"][tg.name] = tg.create_playlist(
                            name, "Synced from Apple Music by playlist-sync")
                    if ids:
                        tg.add(ps["targets"][tg.name], ids)
                    if rm_ids:
                        tg.remove(ps["targets"][tg.name], rm_ids)
                    log(f"  {tg.name}: added {len(ids)}, removed {len(rm_ids)}")

                prev_missing = set(ps["missing"].get(tg.name, []))
                gone = {f"{by_key[k]['artist']} - {by_key[k]['title']}" for k in removed if k in by_key}
                ps["missing"][tg.name] = sorted((prev_missing - gone) | set(missing))
                for miss in missing:
                    log(f"  {tg.name}: no confident match for '{miss}'")
            except Exception as e:
                ok = False
                errors += 1
                log(f"  ERROR syncing to {tg.name}: {e}")

        if ok:  # only advance the snapshot when every target succeeded
            ps["tracks"] = new_keys
        ps["name"] = name
        save_state(state)

    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
