"""One-time reconciliation between the database and the files on disk.

The database recorded absolute paths, so renaming the project folder invalidated
every row at once. Separately, tracks that were downloaded and then filed into
album folders became invisible to the downloader's flat-path lookup, which
re-fetched them and marked working songs as failed when YouTube had since pulled
the video. This module walks the library, matches every row to its real file,
and repairs the paths, statuses and hashes without downloading any audio.

Reports only by default; pass --apply to write.
"""

import json
import os
import sqlite3
from collections import defaultdict

from common import (
    DB_PATH,
    JSON_PATH,
    SONGS_DIR,
    detect_image_format,
    get_conn,
    resolve_path,
    store_path,
    track_filename,
    track_relpath,
)

BACKUP_PATH = DB_PATH + ".bak"

def _load_json_metadata():
    """Maps youtube_link to the current JSON metadata."""
    if not os.path.exists(JSON_PATH):
        return {}
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        songs = json.load(f)
    return {s["youtube_link"]: s for s in songs if s.get("youtube_link")}

def _index_library():
    """Maps every audio filename in the library to the paths that carry it."""
    index = defaultdict(list)
    for root, _dirs, files in os.walk(SONGS_DIR):
        for name in files:
            if name.lower().endswith(".m4a"):
                index[name].append(os.path.join(root, name))
    return index

def _locate(track, canonical_path, library_index):
    """Finds the track's real audio file, most trustworthy location first."""
    from youtube_music_manager import verify_audio_file

    candidates = [
        canonical_path,
        os.path.join(SONGS_DIR, track_filename(track["title"])),
        resolve_path(track.get("file_path")),
    ]

    # Last resort: the file may sit in a folder that was renamed by hand, so
    # look it up by filename anywhere in the library.
    candidates.extend(library_index.get(os.path.basename(canonical_path), []))

    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        is_valid, _reason = verify_audio_file(candidate)
        if is_valid:
            return candidate
    return None

