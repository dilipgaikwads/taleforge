"""
VideoFactory - channels (create and edit).

A channel is one "show" the factory makes videos for, e.g. "Ancient Mysteries".
Each channel has its own niche, audience, language, style, rules and schedule.
New channels start PAUSED: automation is switched on in a later step.
"""
import os
import sqlite3
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, flash, redirect, render_template, request

from .db import get_db, log_event
from .voice import DEFAULT_VOICE, VOICES

bp = Blueprint("channels", __name__)

LIMITS = {
    "name": 60,
    "niche": 300,
    "audience": 300,
    "language": 40,
    "visual_style": 300,
    "content_rules": 1000,
    "timezone": 64,
}


def mac_timezone():
    """Read the Mac's own time zone setting (falls back to UTC)."""
    try:
        real = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in real:
            name = real.split("zoneinfo/", 1)[1]
            ZoneInfo(name)
            return name
    except Exception:
        pass
    return "UTC"


def read_form():
    """Read and check the channel form. Returns (data, list_of_problems)."""
    data = {key: request.form.get(key, "").strip() for key in LIMITS}
    errors = []
    data["voice"] = request.form.get("voice", DEFAULT_VOICE).strip()
    if data["voice"] not in VOICES:
        data["voice"] = DEFAULT_VOICE

    if not data["name"]:
        errors.append("Please give the channel a name.")
    if not data["niche"]:
        errors.append("Please describe the channel's niche (what its videos are about).")
    if not data["language"]:
        data["language"] = "English"
    for key, limit in LIMITS.items():
        if len(data[key]) > limit:
            errors.append(f"'{key.replace('_', ' ').title()}' is too long (max {limit} characters).")

    try:
        data["videos_per_day"] = int(request.form.get("videos_per_day", "1"))
        if not 1 <= data["videos_per_day"] <= 10:
            raise ValueError
    except ValueError:
        data["videos_per_day"] = 1
        errors.append("Videos per day must be a whole number from 1 to 10.")

    try:
        ZoneInfo(data["timezone"])
    except Exception:
        errors.append("Time zone not recognised. Use a name like Europe/London or America/New_York.")

    return data, errors


def show_form(channel, mode, errors=None, status=200):
    for problem in errors or []:
        flash(problem, "bad")
    return render_template("channel_form.html", channel=channel, mode=mode,
                           voices=VOICES, default_voice=DEFAULT_VOICE), status


@bp.route("/channels/new", methods=["GET", "POST"])
def new_channel():
    if request.method == "GET":
        defaults = {"language": "English", "videos_per_day": 1, "timezone": mac_timezone(),
                    "voice": DEFAULT_VOICE}
        return show_form(defaults, "new")

    data, errors = read_form()
    if errors:
        return show_form(data, "new", errors, 400)
    try:
        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO channels (name, niche, audience, language, visual_style, "
                "content_rules, videos_per_day, timezone, voice) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (data["name"], data["niche"], data["audience"], data["language"],
                 data["visual_style"], data["content_rules"], data["videos_per_day"],
                 data["timezone"], data["voice"]),
            )
            new_id = cur.lastrowid
    except sqlite3.IntegrityError:
        return show_form(data, "new", ["A channel with that name already exists."], 400)

    log_event("channel_created", f"{new_id}: {data['name']}")
    flash(f"Channel \u201c{data['name']}\u201d created and saved.", "ok")
    return redirect("/")


@bp.route("/channels/<int:channel_id>/edit", methods=["GET", "POST"])
def edit_channel(channel_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if row is None:
        abort(404)

    if request.method == "GET":
        return show_form(dict(row), "edit")

    data, errors = read_form()
    data["id"] = channel_id
    if errors:
        return show_form(data, "edit", errors, 400)
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE channels SET name = ?, niche = ?, audience = ?, language = ?, "
                "visual_style = ?, content_rules = ?, videos_per_day = ?, timezone = ?, voice = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (data["name"], data["niche"], data["audience"], data["language"],
                 data["visual_style"], data["content_rules"], data["videos_per_day"],
                 data["timezone"], data["voice"], channel_id),
            )
    except sqlite3.IntegrityError:
        return show_form(data, "edit", ["Another channel already uses that name."], 400)

    log_event("channel_updated", f"{channel_id}: {data['name']}")
    flash(f"Changes to \u201c{data['name']}\u201d saved.", "ok")
    return redirect("/")
