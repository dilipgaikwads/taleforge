"""
VideoFactory - set or change the dashboard password.

Run it from the VideoFactory folder on the Mac:
    venv/bin/python -m app.set_password

- Your typing stays invisible.
- Only a scrambled hash of the password is saved (in the hidden .env file).
- Every phone/computer that was logged in is logged out, so after a
  password change only people who know the NEW password can get in.
"""
import getpass
import os
import secrets
import sys

from werkzeug.security import generate_password_hash

from .config import ENV_PATH

MIN_LENGTH = 10


def update_env(updates):
    """Replace or add KEY=VALUE lines in .env, keeping every other line."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    kept = [line for line in lines if line.split("=", 1)[0].strip() not in updates]
    for key, value in updates.items():
        kept.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(kept) + "\n", encoding="utf-8")
    os.chmod(ENV_PATH, 0o600)


def main():
    print()
    print(f"Choose a dashboard password (at least {MIN_LENGTH} characters).")
    print("Nothing will appear while you type. That is normal.")
    print()
    password = getpass.getpass("New password: ")
    if len(password) < MIN_LENGTH:
        print(f"\nToo short. Please use at least {MIN_LENGTH} characters. Nothing was changed.")
        sys.exit(1)
    if getpass.getpass("Type it again: ") != password:
        print("\nThe two passwords did not match. Nothing was changed.")
        sys.exit(1)

    update_env({
        "DASHBOARD_PASSWORD_HASH": generate_password_hash(password),
        "FLASK_SECRET_KEY": secrets.token_hex(32),
    })
    print("\nPASSWORD SAVED. Start (or restart) the app for it to take effect.")


if __name__ == "__main__":
    main()
