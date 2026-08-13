#!/usr/bin/env python3
"""
sync_playlist.py
~~~~~~~~~~~~~~~~~

Sync a Spotify playlist to a local directory using the OnTheSpot v2 backend,
skipping tracks that already exist on disk (matched by embedded ID3 tags,
with a filename-based fallback for untagged files).

Usage:
    .venv/bin/python scripts/sync_playlist.py <playlist_url> <target_dir>
"""

import json
import os
import re
import sys
import time
import unicodedata

os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
os.environ.setdefault("ONTHESPOTDIR", os.path.expanduser("~/.config/onthespot"))

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mutagen import File as MutagenFile  # noqa: E402

from onthespot.accounts import FillAccountPool, get_account_token  # noqa: E402
from onthespot.api.spotify import (  # noqa: E402
    spotify_get_playlist_data,
    spotify_get_playlist_items,
)
from onthespot.constants import ItemStatus  # noqa: E402
from onthespot.downloader import DownloadWorker  # noqa: E402
from onthespot.otsconfig import config  # noqa: E402
from onthespot.runtimedata import download_queue, pending  # noqa: E402
from onthespot.utils import format_local_id  # noqa: E402

# rekordbox writes happen in a separate process/venv (pyrekordbox needs a
# newer `construct` than pywidevine tolerates in this venv) - see
# scripts/add_to_rekordbox.py. This script just appends completed downloads
# to a JSON manifest that script consumes.
DEFAULT_MANIFEST = os.path.join(os.path.dirname(__file__), ".rekordbox_manifest.json")

AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav"}
FEAT_RE = re.compile(r"\s*[\(\[].*?(feat\.?|ft\.?|with)\s.*?[\)\]]", re.IGNORECASE)
FEAT_TAIL_RE = re.compile(r"\s+(feat\.?|ft\.?)\s.*$", re.IGNORECASE)
QUALIFIER_RE = re.compile(
    r"\s*[\(\[][^\)\]]*"
    r"(remix|edit|version|remaster|mix|cover|nightcore|extended|radio|acoustic|live"
    r"|official|audio|video|visualizer|lyrics?|hq|hd|mv|clean|explicit|hearthis)"
    r"[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
TRAILING_JUNK_RE = re.compile(r"[!\.]+$")
NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")
WS_RE = re.compile(r"\s+")
ARTIST_SPLIT_RE = re.compile(r"\s*[,;/&]\s*|\s+feat\.?\s+|\s+ft\.?\s+|\s+x\s+", re.IGNORECASE)
LEADING_BRACKET_RE = re.compile(r"^\s*[\[\(][^\]\)]*[\]\)]\s*[-]?\s*")


def normalize(text: str) -> str:
    """Lowercase, strip accents/punctuation/feat.-clauses for fuzzy matching."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = FEAT_RE.sub(" ", text)
    text = FEAT_TAIL_RE.sub(" ", text)
    text = QUALIFIER_RE.sub(" ", text)
    text = text.lower()
    text = NON_ALNUM_RE.sub(" ", text)
    text = WS_RE.sub(" ", text).strip()
    return text


def split_artists(artist_field: str) -> set[str]:
    """Tags often join multiple artists into one string (e.g. '1tbsp, Mietze Conte').
    Split on common separators and normalize each individually."""
    if not artist_field:
        return set()
    parts = ARTIST_SPLIT_RE.split(artist_field)
    return {normalize(p) for p in parts if normalize(p)}


def build_existing_index(target_dir: str):
    """Scan target_dir for audio files and index them by title.

    title_artists: normalized title -> set of normalized artist tokens seen
                   tagged against that title (empty set if untagged).
    filename_titles: normalized title-guesses derived from filenames, used as
                      a fallback for files with missing/unreadable tags.
    """
    title_artists: dict[str, set[str]] = {}
    filename_titles: set[str] = set()
    scanned = 0
    tagged = 0

    for root, _dirs, files in os.walk(target_dir):
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in AUDIO_EXTS:
                continue
            scanned += 1
            path = os.path.join(root, fname)
            stem = os.path.splitext(fname)[0]

            # Filename fallback candidates: full stem, stem with a leading
            # "[Artist] " prefix stripped, and every individual " - "
            # delimited segment (titles can land in any position, e.g.
            # "Artist - Title (Remix) - HQ!").
            filename_titles.add(normalize(stem))
            stripped = LEADING_BRACKET_RE.sub("", stem)
            filename_titles.add(normalize(stripped))
            for segment in stripped.split(" - "):
                norm_segment = normalize(segment)
                if norm_segment and norm_segment not in {"hq", "hd", "official", "audio", "video"}:
                    filename_titles.add(norm_segment)

            try:
                audio = MutagenFile(path, easy=True)
                if audio is None or not audio.tags:
                    continue
                title = (audio.tags.get("title") or [""])[0]
                artist = (audio.tags.get("artist") or [""])[0]
                if title:
                    tagged += 1
                    norm_title = normalize(title)
                    title_artists.setdefault(norm_title, set())
                    title_artists[norm_title] |= split_artists(artist)
            except Exception:
                continue

    return title_artists, filename_titles, scanned, tagged


def _fuzzy_contains(norm_title: str, candidate: str) -> bool:
    """True if norm_title and candidate are close enough to be the same title,
    tolerating trailing junk (e.g. stray "5 1" from "[5.1]", or one side
    carrying a "- remix"-style suffix the other lacks)."""
    if not candidate or len(candidate) < 4 or len(norm_title) < 4:
        return norm_title == candidate
    if norm_title == candidate:
        return True
    shorter, longer = sorted((norm_title, candidate), key=len)
    if shorter not in longer:
        return False
    # Require the match to cover most of the longer string so unrelated
    # titles that merely share a common short word don't false-positive.
    return len(shorter) / len(longer) >= 0.6


def already_have(track_name: str, artists: list[str], title_artists, filename_titles) -> str | None:
    """Return a reason string if the track appears to already exist locally, else None."""
    norm_title = normalize(track_name)
    if not norm_title:
        return None
    norm_track_artists = {normalize(a) for a in artists}

    for existing_title, existing_artists in title_artists.items():
        if not _fuzzy_contains(norm_title, existing_title):
            continue
        if not existing_artists or existing_artists & norm_track_artists:
            return f"tag match ({', '.join(artists)} - {track_name})"
        # Title matched but tagged artist(s) look unrelated - likely a
        # different song with the same title, don't treat as a duplicate.

    for candidate in filename_titles:
        if _fuzzy_contains(norm_title, candidate):
            return f"filename match ({track_name})"

    return None


def append_to_manifest(manifest_path: str, entry: dict) -> None:
    """Append one completed-download record to the JSON manifest file that
    add_to_rekordbox.py (running in its own venv) will later consume."""
    records = []
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                records = json.load(f)
        except Exception:
            records = []
    records.append(entry)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)


def main():
    raw_args = sys.argv[1:]
    dry_run = "--dry-run" in raw_args
    no_rekordbox = "--no-rekordbox" in raw_args
    added_after = None
    rekordbox_playlist_name = None
    manifest_path = DEFAULT_MANIFEST
    # A Spotify account has exactly one shared session; running multiple
    # concurrent workers against it lets one worker's session reinit (from a
    # transient error) yank the connection out from under another worker's
    # in-flight stream read, which then hangs forever. Default to 1 and let
    # --workers override for setups with multiple Spotify accounts.
    workers_override = 1
    args = []
    i = 0
    while i < len(raw_args):
        a = raw_args[i]
        if a in ("--dry-run", "--no-rekordbox"):
            pass
        elif a == "--added-after":
            i += 1
            added_after = raw_args[i]
        elif a == "--rekordbox-playlist":
            i += 1
            rekordbox_playlist_name = raw_args[i]
        elif a == "--manifest":
            i += 1
            manifest_path = raw_args[i]
        elif a == "--workers":
            i += 1
            workers_override = int(raw_args[i])
        else:
            args.append(a)
        i += 1

    if len(args) != 2:
        print(f"Usage: {sys.argv[0]} [--dry-run] [--added-after YYYY-MM-DD] "
              f"[--rekordbox-playlist NAME] [--no-rekordbox] [--manifest PATH] "
              f"<playlist_url> <target_dir>")
        sys.exit(1)

    playlist_url, target_dir = args
    target_dir = os.path.abspath(target_dir)

    m = re.search(r"playlist/([a-zA-Z0-9]+)", playlist_url)
    if not m:
        print("Could not find a playlist ID in the given URL.")
        sys.exit(1)
    playlist_id = m.group(1)

    os.makedirs(target_dir, exist_ok=True)
    config.set("audio_download_path", target_dir)
    if workers_override is not None:
        config.set("maximum_download_workers", workers_override)

    print(f"Scanning existing files in: {target_dir}")
    title_artists, filename_titles, scanned, tagged = build_existing_index(target_dir)
    print(f"  {scanned} audio files scanned, {tagged} tagged, "
          f"{len(title_artists)} unique titles, "
          f"{len(filename_titles)} filename-fallback titles.")

    print("Logging in...")
    account_pool_loader = FillAccountPool()
    account_pool_loader.start()
    account_pool_loader.thread.join()

    token = get_account_token("spotify")
    if token is None:
        print("No active Spotify account/session available. Aborting.")
        sys.exit(1)

    print(f"Fetching playlist: {playlist_id}")
    playlist_name, playlist_by = spotify_get_playlist_data(token, playlist_id)
    items = spotify_get_playlist_items(token, playlist_id)
    print(f"Playlist '{playlist_name}' by {playlist_by}: {len(items)} items")

    queued = []
    skipped = []
    too_old = 0

    for index, item in enumerate(items):
        track = item.get("track")
        if not track or not track.get("id"):
            continue

        if added_after and item.get("added_at", "") <= added_after:
            too_old += 1
            continue

        track_id = track["id"]
        track_name = track.get("name", "")
        artists = [a.get("name", "") for a in track.get("artists", [])]

        # When --added-after already scopes the candidate set precisely, skip
        # the fuzzy pre-filter and let every candidate go through the pipeline -
        # the downloader's own exact-filename check reports "Already Exists"
        # (without re-downloading) and that status still gets manifested,
        # whereas a fuzzy pre-filter skip here never enters the queue at all.
        if not added_after:
            reason = already_have(track_name, artists, title_artists, filename_titles)
            if reason:
                skipped.append((track_name, artists, reason))
                continue

        queued.append((track_id, track_name, artists, index))

    if added_after:
        print(f"\n{too_old} tracks added on/before {added_after} were excluded outright.")
    print(f"Would queue {len(queued)} new tracks, skip {len(skipped)} already-local tracks.\n")

    if dry_run:
        print("=== DRY RUN: tracks that WOULD be downloaded ===")
        for _, name, artists, _ in queued:
            print(f"  NEW: {', '.join(artists)} - {name}")
        print("\n=== sample of tracks matched as already-local ===")
        for name, artists, reason in skipped[:15]:
            print(f"  SKIP: {', '.join(artists)} - {name}  [{reason}]")
        return

    if not queued:
        print("Nothing to download.")
        return

    write_manifest = not no_rekordbox
    rb_name = rekordbox_playlist_name or playlist_name
    if write_manifest:
        print(f"\nCompleted downloads will be recorded to manifest: {manifest_path}")
        print(f"(run scripts/add_to_rekordbox.py against it to add them to rekordbox playlist '{rb_name}')")

    num_workers = max(1, int(config.get("maximum_download_workers") or 1))
    print(f"Starting {num_workers} download worker(s)...")
    for _ in range(num_workers):
        downloadworker = DownloadWorker()
        downloadworker.start()

    queued_with_ids = []
    for track_id, track_name, artists, index in queued:
        local_id = format_local_id(track_id)
        queued_with_ids.append((local_id, track_name, artists))
        pending.put_nowait(
            {
                "local_id": local_id,
                "item_service": "spotify",
                "item_type": "track",
                "item_id": track_id,
                "parent_category": "playlist",
                "playlist_name": playlist_name,
                "playlist_by": playlist_by,
                "playlist_number": str(index + 1),
                "available": True,
                "item_status": ItemStatus.WAITING,
                "item_url": f"https://open.spotify.com/track/{track_id}",
            }
        )
    queued = queued_with_ids

    terminal = {ItemStatus.DOWNLOADED, ItemStatus.FAILED, ItemStatus.ALREADY_EXISTS,
                ItemStatus.UNAVAILABLE, "Downloaded", "Failed", "Already Exists", "Unavailable"}
    ok_to_add = {ItemStatus.DOWNLOADED, ItemStatus.ALREADY_EXISTS, "Downloaded", "Already Exists"}
    pending_ids = {local_id for local_id, _, _ in queued}
    manifested = 0
    processed_for_manifest = set()

    # A single stalled download (e.g. a stream read that hangs mid-track with
    # no timeout deep in librespot) must not hang the whole sync forever.
    # Track each item's last-seen progress; if nothing has moved on any
    # non-terminal item for STALL_TIMEOUT seconds, give up on the remainder
    # so the run can finish and be retried next time.
    STALL_TIMEOUT = 180
    last_progress = {}
    last_progress_change = time.monotonic()

    poll_count = 0
    incomplete = False
    while True:
        time.sleep(2)
        poll_count += 1
        done = 0
        stuck = []
        for local_id in pending_ids:
            item = download_queue.get(local_id)
            if not item:
                stuck.append((local_id, "not in queue", None))
                continue
            status = item.get("item_status")
            if status in terminal:
                done += 1
            else:
                stuck.append((local_id, status, item.get("item_name") or item.get("name")))
                progress = item.get("progress")
                if last_progress.get(local_id) != progress:
                    last_progress[local_id] = progress
                    last_progress_change = time.monotonic()
            if (
                write_manifest
                and local_id not in processed_for_manifest
                and status in ok_to_add
                and item.get("file_path")
            ):
                processed_for_manifest.add(local_id)
                artist_field = item.get("artist") or ""
                separator = config.get("metadata_separator") or ", "
                append_to_manifest(manifest_path, {
                    "file_path": item["file_path"],
                    "title": item.get("name") or "",
                    "artists": [a.strip() for a in artist_field.split(separator) if a.strip()],
                    "playlist": rb_name,
                })
                manifested += 1
        print(f"Progress: {done}/{len(pending_ids)} resolved, "
              f"{manifested} recorded to manifest", end="\r")
        if poll_count % 15 == 0 and stuck:
            print(f"\nStill waiting on: " + "; ".join(
                f"{name or local_id} [{status}]" for local_id, status, name in stuck))
        if done >= len(pending_ids):
            break
        if stuck and time.monotonic() - last_progress_change > STALL_TIMEOUT:
            print(f"\nGiving up on stalled track(s) after {STALL_TIMEOUT}s with no progress: "
                  + "; ".join(f"{name or local_id} [{status}]" for local_id, status, name in stuck))
            print("They will be retried on the next sync run.")
            incomplete = True
            break

    print("\n\nDone. Results:")
    counts = {}
    for local_id in pending_ids:
        item = download_queue.get(local_id)
        status = str(item.get("item_status")) if item else "Unknown"
        counts[status] = counts.get(status, 0) + 1
        if status not in ("Downloaded", "Already Exists"):
            print(f"  [{status}] {item.get('item_name', local_id) if item else local_id}")

    for status, count in counts.items():
        print(f"  {status}: {count}")

    if write_manifest:
        print(f"\n{manifested} tracks recorded to manifest ({manifest_path}).")
        print(f"Run: {os.path.join(os.path.dirname(__file__), '.venv-rekordbox', 'bin', 'python')} "
              f"{os.path.join(os.path.dirname(__file__), 'add_to_rekordbox.py')} {manifest_path}")

    return incomplete


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
