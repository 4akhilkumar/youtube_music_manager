# Architecture

A guide to how this codebase is put together and, more importantly, *why* it is
put together that way. Several design choices exist to prevent specific failures
that actually happened to this library — those are called out as they come up.

---

## 1. The three sources of truth

The whole system is a reconciliation loop between three places where state lives.
Understanding which one owns what explains almost every design decision.

| Source | Owns | Changed by |
|---|---|---|
| `songs_meta_data.json` | **Intent** — what *should* exist, and its correct metadata | You, by hand |
| `downloads.db` | **History** — what was attempted, what succeeded, where it went | The scripts |
| `Songs/` | **Reality** — the audio files that actually exist | The scripts, and you |

```
   songs_meta_data.json                     Songs/
   (intent)                                 (reality)
        │                                       ▲
        │ sync_json_to_db()                     │ yt-dlp download
        │ inserts new, updates changed          │ mutagen tagging
        ▼                                       │
   ┌─────────────────────────────────────────────────┐
   │                downloads.db                     │
   │   tracks       — one row per song, with status  │
   │   activity_log — append-only audit trail        │
   └─────────────────────────────────────────────────┘
                        ▲
                        │ repair.py reconciles the DB
                        │ back to whatever is on disk
```

The rule that makes this work: **the JSON wins on metadata, the disk wins on
existence, and the database is derived from both.** Whenever they disagree,
`repair.py` rebuilds the database rather than the other way around. The database
is disposable; the JSON and the audio are not. That is why `downloads.db` is
git-ignored.

---

## 2. Module map

```
common.py                  no project imports — pure helpers
   ▲        ▲        ▲
   │        │        │
   │        │        └── organize_albums.py     standalone safety net
   │        │
   │        └─────────── repair.py              imports the manager lazily
   │                        ▲
   └────────────────────────┴── youtube_music_manager.py   entry point
```

### `common.py` — the shared foundation

Everything that both scripts need, and nothing else. It has no project imports,
so it can never participate in a circular dependency.

| Function | Purpose |
|---|---|
| `sanitize_name(name, fallback)` | Strips `\ / * ? : " < > \|` and control characters |
| `track_filename(title)` | `"Song: Part 1"` → `"Song Part 1.m4a"` |
| `album_dirname(album)` | `"Surya S/o Krishnan"` → `"Surya So Krishnan"` |
| `track_relpath(title, album)` | The canonical location: `Songs/<Album>/<Title>.m4a` |
| `store_path(p)` / `resolve_path(p)` | Absolute ⇄ relative path conversion |
| `get_conn(timeout=30)` | A SQLite connection with WAL and a busy timeout |
| `detect_image_format(bytes)` | Magic-number sniffing → `'jpeg'`, `'png'`, or `None` |
| `is_convertible_image` / `convert_image_to_jpeg` | ffmpeg fallback for WEBP/GIF art |

> **Why paths are stored relative.** The database used to store absolute paths.
> Renaming the project folder from `yt/` to `youtube_music_manager/` invalidated
> all 483 rows at once — the organizer could no longer find a single file.
> `store_path()` is called on every write and `resolve_path()` on every read, so
> the library is now portable.

> **Why `sanitize_name` does not strip trailing dots.** Windows dislikes them,
> but stripping them would rename existing tracks like `APT.` and
> `Raalupoola Ragamala...`. Compatibility with the library on disk won.

### `youtube_music_manager.py` — the entry point

Layered top to bottom: database functions → file operations → workers → `main()`.

### `repair.py` — the reconciler

Run when the database and the disk have diverged. Dry-run by default; only
`--apply` writes. Imports from the manager *inside functions* so the manager can
lazily import `run_repair` without a circular import at module load.

### `organize_albums.py` — the safety net

The downloader now writes straight into album folders, so this is no longer a
required step. It exists to sweep up files left in the flat `Songs/` root by
older runs or moved by hand.

---

## 3. Database schema

```sql
CREATE TABLE tracks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT,
    artist        TEXT,
    album         TEXT,
    image_url     TEXT,      -- where cover art comes from
    image_blob    BLOB,      -- cached cover bytes; always JPEG or PNG
    youtube_link  TEXT UNIQUE,   -- the identity key for JSON ⇄ DB matching
    status        TEXT,      -- see the state machine below
    error         TEXT,      -- last failure message
    file_hash     TEXT,      -- SHA-256 of the finished file
    file_path     TEXT       -- RELATIVE to the project root
);

CREATE TABLE activity_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id   INTEGER,
    action     TEXT,
    details    TEXT,
    timestamp  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_tracks_file_hash ON tracks (file_hash);
CREATE INDEX idx_tracks_status    ON tracks (status);
CREATE INDEX idx_log_track_id     ON activity_log (track_id);
```

