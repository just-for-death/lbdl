"""
app/library.py — Library scanning, metadata tagging, and cover art.

Provides:
- scan_library(): walk OUTPUT_DIR and read tags from audio files
- autotag_track(): AcoustID fingerprint → MusicBrainz/iTunes metadata
- apply_metadata_and_reorganize(): write tags + move file into Artist/Title layout
- fetch_candidates() / apply_candidate(): manual tag selection helpers
- Cover art helpers from MusicBrainz CAA, iTunes, JioSaavn, Gaana
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlencode

import requests
import mediafile
from pathvalidate import sanitize_filename
try:
    from rapidfuzz import fuzz as _fuzz
    _RAPIDFUZZ_OK = True
except ImportError:
    _RAPIDFUZZ_OK = False

logger = logging.getLogger("lbdl.library")

# ── Fuzzy match gate ──────────────────────────────────────────────────────────

_OLLAMA_BASE = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
_OLLAMA_MODEL = os.getenv("OLLAMA_VALIDATION_MODEL", "llama3.2:1b")

# Thresholds for fuzzy gating
_FUZZY_ACCEPT  = 65   # score >= this → accept without AI check
_FUZZY_REJECT  = 35   # score <  this → reject outright
# between REJECT and ACCEPT → ask Ollama


def _fuzzy_score(query_artist: str, query_title: str, result: dict) -> int:
    """
    Return 0-100 similarity between the search query and a metadata candidate.
    Uses token_sort_ratio so word-order differences don't cause false rejects.
    Falls back to basic SequenceMatcher if rapidfuzz is not installed.
    """
    r_title  = (result.get("title",  "") or "").lower().strip()
    r_artist = (result.get("artist", "") or "").lower().strip()
    q_title  = (query_title  or "").lower().strip()
    q_artist = (query_artist or "").lower().strip()

    if not q_title:
        return 50  # nothing to compare — neutral

    if _RAPIDFUZZ_OK:
        t_score = _fuzz.token_sort_ratio(q_title, r_title)
        a_score = _fuzz.token_sort_ratio(q_artist, r_artist) if q_artist else 100
    else:
        import difflib
        t_score = int(difflib.SequenceMatcher(None, q_title,  r_title ).ratio() * 100)
        a_score = int(difflib.SequenceMatcher(None, q_artist, r_artist).ratio() * 100) if q_artist else 100

    # Title is weighted 70 %, artist 30 %
    return int(t_score * 0.70 + a_score * 0.30)


def _ollama_validate(query_artist: str, query_title: str, candidate: dict) -> bool:
    """
    Ask the local Ollama model whether a metadata candidate matches the query.
    Returns True (accept) or False (reject).  Falls back to True on any error
    so a flaky LLM never silently kills tagging.
    """
    prompt = (
        f"Music metadata validator. Answer only YES or NO.\n"
        f"Query  → artist: \"{query_artist}\"  title: \"{query_title}\"\n"
        f"Result → artist: \"{candidate.get('artist','')}\"  "
        f"title: \"{candidate.get('title','')}\"  "
        f"album: \"{candidate.get('album','')}\"\n\n"
        "Does the result refer to the SAME song as the query? "
        "Consider alternate romanizations (e.g. Tum Hi Ho / Tum Hi Hoo) as the same. "
        "Reject if it is clearly a different song or a different language version. "
        "Reply with only: YES or NO"
    )
    try:
        r = requests.post(
            f"{_OLLAMA_BASE}/api/generate",
            json={"model": _OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=12,
        )
        if r.ok:
            answer = r.json().get("response", "").strip().upper()
            return answer.startswith("Y")
    except Exception as e:
        logger.debug("Ollama validate failed (falling back to accept): %s", e)
    return True   # fail-open: don't silently drop metadata


def _gate_candidate(
    query_artist: str,
    query_title: str,
    candidate: dict,
    label: str = "",
) -> tuple[bool, str]:
    """
    Run fuzzy gate + optional Ollama AI check on a single candidate.
    Returns (accepted: bool, reason: str) for logging.
    """
    score = _fuzzy_score(query_artist, query_title, candidate)
    if score >= _FUZZY_ACCEPT:
        return True, f"fuzzy={score} ✓ (auto-accept)"
    if score < _FUZZY_REJECT:
        return False, f"fuzzy={score} ✗ (auto-reject)"
    # Uncertain zone — call Ollama
    prefix = f"[{label}] " if label else ""
    logger.debug("%sfuzzy=%d → calling Ollama for validation", prefix, score)
    ok = _ollama_validate(query_artist, query_title, candidate)
    return ok, f"fuzzy={score} → AI={'✓ YES' if ok else '✗ NO'}"

OUTPUT_DIR     = Path(os.getenv("LBDL_DATA_DIR", "/app/music"))
AUDIO_EXTENSIONS = {".opus", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".wav"}

_MB_BASE  = "https://musicbrainz.org/ws/2"
_CAA_BASE = "https://coverartarchive.org/release"
_USER_AGENT = "lbdl/2.0 ( https://github.com/lbdl )"

# ── Generic HTTP helper ────────────────────────────────────────────────────────

def _get(url: str, timeout: int = 15, label: str = "") -> Optional[bytes]:
    """Fetch a URL and return raw bytes, or None on failure."""
    try:
        r = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=timeout)
        if r.ok and r.content:
            return r.content
    except Exception as e:
        logger.debug("[%s] GET failed %s: %s", label or "http", url[:80], e)
    return None


# ── File ID ───────────────────────────────────────────────────────────────────

def _file_id(path: Path) -> str:
    """Stable per-file ID based on path."""
    return hashlib.md5(str(path).encode()).hexdigest()[:16]


# ── Tag reading ────────────────────────────────────────────────────────────────

def read_track_meta(path: Path) -> Optional[dict]:
    """Read tags from an audio file. Returns a dict or None."""
    try:
        mf = mediafile.MediaFile(path)
        stat = path.stat()
        return {
            "id":            _file_id(path),
            "path":          str(path),
            "title":         mf.title or path.stem,
            "artist":        mf.artist or "",
            "albumartist":   mf.albumartist or "",
            "album":         mf.album or "",
            "year":          str(mf.year) if mf.year else "",
            "track":         str(mf.track) if mf.track else "",
            "duration":      int(mf.length or 0),
            "format":        path.suffix.lstrip(".").lower(),
            "folder":        str(path.parent),
            "lyrics_status": lyrics_status(path),
            # Filesystem timestamps for recency filtering and dedup
            "mtime":         stat.st_mtime,
            "ctime":         stat.st_ctime,
        }
    except Exception as e:
        logger.debug("read_track_meta failed for %s: %s", path, e)
        return None


def get_cover_bytes(path: Path) -> Optional[bytes]:
    """Extract the embedded cover image from an audio file."""
    try:
        mf = mediafile.MediaFile(path)
        img = mf.images
        if img:
            return img[0].data if hasattr(img[0], "data") else bytes(img[0])
    except Exception:
        pass
    # Fallback: look for cover.jpg next to the file
    for name in ("cover.jpg", "cover.png", "folder.jpg"):
        sib = path.parent / name
        if sib.exists():
            return sib.read_bytes()
    return None


# ── Tag writing ────────────────────────────────────────────────────────────────

def _make_cover_image(data: bytes) -> "mediafile.Image":
    """Create a mediafile Image object, handling different mediafile versions."""
    # mediafile.ImageKind may not exist in all versions — use int 3 (front cover) as fallback
    try:
        kind = mediafile.ImageKind.other
    except AttributeError:
        try:
            kind = mediafile.ImageKind(3)
        except Exception:
            kind = 3  # raw int: 3 = front cover in ID3 spec
    try:
        return mediafile.Image(data=data, desc="Cover", kind=kind)
    except TypeError:
        # Some versions don't accept kind as positional — try without desc
        return mediafile.Image(data=data)


def _write_metadata(path: Path, meta: dict, cover: Optional[bytes] = None, lyrics: Optional[str] = None) -> None:
    """Write tags (and optionally cover/lyrics) to an audio file in-place."""
    mf = mediafile.MediaFile(path)
    if meta.get("title"):
        mf.title = meta["title"]
    if meta.get("artist"):
        mf.artist = meta["artist"]
    if meta.get("albumartist") or meta.get("album_artist"):
        mf.albumartist = meta.get("albumartist") or meta.get("album_artist")
    if meta.get("album"):
        mf.album = meta["album"]
    if meta.get("year"):
        try:
            mf.year = int(str(meta["year"])[:4])
        except (ValueError, TypeError):
            pass
    if meta.get("track"):
        try:
            mf.track = int(str(meta["track"]).split("/")[0])
        except (ValueError, TypeError):
            pass
    if cover:
        try:
            mf.images = [_make_cover_image(cover)]
        except Exception as e:
            logger.warning("Cover embed failed for %s: %s", path.name, e)
    if lyrics:
        mf.lyrics = lyrics
    mf.save()
    logger.debug("_write_metadata OK: %s", path.name)


# ── File reorganization ────────────────────────────────────────────────────────

def _safe(name: str) -> str:
    return sanitize_filename(name, replacement_text="_").strip() or "Unknown"


def apply_metadata_and_reorganize(
    src: Path,
    meta: dict,
    cover: Optional[bytes],
    output_dir: Path,
    lyrics: Optional[str] = None,
) -> Path:
    """Write tags, move file to Artist/Title layout, return new path."""
    # Write tags first — raise on failure so callers know tags weren't saved
    _write_metadata(src, meta, cover, lyrics=lyrics)

    artist = _safe(meta.get("artist") or meta.get("album_artist") or "Unknown Artist")
    title  = _safe(meta.get("title") or src.stem)
    dest_dir = output_dir / artist
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{title}{src.suffix}"

    if src != dest:
        try:
            src.rename(dest)
        except OSError:
            import shutil
            shutil.copy2(src, dest)
            try:
                src.unlink()
            except OSError:
                pass

    # Save cover.jpg alongside
    if cover:
        try:
            (dest.parent / "cover.jpg").write_bytes(cover)
        except OSError:
            pass

    return dest


# ── Library scan ───────────────────────────────────────────────────────────────

def scan_library(
    output_dir: Path,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> list[dict]:
    """Walk output_dir, read tags from every audio file, return list of dicts."""
    audio_files = [
        p for p in output_dir.rglob("*")
        if p.suffix.lower() in AUDIO_EXTENSIONS
        and not p.name.startswith(".")
    ]
    total  = len(audio_files)
    tracks = []

    for i, path in enumerate(sorted(audio_files)):
        if progress_cb:
            progress_cb(i + 1, total)
        meta = read_track_meta(path)
        if meta:
            tracks.append(meta)

    logger.info("scan_library: found %d tracks in %s", len(tracks), output_dir)
    return tracks


# ── MusicBrainz helpers ────────────────────────────────────────────────────────

def _mb_request(endpoint: str, params: dict) -> Optional[dict]:
    """Make a MusicBrainz API request."""
    url = f"{_MB_BASE}/{endpoint}?{urlencode(params)}"
    time.sleep(1.1)  # MusicBrainz rate limit: 1 req/s
    try:
        r = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=15)
        if r.ok:
            return r.json()
    except Exception as e:
        logger.debug("MB request failed %s: %s", endpoint, e)
    return None


def _clean_search_title(title: str, artist: str = "") -> str:
    """Strip YouTube/yt-dlp noise from a title before text-searching."""
    original = title
    # "Song Name | Artist | Album" — take only the first segment
    if " | " in title:
        title = title.split(" | ")[0].strip()
    # Strip trailing artist name repetition: "Sarfarosh Jagjit Singh" → "Sarfarosh"
    if artist:
        low_t, low_a = title.lower(), artist.lower()
        if low_t.endswith(low_a):
            title = title[:-(len(artist))].strip(" -–|·,")
        elif low_t.startswith(low_a):
            title = title[len(artist):].strip(" -–|·,")
    # Strip feat/official/lyrics/video/audio suffixes
    title = re.sub(
        r'\s*[\(\[](feat\.|ft\.|official|lyrics?|video|audio|hd|4k)[^\)\]]*[\)\]]',
        '', title, flags=re.IGNORECASE,
    ).strip()
    return title or original  # fall back to original if cleaning empties it


def _mb_text_search(artist: str, title: str, limit: int = 6) -> list:
    """Search MusicBrainz for recordings matching artist+title, with keyword fallback."""
    if not title and not artist:
        return []
    # Primary: exact-phrase Lucene query
    query_parts = []
    if title:
        query_parts.append(f'recording:"{title}"')
    if artist:
        query_parts.append(f'artist:"{artist}"')
    query = " AND ".join(query_parts)
    data = _mb_request("recording", {"query": query, "limit": limit, "fmt": "json"})
    results = (data or {}).get("recordings", [])
    if results:
        return results
    # Fallback: keyword (unquoted) search — picks up partial / romanized matches
    kw = f"{title} {artist}".strip()
    data2 = _mb_request("recording", {"query": kw, "limit": limit, "fmt": "json"})
    return (data2 or {}).get("recordings", [])


def _parse_mb_rec(rec: dict, source: str = "musicbrainz") -> Optional[dict]:
    """Parse a MusicBrainz recording dict into our meta format."""
    try:
        title  = rec.get("title", "")
        # artist-credit is a list that can contain dicts OR joining-phrase strings (e.g. " & ")
        credits = [c for c in rec.get("artist-credit", []) if isinstance(c, dict)]
        artist = credits[0].get("name", "") if credits else ""
        releases = rec.get("releases", [])
        if not releases:
            return None
        release   = releases[0]
        album     = release.get("title", "")
        mb_rel_id = release.get("id", "")
        year_str  = (release.get("date") or "")[:4]
        return {
            "title":       title,
            "artist":      artist,
            "album":       album,
            "year":        year_str,
            "albumartist": artist,
            "mb_rel_id":   mb_rel_id,
            "source":      source,
        }
    except Exception:
        return None


def _cover_url_from_caa(mb_rel_id: str, size: int = 250) -> str:
    return f"{_CAA_BASE}/{mb_rel_id}/front-{size}"


def _cover_from_caa(mb_rel_id: str) -> Optional[bytes]:
    """Fetch cover from Cover Art Archive (500 px — suitable for embedding)."""
    url = _cover_url_from_caa(mb_rel_id, size=500)
    return _get(url, timeout=20, label="caa")


def _fetch_cover_from_url(url: str) -> Optional[bytes]:
    return _get(url, timeout=20, label="cover")


# ── iTunes helpers ─────────────────────────────────────────────────────────────

def _itunes_candidates(artist: str, title: str, limit: int = 6) -> list[dict]:
    """Search iTunes Search API for metadata candidates."""
    q = f"{artist} {title}".strip()
    if not q:
        return []
    try:
        r = requests.get(
            "https://itunes.apple.com/search",
            params={"term": q, "entity": "song", "limit": limit},
            timeout=10,
        )
        if not r.ok:
            return []
        results = r.json().get("results", [])
        out = []
        for item in results:
            cover_url = (item.get("artworkUrl100") or "").replace("100x100bb", "250x250bb")
            out.append({
                "title":     item.get("trackName", ""),
                "artist":    item.get("artistName", ""),
                "album":     item.get("collectionName", ""),
                "year":      str(item.get("releaseDate", ""))[:4],
                "cover_url": cover_url,
                "source":    "itunes",
            })
        return out
    except Exception as e:
        logger.debug("iTunes search failed: %s", e)
        return []


# ── JioSaavn helpers ───────────────────────────────────────────────────────────

def _jiosaavn_candidates(artist: str, title: str, limit: int = 6) -> list[dict]:
    q = f"{artist} {title}".strip()
    if not q:
        return []
    try:
        r = requests.get(
            "https://www.jiosaavn.com/api.php",
            params={"__call": "search.getResults", "q": q, "p": 1, "n": limit,
                    "_format": "json", "_marker": 0, "ctx": "web6dot0"},
            timeout=10,
        )
        if not r.ok:
            return []
        results = r.json().get("results", [])
        out = []
        for item in results:
            cover = (item.get("image") or "").replace("150x150", "500x500")
            out.append({
                "title":     item.get("song", ""),
                "artist":    item.get("primary_artists", ""),
                "album":     item.get("album", ""),
                "year":      str(item.get("year", "")),
                "cover_url": cover,
                "source":    "jiosaavn",
            })
        return out
    except Exception as e:
        logger.debug("JioSaavn search failed: %s", e)
        return []


# ── Gaana helpers ──────────────────────────────────────────────────────────────

def _gaana_candidates(artist: str, title: str, limit: int = 4) -> list[dict]:
    q = f"{artist} {title}".strip()
    if not q:
        return []
    try:
        r = requests.get(
            "https://gaana.com/api/search",
            params={"type": "tracks", "q": q, "limit": limit},
            headers={"User-Agent": _USER_AGENT},
            timeout=10,
        )
        if not r.ok:
            return []
        data = r.json()
        entities = data.get("tracks", {}).get("entities", [])
        out = []
        for item in entities:
            cover = item.get("artwork_url", "") or item.get("artwork", "")
            out.append({
                "title":     item.get("title", ""),
                "artist":    item.get("artist", [{}])[0].get("name", "") if isinstance(item.get("artist"), list) else item.get("artist", ""),
                "album":     item.get("albumtitle", ""),
                "year":      "",
                "cover_url": cover,
                "source":    "gaana",
            })
        return out
    except Exception as e:
        logger.debug("Gaana search failed: %s", e)
        return []


# ── Lyrics helpers — multi-source ─────────────────────────────────────────────
#
# Priority (synced LRC first, plain fallback):
#   1. LRCLIB              — English, Japanese, Korean, some Hindi (LRC + plain)
#   2. Musixmatch          — via syncedlyrics pkg; best Hindi/Bollywood/pop synced LRC
#   3. Megalobiz           — via syncedlyrics pkg; additional regional synced LRC
#   4. NetEase Cloud Music — Chinese, Japanese, Korean, Hindi (LRC excellent)
#   5. JioSaavn            — Hindi, Punjabi, Tamil, Telugu, Kannada, Marathi (plain)
#   6. Lyrics.ovh          — Multilingual plain fallback
#   7. Genius              — English, K-pop, Hindi plain fallback
#
# Caller: fetch_lyrics_for_track() — tries sources in order, returns first hit.
# Synced (LRC) lyrics are always preferred over plain text.


def _lrclib_fetch(
    artist: str, title: str, duration: int = 0, album: str = ""
) -> tuple[Optional[str], bool]:
    """LRCLIB — free synced+plain lyrics. Good for EN/JA/KO."""
    def _get_hit(params: dict) -> Optional[dict]:
        try:
            r = requests.get(
                "https://lrclib.net/api/get",
                params=params,
                headers={"User-Agent": _USER_AGENT},
                timeout=12,
            )
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            logger.debug("[lrclib] get failed: %s", e)
        return None

    def _search_hit(q: str) -> Optional[dict]:
        try:
            r = requests.get(
                "https://lrclib.net/api/search",
                params={"q": q},
                headers={"User-Agent": _USER_AGENT},
                timeout=12,
            )
            if r.status_code == 200:
                results = r.json()
                if results:
                    synced = [x for x in results if x.get("syncedLyrics")]
                    return synced[0] if synced else results[0]
        except Exception as e:
            logger.debug("[lrclib] search failed: %s", e)
        return None

    params: dict = {"artist_name": artist, "track_name": title}
    if duration:
        params["duration"] = duration
    if album:
        params["album_name"] = album
    hit = _get_hit(params) or (
        _get_hit({"artist_name": artist, "track_name": title}) if (duration or album) else None
    ) or _search_hit(f"{artist} {title}".strip())

    if not hit or hit.get("instrumental"):
        return None, False
    synced = (hit.get("syncedLyrics") or "").strip()
    plain  = (hit.get("plainLyrics")  or "").strip()
    if synced:
        return synced, True
    if plain:
        return plain, False
    return None, False


def _syncedlyrics_fetch(artist: str, title: str) -> tuple[Optional[str], bool]:
    """
    syncedlyrics package — wraps Musixmatch and Megalobiz for synced LRC.
    Musixmatch has the best Hindi/Bollywood, English pop, and K-pop synced coverage.
    Megalobiz covers additional regional content.
    We skip Lrclib/NetEase here because we query those natively ourselves.
    Returns (lrc_text, is_synced).  Falls back to plain if synced not found.
    Silently skipped if the package is not installed.
    """
    if not title:
        return None, False
    try:
        import syncedlyrics as _sl
        q = f"{artist} {title}".strip()
        # 1. Try synced-only from Musixmatch + Megalobiz
        lrc = _sl.search(q, synced_only=True, providers=["Musixmatch", "Megalobiz"])
        if lrc and len(lrc.strip()) > 30:
            logger.info("[syncedlyrics/Musixmatch] synced LRC found for %r / %r", artist, title)
            return lrc.strip(), True
        # 2. Plain fallback from Musixmatch (still better than nothing for Hindi etc.)
        plain = _sl.search(q, plain_only=True, providers=["Musixmatch"])
        if plain and len(plain.strip()) > 20:
            logger.info("[syncedlyrics/Musixmatch] plain lyrics found for %r / %r", artist, title)
            return plain.strip(), False
    except ImportError:
        logger.debug("[syncedlyrics] package not installed — skipping")
    except Exception as e:
        logger.debug("[syncedlyrics] error: %s", e)
    return None, False


def _netease_fetch(artist: str, title: str, duration: int = 0) -> tuple[Optional[str], bool]:
    """
    NetEase Cloud Music — excellent for Chinese, Japanese, Korean, Hindi.
    Returns synced LRC if available, otherwise (None, False).
    Note: NetEase may be region-blocked outside mainland China; failures are silent.
    """
    q = f"{artist} {title}".strip()
    if not q:
        return None, False
    try:
        # Search — short timeout since this may be blocked
        r = requests.get(
            "https://music.163.com/api/search/get",
            params={"s": q, "type": 1, "limit": 5},
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                "Referer": "https://music.163.com/",
            },
            timeout=8,
        )
        if not r.ok:
            return None, False
        data = r.json()
        # NetEase wraps errors in {"code": 200, "result": {...}} or {"code": 301, ...}
        if data.get("code", 200) not in (200, None):
            return None, False
        songs = data.get("result", {}).get("songs", [])
        if not songs:
            return None, False

        # Pick the best match (duration-aware if provided)
        song_id = songs[0]["id"]
        if duration and len(songs) > 1:
            for s in songs:
                s_dur = s.get("duration", 0) // 1000
                if abs(s_dur - duration) <= 5:
                    song_id = s["id"]
                    break

        # Fetch lyrics
        r2 = requests.get(
            "https://music.163.com/api/song/lyric",
            params={"id": song_id, "lv": 1, "kv": 1, "tv": -1},
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://music.163.com/",
            },
            timeout=8,
        )
        if not r2.ok:
            return None, False
        ldata = r2.json()
        if ldata.get("code", 200) not in (200, None):
            return None, False

        # Prefer karaoke (word-level) > standard LRC
        for key in ("klyric", "lrc"):
            lrc_text = (ldata.get(key) or {}).get("lyric", "")
            if lrc_text and re.search(r"\[\d{1,2}:\d{2}", lrc_text):
                # Strip metadata-only lines that have no lyric text
                clean_lines = []
                for line in lrc_text.splitlines():
                    # Keep lines that have a timestamp followed by text
                    stripped = re.sub(r"\[\d{1,2}:\d{2}[.:]\d{2,3}\]", "", line).strip()
                    if stripped or re.match(r"^\[\d{1,2}:\d{2}", line):
                        clean_lines.append(line)
                clean = "\n".join(clean_lines).strip()
                if clean and len(clean) > 30:
                    logger.info("[netease] synced LRC found for %r / %r", artist, title)
                    return clean, True

        if ldata.get("nolyric"):
            logger.debug("[netease] instrumental track")
        return None, False

    except requests.exceptions.Timeout:
        logger.debug("[netease] timed out (likely region-blocked)")
        return None, False
    except Exception as e:
        logger.debug("[netease] error: %s", e)
        return None, False


def _jiosaavn_lyrics_fetch(artist: str, title: str) -> tuple[Optional[str], bool]:
    """
    JioSaavn — covers Hindi, Punjabi, Tamil, Telugu, Kannada, Marathi, Bengali.
    Returns plain lyrics (no LRC timestamps).
    """
    q = f"{artist} {title}".strip()
    if not q:
        return None, False
    try:
        # Search for song
        r = requests.get(
            "https://www.jiosaavn.com/api.php",
            params={
                "__call": "search.getResults",
                "q": q, "p": 1, "n": 5,
                "_format": "json", "_marker": 0, "ctx": "web6dot0",
            },
            timeout=10,
        )
        if not r.ok:
            return None, False
        results = r.json().get("results", [])
        if not results:
            return None, False

        # Try each result for lyrics
        for item in results:
            song_id = item.get("id", "")
            if not song_id:
                continue
            # Fetch song details with lyrics
            r2 = requests.get(
                "https://www.jiosaavn.com/api.php",
                params={
                    "__call": "song.getLyrics",
                    "lyrics_id": song_id,
                    "_format": "json", "_marker": 0, "ctx": "web6dot0",
                },
                timeout=10,
            )
            if not r2.ok:
                continue
            lyrics_data = r2.json()
            lyrics = lyrics_data.get("lyrics", "").strip()
            if lyrics and len(lyrics) > 20:
                logger.info("[jiosaavn] lyrics found for %r / %r", artist, title)
                return lyrics, False
        return None, False
    except Exception as e:
        logger.debug("[jiosaavn-lyrics] error: %s", e)
        return None, False


def _lyrics_ovh_fetch(artist: str, title: str) -> tuple[Optional[str], bool]:
    """
    Lyrics.ovh — free multilingual plain-text lyrics API.
    Good fallback for many languages including Hindi romaji, Japanese, Korean.
    """
    if not artist or not title:
        return None, False
    try:
        import urllib.parse as _up
        url = f"https://api.lyrics.ovh/v1/{_up.quote(artist)}/{_up.quote(title)}"
        r = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=10)
        if r.status_code == 200:
            lyrics = r.json().get("lyrics", "").strip()
            if lyrics and len(lyrics) > 20:
                logger.info("[lyrics.ovh] found for %r / %r", artist, title)
                return lyrics, False
    except Exception as e:
        logger.debug("[lyrics.ovh] error: %s", e)
    return None, False


def _genius_fetch(artist: str, title: str) -> tuple[Optional[str], bool]:
    """
    Genius — plain lyrics scraped from the web page.
    Good for English, K-pop (Korean), Bollywood/Hindi.
    """
    if not title:
        return None, False
    try:
        q = f"{artist} {title}".strip()
        r = requests.get(
            "https://api.genius.com/search",
            params={"q": q},
            headers={
                "Authorization": "Bearer alXXDbPZtK1m2RrZ1ym7GYvnWg0GZrSqOuSnLDG2sIyRRpF8EsJon3gFhCorIGHi",
                "User-Agent": _USER_AGENT,
            },
            timeout=10,
        )
        if not r.ok:
            return None, False
        hits = r.json().get("response", {}).get("hits", [])
        if not hits:
            return None, False

        for hit in hits[:3]:
            if hit.get("type") != "song":
                continue
            result = hit.get("result", {})
            song_path = result.get("path", "")
            if not song_path:
                continue

            page = requests.get(
                f"https://genius.com{song_path}",
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"},
                timeout=12,
            )
            if not page.ok:
                continue

            html_text = page.text

            # Strategy 1: Extract all data-lyrics-container blocks by counting brace depth
            # rather than relying on a simple regex that breaks on nested divs.
            lyrics_blocks: list[str] = []
            search_str = 'data-lyrics-container="true"'
            pos = 0
            while True:
                idx = html_text.find(search_str, pos)
                if idx == -1:
                    break
                # Find the opening > of this tag
                tag_end = html_text.find('>', idx)
                if tag_end == -1:
                    break
                # Walk forward counting <div> / </div> depth to find matching close
                depth = 1
                cursor = tag_end + 1
                while cursor < len(html_text) and depth > 0:
                    open_pos  = html_text.find('<div',  cursor)
                    close_pos = html_text.find('</div>', cursor)
                    if close_pos == -1:
                        break
                    if open_pos != -1 and open_pos < close_pos:
                        depth += 1
                        cursor = open_pos + 4
                    else:
                        depth -= 1
                        cursor = close_pos + 6
                block = html_text[tag_end + 1 : cursor - 6]  # content between outer tags
                lyrics_blocks.append(block)
                pos = cursor

            if lyrics_blocks:
                raw = "\n".join(lyrics_blocks)
                raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
                raw = re.sub(r"<[^>]+>", "", raw)
                import html as _html
                lyrics = _html.unescape(raw).strip()
                if lyrics and len(lyrics) > 30:
                    logger.info("[genius] lyrics found for %r / %r", artist, title)
                    return lyrics, False

            # Strategy 2: older Genius page layout
            m = re.search(r'class=["\']lyrics["\'][^>]*>(.*?)</div>', html_text, re.DOTALL | re.IGNORECASE)
            if m:
                raw = re.sub(r"<br\s*/?>", "\n", m.group(1), flags=re.IGNORECASE)
                raw = re.sub(r"<[^>]+>", "", raw)
                import html as _html
                lyrics = _html.unescape(raw).strip()
                if lyrics and len(lyrics) > 30:
                    logger.info("[genius] lyrics (legacy) found for %r / %r", artist, title)
                    return lyrics, False

        return None, False
    except requests.exceptions.Timeout:
        logger.debug("[genius] timed out")
        return None, False
    except Exception as e:
        logger.debug("[genius] error: %s", e)
        return None, False


def fetch_lyrics_for_track(
    artist: str,
    title: str,
    duration: int = 0,
    album: str = "",
) -> tuple[Optional[str], bool]:
    """
    Fetch lyrics from multiple sources. Returns (lyrics_text, is_synced).

    Source priority:
      Synced LRC: LRCLIB → Musixmatch (syncedlyrics) → Megalobiz → NetEase
      Plain text: Musixmatch plain → JioSaavn → Lyrics.ovh → Genius → LRCLIB plain

    Indian languages (Hindi/Kannada/Tamil/Punjabi):  Musixmatch + JioSaavn + NetEase
    Japanese:  NetEase (excellent J-Pop coverage) + LRCLIB
    Korean:    NetEase + LRCLIB + Genius (K-pop)
    """
    if not title:
        return None, False

    # ── Pass 1: Synced LRC sources ───────────────────────────────────────────
    # LRCLIB — free, good EN/JA/KO coverage
    lrc, synced = _lrclib_fetch(artist, title, duration, album)
    if lrc and synced:
        return lrc, True

    # Musixmatch + Megalobiz via syncedlyrics package (best Hindi/pop synced coverage)
    sl_lrc, sl_synced = _syncedlyrics_fetch(artist, title)
    if sl_lrc and sl_synced:
        return sl_lrc, True

    # NetEase — great for CJK and Hindi film songs
    lrc_ne, synced_ne = _netease_fetch(artist, title, duration)
    if lrc_ne and synced_ne:
        return lrc_ne, True

    # ── Pass 2: Plain lyrics sources ─────────────────────────────────────────
    # Musixmatch plain (already fetched above as fallback)
    if sl_lrc and not sl_synced:
        return sl_lrc, False

    # JioSaavn — Indian languages
    plain_js, _ = _jiosaavn_lyrics_fetch(artist, title)
    if plain_js:
        return plain_js, False

    # Lyrics.ovh — multilingual
    plain_ovh, _ = _lyrics_ovh_fetch(artist, title)
    if plain_ovh:
        return plain_ovh, False

    # Genius — English/K-pop/Hindi
    plain_g, _ = _genius_fetch(artist, title)
    if plain_g:
        return plain_g, False

    # ── Pass 3: LRCLIB plain (last resort) ───────────────────────────────────
    if lrc and not synced:
        return lrc, False

    logger.debug("[lyrics] no result for %r / %r", artist, title)
    return None, False


def embed_lyrics(path: Path, lyrics_text: str, is_synced: bool) -> bool:
    """
    Write lyrics into the audio file tag (USLT/©lyr/LYRICS field).
    Also saves a sidecar .lrc file when synced lyrics are present.
    Returns True on success.
    """
    try:
        mf = mediafile.MediaFile(path)
        mf.lyrics = lyrics_text
        mf.save()
        logger.debug("[lyrics] embedded into %s", path.name)
    except Exception as e:
        logger.warning("[lyrics] embed failed for %s: %s", path.name, e)
        return False

    if is_synced:
        lrc_path = path.with_suffix(".lrc")
        try:
            lrc_path.write_text(lyrics_text, encoding="utf-8")
            logger.debug("[lyrics] .lrc sidecar saved: %s", lrc_path.name)
        except Exception as e:
            logger.warning("[lyrics] .lrc sidecar write failed: %s", e)

    return True


def get_lyrics_from_file(path: Path) -> Optional[str]:
    """Read embedded lyrics from an audio file, or load from .lrc sidecar."""
    # 1. Try embedded tag
    try:
        mf = mediafile.MediaFile(path)
        if mf.lyrics:
            return mf.lyrics
    except Exception:
        pass
    # 2. Sidecar .lrc
    lrc = path.with_suffix(".lrc")
    if lrc.exists():
        try:
            return lrc.read_text(encoding="utf-8")
        except Exception:
            pass
    return None


def lyrics_status(path: Path) -> str:
    """Return 'synced', 'plain', or 'none' based on what lyrics are stored."""
    text = get_lyrics_from_file(path)
    if not text:
        return "none"
    # LRC format lines start with [MM:SS.xx]
    if re.search(r"\[\d{1,2}:\d{2}[.:]\d{2}\]", text):
        return "synced"
    return "plain"


# ── Full autotag (iTunes → MusicBrainz → AcoustID) ────────────────────────────

def full_autotag_track(
    path: Path | str,
    meta: dict,
    acoustid_key: str = "",
    fetch_lyrics: bool = True,
) -> tuple[Optional[dict], Optional[bytes], list[str]]:
    """
    Full metadata resolution pipeline with detailed logs.
    Priority: iTunes (fast, no key) → MusicBrainz text → AcoustID fingerprint.
    Returns (new_meta_or_None, cover_bytes_or_None, log_lines).
    When fetch_lyrics=True, lyrics are embedded into the file and a .lrc sidecar
    is saved if synced lyrics are available.
    """
    logs: list[str] = []
    path = Path(path)

    artist      = meta.get("artist", "") or ""
    title       = meta.get("title",  "") or ""
    clean_title = _clean_search_title(title, artist)
    search_label = f"{artist} — {clean_title}" if artist else clean_title
    logs.append(f"Searching: {search_label}")

    def _try_fetch_lyrics(res_artist: str, res_title: str, res_album: str = "", duration: int = 0) -> None:
        """Attempt to fetch + embed lyrics; appends result line to logs."""
        if not fetch_lyrics:
            return
        logs.append("  ▸ Lyrics (LRCLIB · NetEase · JioSaavn · Genius)…")
        lyr, synced = fetch_lyrics_for_track(res_artist, res_title, duration, res_album)
        if lyr:
            ok = embed_lyrics(path, lyr, synced)
            kind = "synced LRC" if synced else "plain"
            logs.append(f"  {'✓' if ok else '✗'} Lyrics: {kind} {'embedded' if ok else 'embed failed'}")
        else:
            logs.append("  ✗ Lyrics: not found in any source")

    duration = int(meta.get("duration") or 0)

    # ── 1. iTunes ─────────────────────────────────────────────────────────────
    logs.append("  ▸ iTunes…")
    itunes = _itunes_candidates(artist, clean_title, limit=1)
    itunes_gate_artist = artist  # track which artist we actually searched with
    if not itunes and clean_title:
        itunes = _itunes_candidates("", clean_title, limit=1)
        itunes_gate_artist = ""   # title-only fallback — don't penalise artist mismatch
    if itunes:
        hit   = itunes[0]
        accepted, gate_reason = _gate_candidate(itunes_gate_artist, clean_title, hit, label="iTunes")
        if accepted:
            cover = None
            raw_url = hit.get("cover_url", "")
            if raw_url:
                big_url = raw_url.replace("250x250bb", "600x600bb")
                cover   = _fetch_cover_from_url(big_url)
            meta_out = {
                "title":       hit.get("title", title),
                "artist":      hit.get("artist", artist),
                "album":       hit.get("album", ""),
                "albumartist": hit.get("artist", artist),
                "year":        hit.get("year", ""),
                "source":      "itunes",
            }
            logs.append(
                f"  ✓ iTunes [{gate_reason}]: {meta_out['artist']} — {meta_out['title']}"
                + (f" ({meta_out['album']})" if meta_out['album'] else "")
                + (" [cover ✓]" if cover else " [no cover]")
            )
            _try_fetch_lyrics(meta_out["artist"], meta_out["title"], meta_out.get("album", ""), duration)
            return meta_out, cover, logs
        else:
            logs.append(f"  ✗ iTunes: rejected [{gate_reason}] — {hit.get('artist')} — {hit.get('title')}")
    else:
        logs.append("  ✗ iTunes: no match")

    # ── 2. MusicBrainz text search ────────────────────────────────────────────
    logs.append("  ▸ MusicBrainz…")
    mb_recs = _mb_text_search(artist, clean_title, limit=3)
    if mb_recs:
        parsed = _parse_mb_rec(mb_recs[0])
        if parsed:
            accepted, gate_reason = _gate_candidate(artist, clean_title, parsed, label="MusicBrainz")
            if accepted:
                cover = None
                if parsed.get("mb_rel_id"):
                    cover = _cover_from_caa(parsed["mb_rel_id"])
                logs.append(
                    f"  ✓ MusicBrainz [{gate_reason}]: {parsed.get('artist')} — {parsed.get('title')}"
                    + (f" ({parsed.get('album')})" if parsed.get("album") else "")
                    + (" [cover ✓]" if cover else " [no cover]")
                )
                _try_fetch_lyrics(parsed.get("artist", artist), parsed.get("title", title), parsed.get("album", ""), duration)
                return parsed, cover, logs
            else:
                logs.append(f"  ✗ MusicBrainz: rejected [{gate_reason}] — {parsed.get('artist')} — {parsed.get('title')}")
        else:
            logs.append("  ✗ MusicBrainz: no match")
    else:
        logs.append("  ✗ MusicBrainz: no match")

    # ── 3. AcoustID fingerprint ───────────────────────────────────────────────
    if acoustid_key:
        logs.append("  ▸ AcoustID fingerprint…")
        try:
            import acoustid
            results = acoustid.match(acoustid_key, str(path))
            best_score, best_rec_id, best_title, best_artist = 0.0, None, title, artist
            for score, recording_id, t, a in results:
                if score > best_score:
                    best_score, best_rec_id = score, recording_id
                    best_title, best_artist  = (t or title), (a or artist)

            if best_rec_id and best_score >= 0.5:
                data = _mb_request(f"recording/{best_rec_id}", {
                    "inc": "releases+artists", "fmt": "json"
                })
                ac_meta = _parse_mb_rec(data) if data else None
                if not ac_meta:
                    ac_meta = {"title": best_title, "artist": best_artist,
                               "album": "", "year": "", "albumartist": best_artist}
                accepted, gate_reason = _gate_candidate(artist, clean_title, ac_meta, label="AcoustID")
                if accepted:
                    cover = _cover_from_caa(ac_meta["mb_rel_id"]) if ac_meta.get("mb_rel_id") else None
                    logs.append(
                        f"  ✓ AcoustID ({best_score:.0%}) [{gate_reason}]: {ac_meta.get('artist')} — {ac_meta.get('title')}"
                        + (f" ({ac_meta.get('album')})" if ac_meta.get("album") else "")
                        + (" [cover ✓]" if cover else " [no cover]")
                    )
                    _try_fetch_lyrics(ac_meta.get("artist", artist), ac_meta.get("title", title), ac_meta.get("album", ""), duration)
                    return ac_meta, cover, logs
                else:
                    logs.append(f"  ✗ AcoustID: rejected [{gate_reason}] — {ac_meta.get('artist')} — {ac_meta.get('title')}")
            else:
                logs.append(f"  ✗ AcoustID: no confident match (best {best_score:.0%})")
        except ImportError:
            logs.append("  ✗ AcoustID: pyacoustid not installed")
        except Exception as e:
            logs.append(f"  ✗ AcoustID error: {e}")
    else:
        logs.append("  ▸ AcoustID: skipped (no API key)")

    logs.append("  ✗ No metadata found for this track")
    return None, None, logs


def autotag_track(
    path: Path | str,
    acoustid_key: str = "",
) -> tuple[Optional[dict], Optional[bytes]]:
    """Legacy wrapper — AcoustID only. Prefer full_autotag_track for new code."""
    path = Path(path)
    if not acoustid_key:
        return None, None
    try:
        import acoustid
        results = acoustid.match(acoustid_key, str(path))
        best = None
        best_score = 0.0
        for score, recording_id, title, artist in results:
            if score > best_score:
                best_score = score
                best = (recording_id, title, artist)

        if not best or best_score < 0.5:
            return None, None

        recording_id, title, artist = best
        data = _mb_request(f"recording/{recording_id}", {
            "inc": "releases+artists", "fmt": "json",
        })
        if not data:
            return None, None

        meta = _parse_mb_rec(data)
        if not meta:
            return None, None

        cover = _cover_from_caa(meta["mb_rel_id"]) if meta.get("mb_rel_id") else None
        return meta, cover

    except ImportError:
        return None, None
    except Exception as e:
        logger.debug("autotag error: %s", e)
        return None, None


# ── Candidate selection ────────────────────────────────────────────────────────

def fetch_candidates(
    path: Path,
    meta: dict,
    acoustid_key: str = "",
) -> list[dict]:
    """
    Return a ranked list of metadata candidates for manual selection.
    Combines AcoustID, MusicBrainz text search, and iTunes.
    """
    artist = meta.get("artist", "") or ""
    title  = meta.get("title", "") or ""

    # Clean the title (strip yt-dlp junk, artist-name repetition, etc.)
    clean_title = _clean_search_title(title, artist)

    candidates: list[dict] = []

    # 1. AcoustID — run the raw match so we can attach score + cover info
    if acoustid_key:
        try:
            import acoustid as _acoustid
            results = list(_acoustid.match(acoustid_key, str(path)))
            best_score = 0.0
            best_rec_id = None
            best_title = title
            best_artist = artist
            for score, recording_id, t, a in results:
                if score > best_score:
                    best_score = score
                    best_rec_id = recording_id
                    best_title  = t or title
                    best_artist = a or artist

            if best_rec_id and best_score >= 0.5:
                # Full MB lookup for album / release info
                data = _mb_request(f"recording/{best_rec_id}", {
                    "inc": "releases+artists", "fmt": "json"
                })
                ac_meta = _parse_mb_rec(data) if data else None
                if not ac_meta:
                    ac_meta = {"title": best_title, "artist": best_artist, "album": "", "year": "", "albumartist": best_artist}
                ac_meta["source"]     = "acoustid"
                ac_meta["score"]      = round(best_score, 4)   # Bug 6 fix: attach score
                ac_meta["confidence"] = "high"
                # Bug 7 fix: attach cover_url so the 🖼 Cover button works
                if ac_meta.get("mb_rel_id"):
                    ac_meta["cover_url"] = _cover_url_from_caa(ac_meta["mb_rel_id"], size=500)
                candidates.append(ac_meta)
        except ImportError:
            pass
        except Exception as e:
            logger.debug("AcoustID candidate error: %s", e)

    # 2. MusicBrainz text search — use cleaned title
    mb_recs = _mb_text_search(artist, clean_title, limit=5)
    for rec in mb_recs:
        parsed = _parse_mb_rec(rec)
        if parsed:
            parsed["confidence"] = "medium"
            if parsed.get("mb_rel_id"):
                parsed["cover_url"] = _cover_url_from_caa(parsed["mb_rel_id"], size=500)
            candidates.append(parsed)

    # 3. iTunes — use cleaned title; 500px thumbnails for modal display
    itunes_hits = _itunes_candidates(artist, clean_title, limit=4)
    # Fallback: title-only search if combined yielded nothing
    if not itunes_hits and clean_title:
        itunes_hits = _itunes_candidates("", clean_title, limit=4)
    for item in itunes_hits:
        if item.get("cover_url"):
            item["cover_url"] = item["cover_url"].replace("250x250bb", "500x500bb")
        item["confidence"] = "low"
        candidates.append(item)

    # Deduplicate by (artist, title)
    seen: set[tuple] = set()
    deduped = []
    for c in candidates:
        key = (c.get("artist", "").lower(), c.get("title", "").lower())
        if key not in seen:
            seen.add(key)
            deduped.append(c)

    return deduped


def apply_candidate(
    path: Path,
    candidate: dict,
    output_dir: Path,
    fetch_lyrics: bool = True,
) -> tuple[Path, Optional[bytes]]:
    """Apply a chosen metadata candidate to the file and reorganize.
    Also fetches and embeds lyrics (synced LRC preferred, plain fallback) when
    fetch_lyrics=True.
    """
    cover: Optional[bytes] = None

    if candidate.get("mb_rel_id"):
        cover = _cover_from_caa(candidate["mb_rel_id"])
    elif candidate.get("cover_url"):
        cover = _fetch_cover_from_url(candidate["cover_url"])

    # Fetch lyrics before writing so they can be embedded in one pass
    lyrics_text: Optional[str] = None
    lrc_synced = False
    if fetch_lyrics:
        artist   = candidate.get("artist", "") or ""
        title    = candidate.get("title",  "") or ""
        album    = candidate.get("album",  "") or ""
        duration = int(candidate.get("duration") or 0)
        lyrics_text, lrc_synced = fetch_lyrics_for_track(artist, title, duration, album)

    new_path = apply_metadata_and_reorganize(path, candidate, cover, output_dir, lyrics=lyrics_text)

    # Save .lrc sidecar if synced
    if lyrics_text and lrc_synced:
        try:
            new_path.with_suffix(".lrc").write_text(lyrics_text, encoding="utf-8")
        except Exception as e:
            logger.warning("[lyrics] .lrc sidecar write failed after apply_candidate: %s", e)

    return new_path, cover
