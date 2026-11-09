# YouTube Music Manager

Downloads a curated list of tracks from YouTube Music, tags them with proper
metadata (title, artist, album, year) and album art, and files them into
per-album folders — with a SQLite database tracking the state of every track
so runs are resumable and repeatable.

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

## Correcting metadata

Edit `songs_meta_data.json`, then preview and apply the changes to the
database and the audio files:

```bash
venv/bin/python apply_metadata.py            # dry run: lists every change
venv/bin/python apply_metadata.py --apply    # writes them
```

`apply_metadata.py` never downloads audio. For each song it:

- matches the JSON entry to its database row by `youtube_link`, or — when the
  link was changed — by the same title + album, and stores the new link
- updates the row's title, artist, album, year, image URL and link
- rewrites the file's tags, keeping the embedded cover art (new art is fetched
  only when `image_url` changed)
- moves or renames the file when the album or title changed, and removes album
  folders left empty
- refreshes the stored path and hash, and records the change in `activity_log`

With `--apply` it first backs up the database to
`downloads.db.<YYYYMMDD-HHMMSS>.bak`. Songs it cannot match, or whose file is
missing, are listed and left untouched.

A changed `youtube_link` only updates the record; the audio on disk stays the
old recording. To fetch the new one, move the old file out of `Songs/`, set the
track's status to `pending`, and run `youtube_music_manager.py` (see
[USAGE.md](USAGE.md)).

### Song entry format

```json
{
    "title": "Manasuke",
    "artist": "A. R. Rahman",
    "album": "24 (Telugu)",
    "year": 2016,
    "image_url": "https://…/cover.jpg",
    "youtube_link": "https://music.youtube.com/watch?v=…"
}
```

`year` is written to the MP4 `©day` tag, which players show as *Year*. Leave it
`null` (or omit it) to clear the tag.

## Current library state

| | |
|---|---|
| Tracks tracked | 534 |
| Downloaded | 534 |
| Album folders | 352 |
| Release years | 1953 – 2026, every track has one |
| Audio format | `.m4a` (AAC), tagged with title / artist / album / year / cover art |

## What's in the box

| File | Role |
|---|---|
| `youtube_music_manager.py` | Main entry point — sync, download, tag, report (`--retag`, `--repair`) |
| `apply_metadata.py` | Applies edits in `songs_meta_data.json` to the database and the files, without downloading |
| `repair.py` | Reconciles the database with the files actually on disk |
| `common.py` | Shared config, name sanitization, path handling, DB connections |
| `songs_meta_data.json` | The source of truth for *what* to download and how to tag it |
| `downloads.db` | The source of truth for *what happened* (tracked in git; commit it sparingly — see `.gitignore`) |
| `Songs/` | The audio library (git-ignored) |

## Requirements

- Python 3.12+
- ffmpeg (used by yt-dlp for audio extraction, and to convert WEBP cover art)
- Packages pinned in `requirements.txt`: `yt-dlp`, `mutagen`, `requests`