**`youtube_link` is the identity key.** Not the title, not the id. It is the only
field guaranteed stable when you correct a title or album in the JSON, so it is
what `sync_json_to_db()` matches on and what carries a `UNIQUE` constraint.

**`file_path` is relative.** Always write it through `store_path()` and read it
through `resolve_path()`. `update_track_record()` applies `store_path()` for you.

**`image_blob` caches cover art** so re-tagging does not re-hit the network. It is
only ever populated with verified JPEG or PNG bytes — see §6.

---

## 4. The status state machine

```
   new in JSON
        │
        ▼
     pending ─────────────► processing ─────────────► downloaded
        ▲                        │                         │
        │                        │                         │
        │  requeued at startup   │  error                  │
        └────────────────────────┤                         │
        │   (crash / Ctrl-C)     ▼                         │
        │                     failed                       │
        └────────────────────────┘                         │
             retried next run                              │
                                                           │
                          metadata edited in JSON          │
                     (title / artist / album / image_url)  │
                                    │                      │
                                    ▼                      │
                              needs_retag ◄────────────────┘
                                    │
                    refile + retag, never re-downloaded
                                    │
                                    ▼
                               downloaded
```

| Status | Meaning | Picked up next run? |
|---|---|---|
| `pending` | Queued | Yes |
| `processing` | A run is working on it *right now* | Yes — requeued at startup |
| `downloaded` | Complete and verified | No |
| `failed` | Last attempt failed; `error` says why | Yes |
| `needs_retag` | On disk but its metadata changed | Yes — refiled and re-tagged, never re-downloaded |

> **Why `processing` is requeued at startup.** It used to be a dead end. The retry
> query selected only `('pending','failed')`, but a track is marked `processing`
> before its download begins. Any Ctrl-C, crash, or sleep stranded rows there
> permanently: never retried, and absent from the summary. `reset_stranded_tracks()`
> runs before anything else and flips them back to `pending`. This is safe because
> `processing` is only ever written at the start of a download, so a row still
> holding it when the process *starts* provably belongs to a run that already died.

---

## 5. The download pipeline

`process_song()` is the worker. Every step is inside one `try`, so nothing can
escape and leave a row stranded.

```
process_song(track)
│
├─ 1. candidate_paths()      Where might this already be?
│        a. Songs/<Album>/<Title>.m4a      ← canonical
│        b. Songs/<Title>.m4a              ← legacy flat layout
│        c. whatever file_path says        ← whatever the DB remembers
│
├─ 2. verify_audio_file()    For each candidate: exists? >64KB? opens as MP4?
│        └─ found ──► move_into_place() ──► tag ──► hash ──► 'downloaded' ──► RETURN
│                     (no download — this is the deduplication path)
│
├─ 3. yt-dlp download        Only if no candidate survived verification
│
├─ 4. verify_audio_file()    Again — yt-dlp "succeeding" is not proof
│
├─ 5. resolve_cover_bytes()  Cached blob → convert → download → give up
│
└─ 6. finalize_track()       Tag, hash, mark 'downloaded', warn on duplicates
```

### Why the candidate search exists

This is the single most important loop in the codebase. The downloader used to
build one flat path, `Songs/<Title>.m4a`, and check only there. But the organizer
had already moved everything into `Songs/<Album>/`. So every organized track was
invisible to the dedup check and got downloaded again.

The consequence was not just wasted bandwidth. Thirteen tracks were re-fetched
months after their first successful download, by which time YouTube had pulled
the videos — so working local files were marked `failed`. Checking every plausible
location, and *adopting* whatever is found by moving it into place, is what
stops that.

### Why files are verified, not just checked for existence

`os.path.exists()` was treated as proof of a completed download. A file left
behind by a killed ffmpeg run also exists. `verify_audio_file()` requires three
things — present, at least 64 KB, and parseable by mutagen — and returns
`(bool, reason)` so the reason lands in the activity log.

### Why `move_into_place` refuses to overwrite

`shutil.move` onto an existing path destroys the destination. Two tracks sharing
a title within one album would silently eat each other. When the destination is
occupied, the function leaves both files alone and keeps using the one it found.

