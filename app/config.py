"""
VideoFactory - settings loader.

Reads secret settings (Gemini key, dashboard password hash, session key)
from the hidden .env file. Secrets are never written into code, logs,
or the dashboard.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"


def load_env():
    """Load KEY=VALUE lines from .env into the environment (if not already set)."""
    if not ENV_PATH.exists():
        return
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env()

# --- AI (Google Gemini, free tier) ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

# Free-tier text models to try, in order. If one is unavailable or has hit
# its free limit, the next one is tried. This list can be changed later
# without touching code by adding a line like this to .env:
#   GEMINI_MODELS=gemini-flash-latest,gemini-2.5-flash
GEMINI_MODELS = [
    m.strip()
    for m in os.environ.get(
        "GEMINI_MODELS",
        "gemini-flash-latest,gemini-2.5-flash,gemini-flash-lite-latest,gemini-2.5-flash-lite",
    ).split(",")
    if m.strip()
]

# --- Dashboard login ---
# Only a scrambled "hash" of the password is stored, never the password itself.
DASHBOARD_PASSWORD_HASH = os.environ.get("DASHBOARD_PASSWORD_HASH", "").strip()
# Random secret used to sign the "you are logged in" cookie.
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "").strip()

# --- Free stock photos. Optional: without a key, only Wikimedia Commons is used. ---
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "").strip()
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "").strip()  # used if you ever get one

# --- Voice (Google Gemini text-to-speech, free tier). Tried in this order. ---
GEMINI_TTS_MODELS = [
    m.strip()
    for m in os.environ.get(
        "GEMINI_TTS_MODELS",
        "gemini-3.8-flash-tts,gemini-3.8-flash-lite-tts,gemini-3.1-flash-tts-preview,gemini-2.5-flash-preview-tts",
    ).split(",")
    if m.strip()
]
