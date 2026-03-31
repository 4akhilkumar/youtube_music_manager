import os
import re
import shutil
import sqlite3

# ==== Configuration & Paths ====
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SONGS_DIR = os.path.join(BASE_DIR, "Songs")
DB_PATH = os.path.join(BASE_DIR, "downloads.db")

def sanitize_folder_name(name):
    """Removes invalid characters for OS folder names."""
    if not name or name.strip() == "":
        return "Unknown Album"

    # Replace invalid characters (like / \ : * ? " < > |) with nothing
    sanitized = re.sub(r'[\\/*?:"<>|]', "", name)
    return sanitized.strip()

def organize_songs_by_album():
    if not os.path.exists(DB_PATH):
        print(f"Error: Database not found at {DB_PATH}")
        return

    moved_count = 0
    skipped_count = 0

    with sqlite3.connect(DB_PATH) as conn:
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
            title = track["title"].replace("/", "-").strip()
            album_name = track["album"]
            current_path = track["file_path"]

            # Skip if the file path is missing or the file doesn't actually exist
            if not current_path or not os.path.exists(current_path):
                print(f"Warning: File not found for '{title}' at {current_path}. Skipping.")
                skipped_count += 1
                continue

            # Create a safe folder name for the album
            safe_album_name = sanitize_folder_name(album_name)
            album_dir = os.path.join(SONGS_DIR, safe_album_name)

            # Create the album directory if it does not exist
            os.makedirs(album_dir, exist_ok=True)

            # Define the new target path
            filename = os.path.basename(current_path)
            new_path = os.path.join(album_dir, filename)

            # Move the file if it is not already in the correct folder
            if current_path != new_path:
                try:
                    shutil.move(current_path, new_path)

                    # Update the database with the new file path
                    cursor.execute("UPDATE tracks SET file_path = ? WHERE id = ?", (new_path, track_id))

                    # Log the activity
                    cursor.execute("""
                        INSERT INTO activity_log (track_id, action, details)
                        VALUES (?, ?, ?)
                    """, (track_id, "MOVED_TO_ALBUM", f"Moved to {album_dir}"))

                    print(f"Moved: '{title}' -> {safe_album_name}/")
                    moved_count += 1
                except Exception as e:
                    print(f"Error moving '{title}': {e}")
                    skipped_count += 1
            else:
                # File is already in the correct album folder
                skipped_count += 1

        conn.commit()

    print("\n==== ORGANIZATION SUMMARY ====")
    print(f"Total processed: {len(tracks)}")
    print(f"Successfully moved: {moved_count}")
    print(f"Skipped (already in place or missing): {skipped_count}")
    print("==============================\n")

if __name__ == "__main__":
    organize_songs_by_album()
