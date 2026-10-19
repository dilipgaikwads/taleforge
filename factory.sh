#!/bin/bash
# VideoFactory - control tool.
#
#   bash "$DIR/factory.sh" install    start now AND automatically whenever you log in to the Mac
#   bash "$DIR/factory.sh" status     is it running? + the link for your phone
#   bash "$DIR/factory.sh" restart    restart (e.g. after installing an update)
#   bash "$DIR/factory.sh" stop       stop until you run "start" or log in again
#   bash "$DIR/factory.sh" start      start again after "stop"
#   bash "$DIR/factory.sh" logs       show the last lines of the app's log
#   bash "$DIR/factory.sh" uninstall  remove the automatic start completely
set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.videofactory.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$DIR/data/logs"
DOMAIN="gui/$(id -u)"

TAILSCALE="/Applications/Tailscale.app/Contents/MacOS/Tailscale"

phone_link() {
  local ip ts
  if [ -x "$TAILSCALE" ]; then
    ts=$("$TAILSCALE" ip -4 2>/dev/null | head -n 1)
    if [ -n "$ts" ]; then
      echo "PHONE LINK (works anywhere, never changes): http://$ts:8000"
    else
      echo "Tailscale is installed but not connected. Click its icon in the menu bar and log in."
    fi
  fi
  ip=$(ipconfig getifaddr en0 2>/dev/null || true)
  [ -z "$ip" ] && ip=$(ipconfig getifaddr en1 2>/dev/null || true)
  if [ -n "$ip" ]; then
    echo "PHONE LINK (home Wi-Fi only, may change): http://$ip:8000"
  else
    echo "Home Wi-Fi link: unknown - the Mac does not seem to be on Wi-Fi."
  fi
}

is_loaded()    { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }
is_answering() { curl -s -o /dev/null -m 5 http://127.0.0.1:8000/login; }

wait_until_answering() {
  for _ in $(seq 1 20); do
    is_answering && return 0
    sleep 1
  done
  return 1
}

write_plist() {
  mkdir -p "$HOME/Library/LaunchAgents" "$LOGDIR"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$DIR/start.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOGDIR/app.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/app.log</string>
</dict>
</plist>
EOF
}

report() {
  if is_answering; then
    echo "STATUS: RUNNING"
    phone_link
  elif is_loaded; then
    echo "STATUS: STARTING OR PROBLEM - the Mac is trying to run it but it is not answering yet."
    echo "Wait 30 seconds and run status again. If it stays like this, run: bash "$DIR/factory.sh" logs"
  else
    echo "STATUS: STOPPED"
  fi
}

case "${1:-status}" in
  install)
    if is_answering && ! is_loaded; then
      echo "The app is already running in a Terminal window."
      echo "Go to that window, press Control + C to stop it, then run this command again."
      exit 1
    fi
    chmod +x "$DIR/start.sh" "$DIR/factory.sh"
    write_plist
    launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1
    sleep 1
    if ! launchctl bootstrap "$DOMAIN" "$PLIST"; then
      echo "PROBLEM: macOS did not accept the automatic start. Send this screen to your AI co-founder."
      exit 1
    fi
    echo "Automatic start installed. Starting the factory..."
    if wait_until_answering; then
      echo "INSTALLED AND RUNNING."
      phone_link
      echo "It will now start by itself every time you log in to this Mac."
    else
      echo "Installed, but the app is not answering yet."
      echo "Wait 30 seconds, then run: bash "$DIR/factory.sh" status"
    fi
    ;;
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1
    rm -f "$PLIST"
    echo "Automatic start removed. The factory is stopped."
    ;;
  start)
    if [ ! -f "$PLIST" ]; then echo "Not installed yet. Run: bash "$DIR/factory.sh" install"; exit 1; fi
    launchctl bootstrap "$DOMAIN" "$PLIST" >/dev/null 2>&1 || launchctl kickstart "$DOMAIN/$LABEL"
    wait_until_answering
    report
    ;;
  stop)
    launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1
    echo "STOPPED. It starts again when you run 'start' or next time you log in to the Mac."
    ;;
  restart)
    if ! is_loaded; then
      [ -f "$PLIST" ] && launchctl bootstrap "$DOMAIN" "$PLIST" >/dev/null 2>&1
    else
      launchctl kickstart -k "$DOMAIN/$LABEL"
    fi
    sleep 2
    wait_until_answering
    report
    ;;
  status)
    report
    ;;
  logs)
    tail -n 40 "$LOGDIR/app.log" 2>/dev/null || echo "No log yet."
    ;;
  link)
    phone_link
    ;;
  *)
    echo "Unknown command. Use: install, status, restart, stop, start, logs, link, uninstall"
    exit 1
    ;;
esac