---

## 6. Cover art handling

MP4 cover art can only carry JPEG or PNG. The old code tagged *everything* as
JPEG regardless of the actual bytes.

```
image_url ──► download_image()
                │
                ├─ HTTP 200?            no ──► reject
                ├─ under 8 MB?          no ──► reject
                ├─ magic bytes JPEG/PNG? yes ─► use as-is
                ├─ WEBP or GIF?         yes ─► ffmpeg ──► JPEG
                └─ anything else              ──► reject (log Content-Type)
```

Three separate hazards are covered here:

1. **A lying URL.** Several cover URLs end in `.jpg` but serve `image/webp`
   (Amazon encodes `_FMwebp_` in the path). One track's art was WEBP bytes tagged
   as JPEG — invisible in strict players. Since ffmpeg is already a hard
   dependency via yt-dlp's post-processing, WEBP and GIF are transcoded rather
   than dropped.
2. **A 200-response error page.** An HTML error body would otherwise be embedded
   as "artwork" *and cached in `image_blob`*, so every retry would reuse the same
   garbage forever.
3. **Format mislabelling.** `build_cover()` sniffs the bytes and picks
   `FORMAT_JPEG` or `FORMAT_PNG` accordingly, returning `None` for anything else
   rather than embedding a lie.

`resolve_cover_bytes()` prefers the cached blob, converts it in place if it is
convertible, and only then goes to the network.

---

## 7. Concurrency and SQLite

Four worker threads (`MAX_WORKERS = 4`), each opening its own connection through
`get_conn()`. Connections are not shared across threads.

Every connection gets the same treatment:

```python
PRAGMA journal_mode = WAL       # readers don't block the writer
PRAGMA busy_timeout = 30000     # wait rather than raise "database is locked"
PRAGMA synchronous = NORMAL     # safe under WAL, much faster
```

> **Why this is centralized.** Connections were previously opened ad hoc — some
> with a 15-second timeout, most with the 5-second default, none with WAL. With
> ~80 MB of cover-art blobs flowing through the same table, a writer could hold
> the lock long enough to raise `database is locked`, which in turn left the
> track stranded in `processing`.

`run_pool()` iterates with `as_completed()` and **calls `future.result()`**. The
original code used `concurrent.futures.wait()` and never touched the results, so
any worker exception vanished silently. Now an unhandled error is printed, logged
as `CRASHED`, and the row is marked `failed`.

---

## 8. Metadata drift and `needs_retag`

`sync_json_to_db()` used to only INSERT. Editing a title in the JSON therefore
had no effect on anything, and the DB kept the stale value forever.

It now UPSERTs on `youtube_link`:

```
For each JSON entry:
  link not in DB       ──► INSERT as 'pending'
  fields all match     ──► skip
  image_url changed    ──► also clear image_blob (the cached art is now wrong)
  anything changed
    and already downloaded ──► update + status = 'needs_retag'
  otherwise            ──► update in place
```

**Every tracked field is a retag trigger.** `title`, `artist` and `album` all end
up as tags inside the file, and `title`/`album` additionally decide where it is
filed; `image_url` decides the embedded cover. So the condition is simply
"already downloaded and something changed":

```python
needs_retag = existing["status"] == "downloaded" and bool(changes)
```

> **Why not just title and album.** The trigger originally listed only those two.
> That silently broke the other half of the metadata: correcting an artist name
> or a cover URL updated the database row while the file kept the old `©ART` tag
> and the old artwork forever — the row was `downloaded`, so nothing ever
> reprocessed it. Worse for `image_url`, since the cache was cleared at the same
> time, leaving *neither* the database nor the file holding the new art. Any
> field that reaches the file must trigger a rewrite of the file.

`needs_retag` rows are handled by `retag_song()` *before* the download pass. It
finds the file, moves it to its new canonical path, rewrites the tags, re-fetches
cover art only if the cache was invalidated, and re-hashes — **the audio is never
re-downloaded**. Only if the file is genuinely gone does it fall back to
`pending`.

### `--retag` — the manual entry point

`retag_on_demand(match, dry_run)` reuses the same `retag_song()` worker but
selects rows by a substring match on title, album or artist instead of by JSON
diff. It re-applies what the **database** holds, so it is the tool for forcing a
file back into agreement with its row after a direct database edit or an external
tagger. It uses cached art and therefore needs no network access at all.

