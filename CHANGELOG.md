# Changelog

## [2.0.0] — 2025

### Added
- **Invidious / YouTube playlist support** — paste any `?list=PL…` URL
  - Uses Invidious API directly; video IDs are already known (no YTMusic search)
  - Auto-paginates large playlists
  - YouTube URLs are routed through the configured `LBDL_INVIDIOUS_INSTANCE`
- **`/api/config` endpoint** — exposes runtime config to the UI
- **`source` field** in saved playlists and jobs
- **Deezer-inspired UI redesign**
  - Dark purple/gradient theme with Syne + Plus Jakarta Sans typography
  - Split layout: sidebar (input + saved), jobs list, detail panel
  - Real-time spinning indicator for actively-downloading tracks
  - Auto-detects source from pasted URL (switches tabs automatically)
- **PWA enhancements**
  - Updated manifest with maskable icon and `share_target`
  - Service worker cache version bumped to `lbdl-v2`
- **Mobile improvements**
  - Three-tab bottom nav (Jobs / Detail / Logs)
  - Smooth slide transitions between views

### Fixed
- `_capture_path` postprocessor hook now reads `d.get("filepath")` first
  (the key set by FFmpegExtractAudio) rather than only `info_dict.filepath`
- Skipped (pre-existing) tracks now have their path captured via
  `find_existing_path()` so they appear in the generated M3U
- `DELETE /api/playlists` uses `Request` directly to avoid proxy stripping body
- All `open()` calls use context managers to prevent file handle leaks
- `@app.on_event("startup")` replaced with `lifespan` context manager
- `track_downloading` WebSocket event now fires when download begins
- `ws://` hardcode replaced with protocol-aware `wsProto()` helper
- XSS in saved-playlist `onclick` attributes replaced with `addEventListener`

## [1.0.0] — Initial release

- ListenBrainz playlist download via yt-dlp Python API
- Multi-value artist tags, synced lyrics, cover art
- WebSocket real-time progress UI
- M3U generation with real EXTINF durations
