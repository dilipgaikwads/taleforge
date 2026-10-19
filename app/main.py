"""
VideoFactory - main web application (Step 22).

Pages (all protected by the password login in auth.py):
  /                 Dashboard: your channels + link to system check
  /channels/new     Create a channel           (channels.py)
  /channels/<id>/edit  Edit a channel          (channels.py)
  /channels/<id>/topics  Topic ideas, scores, duplicates (topics.py + topic_engine.py)
  /jobs/<id>        One video moving through the factory (jobs.py + pipeline.py)
  /youtube          YouTube connection (social.py + youtube.py)
  /system           Real system checks (Python, FFmpeg, captions, database, AI, disk)
  /test-ai          Button: one REAL request to Google Gemini
  /health           Same checks, as machine-readable data
"""
import os
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request

from .auth import init_auth
from .config import PEXELS_API_KEY, PIXABAY_API_KEY
from .channels import bp as channels_bp
from .db import DB_PATH, REQUIRED_TABLES, get_db, init_db, log_event
from .jobs import bp as jobs_bp
from .llm import GeminiProvider, LLMError
from .pipeline import STAGE_LABELS, start_worker, worker_alive
from .social import bp as social_bp
from .youtube import connection_status
from .topics import bp as topics_bp

BASE_DIR = Path(__file__).resolve().parent.parent
BIN_DIR = BASE_DIR / "bin"
FFMPEG = BIN_DIR / ("ffmpeg" + (".exe" if os.name == "nt" else ""))
FFPROBE = BIN_DIR / ("ffprobe" + (".exe" if os.name == "nt" else ""))
MIN_FREE_GB = 20

# Create/upgrade the database and record that the app started.
init_db()
log_event("app_started", f"Python {sys.version.split()[0]}")

ai = GeminiProvider()
app = Flask(__name__)
init_auth(app)
app.register_blueprint(channels_bp)
app.register_blueprint(topics_bp)
app.register_blueprint(jobs_bp)
app.register_blueprint(social_bp)

# Start the background production line (one per app).
start_worker()


# ---------- small helpers ----------

