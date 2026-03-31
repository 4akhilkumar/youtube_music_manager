import concurrent.futures
import hashlib
import json
import os
import re
import sqlite3
import sys

import requests
import yt_dlp
from mutagen.mp4 import MP4, MP4Cover

# ==== Configuration & Paths ====
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SONGS_DIR = os.path.join(BASE_DIR, "Songs")
JSON_PATH = os.path.join(BASE_DIR, "songs_meta_data.json")
DB_PATH = os.path.join(BASE_DIR, "downloads.db")

# Ensure the output directory exists without halting the script
os.makedirs(SONGS_DIR, exist_ok=True)

# ==== Helper Functions ====
def sanitize_folder_name(name):
    """Removes invalid characters for OS folder names."""
    if not name or name.strip() == "":
        return "Unknown Album"
    sanitized = re.sub(r'[\\/*?:"<>|]', "", name)
    return sanitized.strip()

# ==== Database Functions ====
def init_db():
    """Initializes the database and safely upgrades existing tables."""
    with sqlite3.connect(DB_PATH) as conn:
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
        conn.commit()

def log_activity(track_id, action, details=""):
    """Records a specific action into the activity log."""
    with sqlite3.connect(DB_PATH, timeout=15) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO activity_log (track_id, action, details)
            VALUES (?, ?, ?)
        """, (track_id, action, details))
        conn.commit()

def sync_json_to_db():
    """Reads the JSON file and inserts new tracks into the database."""
    if not os.path.exists(JSON_PATH):
        print(f"Error: {JSON_PATH} not found.")
        sys.exit(1)

    with open(JSON_PATH, "r", encoding="utf-8") as f:
        songs = json.load(f)

    inserted_count = 0
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        for track in songs:
            # if not track.get("download", False):
            #     continue

            youtube_link = track.get("youtube_link")
            if not youtube_link:
                continue

            cursor.execute("SELECT id FROM tracks WHERE youtube_link = ?", (youtube_link,))
            if not cursor.fetchone():
                cursor.execute("""
                    INSERT INTO tracks (title, artist, album, image_url, youtube_link, status, error)
                    VALUES (?, ?, ?, ?, ?, 'pending', '')
                """, (
                    track.get("title", "unknown_title"),
                    track.get("artist", "Unknown Artist"),
                    track.get("album", "Unknown Album"),
                    track.get("image_url", ""),
                    youtube_link
                ))
                inserted_count += 1
        conn.commit()

    if inserted_count > 0:
        print(f"Added {inserted_count} new tracks to the database.")

def update_track_record(track_id, status, error_msg="", file_hash=None, file_path=None, image_blob=None):
    """Updates the track's status and optionally its hash and path."""
    with sqlite3.connect(DB_PATH, timeout=15) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE tracks
            SET status = ?, error = ?,
                file_hash = COALESCE(?, file_hash),
                file_path = COALESCE(?, file_path),
                image_blob = COALESCE(?, image_blob)
            WHERE id = ?
        """, (status, error_msg, file_hash, file_path, image_blob, track_id))
        conn.commit()

def get_pending_tracks():
    """Retrieves tracks that need processing."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tracks WHERE status IN ('pending', 'failed')")
        return [dict(row) for row in cursor.fetchall()]

