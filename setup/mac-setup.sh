#!/bin/bash
# VideoFactory - one-command setup for macOS.
#
#   bash setup/mac-setup.sh        (run from the VideoFactory folder)
#
# Safe to run again: every step checks first and skips what is already done.
# It never uploads or prints your keys or password.
set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY_FRAMEWORK="/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13"
PY_VERSION="3.13.16"
cd "$DIR" || exit 1

say()  { printf "\n\033[1m== %s\033[0m\n" "$1"; }
ok()   { printf "   OK: %s\n" "$1"; }
fail() { printf "\n   PROBLEM: %s\n   Copy this screen and send it to your AI co-founder.\n" "$1"; exit 1; }
has_key() { [ -f .env ] && grep -q "^$1=." .env; }

say "VideoFactory setup in: $DIR"
[ -d "$DIR/app" ] || fail "This does not look like the VideoFactory folder (no 'app' folder here)."

# ---------------------------------------------------------------- 1. Python
say "1/7  Python $PY_VERSION (official version from python.org)"
if [ -x "$PY_FRAMEWORK" ]; then
  ok "already installed: $("$PY_FRAMEWORK" --version)"
else
  echo "   Downloading the official installer..."
  curl -fL -o "/tmp/python-$PY_VERSION-macos11.pkg" \
    "https://www.python.org/ftp/python/$PY_VERSION/python-$PY_VERSION-macos11.pkg" || fail "Could not download Python."
  echo "   Installing. Your Mac password is needed (typing stays invisible):"
  sudo installer -pkg "/tmp/python-$PY_VERSION-macos11.pkg" -target / || fail "Python did not install."
  rm -f "/tmp/python-$PY_VERSION-macos11.pkg"
  sh "/Applications/Python 3.13/Install Certificates.command" >/dev/null 2>&1
  [ -x "$PY_FRAMEWORK" ] || fail "Python 3.13 is still missing after installing."
  ok "installed: $("$PY_FRAMEWORK" --version)"
fi

# ---------------------------------------------------------------- 2. FFmpeg
say "2/7  FFmpeg and FFprobe (video building, with caption support)"
mkdir -p bin
if [ -x bin/ffmpeg ] && [ -x bin/ffprobe ] && bin/ffmpeg -hide_banner -filters 2>/dev/null | grep -q " subtitles "; then
  ok "already installed: $(bin/ffmpeg -version | head -n 1)"
else
  if [ "$(uname -m)" = "arm64" ]; then
    # Apple-chip Macs (not yet tested by us): builds by Martin Riedl
    FF_URL="https://ffmpeg.martin-riedl.de/redirect/latest/macos/arm64/release/ffmpeg.zip"
    FP_URL="https://ffmpeg.martin-riedl.de/redirect/latest/macos/arm64/release/ffprobe.zip"
  else
    # Intel Macs (tested): builds from evermeet.cx
    FF_URL="https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip"
    FP_URL="https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip"
  fi
  curl -fL -o /tmp/vf-ffmpeg.zip "$FF_URL" && curl -fL -o /tmp/vf-ffprobe.zip "$FP_URL" || fail "Could not download FFmpeg."
  unzip -o -q /tmp/vf-ffmpeg.zip -d bin && unzip -o -q /tmp/vf-ffprobe.zip -d bin || fail "Could not unpack FFmpeg."
  rm -f /tmp/vf-ffmpeg.zip /tmp/vf-ffprobe.zip
  chmod +x bin/ffmpeg bin/ffprobe
  xattr -d com.apple.quarantine bin/ffmpeg bin/ffprobe 2>/dev/null
  bin/ffmpeg -hide_banner -filters 2>/dev/null | grep -q " subtitles " || fail "FFmpeg installed but cannot draw captions."
  ok "installed: $(bin/ffmpeg -version | head -n 1)"
fi

# ---------------------------------------------------------------- 3. Toolbox
say "3/7  Private Python toolbox and add-ons"
if [ ! -x venv/bin/python ]; then
  "$PY_FRAMEWORK" -m venv venv || fail "Could not create the toolbox."
fi
venv/bin/python -m pip install --quiet --upgrade pip && venv/bin/python -m pip install --quiet -r requirements.txt \
  || fail "Could not install the add-ons (check the internet connection)."
ok "ready: $(venv/bin/python --version), flask + waitress installed"

# ---------------------------------------------------------------- 4. Keys
say "4/7  Free AI key (Google Gemini)"
if has_key GEMINI_API_KEY; then
  ok "already saved"
else
  echo "   Create a free key at https://aistudio.google.com/apikey (no billing), copy it, then paste it here."
  venv/bin/python -m app.set_key GEMINI_API_KEY || fail "The Gemini key was not saved."
fi

say "     Optional: free Pixabay key for more pictures"
if has_key PIXABAY_API_KEY; then
  ok "already saved"
else
  read -r -p "   Do you have a Pixabay key to add now? (y/N) " answer
  if [ "${answer:-n}" = "y" ] || [ "${answer:-n}" = "Y" ]; then
    venv/bin/python -m app.set_key PIXABAY_API_KEY
  else
    ok "skipped (Wikimedia Commons will be used; add later with: venv/bin/python -m app.set_key PIXABAY_API_KEY)"
  fi
fi

# ---------------------------------------------------------------- 5. Password
say "5/7  Dashboard password"
if has_key DASHBOARD_PASSWORD_HASH; then
  ok "already set (change any time with: venv/bin/python -m app.set_password)"
else
  venv/bin/python -m app.set_password || fail "The password was not saved."
fi
chmod 600 .env 2>/dev/null

# ---------------------------------------------------------------- 6. Check
say "6/7  Quick check of the code"
venv/bin/python -m py_compile app/*.py || fail "The app code has an error."
ok "code is fine"

# ---------------------------------------------------------------- 7. Start
say "7/7  Start automatically and stay awake"
if curl -s -o /dev/null -m 3 http://127.0.0.1:8000/login && ! launchctl print "gui/$(id -u)/com.videofactory.app" >/dev/null 2>&1; then
  fail "Another copy of VideoFactory is already running on this Mac (port 8000). Stop it first (Control + C in its Terminal window, or 'bash factory.sh stop' in its folder)."
fi
bash "$DIR/factory.sh" install

say "Done. Next steps (see docs/VideoFactory-Setup-Guide.pdf):"
echo "   - Open http://127.0.0.1:8000 on this Mac, log in, open System check, tap Test AI connection."
echo "   - Permanent phone link: install Tailscale (guide step M10), then: bash factory.sh status"
echo "   - YouTube: guide Part C, then step M12."
echo "   - Keep the Mac plugged in with the lid open."
