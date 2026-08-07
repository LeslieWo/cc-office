#!/bin/zsh
# Double-click me. Restarts CC Office and opens it in the browser.
cd "$(dirname "$0")" || exit 1

PORT=8910

# An older copy still holding the port would silently keep serving stale code.
lsof -ti tcp:$PORT | xargs -r kill 2>/dev/null
sleep 1

nohup /usr/bin/python3 server.py > /tmp/cc-office.log 2>&1 &
sleep 1

if lsof -ti tcp:$PORT > /dev/null; then
  open "http://localhost:$PORT"
  echo "CC Office is up:  http://localhost:$PORT"
  echo "To stop it: close this window, or run  lsof -ti tcp:$PORT | xargs kill"
else
  echo "Failed to start. Log:"
  cat /tmp/cc-office.log
fi
