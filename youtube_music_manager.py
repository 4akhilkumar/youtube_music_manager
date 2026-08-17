import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys

import requests
import yt_dlp
from mutagen import MutagenError
from mutagen.mp4 import MP4, MP4Cover

from common import (
    JSON_PATH,
    SONGS_DIR,
    convert_image_to_jpeg,
    detect_image_format,
    get_conn,
    is_convertible_image,
    resolve_path,
    store_path,
    track_filename,
    track_relpath,
)

# A file smaller than this is a truncated download, not a song.
MIN_AUDIO_BYTES = 64 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
# Four simultaneous downloads tripped YouTube's rate limiter: on 2026-08-17 all
# four workers started at once and three died with HTTP 403 in under 8 seconds,
# while the two that got through took ~60. Two still overlaps the network wait
# with the tagging/hashing tail without bursting.
MAX_WORKERS = 2

# Statuses that should be picked up on the next run.
RETRY_STATUSES = ("pending", "failed")

# Ensure the output directory exists without halting the script
os.makedirs(SONGS_DIR, exist_ok=True)

# ==== Database Functions ====
def init_db():
    """Initializes the database and safely upgrades existing tables."""
    with get_conn() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                artist TEXT,
                album TEXT,
                image_url TEXT,
                image_blob BLOB,
                youtube_link TEXT UNIQUE,
                status TEXT,
                error TEXT,
                file_hash TEXT,
                file_path TEXT
            )
        """)

        # Safely attempt to add the image_blob column if upgrading an older DB
        try:
            cursor.execute("ALTER TABLE tracks ADD COLUMN image_blob BLOB")
        except sqlite3.OperationalError:
            pass # Column already exists

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER,
                action TEXT,
                details TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Supports the duplicate-content lookup after each download.
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tracks_file_hash ON tracks (file_hash)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks (status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_log_track_id ON activity_log (track_id)")
        conn.commit()

def log_activity(track_id, action, details=""):
    """Records a specific action into the activity log."""
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO activity_log (track_id, action, details)
            VALUES (?, ?, ?)
        """, (track_id, action, details))
        conn.commit()

def reset_stranded_tracks():
    """Requeues tracks left mid-flight by an interrupted or crashed run.

    'processing' is only ever written at the start of a download, so any row
    still holding it when the program starts belongs to a run that never
    finished. Without this they are stranded: the retry query ignores them.
    """
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, title FROM tracks WHERE status = 'processing'")
        stranded = cursor.fetchall()
        if stranded:
            cursor.execute("UPDATE tracks SET status = 'pending' WHERE status = 'processing'")
            cursor.executemany("""
                INSERT INTO activity_log (track_id, action, details)
                VALUES (?, 'REQUEUED', 'Stranded in processing by an interrupted run.')
            """, [(row[0],) for row in stranded])
            conn.commit()
    return [row[1] for row in stranded]

def sync_json_to_db():
    """Reads the JSON file, inserting new tracks and refreshing changed metadata."""
    if not os.path.exists(JSON_PATH):
        print(f"Error: {JSON_PATH} not found.")
        sys.exit(1)

    with open(JSON_PATH, "r", encoding="utf-8") as f:
        songs = json.load(f)

    inserted_count = 0
    updated_count = 0
    retag_count = 0

    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        for track in songs:
            youtube_link = track.get("youtube_link")
            if not youtube_link:
                continue

            title = track.get("title") or "Unknown Title"
            artist = track.get("artist") or "Unknown Artist"
            album = track.get("album") or "Unknown Album"
            image_url = track.get("image_url") or ""

            cursor.execute("""
                SELECT id, title, artist, album, image_url, status
                FROM tracks WHERE youtube_link = ?
            """, (youtube_link,))
            existing = cursor.fetchone()

            if not existing:
                cursor.execute("""
                    INSERT INTO tracks (title, artist, album, image_url, youtube_link, status, error)
                    VALUES (?, ?, ?, ?, ?, 'pending', '')
                """, (title, artist, album, image_url, youtube_link))
                inserted_count += 1
                continue

            changes = {}
            if existing["title"] != title:
                changes["title"] = title
            if existing["artist"] != artist:
                changes["artist"] = artist
            if existing["album"] != album:
                changes["album"] = album
            if existing["image_url"] != image_url:
                changes["image_url"] = image_url

            if not changes:
                continue

            # Every tracked field ends up inside the file: title, artist and
            # album become tags, and title/album also decide where it is filed.
            # So any correction to an already-downloaded track means the file
            # has to be rewritten, not just the database row - otherwise the
            # JSON and the audio silently diverge.
            needs_retag = existing["status"] == "downloaded" and bool(changes)

            assignments = ", ".join(f"{column} = ?" for column in changes)
            params = list(changes.values())

            # A new cover URL invalidates the cached image bytes.
            if "image_url" in changes:
                assignments += ", image_blob = NULL"

            if needs_retag:
                assignments += ", status = 'needs_retag'"
                retag_count += 1

            params.append(existing["id"])
            cursor.execute(f"UPDATE tracks SET {assignments} WHERE id = ?", params)
            cursor.execute("""
                INSERT INTO activity_log (track_id, action, details)
                VALUES (?, 'METADATA_SYNCED', ?)
            """, (existing["id"], f"Updated from JSON: {', '.join(sorted(changes))}"))
            updated_count += 1

        conn.commit()

    if inserted_count:
        print(f"Added {inserted_count} new tracks to the database.")
    if updated_count:
        print(f"Refreshed metadata for {updated_count} existing tracks from JSON.")
    if retag_count:
        print(f"{retag_count} downloaded tracks need refiling after the metadata change.")