def _plan(conn, json_meta, library_index):
    """Builds the list of changes without touching anything."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM tracks").fetchall()

    plans = []
    for row in rows:
        track = dict(row)
        source = json_meta.get(track["youtube_link"], {})

        metadata_changes = {}
        for field in ("title", "artist", "album", "image_url"):
            new_value = source.get(field)
            if new_value and new_value != track[field]:
                metadata_changes[field] = new_value
                track[field] = new_value

        canonical_path = resolve_path(track_relpath(track["title"], track["album"]))
        found = _locate(track, canonical_path, library_index)

        blob = track.get("image_blob")
        cover_invalid = bool(blob) and detect_image_format(blob) is None
        # No usable art cached at all, but a URL to fetch it from. True for
        # tracks that failed before they ever reached the tagging step.
        cover_missing = not blob and bool(track["image_url"])

        plans.append({
            "id": track["id"],
            "title": track["title"],
            "album": track["album"],
            "artist": track["artist"],
            "image_url": track["image_url"],
            "image_blob": blob,
            "old_status": track["status"],
            "old_path": row["file_path"],
            "found": found,
            "canonical": canonical_path,
            "needs_move": bool(found) and os.path.normpath(found) != os.path.normpath(canonical_path),
            "metadata_changes": metadata_changes,
            "cover_invalid": cover_invalid,
            "cover_missing": cover_missing,
            "needs_hash": bool(found) and not track["file_hash"],
            "path_stale": bool(found) and store_path(found) != row["file_path"],
        })
    return plans

def _report(plans, apply_changes, fetch_art):
    located = [p for p in plans if p["found"]]
    missing = [p for p in plans if not p["found"]]
    promotions = [p for p in located if p["old_status"] != "downloaded"]
    path_fixes = [p for p in located if p["path_stale"]]
    moves = [p for p in located if p["needs_move"]]
    retags = [p for p in located if p["metadata_changes"]]
    covers = [p for p in plans if p["cover_invalid"]]
    artless = [p for p in located if p["cover_missing"] and p["image_url"]]
    stranded = [p for p in plans if p["old_status"] == "processing"]

    mode = "APPLYING" if apply_changes else "DRY RUN (nothing will be written)"
    print(f"\n==== REPAIR — {mode} ====\n")
    print(f"Rows in database:            {len(plans)}")
    print(f"Audio located on disk:       {len(located)}")
    print(f"Audio genuinely missing:     {len(missing)}")
    print(f"file_path rewrites:          {len(path_fixes)}")
    print(f"Files to relocate:           {len(moves)}")
    print(f"Status promotions:           {len(promotions)}")
    print(f"  of which stranded:         {len(stranded)}")
    print(f"Metadata re-syncs from JSON: {len(retags)}")
    art_note = "" if fetch_art else "  (re-run with --fetch-art to fetch)"
    print(f"Unusable cached cover art:   {len(covers)}{art_note}")
    print(f"Tracks with no cover art:    {len(artless)}{art_note}")

    if promotions:
        print("\n-- Status promotions (file verified present, no download needed) --")
        for p in promotions:
            print(f"  [{p['old_status']:>10}] -> downloaded   {p['title']} ({p['album']})")

    if retags:
        print("\n-- Metadata re-synced from songs_meta_data.json --")
        for p in retags:
            for field, value in p["metadata_changes"].items():
                print(f"  id={p['id']} {field}: -> {value!r}")

    if moves:
        print("\n-- Files to relocate into their album folder --")
        for p in moves:
            print(f"  {store_path(p['found'])}\n    -> {store_path(p['canonical'])}")

    if covers:
        print("\n-- Cached cover art that is not JPEG/PNG --")
        for p in covers:
            fmt = (p["image_blob"] or b"")[:4]
            print(f"  id={p['id']} {p['title']} ({p['album']}) magic={fmt!r}")

    if missing:
        print("\n-- No audio found (left for the downloader to fetch) --")
        for p in missing:
            print(f"  [{p['old_status']}] {p['title']} ({p['album']})")

    if path_fixes:
        sample = path_fixes[0]
        print("\n-- Example file_path rewrite --")
        print(f"  from: {sample['old_path']}")
        print(f"    to: {store_path(sample['canonical'] if sample['needs_move'] else sample['found'])}")

    return {"located": located, "missing": missing}

def _backup_database():
    """Snapshots the database through SQLite so WAL content is included."""
    with sqlite3.connect(DB_PATH) as source, sqlite3.connect(BACKUP_PATH) as target:
        source.backup(target)
    print(f"Database backed up to {BACKUP_PATH}")

def _apply(plans, fetch_art):
    import shutil

    from youtube_music_manager import (
        add_metadata,
        calculate_file_hash,
        download_image,
        verify_audio_file,
    )

    moved = retagged = promoted = repathed = art_fixed = requeued = 0

    with get_conn() as conn:
        cursor = conn.cursor()
        for plan in plans:
            track_id = plan["id"]

            if not plan["found"]:
                # Nothing on disk. Anything stuck mid-flight goes back in the
                # queue so the downloader will retry it.
                if plan["old_status"] == "processing":
                    cursor.execute("UPDATE tracks SET status = 'pending' WHERE id = ?", (track_id,))
                    _log(cursor, track_id, "REPAIR_REQUEUED", "Stranded in processing, no file on disk.")
                    requeued += 1
                if plan["metadata_changes"]:
                    _write_metadata(cursor, track_id, plan["metadata_changes"])
                conn.commit()
                continue

            path = plan["found"]

            if plan["needs_move"] and not os.path.exists(plan["canonical"]):
                old_dir = os.path.dirname(path)
                os.makedirs(os.path.dirname(plan["canonical"]), exist_ok=True)
                shutil.move(path, plan["canonical"])
                path = plan["canonical"]
                _prune_empty_dir(old_dir)
                _log(cursor, track_id, "REPAIR_MOVED", f"Relocated to {store_path(path)}")
                moved += 1

            image_bytes = plan["image_blob"]
            new_art = None

            if plan["cover_invalid"]:
                image_bytes = None
                cursor.execute("UPDATE tracks SET image_blob = NULL WHERE id = ?", (track_id,))
                _log(cursor, track_id, "REPAIR_COVER_CLEARED", "Cached art was not usable.")

            if fetch_art and (plan["cover_invalid"] or plan["cover_missing"]) and plan["image_url"]:
                new_art = download_image(plan["image_url"])
                if new_art:
                    image_bytes = new_art
                    art_fixed += 1

            # Retag when the JSON corrected something or when we have new art.
            # Otherwise leave the audio bytes untouched.
            if plan["metadata_changes"] or new_art:
                add_metadata(path, plan["title"], plan["artist"], plan["album"], image_bytes)
                retagged += 1

            if plan["metadata_changes"]:
                _write_metadata(cursor, track_id, plan["metadata_changes"])

            is_valid, reason = verify_audio_file(path)
            if not is_valid:
                _log(cursor, track_id, "REPAIR_VERIFY_FAILED", reason)
                conn.commit()
                continue

            file_hash = None
            if plan["metadata_changes"] or new_art or plan["needs_hash"] \
                    or plan["old_status"] != "downloaded":
                file_hash = calculate_file_hash(path)

            if plan["old_status"] != "downloaded":
                promoted += 1
            if plan["path_stale"] or plan["needs_move"]:
                repathed += 1

            # Only write the blob column when the art actually changed.
            image_bytes = new_art

            cursor.execute("""
                UPDATE tracks
                SET status = 'downloaded',
                    error = '',
                    file_path = ?,
                    file_hash = COALESCE(?, file_hash),
                    image_blob = COALESCE(?, image_blob)
                WHERE id = ?
            """, (store_path(path), file_hash, image_bytes, track_id))
            _log(cursor, track_id, "REPAIRED",
                 f"status {plan['old_status']} -> downloaded, path {store_path(path)}")
            conn.commit()

    print("\n==== REPAIR APPLIED ====")
    print(f"file_path rewrites:   {repathed}")
    print(f"Files relocated:      {moved}")
    print(f"Status promotions:    {promoted}")
    print(f"Files retagged:       {retagged}")
    print(f"Cover art replaced:   {art_fixed}")
    print(f"Requeued for download:{requeued}")
    print("========================\n")

def _prune_empty_dir(path):
    """Removes an album folder left behind by a relocation, if it is now empty."""
    if not path or os.path.normpath(path) == os.path.normpath(SONGS_DIR):
        return
    try:
        remaining = [name for name in os.listdir(path) if name != ".DS_Store"]
        if remaining:
            return
        for name in os.listdir(path):
            os.remove(os.path.join(path, name))
        os.rmdir(path)
        print(f"Removed empty folder: {store_path(path)}")
    except OSError:
        pass

def _write_metadata(cursor, track_id, changes):
    assignments = ", ".join(f"{column} = ?" for column in changes)
    cursor.execute(f"UPDATE tracks SET {assignments} WHERE id = ?",
                   list(changes.values()) + [track_id])
    _log(cursor, track_id, "REPAIR_METADATA_SYNCED", ", ".join(sorted(changes)))

def _log(cursor, track_id, action, details):
    cursor.execute("""
        INSERT INTO activity_log (track_id, action, details) VALUES (?, ?, ?)
    """, (track_id, action, details))

def run_repair(apply_changes=False, fetch_art=False):
    if not os.path.exists(DB_PATH):
        print(f"Error: database not found at {DB_PATH}")
        return

    json_meta = _load_json_metadata()
    library_index = _index_library()

    with get_conn() as conn:
        plans = _plan(conn, json_meta, library_index)

    _report(plans, apply_changes, fetch_art)

    if not apply_changes:
        print("Nothing was written. Re-run with --apply to make these changes.\n")
        return

    _backup_database()
    _apply(plans, fetch_art)

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Reconcile downloads.db with the files on disk.")
    parser.add_argument("--apply", action="store_true", help="Write the changes (default is a dry run).")
    parser.add_argument("--fetch-art", action="store_true", help="Re-download cover art cached in an unusable format.")
    args = parser.parse_args()
    run_repair(apply_changes=args.apply, fetch_art=args.fetch_art)
