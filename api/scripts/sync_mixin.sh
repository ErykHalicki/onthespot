#!/bin/bash
# Syncs the "mixin" Spotify playlist and the "unreleased-music" SoundCloud
# playlist to ~/Documents/Music/tracks and adds any newly-downloaded tracks
# to the matching rekordbox playlist.
set -e

REPO_DIR="/Users/erykhalicki/Documents/Music/code/onthespot/api"
SPOTIFY_PLAYLIST_URL="https://open.spotify.com/playlist/1kZoRkNN11MeRIGtuv2QuF"
SOUNDCLOUD_PLAYLIST_URL="https://soundcloud.com/eryk-halicki-711009316/sets/unreleased-music"
TARGET_DIR="/Users/erykhalicki/Documents/Music/tracks"
MANIFEST="$REPO_DIR/scripts/.rekordbox_manifest.json"
STATE_FILE="$REPO_DIR/scripts/.mixin_last_sync"

cd "$REPO_DIR"

if [ -f "$STATE_FILE" ]; then
    CUTOFF=$(cat "$STATE_FILE")
else
    # First-ever run: everything before this date is assumed already downloaded.
    CUTOFF="2026-06-09"
fi

echo "=== Downloading Spotify tracks added to 'mixin' after $CUTOFF ==="
SYNC_OK=0
HOME="$HOME" "$REPO_DIR/.venv/bin/python" -u "$REPO_DIR/scripts/sync_playlist.py" \
    --added-after "$CUTOFF" \
    --rekordbox-playlist "mixin" \
    --manifest "$MANIFEST" \
    "$SPOTIFY_PLAYLIST_URL" "$TARGET_DIR" || SYNC_OK=$?

echo ""
echo "=== Downloading SoundCloud tracks from 'unreleased-music' ==="
# SoundCloud playlists have no per-track added-date, so this always checks
# the full playlist; already-downloaded tracks are skipped via local
# duplicate detection instead of a date cutoff.
SOUNDCLOUD_SYNC_OK=0
HOME="$HOME" "$REPO_DIR/.venv/bin/python" -u "$REPO_DIR/scripts/sync_playlist.py" \
    --rekordbox-playlist "mixin" \
    --manifest "$MANIFEST" \
    "$SOUNDCLOUD_PLAYLIST_URL" "$TARGET_DIR" || SOUNDCLOUD_SYNC_OK=$?

if [ -f "$MANIFEST" ]; then
    echo ""
    echo "=== Adding downloaded tracks to rekordbox ==="
    "$REPO_DIR/.venv-rekordbox/bin/python" "$REPO_DIR/scripts/add_to_rekordbox.py" "$MANIFEST"
else
    echo "No new tracks to add to rekordbox."
fi

if [ "$SYNC_OK" -eq 0 ]; then
    date -u +%Y-%m-%d > "$STATE_FILE"
else
    echo ""
    echo "=== Spotify sync left tracks unresolved; not advancing cutoff date so they're retried next run ==="
fi

if [ "$SOUNDCLOUD_SYNC_OK" -ne 0 ]; then
    echo ""
    echo "=== SoundCloud sync left tracks unresolved; they'll be retried next run ==="
fi