def update_track_record(track_id, status, error_msg="", file_hash=None, file_path=None, image_blob=None):
    """Updates the track's status and optionally its hash and path."""
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE tracks
            SET status = ?, error = ?,
                file_hash = COALESCE(?, file_hash),
                file_path = COALESCE(?, file_path),
                image_blob = COALESCE(?, image_blob)
            WHERE id = ?
        """, (status, error_msg, file_hash, store_path(file_path), image_blob, track_id))
        conn.commit()

def get_tracks_by_status(statuses):
    """Retrieves tracks in any of the given statuses."""
    placeholders = ", ".join("?" for _ in statuses)
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM tracks WHERE status IN ({placeholders})", tuple(statuses))
        return [dict(row) for row in cursor.fetchall()]

def find_tracks_to_retag(match):
    """Downloaded tracks whose title, album or artist contains `match`.

    An empty match selects every downloaded track.
    """
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        if match:
            pattern = f"%{match}%"
            cursor.execute("""
                SELECT * FROM tracks
                WHERE status = 'downloaded'
                  AND (title LIKE ? OR album LIKE ? OR artist LIKE ?)
                ORDER BY album, title
            """, (pattern, pattern, pattern))
        else:
            cursor.execute("SELECT * FROM tracks WHERE status = 'downloaded' ORDER BY album, title")
        return [dict(row) for row in cursor.fetchall()]

def find_duplicate_by_hash(file_hash, exclude_id):
    """Returns another downloaded track holding byte-identical audio, if any."""
    if not file_hash:
        return None
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, album FROM tracks
            WHERE file_hash = ? AND status = 'downloaded' AND id != ?
            LIMIT 1
        """, (file_hash, exclude_id))
        row = cursor.fetchone()
        return dict(row) if row else None

