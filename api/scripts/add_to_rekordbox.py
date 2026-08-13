#!/usr/bin/env python3
"""
add_to_rekordbox.py
~~~~~~~~~~~~~~~~~~~~

Reads the JSON manifest written by sync_playlist.py (list of
{file_path, title, artists, playlist}) and registers each track in the
rekordbox collection, adding it to the named playlist (created if missing).

Runs in its own venv (.venv-rekordbox) since pyrekordbox needs a newer
`construct` than pywidevine (used elsewhere in this project) tolerates.

Usage:
    .venv-rekordbox/bin/python scripts/add_to_rekordbox.py <manifest.json>
"""

import json
import os
import sys

from pyrekordbox import Rekordbox6Database


def get_or_create_artist(db, name: str):
    if not name:
        return None
    artist = db.get_artist(Name=name).one_or_none()
    if artist is None:
        artist = db.add_artist(name=name)
    return artist


def get_or_create_playlist(db, name: str):
    playlist = db.get_playlist(Name=name).one_or_none()
    if playlist is None:
        print(f"Creating rekordbox playlist '{name}'")
        playlist = db.create_playlist(name)
    return playlist


def add_track(db, playlist, file_path: str, title: str, artists: list) -> str:
    """Returns 'added', 'already_in_playlist', or 'failed'."""
    if not os.path.exists(file_path):
        print(f"  SKIP (file missing): {file_path}")
        return "failed"

    content = db.get_content(FolderPath=file_path).one_or_none()
    if content is None:
        kwargs = {"Title": title}
        if artists:
            artist = get_or_create_artist(db, artists[0])
            if artist is not None:
                kwargs["ArtistID"] = artist.ID
        content = db.add_content(file_path, **kwargs)

    already_in_playlist = any(song.ContentID == content.ID for song in playlist.Songs)
    if already_in_playlist:
        return "already_in_playlist"

    db.add_to_playlist(playlist, content)
    return "added"


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <manifest.json>")
        sys.exit(1)

    manifest_path = sys.argv[1]
    if not os.path.exists(manifest_path):
        print(f"Manifest not found: {manifest_path}")
        sys.exit(1)

    with open(manifest_path, "r", encoding="utf-8") as f:
        records = json.load(f)

    if not records:
        print("Manifest is empty, nothing to do.")
        return

    db = Rekordbox6Database()

    playlists = {}
    added = 0
    already = 0
    failed = 0

    for rec in records:
        playlist_name = rec.get("playlist") or "mixin"
        if playlist_name not in playlists:
            playlists[playlist_name] = get_or_create_playlist(db, playlist_name)
        playlist = playlists[playlist_name]

        title = rec.get("title", "")
        try:
            result = add_track(db, playlist, rec["file_path"], title, rec.get("artists", []))
            if result == "added":
                added += 1
                print(f"  + added: {title}")
            elif result == "already_in_playlist":
                already += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1
            print(f"  FAILED: {title}: {e}")

        db.commit()

    print(f"\nDone. {added} added, {already} already in playlist, {failed} failed "
          f"(out of {len(records)} manifest entries).")

    # Manifest has been fully processed - clear it so re-runs don't redo work.
    os.remove(manifest_path)


if __name__ == "__main__":
    main()
