"""
VideoFactory - save a service key (like your Pixabay key) into the hidden .env file.

Run it from the VideoFactory folder on the Mac, for example:
    venv/bin/python -m app.set_key PIXABAY_API_KEY

- Your typing/pasting stays invisible.
- Every other line in .env (Gemini key, password) is kept unchanged.
"""
import getpass
import os
import sys

from .config import ENV_PATH

ALLOWED_KEYS = {"PIXABAY_API_KEY", "PEXELS_API_KEY", "GEMINI_API_KEY"}


def update_env(updates):
    """Replace or add KEY=VALUE lines in .env, keeping every other line."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    kept = [line for line in lines if line.split("=", 1)[0].strip() not in updates]
    for key, value in updates.items():
        kept.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(kept) + "\n", encoding="utf-8")
    os.chmod(ENV_PATH, 0o600)


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    if name not in ALLOWED_KEYS:
        print("Please say which key to save, e.g.:  venv/bin/python -m app.set_key PIXABAY_API_KEY")
        sys.exit(1)
    print(f"\nPaste your {name} and press Return. Nothing will appear on screen. That is normal.\n")
    value = getpass.getpass(f"{name}: ").strip()
    if len(value) < 10 or any(ch.isspace() for ch in value):
        print("\nThat does not look like a complete key. Nothing was changed. Please try again.")
        sys.exit(1)
    update_env({name: value})
    print(f"\n{name} SAVED SAFELY (first 4 characters: {value[:4]}...). Restart the app to use it.")


if __name__ == "__main__":
    main()