# ==== File Operations ====
def calculate_file_hash(filepath):
    """Calculates the SHA-256 hash of a file."""
    sha256_hash = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            for byte_block in iter(lambda: f.read(65536), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()
    except OSError as e:
        print(f"Error calculating hash for {filepath}: {e}")
        return None

def verify_audio_file(path):
    """Checks that a path holds a complete, readable m4a.

    A file left behind by a killed ffmpeg run still exists on disk, so mere
    existence is not evidence that a download finished.
    """
    if not path or not os.path.isfile(path):
        return False, "missing"
    if os.path.getsize(path) < MIN_AUDIO_BYTES:
        return False, f"truncated ({os.path.getsize(path)} bytes)"
    try:
        MP4(path)
    except (MutagenError, OSError) as e:
        return False, f"unreadable ({e})"
    return True, "ok"

def candidate_paths(track):
    """Every place a track's audio might already be sitting, best first."""
    canonical = resolve_path(track_relpath(track["title"], track["album"]))
    candidates = [canonical]

    # Older runs wrote into the flat Songs/ root before the organizer moved
    # files into album folders.
    candidates.append(os.path.join(SONGS_DIR, track_filename(track["title"])))

    stored = resolve_path(track.get("file_path"))
    if stored:
        candidates.append(stored)

    seen = set()
    unique = []
    for path in candidates:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return canonical, unique

def move_into_place(source, destination):
    """Moves a found file to its canonical location without clobbering anything."""
    if os.path.normpath(source) == os.path.normpath(destination):
        return destination
    if os.path.exists(destination):
        # Something is already there; leave both files alone and keep using
        # the copy we found rather than destroying either one.
        return source
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    shutil.move(source, destination)
    return destination

def download_image(url):
    """Downloads the album cover image, rejecting anything that is not usable art."""
    if not url:
        return None
    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            print(f"Image download failed for {url} (HTTP {response.status_code})")
            return None

        content = response.content
        if len(content) > MAX_IMAGE_BYTES:
            print(f"Image rejected for {url} (too large: {len(content)} bytes)")
            return None

        if detect_image_format(content) is not None:
            return content

        # Several cover URLs end in .jpg but actually serve WEBP, which MP4
        # cover art cannot carry.
        if is_convertible_image(content):
            converted = convert_image_to_jpeg(content)
            if converted:
                print(f"Converted cover art to JPEG for {url}")
                return converted

        # A server can return HTTP 200 with an error page; embedding that as
        # cover art would poison the cached blob permanently.
        content_type = response.headers.get("Content-Type", "unknown")
        print(f"Image rejected for {url} (not usable as cover art, Content-Type: {content_type})")
        return None
    except requests.RequestException as e:
        print(f"Image download failed for {url} ({e})")
    return None

def build_cover(image_bytes):
    """Wraps image bytes in an MP4Cover tagged with their real format."""
    image_format = detect_image_format(image_bytes)
    if image_format == "jpeg":
        return MP4Cover(image_bytes, imageformat=MP4Cover.FORMAT_JPEG)
    if image_format == "png":
        return MP4Cover(image_bytes, imageformat=MP4Cover.FORMAT_PNG)
    return None

def add_metadata(file_path, title, artist, album, image_bytes):
    """Injects metadata into the .m4a file."""
    try:
        audio = MP4(file_path)
        audio["\xa9nam"] = [title]
        audio["\xa9ART"] = [artist]
        audio["\xa9alb"] = [album]

        cover = build_cover(image_bytes)
        if cover is not None:
            audio["covr"] = [cover]
        elif image_bytes:
            print(f"Skipping cover art for '{title}': unsupported image format.")

        audio.save()
        return True
    except (MutagenError, OSError) as e:
        raise RuntimeError(f"Metadata error: {e}") from e

def resolve_cover_bytes(track):
    """Returns usable cover bytes from the cache, or downloads them."""
    cached = track.get("image_blob")
    if cached and detect_image_format(cached) is not None:
        log_activity(track["id"], "IMAGE_CACHE", "Using cached album image from database.")
        return cached, False

    if cached:
        # Convert in place when we can, rather than going back to the network.
        converted = convert_image_to_jpeg(cached) if is_convertible_image(cached) else None
        if converted:
            log_activity(track["id"], "IMAGE_CONVERTED", "Cached image converted to JPEG.")
            return converted, True
        log_activity(track["id"], "IMAGE_CACHE_INVALID", "Cached image is not usable; refetching.")

    if track.get("image_url"):
        log_activity(track["id"], "DOWNLOADING_IMAGE", f"Fetching image from {track['image_url']}")
        return download_image(track["image_url"]), True

    return None, False

def finalize_track(track, file_path, image_bytes):
    """Tags the file, hashes it, and records the result."""
    track_id = track["id"]

    log_activity(track_id, "ADDING_METADATA", "Writing tags and cover art.")
    add_metadata(file_path, track["title"], track["artist"], track["album"], image_bytes)

    log_activity(track_id, "CALCULATING_HASH", "Generating SHA-256 hash.")
    final_hash = calculate_file_hash(file_path)

    update_track_record(
        track_id,
        "downloaded",
        file_hash=final_hash,
        file_path=file_path,
        image_blob=image_bytes,
    )

    duplicate = find_duplicate_by_hash(final_hash, track_id)
    if duplicate:
        log_activity(
            track_id,
            "DUPLICATE_CONTENT",
            f"Byte-identical to '{duplicate['title']}' ({duplicate['album']}).",
        )
        print(f"⚠️  Duplicate audio: '{track['title']}' matches '{duplicate['title']}'")

    log_activity(track_id, "COMPLETED", f"Finished processing with hash: {final_hash}")
    return final_hash

# ==== Core Worker Functions ====
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

# Throttling looks identical to a dead video in the log unless it is called out.
TRANSIENT_ERROR_HINTS = (
    "http error 403",
    "http error 429",
    "too many requests",
    "temporary failure",
    "connection reset",
    "timed out",
    "read timeout",
)

def describe_error(exc):
    """Strips terminal colour codes and flags errors that are worth retrying."""
    message = ANSI_ESCAPE.sub("", str(exc)).strip()
    transient = any(hint in message.lower() for hint in TRANSIENT_ERROR_HINTS)
    if transient:
        return f"[transient] {message}", True
    return message, False

def build_ydl_opts(file_path):
    return {
        'format': 'bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best',
        'outtmpl': file_path,
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        # Four workers interleaving progress bars is unreadable, and newer
        # yt-dlp releases print them even under quiet.
        'noprogress': True,
        'overwrites': True,  # Tells yt-dlp to overwrite existing files
        # 'cookiefile': os.path.join(common.BASE_DIR, 'cookies.txt'),
        'sleep_interval_requests': 1,
        'sleep_interval': 3,
        'max_sleep_interval': 8,
        # Only 'deno' is enabled by default, and without a runtime yt-dlp drops to
        # a deprecated extraction path that warns "some formats may be missing".
        # Naming both keeps deno's priority if it ever gets installed. Note the
        # Python API wants {runtime: config}, not the list --js-runtimes takes.
        'js_runtimes': {'deno': {}, 'node': {}},
        # A 403 here is usually YouTube throttling, not a dead video, so it is
        # worth waiting out. Without these yt-dlp gives up on the first refusal
        # and the track is marked failed for the whole run.
        'retries': 5,
        'extractor_retries': 3,
        'fragment_retries': 10,
        'retry_sleep_functions': {
            'http': lambda n: min(4 * (2 ** n), 60),
            'fragment': lambda n: min(2 * (2 ** n), 30),
        },
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'm4a',
            'preferredquality': '0'
        }],
        'postprocessor_args': [
            '-y'  # Tells FFmpeg to overwrite without prompting [y/N]
        ],
    }

