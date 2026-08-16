# Usage Guide

Everything you can do with this tool, with runnable examples. For how the code
works internally, see [ARCHITECTURE.md](ARCHITECTURE.md).

**Contents**

- [Setup](#setup)
- [The everyday workflow](#the-everyday-workflow)
- [Adding songs](#adding-songs)
- [Correcting metadata](#correcting-metadata)
- [Re-tagging on demand](#re-tagging-on-demand)
- [Command reference](#command-reference)
- [Repairing the database](#repairing-the-database)
- [Organizing stray files](#organizing-stray-files)
- [Reading the status values](#reading-the-status-values)
- [Troubleshooting](#troubleshooting)
- [Inspecting the database](#inspecting-the-database)
- [Regression recipes](#regression-recipes)
- [Backup and portability](#backup-and-portability)

---

## Setup

**1. ffmpeg** — required by yt-dlp for audio extraction, and used to convert
WEBP cover art.

```bash
brew install ffmpeg
```

**2. Virtual environment and dependencies**

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
```

**3. Confirm it works** — this is read-only and safe to run any time:

```bash
venv/bin/python youtube_music_manager.py --repair
```

All examples below use `venv/bin/python`. If you prefer, activate the
environment first with `source venv/bin/activate` and just use `python`.

---

## The everyday workflow

```bash
venv/bin/python youtube_music_manager.py
```

That single command does all of this:

1. Creates the database and tables if they don't exist
2. Requeues any track stranded by an interrupted previous run
3. Syncs `songs_meta_data.json` into the database — new songs in, corrected metadata updated
4. Refiles and re-tags any track whose title or album changed
5. Downloads everything still `pending` or `failed`, four at a time
6. Prints a summary

**When everything is already downloaded:**

```
✅ All tracks are already downloaded or no pending tracks found.

==== SUMMARY ====
✅ Downloaded: 500
📊 Total tracks: 500
🎵 Audio files located in: /Users/you/Downloads/youtube_music_manager/Songs
=================
```

**When there is work to do:**

```
Added 3 new tracks to the database.

🎵 Processing tracks: 3 track(s).

🎶 Started: Ordinary Person
🎶 Started: Bloody Sweet
🎶 Started: Naa Ready
✅ Completed: Bloody Sweet
⏩ Skipped (already on disk, DB updated): Ordinary Person
❌ Failed: Naa Ready (ERROR: [youtube] xyz: Video unavailable)

==== SUMMARY ====
✅ Downloaded: 502
❌ Failed: 1
📊 Total tracks: 503
=================
```

Re-running is always safe. Completed tracks are skipped, failures are retried.

---

## Adding songs

Append an entry to `songs_meta_data.json`. All five fields matter:

```json
{
  "title": "Diyalo Diyala",
  "artist": "Priya Hemesh",
  "album": "100% Love",
  "image_url": "https://m.media-amazon.com/images/I/81s8F+FP5DL._SX472_.jpg",
  "youtube_link": "https://music.youtube.com/watch?v=eIXVLAu60-E"
}
```

| Field | Notes |
|---|---|
| `title` | Becomes the filename and the `©nam` tag. Illegal characters are stripped automatically |
| `artist` | The `©ART` tag |
| `album` | Becomes the **folder name** and the `©alb` tag |
| `image_url` | Cover art. JPEG or PNG preferred; WEBP and GIF are converted automatically |
| `youtube_link` | **The identity key.** Must be unique — it is how a JSON entry is matched to its database row |

Then run the manager:

```bash
venv/bin/python youtube_music_manager.py
```

Output: `Added 1 new tracks to the database.` followed by the download.

> **Two songs, same album?** Use the identical `album` string for both and they
> land in the same folder. The folder is derived from the album name, not stored
> separately.

---

## Correcting metadata

Edit the entry in `songs_meta_data.json` — **keep `youtube_link` unchanged** —
and re-run. The tool does the rest.

**Example: fixing a typo in a title.**

```diff
- "title": "Sunflower (Spider-Man: Into the Spider-Verse)",
+ "title": "Sunflower (Spider-Man - Into the Spider-Verse)",
```

```bash
venv/bin/python youtube_music_manager.py
```

```
Refreshed metadata for 1 existing tracks from JSON.
1 downloaded tracks need refiling after the metadata change.

🎵 Refiling tracks whose metadata changed: 1 track(s).

🏷️  Retagged: Sunflower (Spider-Man - Into the Spider-Verse)
```

That renamed the file on disk, rewrote the tag inside it, and re-hashed it —
**with no download**.

**What each kind of edit triggers:**

| You change | What happens |
|---|---|
| `title` | File renamed, `©nam` tag rewritten, re-hashed |
| `album` | File **moved to the new album folder**, `©alb` tag rewritten |
| `artist` | `©ART` tag rewritten in the file |
| `image_url` | Cached art discarded, new art downloaded and embedded |
| Several at once | All applied in one pass |
| `youtube_link` | Treated as a **brand new song** — see the warning below |

Any change to a downloaded track's `title`, `album`, `artist` or `image_url`
marks it `needs_retag`, and the next plain run rewrites the actual file — not
just the database row. **No re-download**; only new cover art is fetched.

**Example: correcting an artist name and the cover in one go.**

```diff
- "artist": "Anuv Jain",
- "image_url": "https://m.media-amazon.com/images/I/61W0y6YhVWL._SX472_.jpg",
+ "artist": "Anuv Jain (Corrected)",
+ "image_url": "https://m.media-amazon.com/images/I/71hqO4JCcgL._SX472_.jpg",
```

```bash
venv/bin/python youtube_music_manager.py
```

```
Refreshed metadata for 1 existing tracks from JSON.
1 downloaded tracks need refiling after the metadata change.

🎵 Refiling tracks whose metadata changed: 1 track(s).

🏷️  Retagged: Husn
```

Both the `©ART` tag and the embedded cover art in `Husn.m4a` are now updated.

---

## Re-tagging on demand

Sometimes you want to force a file back into agreement with its database row —
after editing the database directly, or when a file's tags were changed by
another program. `--retag` rewrites tags and refiles from what the database
holds, **without touching the JSON and without any network access** (it reuses
the cached cover art).

**Always preview first with `--dry-run`:**

```bash
venv/bin/python youtube_music_manager.py --retag "Salaar Cease Fire" --dry-run
```

```
7 track(s) matching 'Salaar Cease Fire':

  Ravi Basrur - Prathi Gaadhalo (Salaar Cease Fire)
  Harini Ivaturi - Sooreede (Salaar Cease Fire)
  Ravi Basrur - Wrath of Salaar (Salaar Cease Fire)
  ...

Dry run - nothing was changed. Drop --dry-run to apply.
```

Then apply:

```bash
venv/bin/python youtube_music_manager.py --retag "Salaar Cease Fire"
```

`MATCH` is a substring tested against **title, album and artist**, so any of
these work:

```bash
venv/bin/python youtube_music_manager.py --retag "Ravi Basrur"      # by artist
venv/bin/python youtube_music_manager.py --retag "Premalu"          # by album
venv/bin/python youtube_music_manager.py --retag "Sound of Salaar"  # by title
venv/bin/python youtube_music_manager.py --retag                    # everything
```

Any track whose canonical location changed is also moved, and the dry run shows
you the move before it happens:

```
  Ravi Basrur - Sooreede (Salaar Cease Fire)  ← will move
      Songs/Salaar/Sooreede.m4a  ->  Songs/Salaar Cease Fire/Sooreede.m4a
```

> To change *what* the tags say, edit `songs_meta_data.json` and run the plain
> command. `--retag` re-applies what the database already holds; it does not
> read the JSON.

### ⚠️ Replacing a `youtube_link`

`youtube_link` is the identity key, so changing it is **not** an edit — the tool
sees a song it has never met. Run the plain command and you get:

- a second row for the same song (the old one is orphaned, still `downloaded`)
- and, because the file already exists at the canonical path, the new row
  **adopts the old audio and never fetches the new link**

So if you swapped the link because the downloaded audio is wrong, the plain
command will not fix it. Remove the old row and its file first, then run:

```bash
venv/bin/python - <<'PY'
import sqlite3, os, common
OLD = "https://music.youtube.com/watch?v=OLD_ID"          # the link you replaced
c = sqlite3.connect('downloads.db')
row = c.execute("SELECT id, file_path FROM tracks WHERE youtube_link = ?", (OLD,)).fetchone()
if row:
    path = common.resolve_path(row[1])
    if path and os.path.exists(path):
        os.rename(path, path + '.old')                    # keep a copy, don't destroy it
    c.execute("DELETE FROM tracks WHERE id = ?", (row[0],))
    c.commit()
    print('removed row', row[0])
PY
venv/bin/python youtube_music_manager.py
```

Renaming to `.old` rather than deleting means you can compare the two and undo
if the new version is worse. Delete the `.old` file once you are happy.

If you only tidied the URL and the audio is fine, update the link in place
instead — no re-download, no duplicate row:

```bash
venv/bin/python -c "
import sqlite3
c = sqlite3.connect('downloads.db')
c.execute('UPDATE tracks SET youtube_link = ? WHERE youtube_link = ?',
          ('NEW_LINK', 'OLD_LINK'))
c.commit(); print('remapped', c.total_changes)
"
```

---

## Command reference

```bash
venv/bin/python youtube_music_manager.py [--repair [--apply] [--fetch-art]]
                                         [--retag [MATCH] [--dry-run]]
```

| Flag | Effect |
|---|---|
| *(none)* | Sync, refile, download, report. The normal command |
| `--retag [MATCH]` | Rewrite tags and refile downloaded tracks matching MATCH. Omit MATCH for all |
| `--retag [MATCH] --dry-run` | List what would be re-tagged, change nothing |
| `--repair` | Reconcile the database against the disk. **Dry run — writes nothing** |
| `--repair --apply` | Actually write the repairs. Backs up to `downloads.db.bak` first |
| `--repair --apply --fetch-art` | Also download missing or unusable cover art |

Standalone scripts:

```bash
venv/bin/python organize_albums.py            # file stray tracks into album folders
venv/bin/python repair.py --apply             # same as --repair --apply
```

---

## Repairing the database

Use `--repair` whenever the database and the files on disk disagree. It is the
right tool for **all** of these:

- You moved or renamed the project folder
- You renamed album folders or song files by hand
- A run was interrupted and tracks are stuck in `processing`
- Tracks are marked `failed` even though the audio is clearly on disk
- You restored `Songs/` from a backup

**Always dry-run first.** It writes nothing:

```bash
venv/bin/python youtube_music_manager.py --repair
```

```
==== REPAIR — DRY RUN (nothing will be written) ====

Rows in database:            500
Audio located on disk:       500
Audio genuinely missing:     0
file_path rewrites:          500
Files to relocate:           1
Status promotions:           17
  of which stranded:         4
Metadata re-syncs from JSON: 2
Unusable cached cover art:   1  (re-run with --fetch-art to fetch)
Tracks with no cover art:    18  (re-run with --fetch-art to fetch)

-- Status promotions (file verified present, no download needed) --
  [    failed] -> downloaded   Kutty Kudiye (Premalu)
  [processing] -> downloaded   Sound of Salaar (Salaar Cease Fire)
  ...

-- Files to relocate into their album folder --
  Songs/Moral of the Story_ Chapter 1/Moral of the Story.m4a
    -> Songs/Moral of the Story Chapter 1/Moral of the Story.m4a

Nothing was written. Re-run with --apply to make these changes.
```

Read that report. If it looks right, apply it:

```bash
venv/bin/python youtube_music_manager.py --repair --apply --fetch-art
```

### What each line of the report means

| Line | Meaning |
|---|---|
| **Audio located on disk** | Rows whose file was found and verified — searched canonically, then at the legacy flat path, then the stored path, then by filename anywhere in `Songs/` |
| **Audio genuinely missing** | Nothing found. These stay queued for download |
| **file_path rewrites** | Rows whose stored path was wrong or still absolute |
| **Files to relocate** | Files sitting outside their canonical album folder |
| **Status promotions** | Rows marked `failed`/`processing` whose audio is verifiably present. Promoted to `downloaded` — **this is what stops working songs being re-fetched from YouTube** |
| **Metadata re-syncs** | Fields the JSON has corrected but the database still holds stale |
| **Unusable cached cover art** | Cached blobs that are not JPEG/PNG |
| **Tracks with no cover art** | Downloaded tracks with an `image_url` but no embedded art |

### Safety properties

- Dry run is the default; `--apply` is required to write
- A `downloads.db.bak` snapshot is taken before any write, using SQLite's backup API
- Changes are committed per track, so an interruption leaves a consistent database
- **No audio is ever downloaded.** Only `--fetch-art` allows any network access
- Files are never overwritten — a name collision is reported and skipped

---

## Organizing stray files

The downloader writes straight into `Songs/<Album>/`, so this is only a safety
net for files left in the flat root by older runs or moved by hand.

```bash
venv/bin/python organize_albums.py
```

```
Found 500 downloaded tracks. Organizing by album...

==== ORGANIZATION SUMMARY ====
Total processed: 500
Successfully moved: 0
Already in place: 500
Missing files: 0
Name conflicts: 0
==============================
```

| Counter | Meaning |
|---|---|
| Already in place | Correct — nothing to do |
| Missing files | The database points somewhere with no file. Run `--repair` |
| Name conflicts | Two tracks want the same path. Both left untouched; rename one in the JSON |

---

## Reading the status values

| Status | Meaning | Retried? |
|---|---|---|
| `pending` | Queued for download | Yes |
| `processing` | Being worked on right now | Yes — requeued at startup |
| `downloaded` | Complete and verified | No |
| `failed` | Last attempt failed; see the `error` column | Yes |
| `needs_retag` | On disk, but its JSON metadata changed | Yes — refiled, not re-downloaded |

If you see `processing` in a summary while nothing is running, a previous run was
killed. The next run requeues those automatically — no action needed.

---

## Troubleshooting

### Every download fails with "HTTP Error 403: Forbidden"

**This is a stale yt-dlp, not a problem with your links.** YouTube periodically
changes how streams are served, and an outdated yt-dlp can still read metadata
while being refused the actual audio. The giveaway is that it affects *every*
track, including ones that downloaded fine before.

```bash
venv/bin/pip install --upgrade yt-dlp && venv/bin/python youtube_music_manager.py
```

Failed tracks are retried automatically, so the re-run picks them straight up.
Then re-pin the working version in `requirements.txt`:

```bash
venv/bin/pip freeze | grep yt-dlp
```

To confirm the diagnosis before upgrading — if metadata resolves but the
download is refused, it is the version:

```bash
venv/bin/python -c "
import json, yt_dlp
url = json.load(open('songs_meta_data.json'))[0]['youtube_link']
with yt_dlp.YoutubeDL({'quiet': True}) as y:
    print('metadata OK:', y.extract_info(url, download=False)['title'])
"
```

### A track keeps failing with "Video unavailable"

YouTube removed it. Check whether you already have the audio from an earlier run:

```bash
find Songs -name "*Malabari*"
```

If the file is there, `--repair` will promote the row to `downloaded` and stop
retrying it:

```bash
venv/bin/python youtube_music_manager.py --repair --apply
```

If the file genuinely isn't there, replace the `youtube_link` in the JSON with a
working one.

### "This video is only available to Music Premium members"

Uncomment the `cookiefile` line in `build_ydl_opts()` in
[youtube_music_manager.py](youtube_music_manager.py) and export your browser
cookies to `cookies.txt` in the project root.

### Everything shows "File not found" in the organizer

The stored paths are stale — usually because the project folder was moved.

```bash
venv/bin/python youtube_music_manager.py --repair --apply
```

### A song downloaded but has no album art

Its `image_url` is missing, unreachable, or serving an unusable format.

```bash
venv/bin/python youtube_music_manager.py --repair --fetch-art          # check
venv/bin/python youtube_music_manager.py --repair --apply --fetch-art  # fix
```

If it still fails, the report prints the `Content-Type` the server actually
returned. Put a direct JPEG or PNG URL in the JSON.

### "database is locked"

Two runs at once. Only run one at a time. WAL and a 30-second busy timeout are
already enabled, so this should be rare.

### I interrupted a run with Ctrl-C

Nothing to do. Just run it again — stranded tracks are requeued automatically:

```
Requeued 2 track(s) stranded by a previous run: Sound of Salaar, Wrath of Salaar
```

### A "Duplicate audio" warning

Two `youtube_link`s point at byte-identical audio. Nothing is deleted — it is
informational. Find them with:

```bash
venv/bin/python -c "
import sqlite3
c = sqlite3.connect('downloads.db')
for h, n in c.execute('''SELECT file_hash, COUNT(*) c FROM tracks
                         WHERE file_hash IS NOT NULL
                         GROUP BY file_hash HAVING c > 1'''):
    for t, a in c.execute('SELECT title, album FROM tracks WHERE file_hash = ?', (h,)):
        print(f'{h[:12]}  {t} ({a})')
"
```

---

## Inspecting the database

**Status breakdown**

```bash
venv/bin/python -c "
import sqlite3
c = sqlite3.connect('downloads.db')
print(dict(c.execute('SELECT status, COUNT(*) FROM tracks GROUP BY status')))
"
```

**Why a specific track failed**

```bash
venv/bin/python -c "
import sqlite3
c = sqlite3.connect('downloads.db')
for t, e in c.execute(\"SELECT title, error FROM tracks WHERE status='failed'\"):
    print(f'{t}: {e[:100]}')
"
```

**Full history of one track**

```bash
venv/bin/python -c "
import sqlite3
c = sqlite3.connect('downloads.db')
rows = c.execute('''SELECT l.timestamp, l.action, l.details
                    FROM activity_log l JOIN tracks t ON t.id = l.track_id
                    WHERE t.title = ? ORDER BY l.id''', ('Kutty Kudiye',))
for ts, action, details in rows:
    print(f'{ts}  {action:22} {details[:70]}')
"
```

**Verify every file the database claims to have**

```bash
venv/bin/python -c "
import sqlite3, common
c = sqlite3.connect('downloads.db')
bad = [p for (p,) in c.execute(\"SELECT file_path FROM tracks WHERE status='downloaded'\")
       if not common.resolve_path(p or '')
       or not __import__('os').path.exists(common.resolve_path(p))]
print(f'{len(bad)} broken paths')
"
```

**Delete a track you no longer want** (removes the row; the file stays on disk)

```bash
venv/bin/python -c "
import sqlite3
c = sqlite3.connect('downloads.db')
c.execute('DELETE FROM tracks WHERE youtube_link = ?', ('https://music.youtube.com/watch?v=XXXX',))
c.commit(); print('deleted', c.total_changes)
"
```

Remember to remove it from `songs_meta_data.json` too, or the next run re-adds it.

---

## Regression recipes

Manual checks for the failure modes this tool is built to prevent. All are
self-reverting.

**A file on disk is never re-downloaded**

```bash
venv/bin/python -c "
import sqlite3; c=sqlite3.connect('downloads.db')
c.execute(\"UPDATE tracks SET status='failed' WHERE title='Kutty Kudiye'\"); c.commit()"
venv/bin/python youtube_music_manager.py
```

Expect `⏩ Skipped (already on disk, DB updated)` — adopted from its album
folder, no network call.

**An interrupted run recovers**

```bash
venv/bin/python -c "
import sqlite3; c=sqlite3.connect('downloads.db')
c.execute(\"UPDATE tracks SET status='processing' WHERE title='Ordinary Person'\"); c.commit()"
venv/bin/python youtube_music_manager.py
```

Expect `Requeued 1 track(s) stranded by a previous run` followed by the skip.

**Corrupt files are rejected, not accepted**

```bash
venv/bin/python -c "
import sys; sys.path.insert(0,'.')
from youtube_music_manager import verify_audio_file
open('/tmp/t.m4a','wb').write(b'\x00'*1024)
print('truncated:', verify_audio_file('/tmp/t.m4a'))
print('missing:  ', verify_audio_file('/tmp/nope.m4a'))
"
```

Expect `(False, 'truncated (1024 bytes)')` and `(False, 'missing')`.

**Filenames are sanitized**

```bash
venv/bin/python -c "
import sys; sys.path.insert(0,'.')
from common import track_relpath
print(track_relpath('Song: Part 1', 'Surya S/o Krishnan'))
"
```

Expect `Songs/Surya So Krishnan/Song Part 1.m4a`.

---

## Backup and portability

**What to back up:** `songs_meta_data.json` and `Songs/`. That is the whole
library. `downloads.db` is derived state — if you lose it, `--repair` rebuilds it
from the JSON and the files on disk.

**Moving the library to another machine or folder:**

```bash
cp -R youtube_music_manager /Volumes/Backup/
cd /Volumes/Backup/youtube_music_manager
python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/python youtube_music_manager.py --repair       # verify
venv/bin/python youtube_music_manager.py --repair --apply
```

Paths are stored relative to the project root, so a move needs no repair at all
in most cases — run the dry run to confirm.

**Rebuilding the database from scratch** (if `downloads.db` is lost or corrupt):

```bash
mv downloads.db downloads.db.old
venv/bin/python youtube_music_manager.py
```

Use the **plain command**, not `--repair`. `--repair` reconciles rows that already
exist, so on an empty database it finds nothing to do. The normal run recreates
every row from `songs_meta_data.json`, then finds each file already on disk and
adopts it:

```
Added 500 new tracks to the database.
🎶 Started: Husn
⏩ Skipped (already on disk, DB updated): Husn
...
```

Every track whose audio is present is restored to `downloaded` with **no audio
downloaded** and the files left byte-identical. Two caveats:

- The cached cover art is gone with the old database, so this re-fetches
  `image_url` for every track — one HTTP request per song.
- The `activity_log` history is not recoverable.

If you only want to keep the old log, copy the table across from
`downloads.db.old` before deleting it.

**Git:** `downloads.db`, `downloads.db.bak`, `Songs/` and `venv/` are ignored.
The repository tracks only source, `requirements.txt`, and
`songs_meta_data.json`.
