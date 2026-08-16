# YouTube Music Manager

Downloads a curated list of tracks from YouTube Music, tags them with proper
metadata and album art, and files them into per-album folders — with a SQLite
database tracking the state of every track so runs are resumable and repeatable.

```
songs_meta_data.json  ──►  downloads.db  ──►  Songs/<Album>/<Title>.m4a
   (what you want)          (what happened)      (what you got)
```

## Documentation

| Document | Read it when |
|---|---|
| **[USAGE.md](USAGE.md)** | You want to run it, add songs, or fix something that went wrong |
| **[ARCHITECTURE.md](ARCHITECTURE.md)** | You want to change the code |

## Quick start

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
```

ffmpeg must be on your `PATH` (`brew install ffmpeg` on macOS). Then:

```bash
venv/bin/python youtube_music_manager.py
```

That reads `songs_meta_data.json`, downloads anything not already on disk, tags
it, and writes it to `Songs/<Album>/<Title>.m4a`. Re-running is safe — it only
picks up what is pending or failed.

## Current library state

| | |
|---|---|
| Tracks tracked | 500 |
| Downloaded | 500 |
| Album folders | 330 |
| Audio format | `.m4a` (AAC), tagged with title / artist / album / cover art |

## What's in the box

| File | Role |
|---|---|
| `youtube_music_manager.py` | Main entry point — sync, download, tag, report |
| `repair.py` | Reconciles the database with the files actually on disk |
| `organize_albums.py` | Safety net that files stray tracks into album folders |
| `common.py` | Shared config, name sanitization, path handling, DB connections |
| `songs_meta_data.json` | The source of truth for *what* to download |
| `downloads.db` | The source of truth for *what happened* (git-ignored) |
| `Songs/` | The audio library (git-ignored) |

## Requirements

- Python 3.12+
- ffmpeg (used by yt-dlp for audio extraction, and to convert WEBP cover art)
- Packages pinned in `requirements.txt`: `yt-dlp`, `mutagen`, `requests`