def process_song(track):
    """Handles deduplication, downloading, tagging and logging for one track."""
    track_id = track["id"]
    title = track["title"]

    try:
        canonical_path, candidates = candidate_paths(track)

        update_track_record(track_id, "processing")
        log_activity(track_id, "STARTED", f"Began processing track: {title}")
        print(f"🎶 Started: {title}")

        # Deduplication Check & Database Healing.
        # The organizer files tracks under Songs/<album>/, so a song already on
        # disk will not be at the flat path older versions looked at. Checking
        # every candidate is what stops good downloads being fetched again.
        for candidate in candidates:
            is_valid, reason = verify_audio_file(candidate)
            if not is_valid:
                if reason != "missing":
                    log_activity(track_id, "REJECTED_LOCAL_FILE", f"{candidate}: {reason}")
                continue

            log_activity(track_id, "FILE_EXISTS", f"Found usable audio at {candidate}.")
            final_path = move_into_place(candidate, canonical_path)
            image_bytes, _ = resolve_cover_bytes(track)
            finalize_track(track, final_path, image_bytes)
            print(f"⏩ Skipped (already on disk, DB updated): {title}")
            return "skipped"

        os.makedirs(os.path.dirname(canonical_path), exist_ok=True)

        log_activity(track_id, "DOWNLOADING_AUDIO", f"Downloading from {track['youtube_link']}")
        with yt_dlp.YoutubeDL(build_ydl_opts(canonical_path)) as ydl:
            ydl.download([track["youtube_link"]])
        log_activity(track_id, "AUDIO_SUCCESS", "Audio downloaded successfully.")

        is_valid, reason = verify_audio_file(canonical_path)
        if not is_valid:
            raise FileNotFoundError(f"Audio unusable after yt-dlp finished: {reason}")

        image_bytes, _ = resolve_cover_bytes(track)
        finalize_track(track, canonical_path, image_bytes)
        print(f"✅ Completed: {title}")
        return "downloaded"

    except Exception as e:
        error_msg, transient = describe_error(e)
        try:
            update_track_record(track_id, "failed", error_msg)
            log_activity(track_id, "FAILED", error_msg)
        except sqlite3.Error as db_error:
            # Never let a logging failure leave the row stuck in 'processing'.
            print(f"⚠️  Could not record failure for {title}: {db_error}")
        if transient:
            print(f"⏳ Throttled: {title} ({error_msg}) — will retry on the next run")
        else:
            print(f"❌ Failed: {title} ({error_msg})")
        return "failed"

def retag_song(track):
    """Refiles and retags a downloaded track after its JSON metadata changed."""
    track_id = track["id"]
    title = track["title"]

    try:
        canonical_path, candidates = candidate_paths(track)

        for candidate in candidates:
            is_valid, _ = verify_audio_file(candidate)
            if not is_valid:
                continue

            final_path = move_into_place(candidate, canonical_path)
            image_bytes, _ = resolve_cover_bytes(track)
            finalize_track(track, final_path, image_bytes)
            print(f"🏷️  Retagged: {title}")
            return "retagged"

        # The file is genuinely gone, so it has to be fetched again.
        log_activity(track_id, "RETAG_MISSING_FILE", "No audio found; requeued for download.")
        update_track_record(track_id, "pending")
        print(f"↩️  Requeued (file missing): {title}")
        return "requeued"

    except Exception as e:
        error_msg = str(e)
        update_track_record(track_id, "failed", error_msg)
        log_activity(track_id, "RETAG_FAILED", error_msg)
        print(f"❌ Retag failed: {title} ({error_msg})")
        return "failed"

