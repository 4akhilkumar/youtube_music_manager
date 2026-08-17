"""Shared configuration and helpers for the downloader and the album organizer.

Both scripts used to keep their own copy of these, which is how they drifted apart.
"""

import os
import re
import shutil
import sqlite3
import subprocess

# ==== Configuration & Paths ====
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SONGS_DIR = os.path.join(BASE_DIR, "Songs")
JSON_PATH = os.path.join(BASE_DIR, "songs_meta_data.json")
DB_PATH = os.path.join(BASE_DIR, "downloads.db")

# Characters that are illegal in a filename on Windows/exFAT/SMB, plus the path
# separators. macOS tolerates most of these at the POSIX layer but renders ":"
# as "/" in Finder, which is how two titles ended up needing a manual rename.
# Separators are dropped rather than substituted to stay consistent with the
# album folders already on disk (e.g. "Surya S/o Krishnan" -> "Surya So Krishnan").
_INVALID_CHARS = re.compile(r'[\\/*?:"<>|\x00-\x1f]')

# ==== Name Handling ====
def sanitize_name(name, fallback="Unknown"):
    """Strips characters that are invalid in a file or folder name."""
    if not name:
        return fallback

    sanitized = _INVALID_CHARS.sub("", name).strip()

    # A name that sanitizes down to nothing, or to a leading dot, would produce
    # an unusable or hidden file. Trailing dots are left alone on purpose:
    # Windows dislikes them, but stripping them would rename existing tracks
    # such as "APT." and "Raalupoola Ragamala...".
    if not sanitized or sanitized.startswith("."):
        return fallback
    return sanitized

def track_filename(title):
    """The .m4a filename for a track title."""
    return f"{sanitize_name(title, 'Unknown Title')}.m4a"

def album_dirname(album):
    """The folder name for an album."""
    return sanitize_name(album, "Unknown Album")

def track_relpath(title, album):
    """Canonical location of a track, relative to BASE_DIR."""
    return os.path.join("Songs", album_dirname(album), track_filename(title))

# ==== Path Storage ====
# Paths are stored relative to BASE_DIR so the project folder can be moved or
# renamed without invalidating every row in the database.
def store_path(path):
    """Converts a path into the relative form kept in the database."""
    if not path:
        return None
    if not os.path.isabs(path):
        return os.path.normpath(path)
    try:
        return os.path.relpath(path, BASE_DIR)
    except ValueError:
        # Different drive on Windows - nothing sensible to store but the original.
        return path

def resolve_path(stored):
    """Expands a stored path back into an absolute path."""
    if not stored:
        return None
    if os.path.isabs(stored):
        return stored
    return os.path.normpath(os.path.join(BASE_DIR, stored))

# ==== Database ====
def get_conn(timeout=30):
    """Opens a connection tuned for concurrent access from the worker threads."""
    conn = sqlite3.connect(DB_PATH, timeout=timeout)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

# ==== Image Handling ====
def detect_image_format(data):
    """Identifies image bytes by magic number. Returns 'jpeg', 'png' or None."""
    if not data or len(data) < 12:
        return None
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    # WEBP, GIF, SVG, and HTML error pages all land here: MP4 cover art only
    # supports JPEG and PNG, so anything else must be rejected rather than
    # embedded under a lie about its format.
    return None

def is_convertible_image(data):
    """True for image formats ffmpeg can turn into cover art (currently WEBP/GIF)."""
    if not data or len(data) < 12:
        return False
    return (data[:4] == b"RIFF" and data[8:12] == b"WEBP") or data[:4] in (b"GIF8",)

def convert_image_to_jpeg(data):
    """Transcodes an image ffmpeg understands into JPEG, or returns None.

    Some cover URLs end in .jpg but serve WEBP, which MP4 cover art cannot
    carry. ffmpeg is already required for the audio post-processing.
    """
    if not shutil.which("ffmpeg"):
        return None
    try:
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", "pipe:0", "-frames:v", "1",
             "-f", "mjpeg", "-q:v", "2", "pipe:1"],
            input=data, capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode != 0 or detect_image_format(result.stdout) != "jpeg":
        return None
    return result.stdout

# ==== Audio Decoding ====
# The classifier needs raw samples, and the two encoders disagree on rate:
# MERT wants 24 kHz, CLAP wants 48 kHz. ffmpeg is already a hard dependency for
# the audio post-processing, so it does the resampling rather than a new library.
def decode_audio(path, sample_rate, seconds=None, offset=0.0, timeout=120):
    """Decodes an audio file to a mono float32 numpy array at sample_rate.

    Returns None if ffmpeg is unavailable or the file cannot be decoded, so a
    single unreadable track cannot take down a whole analysis run.
    """
    import numpy as np

    if not shutil.which("ffmpeg"):
        return None

    # -ss before -i seeks by keyframe, which is fast and accurate enough here.
    command = ["ffmpeg", "-v", "error"]
    if offset:
        command += ["-ss", str(offset)]
    if seconds:
        command += ["-t", str(seconds)]
    command += ["-i", path, "-f", "f32le", "-ac", "1", "-ar", str(sample_rate), "pipe:1"]

    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode != 0 or not result.stdout:
        return None

    # np.frombuffer returns a read-only view over the subprocess buffer; the
    # model code writes into these arrays, so hand back an owned copy.
    samples = np.frombuffer(result.stdout, dtype=np.float32).copy()
    return samples if samples.size else None
