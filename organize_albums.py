"""Files downloaded tracks into Songs/<album>/ folders.

The downloader now writes straight into the album folder, so this is a
safety net for tracks left in the flat Songs/ root by older runs or moved
by hand - not a required second step.
"""

import os
import shutil
import sqlite3

from common import SONGS_DIR, album_dirname, get_conn, resolve_path, store_path, track_filename

def organize_songs_by_album():
    moved_count = 0
    skipped_count = 0
    missing_count = 0
    conflict_count = 0

    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Fetch all successfully downloaded tracks
        cursor.execute("SELECT id, title, album, file_path FROM tracks WHERE status = 'downloaded'")
        tracks = cursor.fetchall()

        if not tracks:
            print("No downloaded tracks found in the database to organize.")
            return

        print(f"Found {len(tracks)} downloaded tracks. Organizing by album...\n")

        for track in tracks:
            track_id = track["id"]
            title = track["title"]
            current_path = resolve_path(track["file_path"])

            album_dir = os.path.join(SONGS_DIR, album_dirname(track["album"]))
            new_path = os.path.join(album_dir, track_filename(title))

            # Already filed correctly - by far the common case now.
            if current_path and os.path.normpath(current_path) == os.path.normpath(new_path) \
                    and os.path.exists(new_path):
                skipped_count += 1
                continue

            # Skip if the file path is missing or the file doesn't actually exist
            if not current_path or not os.path.exists(current_path):
                print(f"Warning: File not found for '{title}' at {track['file_path']}. Skipping.")
                missing_count += 1
                continue

            # Never overwrite: two tracks sharing a title within one album would
            # otherwise silently destroy each other's audio.
            if os.path.exists(new_path):
                print(f"Conflict: '{title}' - {store_path(new_path)} already exists. Leaving in place.")
                conflict_count += 1
                continue

            try:
                os.makedirs(album_dir, exist_ok=True)
                shutil.move(current_path, new_path)

                # Commit per file. Moving everything and committing once at the
                # end leaves the database describing the old layout if the run
                # is interrupted partway through.
                cursor.execute("UPDATE tracks SET file_path = ? WHERE id = ?",
                               (store_path(new_path), track_id))
                cursor.execute("""
                    INSERT INTO activity_log (track_id, action, details)
                    VALUES (?, ?, ?)
                """, (track_id, "MOVED_TO_ALBUM", f"Moved to {store_path(album_dir)}"))
                conn.commit()

                print(f"Moved: '{title}' -> {os.path.basename(album_dir)}/")
                moved_count += 1
            except (OSError, sqlite3.Error) as e:
                conn.rollback()
                print(f"Error moving '{title}': {e}")
                skipped_count += 1

    print("\n==== ORGANIZATION SUMMARY ====")
    print(f"Total processed: {len(tracks)}")
    print(f"Successfully moved: {moved_count}")
    print(f"Already in place: {skipped_count}")
    print(f"Missing files: {missing_count}")
    print(f"Name conflicts: {conflict_count}")
    print("==============================\n")

if __name__ == "__main__":
    organize_songs_by_album()
