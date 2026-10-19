#!/bin/bash
# VideoFactory - start script (macOS runs this automatically; you never need to).
# "caffeinate" keeps the Mac from going to sleep while the factory is running
# (when plugged in). The screen can still turn off to save power.
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1
source venv/bin/activate
export PYTHONUNBUFFERED=1
echo "=== VideoFactory starting: $(date) ==="
exec /usr/bin/caffeinate -i -s "$PWD/venv/bin/waitress-serve" --listen=0.0.0.0:8000 app.main:app
