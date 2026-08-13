#!/bin/bash
# Syncs the "mixin" Spotify playlist to ~/Documents/Music/tracks and adds any
# newly-downloaded tracks to the matching rekordbox playlist.
set -e

REPO_DIR="/Users/erykhalicki/Documents/Music/code/onthespot/api"
PLAYLIST_URL="https://open.spotify.com/playlist/1kZoRkNN11MeRIGtuv2QuF"
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

echo "=== Downloading tracks added to 'mixin' after $CUTOFF ==="
SYNC_OK=0
HOME="$HOME" "$REPO_DIR/.venv/bin/python" -u "$REPO_DIR/scripts/sync_playlist.py" \
    --added-after "$CUTOFF" \
    --rekordbox-playlist "mixin" \
    --manifest "$MANIFEST" \
    "$PLAYLIST_URL" "$TARGET_DIR" || SYNC_OK=$?

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
    echo "=== Sync left tracks unresolved; not advancing cutoff date so they're retried next run ==="
fi