def is_hash_in_db(file_hash):
    """Checks if a specific file hash already exists as a downloaded file."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM tracks WHERE file_hash = ? AND status = 'downloaded'", (file_hash,))
        return cursor.fetchone() is not None

# ==== File Operations ====
def calculate_file_hash(filepath):
    """Calculates the SHA-256 hash of a file."""
    sha256_hash = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()
    except Exception as e:
        print(f"Error calculating hash for {filepath}: {e}")
        return None

def download_image(url):
    """Downloads the album cover image."""
    if not url:
        return None
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return response.content
    except Exception as e:
        print(f"Image download failed for {url} ({e})")
    return None

def add_metadata(file_path, title, artist, album, image_bytes):
    """Injects metadata into the .m4a file."""
    try:
        audio = MP4(file_path)
        audio["\xa9nam"] = [title]
        audio["\xa9ART"] = [artist]
        audio["\xa9alb"] = [album]

        if image_bytes:
            cover = MP4Cover(image_bytes, imageformat=MP4Cover.FORMAT_JPEG)
            audio["covr"] = [cover]

        audio.save()
        return True
    except Exception as e:
        raise Exception(f"Metadata error: {e}")

# ==== Core Worker Function ====
def process_song(track):
    """Handles logic, deduplication, downloading, and logging for one track."""
    track_id = track["id"]
    title = track["title"].replace("/", "-").strip()
    album_name = sanitize_folder_name(track["album"])
    url = track["youtube_link"]

    update_track_record(track_id, "processing")
    log_activity(track_id, "STARTED", f"Began processing track: {title}")
    print(f"🎶 Started: {title}")

    # Create the album directory dynamically
    # album_dir = os.path.join(SONGS_DIR, album_name)
    # os.makedirs(album_dir, exist_ok=True)
    # file_path = os.path.join(album_dir, f"{title}.m4a")

    file_path = os.path.join(SONGS_DIR, f"{title}.m4a")

    # Deduplication Check & Database Healing
    if os.path.exists(file_path):
        log_activity(track_id, "CHECKING_LOCAL_FILE", "File exists locally.")
        existing_hash = calculate_file_hash(file_path)

        if existing_hash:
            # If the file is on disk, assume it is complete and heal the database
            log_activity(track_id, "FILE_EXISTS", f"Updating DB with hash {existing_hash}.")
            update_track_record(track_id, "downloaded", file_hash=existing_hash, file_path=file_path)
            print(f"⏩ Skipped (file already on disk, DB updated): {title}")
            return

    opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best',
        'outtmpl': file_path,
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'overwrites': True,  # Tells yt-dlp to overwrite existing files
        # 'cookiefile': os.path.join(BASE_DIR, 'cookies.txt'),
        'sleep_interval_requests': 1,
        'sleep_interval': 3,
        'max_sleep_interval': 8,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'm4a',
            'preferredquality': '0'
        }],
        'postprocessor_args': [
            '-y'  # Tells FFmpeg to overwrite without prompting [y/N]
        ],
    }

    try:
        # 1. Download audio
        log_activity(track_id, "DOWNLOADING_AUDIO", f"Downloading from {url}")
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        log_activity(track_id, "AUDIO_SUCCESS", "Audio downloaded successfully.")

        # 2. Handle Image Data (Cache vs Download)
        img_bytes = track.get("image_blob")
        if img_bytes:
            log_activity(track_id, "IMAGE_CACHE", "Using cached album image from database.")
        elif track.get("image_url"):
            log_activity(track_id, "DOWNLOADING_IMAGE", f"Fetching image from {track['image_url']}")
            img_bytes = download_image(track["image_url"])

        # 3. Add metadata
        if os.path.exists(file_path):
            log_activity(track_id, "ADDING_METADATA", "Writing ID3 tags and cover art.")
            add_metadata(file_path, title, track["artist"], track["album"], img_bytes)

            # 4. Hash final file and update DB (Saving the image blob if newly downloaded)
            log_activity(track_id, "CALCULATING_HASH", "Generating SHA-256 hash.")
            final_hash = calculate_file_hash(file_path)

            update_track_record(
                track_id,
                "downloaded",
                file_hash=final_hash,
                file_path=file_path,
                image_blob=img_bytes
            )
            log_activity(track_id, "COMPLETED", f"Finished processing with hash: {final_hash}")
            print(f"✅ Completed: {title}")
        else:
            raise FileNotFoundError("File not found on disk after yt-dlp completed.")

    except Exception as e:
        error_msg = str(e)
        update_track_record(track_id, "failed", error_msg)
        log_activity(track_id, "FAILED", error_msg)
        print(f"❌ Failed: {title} ({error_msg})")

# ==== Main Execution ====
def main():
    init_db()
    sync_json_to_db()

    tracks_to_process = get_pending_tracks()
    if not tracks_to_process:
        print("\n✅ All tracks are already downloaded or no pending tracks found.\n")
        return

    print(f"\n🎵 Found {len(tracks_to_process)} tracks to process.\n")

    max_workers = 4

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_song, track) for track in tracks_to_process]
        concurrent.futures.wait(futures)

    # Print summary
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, COUNT(*) FROM tracks GROUP BY status")
        summary = dict(cursor.fetchall())

    print("\n==== SUMMARY ====")
    print(f"✅ Downloaded: {summary.get('downloaded', 0)}")
    print(f"❌ Failed: {summary.get('failed', 0)}")
    print(f"⏳ Pending: {summary.get('pending', 0)}")
    print(f"🎵 Audio files located in: {SONGS_DIR}")
    print("=================\n")

if __name__ == "__main__":
    main()
