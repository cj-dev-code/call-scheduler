#!/bin/bash

REPO_DIR="/home/clarify/Desktop/hosting/Archive"
PYTHON="/home/clarify/Desktop/hosting/Archive/.venv/bin/python"
SCRIPT="call_scripy.py"

cd "$REPO_DIR" || exit 1

# Fetch latest from remote
git fetch origin main

# Check if there are any changes
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" != "$REMOTE" ]; then
    echo "[$(date)] Changes detected, pulling and restarting..."
    git pull origin main

    # Kill the running script
    pkill -f "$SCRIPT"

    # Wait for it to die
    sleep 2

    # Restart it
    nohup "$PYTHON" "$REPO_DIR/$SCRIPT" &

    echo "[$(date)] Restarted."
else
    echo "[$(date)] No changes."
fi
