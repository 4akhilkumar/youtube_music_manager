"""Classifies the library by rhythm, mood and activity.

The pipeline is deliberately split so the expensive part happens once:

    --analyze    decode every track, measure BPM/energy, cache MERT and CLAP
                 embeddings in the database. Slow (~30-60 min for 508 tracks).
    --prompts    ask a local LLM to expand each category seed into a prompt
                 ensemble. Cached to disk so runs are reproducible.
    --bootstrap  propose labels for everything using CLAP + the LLM + BPM rules.
    --review     export the least-confident tracks for hand correction.
    --learn      train a multi-label probe on the cached embeddings.
    --predict    score every track and write track_tags.
    --export     write m3u8 playlists and optionally embed MP4 tags.

Everything after --analyze reads cached vectors, so re-tuning a category or
retraining the probe takes seconds rather than re-decoding 2 GB of audio.

Nothing here is imported by the downloader; the heavy dependencies stay local
to this module and are imported lazily inside the functions that need them.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

import common
from common import BASE_DIR, decode_audio, get_conn, resolve_path

CATEGORIES_PATH = os.path.join(BASE_DIR, "categories.json")
PROMPTS_PATH = os.path.join(BASE_DIR, "category_prompts.json")
PLAYLIST_DIR = os.path.join(BASE_DIR, "Playlists")

MERT_MODEL = "m-a-p/MERT-v1-330M"
MERT_SR = 24000

# NOT laion/larger_clap_music: its feature-fusion path is broken under
# transformers 5.x and produces collapsed embeddings (silence and white noise
# come out 0.99 cosine similar, audio-vs-text ~0.005). The unfused checkpoint
# behaves correctly - verified against synthetic tones and real tracks.
CLAP_MODEL = "laion/clap-htsat-unfused"
CLAP_SR = 48000

# Three 10s windows sampled across the track, mean-pooled. Enough to cover an
# intro, a chorus and an outro without embedding four minutes of audio.
WINDOW_SECONDS = 10
WINDOW_COUNT = 3

TEMPO_BANDS = (("Slow", 0, 90), ("Mid", 90, 120), ("Fast", 120, 10_000))

# gemma4:12b is the default because the larger local variants degenerate badly on
# this task - 26b/31b emit repetition loops and corrupted tokens ("driving driving
# driving...") that truncate the JSON. Any Ollama model can be substituted via the
# "_llm" block in categories.json; bigger is not automatically better here.
DEFAULT_LLM = {"provider": "ollama", "model": "gemma4:12b",
               "endpoint": "http://localhost:11434"}

# These models advertise a "thinking" capability, and its output leaks into the
# JSON-constrained response and truncates it. think=False is required, not optional.
LLM_OPTIONS = {"temperature": 0.7, "top_p": 0.9, "repeat_penalty": 1.15,
               "num_predict": 1500}


# ==== Database ====
def init_schema():
    """Creates the classification tables. Existing tables are untouched."""
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS track_features (
                track_id INTEGER PRIMARY KEY,
                bpm REAL, energy REAL, duration REAL,
                mert_embedding BLOB, clap_embedding BLOB,
                model_version TEXT, analyzed_at DATETIME
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS track_labels (
                track_id INTEGER, tag TEXT, present INTEGER,
                labelled_at DATETIME,
                PRIMARY KEY (track_id, tag)
            )
        """)
        # source belongs in the key: bootstrap, llm and probe all hold opinions
        # about the same (track, tag) pair and must not overwrite one another.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS track_tags (
                track_id INTEGER, tag TEXT, score REAL, source TEXT,
                PRIMARY KEY (track_id, tag, source)
            )
        """)
        # Migrate an existing table built with the older (track_id, tag) key,
        # under which one source silently clobbered another.
        cursor.execute("PRAGMA table_info(track_tags)")
        keyed = [row[1] for row in cursor.fetchall() if row[5]]
        if keyed and "source" not in keyed:
            cursor.execute("ALTER TABLE track_tags RENAME TO track_tags_old")
            cursor.execute("""
                CREATE TABLE track_tags (
                    track_id INTEGER, tag TEXT, score REAL, source TEXT,
                    PRIMARY KEY (track_id, tag, source)
                )
            """)
            cursor.execute("INSERT OR IGNORE INTO track_tags SELECT * FROM track_tags_old")
            cursor.execute("DROP TABLE track_tags_old")
            print("Migrated track_tags to a (track_id, tag, source) key.")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_track_tags_tag ON track_tags (tag)")
        conn.commit()


def log(track_id, action, details=""):
    """Appends to the same activity_log the rest of the project writes to."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO activity_log (track_id, action, details) VALUES (?, ?, ?)",
            (track_id, action, details),
        )
        conn.commit()


def downloaded_tracks(match=None):
    """Every downloaded track, optionally filtered the way --retag filters."""
    sql = ("SELECT id, title, artist, album, file_path FROM tracks "
           "WHERE status = 'downloaded'")
    params = []
    if match:
        sql += " AND (title LIKE ? OR album LIKE ? OR artist LIKE ?)"
        params = [f"%{match}%"] * 3
    sql += " ORDER BY album, title"
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params)]


def analyzed_ids():
    """Track ids that already have cached features, so runs are resumable."""
    with get_conn() as conn:
        return {row[0] for row in conn.execute(
            "SELECT track_id FROM track_features WHERE mert_embedding IS NOT NULL")}