def run_pool(tracks, worker, label):
    """Runs a worker over tracks, surfacing any exception it fails to handle."""
    print(f"\n🎵 {label}: {len(tracks)} track(s).\n")
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(worker, track): track for track in tracks}
        for future in concurrent.futures.as_completed(futures):
            track = futures[future]
            try:
                # Calling result() is what makes a worker crash visible; without
                # it the exception is swallowed and the row stays 'processing'.
                future.result()
            except Exception as e:
                print(f"💥 Unhandled error on '{track['title']}': {e}")
                try:
                    update_track_record(track["id"], "failed", f"Unhandled error: {e}")
                    log_activity(track["id"], "CRASHED", str(e))
                except sqlite3.Error:
                    pass

def retag_on_demand(match, dry_run=False):
    """Rewrites tags and refiles selected tracks from what the database holds.

    Use after editing metadata directly in the database, or to force a file
    back into agreement with its row. Uses cached cover art, so it needs no
    network access.
    """
    tracks = find_tracks_to_retag(match)
    scope = f"matching {match!r}" if match else "in the library"

    if not tracks:
        print(f"\nNo downloaded tracks {scope}.\n")
        return

    print(f"\n{len(tracks)} track(s) {scope}:\n")
    for track in tracks:
        destination = track_relpath(track["title"], track["album"])
        moving = "" if store_path(track["file_path"]) == destination else "  ← will move"
        print(f"  {track['artist']} - {track['title']} ({track['album']}){moving}")
        if not moving:
            continue
        print(f"      {track['file_path']}  ->  {destination}")

    if dry_run:
        print("\nDry run - nothing was changed. Drop --dry-run to apply.\n")
        return

    run_pool(tracks, retag_song, "Re-tagging")
    print_summary()

def print_summary():
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, COUNT(*) FROM tracks GROUP BY status")
        summary = dict(cursor.fetchall())
        total = sum(summary.values())

    labels = {
        "downloaded": "✅ Downloaded",
        "failed": "❌ Failed",
        "pending": "⏳ Pending",
        "processing": "🔄 Processing (interrupted)",
        "needs_retag": "🏷️  Awaiting retag",
    }

    print("\n==== SUMMARY ====")
    for status in sorted(summary):
        print(f"{labels.get(status, status)}: {summary[status]}")
    print(f"📊 Total tracks: {total}")
    print(f"🎵 Audio files located in: {SONGS_DIR}")
    print("=================\n")

# ==== Main Execution ====
def main():
    parser = argparse.ArgumentParser(description="Download and tag the tracks listed in songs_meta_data.json.")
    parser.add_argument("--repair", action="store_true",
                        help="Reconcile the database with the files on disk. Reports only unless --apply is given.")
    parser.add_argument("--apply", action="store_true",
                        help="With --repair, write the proposed changes to the database.")
    parser.add_argument("--fetch-art", action="store_true",
                        help="With --repair, allow re-downloading cover art that is cached in an unusable format.")
    parser.add_argument("--retag", nargs="?", const="", metavar="MATCH",
                        help="Rewrite tags and refile downloaded tracks whose title, album or "
                             "artist contains MATCH. Omit MATCH to re-tag everything.")
    parser.add_argument("--dry-run", action="store_true",
                        help="With --retag, list what would be re-tagged without changing anything.")
    args = parser.parse_args()

    init_db()

    if args.repair:
        from repair import run_repair
        run_repair(apply_changes=args.apply, fetch_art=args.fetch_art)
        return

    if args.retag is not None:
        retag_on_demand(args.retag, dry_run=args.dry_run)
        return

    requeued = reset_stranded_tracks()
    if requeued:
        print(f"Requeued {len(requeued)} track(s) stranded by a previous run: {', '.join(requeued)}")

    sync_json_to_db()

    retag_tracks = get_tracks_by_status(["needs_retag"])
    if retag_tracks:
        run_pool(retag_tracks, retag_song, "Refiling tracks whose metadata changed")

    tracks_to_process = get_tracks_by_status(RETRY_STATUSES)
    if tracks_to_process:
        run_pool(tracks_to_process, process_song, "Processing tracks")
    elif not retag_tracks:
        print("\n✅ All tracks are already downloaded or no pending tracks found.\n")

    print_summary()

if __name__ == "__main__":
    main()
