# YouTube Music Manager

Downloads the songs listed in `songs_meta_data.json` from YouTube Music, writes
their metadata (title, artist, album, year, cover art) into each file, and files
them as `Songs/<Album>/<Title>.m4a`. A SQLite database (`downloads.db`) remembers
what happened to every song, so every command is safe to run again.

```
songs_meta_data.json  ──►  downloads.db  ──►  Songs/<Album>/<Title>.m4a
   (you edit this)          (the scripts       (your music library)
                             keep this)
```

**Contents**

1. [Setup (once)](#1-setup-once)
2. [Which command do I run?](#2-which-command-do-i-run)
3. [The song list: `songs_meta_data.json`](#3-the-song-list-songs_meta_datajson)
4. [Scenarios, step by step](#4-scenarios-step-by-step)
5. [Command reference](#5-command-reference)
6. [Troubleshooting](#6-troubleshooting)
7. [How it works (for changing the code)](#7-how-it-works-for-changing-the-code)

---

## 1. Setup (once)

You need **Python 3.12+** and **ffmpeg**.

```bash
brew install ffmpeg
```

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
```

Check everything is in place. This only reads, it changes nothing:

```bash
venv/bin/python youtube_music_manager.py --repair
```

> All commands are run from the project folder and use `venv/bin/python`.
> If you prefer, run `source venv/bin/activate` once and type `python` instead.

---

## 2. Which command do I run?

There are **three scripts**. You will almost always use the first two.

| Script | What it does | Downloads audio? |
|---|---|---|
| `youtube_music_manager.py` | Adds new songs from the JSON, downloads them, tags them | **Yes** (only songs not already on disk) |
| `apply_metadata.py` | Pushes your JSON corrections (title, artist, album, year, cover, link) into the files and the database | No |
| `repair.py` | Fixes the database when files were moved, renamed or restored by hand (same as `youtube_music_manager.py --repair`) | No |

**Pick by what you just did:**

| You… | Run | See |
|---|---|---|
| Added new songs to the JSON | `venv/bin/python youtube_music_manager.py` | [4.1](#41-add-new-songs) |
| Corrected a title / artist / album / year / cover in the JSON | `venv/bin/python apply_metadata.py` then `--apply` | [4.2](#42-correct-metadata-title-artist-album-year-cover) |
| Changed a song's `youtube_link` | `venv/bin/python apply_metadata.py` then `--apply` | [4.3](#43-change-a-songs-youtube-link) |
| Want the audio from a new link | requeue + `youtube_music_manager.py` | [4.4](#44-re-download-a-song-from-a-new-link) |
| Removed a song from your library | small snippet | [4.5](#45-remove-a-song) |
| Moved/renamed files or the project folder, or restored `Songs/` | `venv/bin/python youtube_music_manager.py --repair` then `--apply` | [4.6](#46-files-were-moved-renamed-or-restored) |
| Changed a file's tags in another app and want them back | `venv/bin/python apply_metadata.py --apply` | [4.7](#47-tags-were-changed-by-another-app) |
| Just want to check everything is in sync | `venv/bin/python apply_metadata.py` | [4.8](#48-check-that-everything-is-in-sync) |

**Golden rule:** commands that change things always have a preview. Run without
`--apply` (or with `--dry-run`) first, read the report, then apply.

---

## 3. The song list: `songs_meta_data.json`

A JSON array with one entry per song:

```json
{
    "title": "Manasuke",
    "artist": "A. R. Rahman",
    "album": "24 (Telugu)",
    "year": 2016,
    "image_url": "https://m.media-amazon.com/images/I/71dWn7yXjcL._SX472_SY472_BL0_QL100_.jpg",
    "youtube_link": "https://music.youtube.com/watch?v=Pn4ks1THK0g"
}
```

| Field | Becomes | Notes |
|---|---|---|
| `title` | the **file name** and the Title tag | Characters like `: / ? *` are removed from the file name automatically |
| `artist` | the Artist tag | Separate several artists with `, ` |
| `album` | the **folder name** and the Album tag | Songs with the identical album text share a folder |
| `year` | the Year tag | A number like `2016`. `null` or missing leaves the tag empty |
| `image_url` | the embedded cover art | JPEG/PNG preferred; WEBP and GIF are converted |
| `youtube_link` | where the audio comes from | **Must be unique.** It is how a JSON entry is matched to its database row |

So the song above ends up at `Songs/24 (Telugu)/Manasuke.m4a`.

---

## 4. Scenarios, step by step

### 4.1 Add new songs

1. Append entries to `songs_meta_data.json` (all six fields, see [section 3](#3-the-song-list-songs_meta_datajson)).
2. Run:

   ```bash
   venv/bin/python youtube_music_manager.py
   ```

   ```
   Added 2 new tracks to the database.

   🎵 Processing tracks: 2 track(s).

   🎶 Started: Ordinary Person
   🎶 Started: Naa Ready
   ✅ Completed: Ordinary Person
   ❌ Failed: Naa Ready (ERROR: [youtube] xyz: Video unavailable)

   ==== SUMMARY ====
   ✅ Downloaded: 535
   ❌ Failed: 1
   📊 Total tracks: 536
   =================
   ```

3. Anything that failed is retried automatically next time you run the same
   command. For `Video unavailable`, replace the link in the JSON (see
   [4.3](#43-change-a-songs-youtube-link)).

Songs already on disk are never downloaded again. Downloads run two at a time.

### 4.2 Correct metadata (title, artist, album, year, cover)

1. Edit the song(s) in `songs_meta_data.json`. Keep `youtube_link` as it is.
2. Preview:

   ```bash
   venv/bin/python apply_metadata.py
   ```

   ```
   JSON songs: 534   database rows: 534   matched: 534
   Songs to update: 2   (files moved/renamed: 2)
     title         changes: 0
     artist        changes: 0
     album         changes: 2
     year          changes: 0
     image_url     changes: 0
     youtube_link  changes: 0

   -- Changes other than year --
     [316] Rockaankuthu (Premam (Malayalam))
         album: 'Premam' -> 'Premam (Malayalam)'
         file: Songs/Premam/Rockaankuthu.m4a -> Songs/Premam (Malayalam)/Rockaankuthu.m4a
     [317] Evare (Premam (Telugu))
         album: 'Premam' -> 'Premam (Telugu)'
         file: Songs/Premam/Evare.m4a -> Songs/Premam (Telugu)/Evare.m4a

   Dry run - nothing was changed. Re-run with --apply to write these changes.
   ```

   Year-only changes are counted but not listed one by one.

3. If it looks right, apply:

   ```bash
   venv/bin/python apply_metadata.py --apply
   ```

   ```
   Database backed up to downloads.db.20261109-150212.bak
   ...
   Updated 2 song(s), 0 failed.
   ```

What happens per kind of edit — **no audio is downloaded**:

| You change | Result |
|---|---|
| `title` | file renamed, Title tag rewritten |
| `album` | file moved to the new album folder (empty old folder removed), Album tag rewritten |
| `artist` | Artist tag rewritten |
| `year` | Year tag rewritten |
| `image_url` | new cover downloaded and embedded |

Each `--apply` makes a backup `downloads.db.<date-time>.bak`. Delete old ones once
you are happy.

> **Alternative:** a plain `venv/bin/python youtube_music_manager.py` also picks
> up these edits and rewrites the files, but without a preview.

### 4.3 Change a song's YouTube link

Edit the `youtube_link` in the JSON, then **use `apply_metadata.py`, not the
plain command**:

```bash
venv/bin/python apply_metadata.py
venv/bin/python apply_metadata.py --apply
```

The preview shows:

```
-- Matched by title + album (YouTube link updated in the database) --
  Apna Bana Le (Bhediya): https://music.youtube.com/watch?v=Fn2IuyDfmOc -> https://music.youtube.com/watch?v=YALvuUpY_b0
```

This stores the new link on the existing song. **The audio on disk is kept.**
If you only tidied the URL, you are done. If you want the audio from the new
link, continue with [4.4](#44-re-download-a-song-from-a-new-link).

> Why not the plain command? It matches songs by link, so a changed link looks
> like a brand-new song: it creates a duplicate entry and reuses the old file
> instead of downloading the new one.

### 4.4 Re-download a song from a new link

After [4.3](#43-change-a-songs-youtube-link), move the old file aside and queue
the song. Set `TITLE` and `ALBUM` to the song's values:

```bash
venv/bin/python - <<'PY'
import os, sqlite3, common
TITLE, ALBUM = "Apna Bana Le", "Bhediya"
c = sqlite3.connect("downloads.db")
row = c.execute("SELECT id, file_path FROM tracks WHERE title = ? AND album = ?", (TITLE, ALBUM)).fetchone()
path = common.resolve_path(row[1])
if path and os.path.exists(path):
    os.rename(path, path + ".old")          # keep the old audio, just in case
c.execute("UPDATE tracks SET status = 'pending', error = '' WHERE id = ?", (row[0],))
c.execute("INSERT INTO activity_log (track_id, action, details) VALUES (?, 'REQUEUED', 'Re-download from new link')", (row[0],))
c.commit()
print("queued", TITLE)
PY
```

```bash
venv/bin/python youtube_music_manager.py
```

Expect `🎶 Started: Apna Bana Le` and `✅ Completed: Apna Bana Le`. Listen to the
new file; delete the `.old` file once you are happy (or rename it back to undo).

The old file must be moved first, otherwise the run finds it on disk and reuses it.

### 4.5 Remove a song

1. Delete its entry from `songs_meta_data.json`.
2. Remove its database row and file (set the link):

   ```bash
   venv/bin/python - <<'PY'
   import os, sqlite3, common
   LINK = "https://music.youtube.com/watch?v=XXXX"
   c = sqlite3.connect("downloads.db")
   row = c.execute("SELECT id, file_path FROM tracks WHERE youtube_link = ?", (LINK,)).fetchone()
   path = common.resolve_path(row[1])
   if path and os.path.exists(path):
       os.remove(path)
   c.execute("DELETE FROM tracks WHERE id = ?", (row[0],))
   c.commit()
   print("removed", row[0])
   PY
   ```

If you skip step 1, the next plain run adds the song back and downloads it again.

### 4.6 Files were moved, renamed or restored

Use this after you moved the project folder, renamed folders/files by hand,
restored `Songs/` from a backup, or when songs are marked `failed` although the
audio is clearly there.

```bash
venv/bin/python youtube_music_manager.py --repair
```

```
==== REPAIR — DRY RUN (nothing will be written) ====

Rows in database:            534
Audio located on disk:       534
Audio genuinely missing:     0
file_path rewrites:          0
Files to relocate:           0
Status promotions:           0
  of which stranded:         0
Metadata re-syncs from JSON: 0
Unusable cached cover art:   0  (re-run with --fetch-art to fetch)
Tracks with no cover art:    0  (re-run with --fetch-art to fetch)
Nothing was written. Re-run with --apply to make these changes.
```

If any number is not 0, apply it:

```bash
venv/bin/python youtube_music_manager.py --repair --apply
```

Add `--fetch-art` to also download missing or broken cover art. Repair finds
files by searching the whole `Songs/` folder, moves them back to
`Songs/<Album>/<Title>.m4a`, backs up the database to `downloads.db.bak`, and
never downloads audio.

| Report line | Meaning |
|---|---|
| Audio located on disk | Songs whose file was found and verified |
| Audio genuinely missing | No file anywhere; these stay queued for download |
| file_path rewrites | Stored path was wrong; will be corrected |
| Files to relocate | File is outside its album folder; will be moved |
| Status promotions | Marked failed/processing but the file is fine; will be marked downloaded |
| Metadata re-syncs from JSON | Title/artist/album/cover differ from the JSON (for year and link use `apply_metadata.py`) |

### 4.7 Tags were changed by another app

`apply_metadata.py` compares the tags **inside each file** with the JSON, so it
also repairs tags another program changed:

```bash
venv/bin/python apply_metadata.py
venv/bin/python apply_metadata.py --apply
```

To rewrite tags from the **database** instead (for one album, artist or title,
or everything), use `--retag`. It never touches the network:

```bash
venv/bin/python youtube_music_manager.py --retag "Salaar Cease Fire" --dry-run   # preview
venv/bin/python youtube_music_manager.py --retag "Salaar Cease Fire"             # do it
venv/bin/python youtube_music_manager.py --retag "Anirudh"                       # by artist
venv/bin/python youtube_music_manager.py --retag                                 # every song
```

`MATCH` is a piece of text matched against title, album and artist.

### 4.8 Check that everything is in sync

```bash
venv/bin/python apply_metadata.py
```

`Songs to update: 0` means every file and database row matches the JSON.
`--repair` without `--apply` gives the same assurance for files on disk.

### 4.9 Move the library to another computer or folder

Copy the whole project folder, then:

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/python youtube_music_manager.py --repair
```

Paths are stored relative to the project folder, so usually nothing needs fixing.

### 4.10 Rebuild a lost or broken database

```bash
mv downloads.db downloads.db.old
venv/bin/python youtube_music_manager.py
```

Every song is re-created from the JSON and adopted from `Songs/` as
`⏩ Skipped (already on disk, DB updated)` — no audio is downloaded. Cover art is
fetched again (one request per song) and the old activity history is lost.

---

## 5. Command reference

### `youtube_music_manager.py`

| Command | Effect |
|---|---|
| `venv/bin/python youtube_music_manager.py` | Normal run: add new songs, refile edited ones, download pending/failed, print summary |
| `… --repair` | Report how the database differs from the files on disk. Writes nothing |
| `… --repair --apply` | Write those repairs (backs up to `downloads.db.bak` first) |
| `… --repair --apply --fetch-art` | Also download missing or unusable cover art |
| `… --retag MATCH --dry-run` | List songs whose title/album/artist contains MATCH |
| `… --retag MATCH` | Rewrite their tags from the database and move them to their canonical path |
| `… --retag` | Same, for every downloaded song |
| `… --help` | Show the options |

A normal run, step by step:

1. Creates the database or adds missing columns.
2. Requeues songs stuck from an interrupted run.
3. Syncs the JSON: inserts new songs, updates changed ones.
4. Rewrites files whose metadata changed (no download).
5. Downloads everything `pending` or `failed`.
6. Prints a summary.

### `apply_metadata.py`

| Command | Effect |
|---|---|
| `venv/bin/python apply_metadata.py` | Preview every difference between the JSON and the database/files |
| `venv/bin/python apply_metadata.py --apply` | Back up the database, then apply them |

Messages that start with `!!` mean the song was skipped:

| Message | What to do |
|---|---|
| `audio file missing` | Restore the file, or run `--repair` ([4.6](#46-files-were-moved-renamed-or-restored)) |
| `destination already holds another file` | Two songs want the same path; rename one in the JSON |
| `In JSON but no database row matched` | New song: run the plain command ([4.1](#41-add-new-songs)) |
| `In database but not in JSON` | Old row; remove it ([4.5](#45-remove-a-song)) or add it back to the JSON |

### `repair.py`

`venv/bin/python repair.py [--apply] [--fetch-art]` — identical to
`youtube_music_manager.py --repair …`.

### Song statuses

| Status | Meaning | What happens next run |
|---|---|---|
| `pending` | Waiting to download | Downloaded |
| `processing` | Being downloaded now (or a run was interrupted) | Requeued automatically |
| `downloaded` | Done and verified | Nothing |
| `failed` | Last attempt failed (see the `error` column) | Retried |
| `needs_retag` | On disk, but its metadata changed | File rewritten, not re-downloaded |

---

## 6. Troubleshooting

**Every download fails with `HTTP Error 403: Forbidden`.** yt-dlp is outdated
(YouTube changes often). Update it and run again; failed songs retry automatically:

```bash
venv/bin/pip install --upgrade yt-dlp && venv/bin/python youtube_music_manager.py
```

Then pin the working version in `requirements.txt` (`venv/bin/pip freeze | grep yt-dlp`).

**A song keeps failing with `Video unavailable`.** YouTube removed it. If you
already have the file, `--repair --apply` marks it downloaded and stops the
retries. Otherwise put a working link in the JSON ([4.3](#43-change-a-songs-youtube-link)).

**"This video is only available to Music Premium members".** Uncomment the
`cookiefile` line in `build_ydl_opts()` in `youtube_music_manager.py` and export
your browser cookies to `cookies.txt` in the project folder.

**A song has no cover art.**
`venv/bin/python youtube_music_manager.py --repair --apply --fetch-art`. If it
still fails, the report prints what the server returned; use a direct JPEG/PNG
`image_url`.

**`database is locked`.** Two commands are running at once. Run one at a time.

**I pressed Ctrl-C during a run.** Just run it again; interrupted songs are
requeued automatically.

**`⚠️ Duplicate audio` warning.** Two links downloaded identical audio. Nothing
is deleted; check the two songs in the JSON.

**See why songs failed:**

```bash
sqlite3 downloads.db "SELECT title, error FROM tracks WHERE status = 'failed'"
```

**See the history of one song:**

```bash
sqlite3 downloads.db "SELECT l.timestamp, l.action, l.details FROM activity_log l JOIN tracks t ON t.id = l.track_id WHERE t.title = 'Manasuke' ORDER BY l.id"
```

---

## 7. How it works (for changing the code)

### Files

| File | Role |
|---|---|
| `youtube_music_manager.py` | Entry point: JSON → database sync, download (yt-dlp, 2 workers), tagging (mutagen), `--retag` |
| `apply_metadata.py` | Applies JSON edits to the database and files without downloading |
| `repair.py` | Reconciles the database with what is on disk; run via `--repair` |
| `common.py` | Shared helpers: paths, name sanitising, DB connections, image checks |
| `songs_meta_data.json` | What should exist (you edit it) |
| `downloads.db` | What happened (tracked in git; ~87 MB, mostly cached cover art — commit sparingly, GitHub rejects files over 100 MB) |
| `Songs/`, `venv/` | Your library and the Python environment (git-ignored) |

### Rules the code follows

- **The JSON wins on metadata, the disk wins on existence**, and the database is
  derived from both.
- **`youtube_link` identifies a song.** `apply_metadata.py` additionally falls
  back to exact title + album, so a changed link is moved onto the existing row.
- **Paths are stored relative** to the project folder (`store_path()` to write,
  `resolve_path()` to read), and always built with `track_relpath()`.
- **Files are verified, not just checked for existence** (`verify_audio_file()`:
  present, ≥ 64 KB, readable as MP4). Existing files are never overwritten.
- **Cover art is only ever embedded as verified JPEG/PNG**; WEBP/GIF are
  converted with ffmpeg, anything else is rejected.
- **Database connections go through `get_conn()`** (WAL mode, 30 s busy timeout).
- **Everything that writes is logged** to `activity_log`.
- **A tag change changes the file's SHA-256** (`file_hash`), since the hash covers
  the whole file.

### Database

```sql
tracks (id, title, artist, album, image_url, image_blob, youtube_link UNIQUE,
        status, error, file_hash, file_path, year)
activity_log (id, track_id, action, details, timestamp)
```

`image_blob` caches cover art so re-tagging needs no network. `year` is added
automatically to older databases by `init_db()`.

### Adding a new tag field

Change four places: the column in `init_db()` (as an `ALTER TABLE` migration),
the comparison in `sync_json_to_db()`, the write in `add_metadata()`, and
`FIELDS`/`TAGS` in `apply_metadata.py`. `year` is the worked example.

### Known limitations

- There is no automated test suite; use the dry runs and [4.8](#48-check-that-everything-is-in-sync) to check.
- `repair.py` does not compare `year` or `youtube_link`; `apply_metadata.py` does.
- A changed link never re-downloads by itself ([4.4](#44-re-download-a-song-from-a-new-link)).
- Names keep trailing dots (e.g. `APT.`), which Windows/exFAT drives may dislike.