def load_categories():
    if not os.path.exists(CATEGORIES_PATH):
        print(f"Error: {CATEGORIES_PATH} not found.")
        sys.exit(1)
    with open(CATEGORIES_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def tempo_band(bpm):
    if not bpm:
        return None
    for name, low, high in TEMPO_BANDS:
        if low <= bpm < high:
            return name
    return None


# ==== Feature extraction ====
_models = {}


def _device():
    import torch
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _load_models():
    """Loads MERT and CLAP once per process. Imported lazily on purpose."""
    if _models:
        return _models
    import torch
    from transformers import AutoModel, ClapModel, ClapProcessor, Wav2Vec2FeatureExtractor

    device = _device()
    print(f"Loading models on {device} (first run downloads ~2 GB)...")

    mert = AutoModel.from_pretrained(MERT_MODEL, trust_remote_code=True).eval()
    mert_fe = Wav2Vec2FeatureExtractor.from_pretrained(MERT_MODEL, trust_remote_code=True)
    clap = ClapModel.from_pretrained(CLAP_MODEL).eval()
    clap_proc = ClapProcessor.from_pretrained(CLAP_MODEL)

    try:
        mert = mert.to(device)
        clap = clap.to(device)
    except (RuntimeError, NotImplementedError) as error:
        # Some MPS kernels are still missing; a slow run beats a failed one.
        print(f"  Falling back to CPU: {error}")
        device, mert, clap = "cpu", mert.to("cpu"), clap.to("cpu")

    _models.update(torch=torch, device=device, mert=mert, mert_fe=mert_fe,
                   clap=clap, clap_proc=clap_proc)
    return _models


def _windows(path, sample_rate, duration):
    """Samples WINDOW_COUNT excerpts spread across the track."""
    clips = []
    if duration and duration > WINDOW_SECONDS * (WINDOW_COUNT + 1):
        # Skip the first and last eighth: intros and fades are unrepresentative.
        usable_start, usable_end = duration * 0.125, duration * 0.875
        step = (usable_end - usable_start) / WINDOW_COUNT
        offsets = [usable_start + step * index for index in range(WINDOW_COUNT)]
    else:
        offsets = [0.0]
    for offset in offsets:
        clip = decode_audio(path, sample_rate, seconds=WINDOW_SECONDS, offset=offset)
        if clip is not None and clip.size:
            clips.append(clip)
    return clips


def extract_features(path):
    """Returns (bpm, energy, duration, mert_vector, clap_vector) or None."""
    import numpy as np
    import librosa

    models = _load_models()
    torch = models["torch"]
    device = models["device"]

    probe = decode_audio(path, 22050, seconds=90, offset=30)
    if probe is None:
        probe = decode_audio(path, 22050)
    if probe is None:
        return None

    # mutagen is already a dependency and reads the container duration directly;
    # librosa would have to decode the whole file to work it out.
    try:
        from mutagen.mp4 import MP4
        duration = float(MP4(path).info.length)
    except Exception:
        duration = probe.size / 22050

    tempo, _ = librosa.beat.beat_track(y=probe, sr=22050)
    bpm = float(np.atleast_1d(tempo)[0])
    energy = float(np.sqrt(np.mean(probe ** 2)))

    mert_clips = _windows(path, MERT_SR, duration)
    clap_clips = _windows(path, CLAP_SR, duration)
    if not mert_clips or not clap_clips:
        return None

    mert_vectors = []
    for clip in mert_clips:
        inputs = models["mert_fe"](clip, sampling_rate=MERT_SR, return_tensors="pt")
        inputs = {key: value.to(device) for key, value in inputs.items()}
        with torch.no_grad():
            output = models["mert"](**inputs)
        # Mean over time gives a fixed-size vector regardless of clip length.
        mert_vectors.append(output.last_hidden_state.mean(dim=1).squeeze(0).float().cpu().numpy())

    clap_vectors = []
    for clip in clap_clips:
        inputs = models["clap_proc"](audio=clip, sampling_rate=CLAP_SR, return_tensors="pt")
        inputs = {key: value.to(device) for key, value in inputs.items()}
        with torch.no_grad():
            output = models["clap"].get_audio_features(**inputs)
        clap_vectors.append(output.pooler_output.squeeze(0).float().cpu().numpy())

    mert_vector = np.mean(mert_vectors, axis=0).astype(np.float32)
    clap_vector = np.mean(clap_vectors, axis=0).astype(np.float32)
    # Re-normalise: the mean of unit vectors is not a unit vector.
    clap_vector /= (np.linalg.norm(clap_vector) or 1.0)
    return bpm, energy, duration, mert_vector, clap_vector


def store_features(track_id, bpm, energy, duration, mert_vector, clap_vector):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO track_features
                (track_id, bpm, energy, duration, mert_embedding, clap_embedding,
                 model_version, analyzed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(track_id) DO UPDATE SET
                bpm=excluded.bpm, energy=excluded.energy, duration=excluded.duration,
                mert_embedding=excluded.mert_embedding,
                clap_embedding=excluded.clap_embedding,
                model_version=excluded.model_version, analyzed_at=excluded.analyzed_at
        """, (track_id, bpm, energy, duration, mert_vector.tobytes(),
              clap_vector.tobytes(), f"{MERT_MODEL}|{CLAP_MODEL}",
              datetime.now(timezone.utc).isoformat(timespec="seconds")))
        conn.commit()


def run_analyze(match=None, apply_changes=False, limit=None):
    """Decodes and embeds every track that has no cached features yet."""
    tracks = downloaded_tracks(match)
    done = analyzed_ids()
    todo = [track for track in tracks if track["id"] not in done]
    if limit:
        todo = todo[:limit]

    print(f"Downloaded tracks:     {len(tracks)}")
    print(f"Already analyzed:      {len(tracks) - len([t for t in tracks if t['id'] not in done])}")
    print(f"To analyze this run:   {len(todo)}")

    if not apply_changes:
        print("\nDry run - nothing written. Re-run with --apply to analyze.")
        for track in todo[:10]:
            print(f"   would analyze: {track['title']} - {track['album']}")
        if len(todo) > 10:
            print(f"   ... and {len(todo) - 10} more")
        return

    if not todo:
        print("\nNothing to do.")
        return

    failures = []
    for index, track in enumerate(todo, start=1):
        path = resolve_path(track["file_path"])
        if not path or not os.path.exists(path):
            failures.append((track, "file missing"))
            continue
        try:
            result = extract_features(path)
        except Exception as error:  # one bad file must not kill the run
            failures.append((track, f"{type(error).__name__}: {error}"))
            continue
        if result is None:
            failures.append((track, "could not decode"))
            continue

        bpm, energy, duration, mert_vector, clap_vector = result
        store_features(track["id"], bpm, energy, duration, mert_vector, clap_vector)
        log(track["id"], "ANALYZED", f"bpm={bpm:.1f} energy={energy:.4f}")
        print(f"[{index}/{len(todo)}] {track['title'][:44]:<46} "
              f"{bpm:6.1f} BPM  {tempo_band(bpm)}")

    print(f"\nAnalyzed: {len(todo) - len(failures)}   Failed: {len(failures)}")
    for track, reason in failures:
        print(f"   FAILED {track['title']} - {reason}")


# ==== Local LLM (any open-weight model served by Ollama) ====
def llm_config():
    """LLM settings from categories.json, falling back to the defaults."""
    categories = load_categories()
    config = dict(DEFAULT_LLM)
    config.update(categories.get("_llm", {}) if isinstance(categories, dict) else {})
    return config


def _salvage_strings(text, key):
    """Recovers list entries from JSON that was truncated mid-generation.

    A long generation can hit num_predict partway through a string, leaving an
    unterminated array. The complete entries before that point are still good.
    """
    import re
    found = re.findall(r'"((?:[^"\\]|\\.){4,})"', text or "")
    return [item for item in found if item != key]


def ask_llm(prompt, key=None, timeout=300, attempts=2):
    """Sends one prompt to Ollama and returns parsed JSON, or None.

    Uses the HTTP API directly so no extra client library is needed - requests
    is already a dependency of this project.
    """
    import requests

    config = llm_config()
    for attempt in range(attempts):
        try:
            response = requests.post(
                f"{config['endpoint']}/api/generate",
                json={"model": config["model"], "prompt": prompt, "stream": False,
                      "format": "json", "think": False, "options": LLM_OPTIONS},
                timeout=timeout,
            )
            response.raise_for_status()
            text = response.json().get("response", "")
        except Exception as error:
            print(f"   LLM call failed ({type(error).__name__}: {error})")
            continue

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            if key:
                salvaged = _salvage_strings(text, key)
                if salvaged:
                    print(f"   (recovered {len(salvaged)} entries from a truncated reply)")
                    return {key: salvaged}
            if attempt + 1 < attempts:
                print("   Unparseable JSON from the LLM; retrying once.")
    return None


PROMPT_TEMPLATE = """You are helping tag a personal music library for playlist building.
The library is mostly Indian film music - Telugu, Tamil, Malayalam and Hindi - plus some
Japanese, Korean and English tracks.

Write {count} short English descriptions of what the category "{name}" sounds like as music.
Meaning of the category: {seed}

Rules:
- Each description is a phrase of 4 to 12 words describing the SOUND, not the lyrics.
- Vary instrumentation, tempo feel and energy across the descriptions.
- Include at least three phrasings rooted in Indian film or classical music idiom.
- Do not mention the category name itself.

Return strict JSON: {{"prompts": ["...", "..."]}}"""


def run_prompts(count=15, apply_changes=False):
    """Expands each category seed into a prompt ensemble using the local LLM."""
    categories = {name: spec for name, spec in load_categories().items()
                  if not name.startswith("_")}
    config = llm_config()
    print(f"Generating prompt ensembles with {config['model']} at {config['endpoint']}")

    ensembles = {}
    for name, spec in categories.items():
        result = ask_llm(PROMPT_TEMPLATE.format(count=count, name=name, seed=spec["seed"]),
                         key="prompts")
        prompts = (result or {}).get("prompts") or []
        prompts = [p.strip() for p in prompts if isinstance(p, str) and p.strip()]
        if not prompts:
            # Never leave a category with nothing to match against.
            prompts = [spec["seed"]]
            print(f"   {name:<12} LLM gave nothing usable; falling back to the seed")
        else:
            print(f"   {name:<12} {len(prompts)} prompts   e.g. \"{prompts[0][:58]}\"")
        # The seed is always kept: it is the human-authored definition.
        ensembles[name] = sorted(set(prompts + [spec["seed"]]))

    if not apply_changes:
        print("\nDry run - nothing written. Re-run with --apply to save.")
        return ensembles

    with open(PROMPTS_PATH, "w", encoding="utf-8") as handle:
        json.dump({"model": config["model"], "ensembles": ensembles}, handle,
                  indent=4, ensure_ascii=False)
    print(f"\nWrote {PROMPTS_PATH}")
    return ensembles


def load_prompt_ensembles():
    """Prompt ensembles from disk, falling back to the raw seeds."""
    if os.path.exists(PROMPTS_PATH):
        with open(PROMPTS_PATH, encoding="utf-8") as handle:
            return json.load(handle).get("ensembles", {})
    print(f"No {os.path.basename(PROMPTS_PATH)} yet - using bare seeds. "
          f"Run --prompts --apply for better coverage.")
    return {name: [spec["seed"]] for name, spec in load_categories().items()
            if not name.startswith("_")}


# ==== Bootstrap labelling ====
def load_features(match=None):
    """Cached features joined to track metadata, as a list of dicts."""
    import numpy as np

    sql = """SELECT t.id, t.title, t.artist, t.album, t.file_path,
                    f.bpm, f.energy, f.duration, f.mert_embedding, f.clap_embedding
             FROM tracks t JOIN track_features f ON f.track_id = t.id
             WHERE t.status = 'downloaded'"""
    params = []
    if match:
        sql += " AND (t.title LIKE ? OR t.album LIKE ? OR t.artist LIKE ?)"
        params = [f"%{match}%"] * 3
    sql += " ORDER BY t.album, t.title"

    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute(sql, params)]
    for row in rows:
        row["mert"] = np.frombuffer(row.pop("mert_embedding"), dtype=np.float32)
        row["clap"] = np.frombuffer(row.pop("clap_embedding"), dtype=np.float32)
    return rows


def embed_category_texts(ensembles):
    """One unit vector per category: the mean of its prompt embeddings."""
    import numpy as np
    import torch

    models = _load_models()
    vectors = {}
    for name, prompts in ensembles.items():
        inputs = models["clap_proc"](text=prompts, return_tensors="pt", padding=True)
        inputs = {key: value.to(models["device"]) for key, value in inputs.items()}
        with torch.no_grad():
            output = models["clap"].get_text_features(**inputs)
        matrix = output.pooler_output.float().cpu().numpy()
        mean = matrix.mean(axis=0)
        vectors[name] = mean / (np.linalg.norm(mean) or 1.0)
    return vectors


def bpm_allows(spec, bpm):
    """False when a category's BPM gate rules the track out."""
    if bpm is None:
        return True
    if spec.get("min_bpm") and bpm < spec["min_bpm"]:
        return False
    if spec.get("max_bpm") and bpm > spec["max_bpm"]:
        return False
    return True


def run_bootstrap(match=None, apply_changes=False, z_threshold=0.6):
    """Proposes labels for every analyzed track using CLAP plus the BPM gates.

    Raw CLAP cosines sit in a narrow band and differ per category, so an
    absolute cutoff is meaningless. Scores are standardised within each
    category across the library and thresholded on the z-score instead.
    """
    import numpy as np

    categories = {name: spec for name, spec in load_categories().items()
                  if not name.startswith("_")}
    tracks = load_features(match)
    if not tracks:
        print("No analyzed tracks yet. Run --analyze --apply first.")
        return

    ensembles = load_prompt_ensembles()
    text_vectors = embed_category_texts(ensembles)

    clap_matrix = np.stack([row["clap"] for row in tracks])
    proposals = {}
    print(f"Scoring {len(tracks)} tracks against {len(categories)} categories\n")
    print(f"{'CATEGORY':<12}{'mean':>9}{'std':>8}{'proposed':>10}")

    for name, spec in categories.items():
        if name not in text_vectors:
            continue
        raw = clap_matrix @ text_vectors[name]
        mean, std = float(raw.mean()), float(raw.std()) or 1e-6
        zscores = (raw - mean) / std
        chosen = 0
        for row, z in zip(tracks, zscores):
            if z >= z_threshold and bpm_allows(spec, row["bpm"]):
                proposals.setdefault(row["id"], []).append((name, float(z)))
                chosen += 1
        print(f"{name:<12}{mean:9.4f}{std:8.4f}{chosen:10d}")

    # Tempo bands are measured, not predicted - always attached.
    for row in tracks:
        band = tempo_band(row["bpm"])
        if band:
            proposals.setdefault(row["id"], []).append((band, 1.0))

    tagged = sum(1 for tags in proposals.values() if any(t[0] in categories for t in tags))
    print(f"\nTracks receiving at least one activity/mood tag: {tagged}/{len(tracks)}")

    if not apply_changes:
        print("\nDry run - nothing written. Re-run with --apply to save proposals.")
        for row in tracks[:8]:
            tags = ", ".join(f"{n}({z:.1f})" for n, z in proposals.get(row["id"], []))
            print(f"   {row['title'][:34]:<36} {tags or '(none)'}")
        return

    with get_conn() as conn:
        conn.execute("DELETE FROM track_tags WHERE source = 'bootstrap'")
        conn.executemany(
            "INSERT OR REPLACE INTO track_tags (track_id, tag, score, source) "
            "VALUES (?, ?, ?, 'bootstrap')",
            [(track_id, name, score)
             for track_id, tags in proposals.items() for name, score in tags])
        conn.commit()
    print(f"\nWrote {sum(len(v) for v in proposals.values())} bootstrap tags.")


METADATA_TEMPLATE = """You are tagging songs for playlist building. Most are Indian film
songs (Telugu, Tamil, Malayalam, Hindi); some are Japanese, Korean or English.

Categories: {categories}

For each song below, list the categories that fit. Use only what you actually recognise or
can reasonably infer from the title, artist and film/album. If you do not recognise a song,
return an empty list for it rather than guessing.

Songs:
{songs}

Return strict JSON: {{"results": [{{"id": <id>, "tags": ["..."]}}]}}"""


def run_llm_tags(batch_size=12, limit=None, apply_changes=False):
    """Adds a metadata-only opinion from the LLM, independent of the audio.

    This is a genuinely separate signal: CLAP hears the track but knows nothing
    about Telugu cinema, while the LLM may recognise the film and its context
    but never hears a note. Stored under its own source so the two stay
    distinguishable and can be weighed separately.
    """
    categories = activity_categories()
    tracks = downloaded_tracks()
    if limit:
        tracks = tracks[:limit]

    print(f"Asking {llm_config()['model']} about {len(tracks)} tracks "
          f"in batches of {batch_size}")

    proposals, unrecognised = {}, 0
    for start in range(0, len(tracks), batch_size):
        batch = tracks[start:start + batch_size]
        listing = "\n".join(
            f"  id={t['id']}: \"{t['title']}\" by {t['artist']} (from \"{t['album']}\")"
            for t in batch)
        result = ask_llm(METADATA_TEMPLATE.format(
            categories=", ".join(categories), songs=listing), key="results")
        entries = (result or {}).get("results") or []
        if not isinstance(entries, list):
            entries = []
        valid = {t["id"] for t in batch}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                track_id = int(entry.get("id"))
            except (TypeError, ValueError):
                continue
            if track_id not in valid:
                continue
            tags = [tag for tag in (entry.get("tags") or []) if tag in categories]
            if tags:
                proposals[track_id] = tags
            else:
                unrecognised += 1
        print(f"   {min(start + batch_size, len(tracks))}/{len(tracks)} "
              f"({len(proposals)} tagged so far)")

    print(f"\nTagged by the LLM: {len(proposals)}   "
          f"Returned nothing for: {unrecognised}")

    if not apply_changes:
        print("\nDry run - nothing written. Re-run with --apply to save.")
        for track in tracks[:8]:
            print(f"   {track['title'][:34]:<36} {proposals.get(track['id'], [])}")
        return

    with get_conn() as conn:
        conn.execute("DELETE FROM track_tags WHERE source = 'llm'")
        conn.executemany(
            "INSERT OR REPLACE INTO track_tags (track_id, tag, score, source) "
            "VALUES (?, ?, 1.0, 'llm')",
            [(track_id, tag) for track_id, tags in proposals.items() for tag in tags])
        conn.commit()
    print(f"Wrote {sum(len(v) for v in proposals.values())} LLM tags.")


# ==== Review: turn proposals into ground truth ====
REVIEW_PATH = os.path.join(BASE_DIR, "review.json")


def activity_categories():
    return [name for name in load_categories() if not name.startswith("_")]


def run_review(count=150, apply_changes=False):
    """Exports the least-confident tracks for hand correction.

    Confidence is |z| summed over categories: a track every category is neutral
    about teaches the probe far more than one CLAP is already sure of. Spending
    the review budget there is what makes 150 labels worth more than 150 random.
    """
    import numpy as np

    categories = activity_categories()
    tracks = load_features()
    if not tracks:
        print("No analyzed tracks. Run --analyze --apply first.")
        return

    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        clap, llm = {}, {}
        for row in conn.execute(
                "SELECT track_id, tag, score, source FROM track_tags "
                "WHERE source IN ('bootstrap', 'llm')"):
            target = clap if row["source"] == "bootstrap" else llm
            target.setdefault(row["track_id"], {})[row["tag"]] = row["score"]
        already = {row[0] for row in conn.execute(
            "SELECT DISTINCT track_id FROM track_labels")}

    # An in-progress review.json is hand-edited work: never regenerate over it.
    # Existing entries keep their tags verbatim and the batch grows around them.
    existing, existing_order = {}, []
    if os.path.exists(REVIEW_PATH):
        with open(REVIEW_PATH, encoding="utf-8") as handle:
            for entry in json.load(handle).get("tracks", []):
                existing[entry["id"]] = entry
                existing_order.append(entry["id"])
        print(f"Preserving {len(existing)} entries already in review.json")

    scored = []
    for row in tracks:
        if row["id"] in already or row["id"] in existing:
            continue
        heard = {t for t in clap.get(row["id"], {}) if t in categories}
        known = {t for t in llm.get(row["id"], {}) if t in categories}

        # Two independent opinions: CLAP hears the audio but knows nothing about
        # Telugu cinema; the LLM knows the film but never hears a note. Where they
        # disagree, a human decides something neither can - so those go first.
        disagreement = len(heard ^ known)
        confidence = sum(abs(v) for k, v in clap.get(row["id"], {}).items()
                         if k in categories)
        # Seed with what both agree on, not the union: correcting a review entry
        # should mean adding an occasional missing tag, not deleting six wrong
        # ones. Where they agree on nothing, the LLM's sparser opinion is the
        # better starting point than CLAP's habit of firing on everything.
        seed = (heard & known) or known or set()
        # Lower sorts first: maximum disagreement, then weakest confidence.
        scored.append(((-disagreement, confidence), row, seed))
    scored.sort(key=lambda item: item[0])

    # count is how many NEW tracks to add, not the target file size. Sizing it
    # against the file total meant that once review.json covered the library,
    # newly downloaded tracks were silently never offered for review.
    batch = scored[:count]
    print(f"Analyzed tracks:        {len(tracks)}")
    print(f"Already hand-labelled:  {len(already)}")
    print(f"Awaiting review:        {len(scored)}")
    print(f"Added to review file:   {len(batch)}  (most contested first)")
    print(f"Review file total:      {len(existing) + len(batch)}")

    by_id = {row["id"]: row for row in tracks}

    def entry_for(track_id, seed_tags):
        row = by_id[track_id]
        return {"id": track_id, "title": row["title"], "artist": row["artist"],
                "album": row["album"], "bpm": round(row["bpm"], 1),
                "tags": sorted(seed_tags)}

    entries = []
    for track_id in existing_order:
        kept = existing[track_id]
        # Their edits are authoritative; only drop categories that no longer exist.
        kept["tags"] = sorted(t for t in kept.get("tags", []) if t in categories)
        entries.append(kept)
    entries.extend(entry_for(row["id"], tags) for _, row, tags in batch)

    payload = {
        "_instructions": (
            "Edit the 'tags' list for each track: keep what fits, delete what does "
            "not, add any category from '_categories'. A track can have several. "
            "Tempo tags (Slow/Mid/Fast) are measured separately and ignored here. "
            "Leave 'tags' empty if none apply - that is useful signal, not a gap. "
            "When done run: classify.py --learn --apply"
        ),
        "_categories": categories,
        "tracks": entries,
    }

    if not apply_changes:
        print("\nDry run - nothing written. Re-run with --apply to write review.json.")
        for _, row, tags in batch[:8]:
            print(f"   {row['title'][:34]:<36} {sorted(tags) or '(no proposal)'}")
        return

    with open(REVIEW_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=4, ensure_ascii=False)
    print(f"\nWrote {REVIEW_PATH}")
    print("Edit the tags, then run: classify.py --learn --apply")


def import_review():
    """Reads corrected review.json into track_labels as ground truth."""
    if not os.path.exists(REVIEW_PATH):
        return 0
    with open(REVIEW_PATH, encoding="utf-8") as handle:
        payload = json.load(handle)

    categories = set(activity_categories())
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for entry in payload.get("tracks", []):
        chosen = {tag for tag in entry.get("tags", []) if tag in categories}
        # Absence is a label too: an unticked category is a negative example,
        # which is what lets the probe learn a boundary rather than just a mean.
        for name in categories:
            rows.append((entry["id"], name, 1 if name in chosen else 0, stamp))

    with get_conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO track_labels (track_id, tag, present, labelled_at) "
            "VALUES (?, ?, ?, ?)", rows)
        conn.commit()
    return len({row[0] for row in rows})


# ==== Probe: the only component fitted to this library ====
def run_learn(apply_changes=False):
    """Trains a multi-label probe on cached MERT embeddings + tempo features."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold, cross_val_predict
    from sklearn.metrics import precision_score, recall_score, f1_score
    from sklearn.preprocessing import StandardScaler
    from sklearn.decomposition import PCA
    import pickle

    imported = import_review()
    if imported:
        print(f"Imported hand labels for {imported} tracks from review.json")
        # Mirror the confirmed labels into track_tags so export can prefer them
        # over any prediction. track_labels stays the canonical record.
        with get_conn() as conn:
            conn.execute("DELETE FROM track_tags WHERE source = 'labels'")
            conn.execute(
                "INSERT OR REPLACE INTO track_tags (track_id, tag, score, source) "
                "SELECT track_id, tag, 1.0, 'labels' FROM track_labels WHERE present = 1")
            conn.commit()

    categories = activity_categories()
    tracks = {row["id"]: row for row in load_features()}

    with get_conn() as conn:
        labels = {}
        for track_id, tag, present in conn.execute(
                "SELECT track_id, tag, present FROM track_labels"):
            labels.setdefault(track_id, {})[tag] = present

    usable = [tid for tid in labels if tid in tracks]
    if len(usable) < 20:
        print(f"Only {len(usable)} labelled tracks. Label at least ~20 "
              f"(run --review --apply, edit review.json) before training.")
        return

    raw = np.stack([_feature_vector(tracks[tid]) for tid in usable])
    scaler = StandardScaler().fit(raw)

    # A 1024-d MERT vector against ~150 labels overfits badly, and no amount of
    # regularisation recovers what the split throws away. Project down to a
    # fraction of the sample count first; the probe learns a direction in MERT
    # space, which needs far fewer dimensions than the encoder produces.
    components = max(8, min(64, len(usable) // 3))
    reducer = PCA(n_components=components, random_state=0).fit(scaler.transform(raw))
    features = reducer.transform(scaler.transform(raw))
    retained = reducer.explained_variance_ratio_.sum()
    print(f"Training on {len(usable)} labelled tracks: "
          f"{raw.shape[1]} features -> {components} components "
          f"({retained:.0%} variance retained)\n")
    print(f"{'CATEGORY':<12}{'pos':>5}{'precision':>11}{'recall':>9}{'F1':>7}")

    models, report = {}, {}
    for name in categories:
        target = np.array([labels[tid].get(name, 0) for tid in usable])
        positives = int(target.sum())
        if positives < 5 or positives == len(target):
            print(f"{name:<12}{positives:>5}   too few examples to train")
            continue

        # Independent binary classifier per category - never a softmax, so a
        # track can hold Workout and Travel at the same time.
        model = LogisticRegression(max_iter=2000, class_weight="balanced", C=1.0)
        folds = min(5, positives, len(target) - positives)
        if folds >= 2:
            predicted = cross_val_predict(
                model, features, target,
                cv=StratifiedKFold(n_splits=folds, shuffle=True, random_state=0))
            precision = precision_score(target, predicted, zero_division=0)
            recall = recall_score(target, predicted, zero_division=0)
            f1 = f1_score(target, predicted, zero_division=0)
        else:
            precision = recall = f1 = float("nan")
        report[name] = (positives, precision, recall, f1)
        print(f"{name:<12}{positives:>5}{precision:>11.2f}{recall:>9.2f}{f1:>7.2f}")

        model.fit(features, target)
        models[name] = model

    if not models:
        print("\nNothing trainable yet.")
        return
    if not apply_changes:
        print("\nDry run - model not saved. Re-run with --apply to persist.")
        return

    with open(os.path.join(BASE_DIR, "probe.pkl"), "wb") as handle:
        pickle.dump({"scaler": scaler, "reducer": reducer, "models": models,
                     "report": report, "categories": categories}, handle)
    print(f"\nSaved probe.pkl ({len(models)} categories)")


def _feature_vector(row):
    """MERT embedding plus the two measured signals the model cannot infer."""
    import numpy as np
    bpm = row["bpm"] or 0.0
    return np.concatenate([row["mert"], [bpm / 200.0, row["energy"] or 0.0]])


# ==== Predict ====
def run_predict(apply_changes=False, threshold=0.5):
    """Scores every analyzed track with the trained probe."""
    import pickle
    import numpy as np

    probe_path = os.path.join(BASE_DIR, "probe.pkl")
    if not os.path.exists(probe_path):
        print("No probe.pkl yet. Run --learn --apply first.")
        return
    with open(probe_path, "rb") as handle:
        probe = pickle.load(handle)

    tracks = load_features()
    raw = probe["scaler"].transform(np.stack([_feature_vector(row) for row in tracks]))
    features = probe["reducer"].transform(raw)

    predictions = {}
    print(f"{'CATEGORY':<12}{'predicted':>11}")
    for name, model in probe["models"].items():
        scores = model.predict_proba(features)[:, 1]
        hits = 0
        for row, score in zip(tracks, scores):
            if score >= threshold:
                predictions.setdefault(row["id"], []).append((name, float(score)))
                hits += 1
        print(f"{name:<12}{hits:>11}")

    for row in tracks:
        band = tempo_band(row["bpm"])
        if band:
            predictions.setdefault(row["id"], []).append((band, 1.0))

    if not apply_changes:
        print("\nDry run - nothing written. Re-run with --apply to save.")
        return

    with get_conn() as conn:
        conn.execute("DELETE FROM track_tags WHERE source = 'probe'")
        conn.executemany(
            "INSERT OR REPLACE INTO track_tags (track_id, tag, score, source) "
            "VALUES (?, ?, ?, 'probe')",
            [(tid, name, score) for tid, tags in predictions.items() for name, score in tags])
        conn.commit()
    print(f"\nWrote {sum(len(v) for v in predictions.values())} probe tags.")


# ==== Export to Apple Music and VLC ====
def effective_tags(source_priority=("labels", "probe", "bootstrap")):
    """Per-track tag lists, best available source first.

    Hand/reviewed labels beat any prediction: where a track has been labelled
    there is nothing for a model to add. The probe only earns its keep on tracks
    that were never labelled - newly downloaded ones, mainly.
    """
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        by_source = {}
        for row in conn.execute(
                "SELECT track_id, tag, source FROM track_tags ORDER BY tag"):
            by_source.setdefault(row["source"], {}).setdefault(
                row["track_id"], []).append(row["tag"])

    # Resolve per track, not per library: a labelled track uses its labels even
    # when the probe also has an opinion, but an unlabelled one still gets tagged.
    result, used = {}, []
    for source in source_priority:
        tags = by_source.get(source)
        if not tags:
            continue
        fresh = {tid: t for tid, t in tags.items() if tid not in result}
        if fresh:
            result.update(fresh)
            used.append(f"{source}({len(fresh)})")
    return result, " + ".join(used) if used else None


def run_export(write_playlists=False, embed_tags=False, apply_changes=False):
    """Writes m3u8 playlists for VLC and MP4 atoms for Apple Music."""
    tags_by_track, source = effective_tags()
    if not source:
        print("No tags yet. Run --bootstrap --apply or --predict --apply first.")
        return
    print(f"Using tags from source: {source}")

    tracks = {row["id"]: row for row in load_features()}

    # Tempo bands are measured from the audio, never predicted, so they attach
    # to every analyzed track regardless of which source supplied its categories.
    for track_id, row in tracks.items():
        band = tempo_band(row["bpm"])
        if band:
            tags_by_track.setdefault(track_id, [])
            if band not in tags_by_track[track_id]:
                tags_by_track[track_id].append(band)

    by_tag = {}
    for track_id, tags in tags_by_track.items():
        if track_id in tracks:
            for tag in tags:
                by_tag.setdefault(tag, []).append(tracks[track_id])

    print(f"\n{'PLAYLIST':<14}{'tracks':>8}")
    for tag in sorted(by_tag):
        print(f"{tag:<14}{len(by_tag[tag]):>8}")

    if not apply_changes:
        print("\nDry run - nothing written. Re-run with --apply.")
        return

    if write_playlists:
        os.makedirs(PLAYLIST_DIR, exist_ok=True)
        for tag, rows in sorted(by_tag.items()):
            path = os.path.join(PLAYLIST_DIR, f"{common.sanitize_name(tag)}.m3u8")
            # .m3u8 is UTF-8 by definition, which the Japanese and Telugu titles
            # in this library require. Paths are relative to the playlist file so
            # the tree can be copied to a phone and still resolve.
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("#EXTM3U\n")
                for row in sorted(rows, key=lambda r: (r["album"], r["title"])):
                    audio = resolve_path(row["file_path"])
                    if not audio or not os.path.exists(audio):
                        continue
                    relative = os.path.relpath(audio, PLAYLIST_DIR)
                    seconds = int(row["duration"] or 0)
                    handle.write(f"#EXTINF:{seconds},{row['artist']} - {row['title']}\n")
                    handle.write(f"{relative}\n")
        print(f"\nWrote {len(by_tag)} playlists to {PLAYLIST_DIR}")

    if embed_tags:
        written = write_mp4_tags(tags_by_track, tracks)
        print(f"Embedded tags into {written} files.")
        print("\nNOTE: Apple Music keeps its own metadata database and will NOT")
        print("re-read tags for tracks it has already imported. Remove and re-add")
        print("those tracks, or import them fresh, for the Grouping/BPM to appear.")


def write_mp4_tags(tags_by_track, tracks):
    """Writes categories to the grouping atom and BPM to tmpo.

    Deliberately a separate writer from add_metadata() in the downloader: that
    function takes positional scalars and is called from repair.py, so widening
    it would break repair. It only ever assigns the four atoms it owns and never
    deletes, so writing different atoms here cannot collide with it.
    """
    from mutagen.mp4 import MP4

    written = 0
    for track_id, tags in tags_by_track.items():
        row = tracks.get(track_id)
        if not row:
            continue
        path = resolve_path(row["file_path"])
        if not path or not os.path.exists(path):
            continue
        try:
            audio = MP4(path)
            audio["\xa9grp"] = "; ".join(sorted(tags))
            if row["bpm"]:
                audio["tmpo"] = [int(round(row["bpm"]))]
            audio.save()
            written += 1
            log(track_id, "TAGGED", f"grouping={'; '.join(sorted(tags))}")
        except Exception as error:
            print(f"   could not tag {row['title']}: {type(error).__name__}: {error}")
    return written


def main():
    parser = argparse.ArgumentParser(
        description="Classify the library by rhythm, mood and activity.")
    parser.add_argument("--analyze", action="store_true",
                        help="decode tracks and cache BPM/energy/embeddings")
    parser.add_argument("--prompts", action="store_true",
                        help="expand category seeds into prompt ensembles via the LLM")
    parser.add_argument("--bootstrap", action="store_true",
                        help="propose labels from CLAP + BPM rules")
    parser.add_argument("--llm-tags", action="store_true",
                        help="ask the LLM to tag tracks from title/artist/album")
    parser.add_argument("--review", action="store_true",
                        help="export the least-confident tracks for hand correction")
    parser.add_argument("--learn", action="store_true",
                        help="import corrections and train the probe")
    parser.add_argument("--predict", action="store_true",
                        help="score every track with the trained probe")
    parser.add_argument("--export", action="store_true",
                        help="write playlists and/or embed MP4 tags")
    parser.add_argument("--playlists", action="store_true",
                        help="with --export: write Playlists/*.m3u8")
    parser.add_argument("--embed-tags", action="store_true",
                        help="with --export: write grouping/BPM atoms into the files")
    parser.add_argument("--apply", action="store_true",
                        help="actually write; without it every stage is a dry run")
    parser.add_argument("--only", metavar="MATCH",
                        help="restrict to tracks matching title/album/artist")
    parser.add_argument("--limit", type=int,
                        help="stop after N tracks (useful for a first pass)")
    parser.add_argument("--count", type=int, default=15,
                        help="prompts to generate per category (default 15)")
    parser.add_argument("--count-review", type=int, default=150, metavar="N",
                        help="NEW tracks to add to the review file (default 150)")
    args = parser.parse_args()

    init_schema()

    if args.analyze:
        run_analyze(args.only, args.apply, args.limit)
    elif args.prompts:
        run_prompts(args.count, args.apply)
    elif args.bootstrap:
        run_bootstrap(args.only, args.apply)
    elif args.llm_tags:
        run_llm_tags(limit=args.limit, apply_changes=args.apply)
    elif args.review:
        run_review(args.count_review, args.apply)
    elif args.learn:
        run_learn(args.apply)
    elif args.predict:
        run_predict(args.apply)
    elif args.export:
        run_export(args.playlists, args.embed_tags, args.apply)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
