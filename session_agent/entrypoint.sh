#!/bin/bash
set -e

# Working dir is /home/computeruse (set by base image)
# Start virtual display, window manager, VNC
./start_all.sh

# Start noVNC WebSocket bridge (browser-accessible VNC on port 6080)
./novnc_startup.sh

# Start static HTTP server
python http_server.py > /tmp/http_server.log 2>&1 &

echo "✅ Desktop services started"

# Start session agent (replaces Streamlit)
DISPLAY=:${DISPLAY_NUM:-1} exec python session_agent.py