def get_setting(key, default=None):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def run_command(args):
    """Run a command and return (success, combined output text)."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        return result.returncode == 0, (result.stdout or "") + (result.stderr or "")
    except Exception as exc:
        return False, str(exc)


# ---------- checks ----------

def check_python():
    in_venv = sys.prefix != sys.base_prefix
    version = sys.version.split()[0]
    where = "inside" if in_venv else "NOT inside"
    return {
        "name": "Python (project toolbox)",
        "ok": in_venv and version.startswith("3.13"),
        "detail": f"Python {version}, {where} the VideoFactory toolbox",
    }


def check_binary(label, path):
    if not path.exists():
        return {"name": label, "ok": False, "detail": f"Missing: {path}"}
    ok, output = run_command([str(path), "-version"])
    text = output.strip()
    first_line = text.splitlines()[0] if text else "No output"
    return {"name": label, "ok": ok, "detail": first_line}


def check_captions():
    if not FFMPEG.exists():
        return {"name": "Caption drawing", "ok": False, "detail": "FFmpeg is missing"}
    ok, output = run_command([str(FFMPEG), "-hide_banner", "-filters"])
    has_drawtext = " drawtext " in output
    has_subtitles = " subtitles " in output
    return {
        "name": "Caption drawing",
        "ok": ok and has_drawtext and has_subtitles,
        "detail": f"drawtext: {'yes' if has_drawtext else 'NO'} | subtitles: {'yes' if has_subtitles else 'NO'}",
    }


def check_database():
    name = "Database (app memory)"
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            present = {r["name"] for r in rows}
            missing = [t for t in REQUIRED_TABLES if t not in present]

            # Real write test, then undo it so nothing is left behind.
            conn.execute("INSERT INTO audit_logs (event, details) VALUES ('write_test', 'temporary')")
            wrote = conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE event = 'write_test'"
            ).fetchone()[0] >= 1
            conn.rollback()

            starts = conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE event = 'app_started'"
            ).fetchone()[0]
            channels = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]

        size_kb = DB_PATH.stat().st_size / 1024
        if missing:
            return {"name": name, "ok": False, "detail": "Missing tables: " + ", ".join(missing)}
        return {
            "name": name,
            "ok": wrote,
            "detail": (f"{len(REQUIRED_TABLES)} tables ready | write test: "
                       f"{'passed' if wrote else 'FAILED'} | app starts recorded: {starts} "
                       f"| channels: {channels} | file size: {size_kb:.0f} KB"),
        }
    except Exception as exc:
        return {"name": name, "ok": False, "detail": f"Error: {exc}"}


def check_ai():
    name = "AI brain (Google Gemini, free tier)"
    if not ai.is_configured():
        return {"name": name, "ok": False, "detail": "No key found in .env. Redo Step 9."}

    with get_db() as conn:
        today = conn.execute(
            "SELECT COUNT(*) FROM provider_usage "
            "WHERE category = 'llm' AND date(created_at) = date('now')"
        ).fetchone()[0]

    last = get_setting("ai_last_test")
    if not last:
        return {"name": name, "ok": False,
                "detail": "Key found. Not tested yet: tap 'Test AI connection' below."}
    result = json.loads(last)
    if result.get("ok"):
        return {"name": name, "ok": True,
                "detail": (f"Working | model: {result['model']} | tested {result['at']} "
                           f"| AI requests today: {today} | AI said: \u201c{result['text']}\u201d")}
    return {"name": name, "ok": False,
            "detail": f"Last test FAILED ({result['at']}): {result['error']}"}


def check_pictures():
    if PEXELS_API_KEY:
        detail = "Wikimedia Commons (real historical images) + Pexels (free stock photos, key saved)"
    elif PIXABAY_API_KEY:
        detail = "Wikimedia Commons (real historical images) + Pixabay (free stock photos, key saved)"
    else:
        detail = "Wikimedia Commons only. Add a free Pixabay key for many more pictures (Step 15)."
    return {"name": "Picture sources", "ok": True, "detail": detail}


def check_youtube():
    yt = connection_status()
    labels = {"not_set_up": "Not set up (Step 21)", "not_connected": "Set up, not connected yet",
              "needs_reconnect": "Google ended the connection: reconnect on the Mac"}
    if yt["state"] == "connected":
        detail = f"Connected: {yt['name']}" + (" (reconnect soon: 7-day Testing limit)" if yt["reconnect_soon"] else "")
    else:
        detail = labels.get(yt["state"], yt["state"])
    return {"name": "YouTube", "ok": yt["state"] == "connected", "detail": detail}


def check_worker():
    alive = worker_alive()
    return {
        "name": "Production line (background worker)",
        "ok": alive,
        "detail": "Running and checking for work every few seconds" if alive
                  else "NOT running. Restart the app.",
    }


def check_disk():
    free_gb = shutil.disk_usage(BASE_DIR).free / (1000 ** 3)
    return {
        "name": "Free disk space",
        "ok": free_gb >= MIN_FREE_GB,
        "detail": f"{free_gb:.0f} GB free (minimum needed: {MIN_FREE_GB} GB)",
    }


def run_all_checks():
    return [
        check_python(),
        check_binary("FFmpeg (video builder)", FFMPEG),
        check_binary("FFprobe (video inspector)", FFPROBE),
        check_captions(),
        check_database(),
        check_ai(),
        check_pictures(),
        check_youtube(),
        check_worker(),
        check_disk(),
    ]


# ---------- pages ----------

@app.route("/")
def home():
    # Google returns here (http://127.0.0.1:8000/) after you approve YouTube access.
    if request.args.get("state") and (request.args.get("code") or request.args.get("error")):
        return redirect("/youtube/callback?" + request.query_string.decode("utf-8"))
    with get_db() as conn:
        channels = [dict(r) for r in conn.execute(
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM topics t WHERE t.channel_id = c.id AND t.status = 'candidate') AS idea_count, "
            "(SELECT title FROM topics t WHERE t.channel_id = c.id AND t.status = 'selected' LIMIT 1) AS selected_topic "
            "FROM channels c ORDER BY c.created_at, c.id").fetchall()]
        jobs = [dict(r) for r in conn.execute(
            "SELECT j.*, t.title AS topic_title, c.name AS channel_name FROM jobs j "
            "LEFT JOIN topics t ON t.id = j.topic_id JOIN channels c ON c.id = j.channel_id "
            "WHERE j.status != 'CANCELLED' ORDER BY j.id DESC LIMIT 8").fetchall()]
    for j in jobs:
        if j["status"] in ("EXCEPTION", "RETRYING", "CANCELLED", "FAILED"):
            j["status_label"] = {"EXCEPTION": "NEEDS ATTENTION", "RETRYING": "RETRYING"}.get(j["status"], j["status"])
        else:
            j["status_label"] = STAGE_LABELS.get(j["stage"], j["status"]).upper()
    return render_template("home.html", channels=channels, jobs=jobs, youtube=connection_status())


@app.route("/system")
def system():
    checks = run_all_checks()
    failures = sum(1 for c in checks if not c["ok"])
    checked_at = datetime.now().strftime("%d %b %Y, %H:%M:%S")
    return render_template("system.html", checks=checks, failures=failures, checked_at=checked_at)


@app.post("/test-ai")
def test_ai():
    at = datetime.now().strftime("%d %b %Y, %H:%M")
    prompt = ("In one short sentence, tell me one surprising but true fact about "
              "ancient history. No introduction, just the fact.")
    try:
        reply = ai.generate(prompt)
        result = {"ok": True, "model": reply["model"], "text": reply["text"][:300], "at": at}
        log_event("ai_test_passed", reply["model"])
    except LLMError as exc:
        result = {"ok": False, "error": str(exc), "at": at}
        log_event("ai_test_failed", str(exc)[:300])
    set_setting("ai_last_test", json.dumps(result))
    return redirect("/system")


@app.route("/health")
def health():
    checks = run_all_checks()
    return jsonify({"ok": all(c["ok"] for c in checks), "checks": checks})