---

## 9. The activity log

Every meaningful step appends a row. It is append-only and is the first place to
look when something behaves unexpectedly.

| Group | Actions |
|---|---|
| Lifecycle | `STARTED`, `COMPLETED`, `FAILED`, `CRASHED`, `REQUEUED` |
| Deduplication | `FILE_EXISTS`, `REJECTED_LOCAL_FILE`, `DUPLICATE_CONTENT` |
| Download | `DOWNLOADING_AUDIO`, `AUDIO_SUCCESS` |
| Tagging | `ADDING_METADATA`, `CALCULATING_HASH` |
| Cover art | `IMAGE_CACHE`, `IMAGE_CACHE_INVALID`, `IMAGE_CONVERTED`, `DOWNLOADING_IMAGE` |
| Metadata sync | `METADATA_SYNCED`, `RETAG_MISSING_FILE`, `RETAG_FAILED` |
| Organizer | `MOVED_TO_ALBUM` |
| Repair | `REPAIRED`, `REPAIR_MOVED`, `REPAIR_REQUEUED`, `REPAIR_METADATA_SYNCED`, `REPAIR_COVER_CLEARED`, `REPAIR_VERIFY_FAILED` |

---

## 10. `repair.py` in detail

```
run_repair(apply_changes, fetch_art)
│
├─ _load_json_metadata()   link → current JSON values
├─ _index_library()        walk Songs/ once: filename → [paths]
│
├─ _plan()                 for every row, decide — but write nothing
│     ├─ apply JSON metadata over the DB values
│     ├─ compute the canonical path from the corrected metadata
│     └─ _locate(): canonical → flat → stored path → search the whole library
│                   by filename (catches hand-renamed folders)
│
├─ _report()               print the full diff
│
└─ if --apply:
      ├─ _backup_database()   via SQLite's backup API (WAL-safe)
      └─ _apply()             commits per track, never in one big transaction
```

Design points worth preserving if you modify it:

- **Dry run is the default.** `--apply` is required to write anything.
- **The backup uses `sqlite3.Connection.backup()`, not `shutil.copy`.** With WAL
  enabled, copying the `.db` file alone can miss committed data sitting in the
  `-wal` sidecar.
- **Per-track commits.** A crash halfway through leaves a consistent database.
- **`_locate()` has a whole-library fallback.** One album folder had been renamed
  by hand (`Moral of the Story_ Chapter 1`), so neither the canonical nor the
  stored path matched. Searching by filename found it.
- **It never downloads audio.** Only `--fetch-art` permits any network access.

---

## 11. Conventions to follow when extending this

1. **Never build a path by hand.** Use `track_relpath()`. Two functions computing
   "where the file goes" is exactly how the downloader and organizer drifted apart.
2. **Store paths through `store_path()`, read through `resolve_path()`.** Absolute
   paths in the database are a latent breakage waiting for a folder rename.
3. **Open connections through `get_conn()`.** Never `sqlite3.connect(DB_PATH)`
   directly — it silently opts out of WAL and the busy timeout.
4. **Any new terminal status must appear in `RETRY_STATUSES`, `print_summary()`,
   or be actively cleared at startup.** A status that no query selects is a hole
   tracks disappear into.
5. **Verify files, don't just stat them.** Use `verify_audio_file()`.
6. **Keep the whole worker body inside its `try`.** An exception raised before
   the `try` leaves the row in `processing`.
7. **Log state transitions to `activity_log`.** It is the only forensic trail.
8. **Never embed unverified bytes as cover art.** Route through
   `detect_image_format()` / `build_cover()`.

---

## 12. Known limitations

- **Duplicate detection is post-hoc.** A file's hash cannot be known before it is
  downloaded, so `find_duplicate_by_hash()` runs *after* the fact: it logs
  `DUPLICATE_CONTENT` and prints a warning, but does not delete anything.
  Automatically removing audio is the wrong default.
- **`sanitize_name` leaves trailing dots** — see §2. Copying the library to a
  Windows or exFAT volume may still need manual attention for a handful of names.
- **No automated test suite.** Behaviour is verified by running `--repair` in dry
  mode and by the manual regression recipes in [USAGE.md](USAGE.md#regression-recipes).
- **`.git` still carries ~137 MB** of historical `downloads.db` snapshots. The
  database is now git-ignored so it stops growing, but reclaiming that space
  requires a history rewrite.
