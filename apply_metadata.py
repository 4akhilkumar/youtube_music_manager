#!/usr/bin/env python3
"""
Rewrite the metadata of every downloaded song from songs_meta_data.json.

For each song in the JSON this:
  * finds its row in downloads.db - by youtube_link, or, when the link was
    changed in the JSON, by the same title + album (the link is then updated
    in the database instead of the song being downloaded again)
  * updates the row's title, artist, album, year, image_url and youtube_link
  * writes title / artist / album / year tags into the .m4a file, keeping the
    cover art already embedded (new art is downloaded only if image_url changed)
  * moves or renames the file to Songs/<Album>/<Title>.m4a when the album or
    title changed, and removes album folders left empty
  * refreshes the stored file path and hash, and logs every change

No audio is downloaded. Reports only by default; pass --apply to write.
The database is backed up before anything is changed.

Usage:
    venv/bin/python apply_metadata.py            # dry run: show what would change
    venv/bin/python apply_metadata.py --apply    # do it
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time

from mutagen.mp4 import MP4

from common import DB_PATH, JSON_PATH, SONGS_DIR, get_conn, resolve_path, store_path, track_relpath
from youtube_music_manager import (
    add_metadata,
    calculate_file_hash,
    download_image,
    init_db,
    parse_year,
    verify_audio_file,
)

FIELDS = ("title", "artist", "album", "year", "image_url", "youtube_link")
TAGS = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "year": "\xa9day"}


def load_json():
    with open(JSON_PATH, encoding="utf-8") as f:
        songs = json.load(f)
    wanted = []
    for s in songs:
        wanted.append({
            "title": s.get("title") or "Unknown Title",
            "artist": s.get("artist") or "Unknown Artist",
            "album": s.get("album") or "Unknown Album",
            "year": parse_year(s.get("year")),
            "image_url": s.get("image_url") or "",
            "youtube_link": s.get("youtube_link"),
        })
    return wanted


def match_rows(wanted, rows):
    """Pairs each JSON song with a database row. Returns (pairs, unmatched_json, unmatched_rows)."""
    by_link = {r["youtube_link"]: r for r in rows}
    claimed, pairs, leftover = set(), [], []
    for song in wanted:
        row = by_link.get(song["youtube_link"])
        if row and row["id"] not in claimed:
            claimed.add(row["id"])
            pairs.append((song, row, "link"))
        else:
            leftover.append(song)

    unmatched = []
    for song in leftover:
        same = [r for r in rows if r["id"] not in claimed
                and r["title"] == song["title"] and r["album"] == song["album"]]
        if len(same) == 1:
            claimed.add(same[0]["id"])
            pairs.append((song, same[0], "title+album"))
        else:
            unmatched.append(song)
    return pairs, unmatched, [r for r in rows if r["id"] not in claimed]


def file_tags(path):
    audio = MP4(path)
    return {field: (audio.get(tag) or [None])[0] for field, tag in TAGS.items()}


def plan_song(song, row):
    # .get: on a dry run the year column may not exist yet
    changes = {f: (row.get(f), song[f]) for f in FIELDS if row.get(f) != song[f]}
    current = resolve_path(row["file_path"])
    target = resolve_path(track_relpath(song["title"], song["album"]))

    ok, reason = verify_audio_file(current)
    if not ok:
        return {"song": song, "row": row, "changes": changes, "error": f"audio file {reason}: {row['file_path']}"}

    tags = file_tags(current)
    expected = {"title": song["title"], "artist": song["artist"], "album": song["album"],
                "year": str(song["year"]) if song["year"] else None}
    retag = tags != expected

    move = os.path.normpath(current) != os.path.normpath(target)
    error = None
    if move and os.path.exists(target) and not os.path.samefile(current, target):
        error = f"destination already holds another file: {store_path(target)}"
    return {"song": song, "row": row, "changes": changes, "current": current, "target": target,
            "move": move, "retag": retag, "error": error}


def move_file(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst) and os.path.samefile(src, dst):
        # Case-only rename on a case-insensitive disk: go through a temp name.
        tmp = dst + ".renaming"
        os.rename(src, tmp)
        os.rename(tmp, dst)
    else:
        shutil.move(src, dst)


def prune_empty_dir(path):
    if not path or os.path.normpath(path) == os.path.normpath(SONGS_DIR) or not os.path.isdir(path):
        return
    if any(name != ".DS_Store" for name in os.listdir(path)):
        return
    for name in os.listdir(path):
        os.remove(os.path.join(path, name))
    os.rmdir(path)
    print(f"  removed empty folder {store_path(path)}")


def backup_database():
    backup = f"{DB_PATH}.{time.strftime('%Y%m%d-%H%M%S')}.bak"
    with sqlite3.connect(DB_PATH) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    print(f"Database backed up to {os.path.basename(backup)}")


def apply_plan(plan, conn):
    song, row = plan["song"], plan["row"]
    path = plan["current"]
    old_dir = os.path.dirname(path)

    if plan["move"]:
        move_file(path, plan["target"])
        path = plan["target"]

    image_bytes = None  # None keeps the cover already embedded in the file
    if "image_url" in plan["changes"] and song["image_url"]:
        image_bytes = download_image(song["image_url"])

    add_metadata(path, song["title"], song["artist"], song["album"], image_bytes, year=song["year"])
    new_hash = calculate_file_hash(path)

    assignments = ", ".join(f"{f} = ?" for f in FIELDS)
    params = [song[f] for f in FIELDS] + [store_path(path), new_hash]
    extra = ""
    if "image_url" in plan["changes"]:
        extra = ", image_blob = ?"
        params.append(image_bytes)
    conn.execute(f"UPDATE tracks SET {assignments}, file_path = ?, file_hash = ?, status = 'downloaded', "
                 f"error = ''{extra} WHERE id = ?", params + [row["id"]])

    details = "; ".join(f"{f}: {old!r} -> {new!r}" for f, (old, new) in plan["changes"].items())
    if plan["move"]:
        details += f"; moved {store_path(plan['current'])} -> {store_path(path)}"
    conn.execute("INSERT INTO activity_log (track_id, action, details) VALUES (?, 'METADATA_APPLIED', ?)",
                 (row["id"], details or "tags rewritten"))
    conn.commit()

    if plan["move"]:
        prune_empty_dir(old_dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Write the changes (default is a dry run).")
    args = parser.parse_args()

    if args.apply:
        backup_database()  # before init_db, so the backup is the untouched database
        init_db()  # adds the year column if this database predates it
    wanted = load_json()
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM tracks")]

    pairs, unmatched_json, unmatched_rows = match_rows(wanted, rows)
    plans = [plan_song(song, row) for song, row, _ in pairs]
    relinked = [(s, r) for s, r, how in pairs if how == "title+album"]

    errors = [p for p in plans if p["error"]]
    work = [p for p in plans if not p["error"] and (p["changes"] or p["move"] or p["retag"])]
    moves = [p for p in work if p["move"]]
    field_counts = {f: sum(1 for p in work if f in p["changes"]) for f in FIELDS}

    print(f"\nJSON songs: {len(wanted)}   database rows: {len(rows)}   matched: {len(pairs)}")
    print(f"Songs to update: {len(work)}   (files moved/renamed: {len(moves)})")
    for f in FIELDS:
        print(f"  {f:13} changes: {field_counts[f]}")

    if relinked:
        print("\n-- Matched by title + album (YouTube link updated in the database) --")
        for s, r in relinked:
            print(f"  {s['title']} ({s['album']}): {r['youtube_link']} -> {s['youtube_link']}")

    shown = [p for p in work if set(p["changes"]) - {"year"} or p["move"]]
    if shown:
        print("\n-- Changes other than year --")
        for p in shown:
            print(f"  [{p['row']['id']}] {p['song']['title']} ({p['song']['album']})")
            for f, (old, new) in p["changes"].items():
                if f != "year":
                    print(f"      {f}: {old!r} -> {new!r}")
            if p["move"]:
                print(f"      file: {store_path(p['current'])} -> {store_path(p['target'])}")

    problems = bool(errors or unmatched_json or unmatched_rows)
    for p in errors:
        print(f"\n!! [{p['row']['id']}] {p['song']['title']}: {p['error']}")
    for s in unmatched_json:
        print(f"\n!! In JSON but no database row matched: {s['title']} ({s['album']}) {s['youtube_link']}")
    for r in unmatched_rows:
        print(f"\n!! In database but not in JSON (left untouched): [{r['id']}] {r['title']} ({r['album']})")

    if not args.apply:
        print("\nDry run - nothing was changed. Re-run with --apply to write these changes.\n")
        return
    if not work:
        print("\nNothing to do.\n")
        return

    done = failed = 0
    with get_conn() as conn:
        for p in work:
            try:
                apply_plan(p, conn)
                done += 1
            except Exception as e:  # keep going; report at the end
                conn.rollback()
                failed += 1
                print(f"!! Failed on [{p['row']['id']}] {p['song']['title']}: {e}")
                conn.execute("INSERT INTO activity_log (track_id, action, details) VALUES (?, 'METADATA_APPLY_FAILED', ?)",
                             (p["row"]["id"], str(e)))
                conn.commit()
    print(f"\nUpdated {done} song(s), {failed} failed.\n")
    sys.exit(1 if failed or problems else 0)


if __name__ == "__main__":
    main()
