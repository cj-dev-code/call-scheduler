#!/bin/bash

REPO_DIR="/home/clarify/Desktop/hosting/Archive"
PYTHON="/home/clarify/Desktop/hosting/Archive/.venv/bin/python"
SCRIPT="call_scripy.py"
PID_FILE="/tmp/call_scripy.pid"
LOCK_FILE="/tmp/update_script.lock"

# Use flock to prevent multiple simultaneous runs of this script
exec 200>"$LOCK_FILE"
flock -n 200 || exit 1

cd "$REPO_DIR" || exit 1

# Function to check if the script is actually running
is_running() {
    if [ -f "$PID_FILE" ]; then
        local pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null && grep -q "$SCRIPT" /proc/$pid/cmdline 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

# Function to start the script
start_script() {
    echo "[$(date)] Starting script..."
    nohup "$PYTHON" "$REPO_DIR/$SCRIPT" > /tmp/call_scripy.log 2>&1 &
    echo $! > "$PID_FILE"
    echo "[$(date)] Started with PID $(cat $PID_FILE)"
}

# Git update check
git fetch origin main
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" != "$REMOTE" ]; then
    echo "[$(date)] Changes detected, pulling and restarting..."
    git pull origin main
    
    # Kill the old process
    if is_running; then
        kill -TERM $(cat "$PID_FILE")
        sleep 2
        # Force kill if still running
        if is_running; then
            kill -9 $(cat "$PID_FILE")
        fi
    fi
    
    # Clear old PID file
    rm -f "$PID_FILE"
    
    # Start fresh
    start_script
    exit 0
fi

# Ensure script is running
if ! is_running; then
    echo "[$(date)] Script not running, starting..."
    rm -f "$PID_FILE"
    start_script
fi