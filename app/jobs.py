"""
VideoFactory - video job pages.

  /topics/<id>/start   button: start a video job from a topic
  /jobs/<id>           live progress: stages, sources, checked facts, script
  /jobs/<id>/cancel    button: stop this job
  /jobs/<id>/retry     button: try the current stage again
  /jobs/<id>/visual/<file>  a scene picture (only scene_NN_916.jpg files)
  /jobs/<id>/audio/voice_preview.m4a  the narration, to listen on the phone
  /jobs/<id>/video/final.mp4          the finished video (watch or download)
  /jobs/<id>/approve                  button: owner accepts a video that failed quality control
  /jobs/<id>/publish/youtube          button: upload a Ready video to YouTube (private)
"""
import json
import re

from flask import Blueprint, abort, flash, redirect, render_template, send_from_directory

from .db import get_db, log_event
from .youtube import connection_status
from .pipeline import (BUILT_STAGES, PIPELINE_ORDER, STAGE_LABELS, job_dir,
                       load_json_file)

bp = Blueprint("jobs", __name__)


@bp.post("/topics/<int:topic_id>/start")
def start(topic_id):
    with get_db() as conn:
        topic = conn.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()
        if topic is None:
            abort(404)
        if topic["status"] not in ("selected", "candidate"):
            flash("That topic was already used or rejected. Pick another one.", "bad")
            return redirect(f"/channels/{topic['channel_id']}/topics")
        cur = conn.execute(
            "INSERT INTO jobs (channel_id, topic_id, status, stage) VALUES (?, ?, 'QUEUED', 'RESEARCHING')",
            (topic["channel_id"], topic_id))
        job_id = cur.lastrowid
        conn.execute("UPDATE topics SET status = 'used' WHERE id = ?", (topic_id,))
    log_event("job_created", f"job {job_id} from topic {topic_id}")
    flash("Video job started. The factory is working on it in the background.", "ok")
    return redirect(f"/jobs/{job_id}")


@bp.route("/jobs/<int:job_id>")
def view(job_id):
    with get_db() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            abort(404)
        job = dict(job)
        channel = dict(conn.execute("SELECT * FROM channels WHERE id = ?", (job["channel_id"],)).fetchone())
        topic_row = conn.execute("SELECT * FROM topics WHERE id = ?", (job["topic_id"],)).fetchone()
        topic = dict(topic_row) if topic_row else {"title": "(deleted topic)", "summary": "", "id": 0}
        facts = [dict(r) for r in conn.execute(
            "SELECT * FROM research_sources WHERE topic_id = ? ORDER BY verified DESC, id",
            (topic["id"],)).fetchall()]
        scenes = [dict(r) for r in conn.execute(
            "SELECT s.*, a.license AS credit, a.source_url AS credit_url, a.file_path AS picture "
            "FROM scenes s LEFT JOIN assets a ON a.scene_id = s.id AND a.kind = 'image' "
            "WHERE s.job_id = ? ORDER BY s.scene_number", (job_id,)).fetchall()]
        problems = [dict(r) for r in conn.execute(
            "SELECT * FROM exceptions WHERE job_id = ? AND resolved = 0 ORDER BY id DESC",
            (job_id,)).fetchall()]

    for scene in scenes:
        scene["plan"] = json.loads(scene["visual_prompt"] or "{}")
        scene["picture_name"] = f"scene_{scene['scene_number']:02d}_916.jpg" if scene["picture"] else ""
    folder = job_dir(job_id)
    sources = load_json_file(folder / "sources.json", [])
    script = load_json_file(folder / "script.json", None)
    voice = load_json_file(folder / "voice.json", None)
    with get_db() as conn:
        video_row = conn.execute("SELECT * FROM videos WHERE job_id = ?", (job_id,)).fetchone()
    video = dict(video_row) if video_row and (folder / "video" / "final.mp4").exists() else None
    qc = load_json_file(folder / "qc_report.json", None)
    with get_db() as conn:
        posts = [dict(r) for r in conn.execute(
            "SELECT * FROM platform_posts WHERE job_id = ? ORDER BY id DESC", (job_id,)).fetchall()]
    metadata = load_json_file(folder / "metadata.json", None)
    if voice and not (folder / "audio" / "voice_preview.m4a").exists():
        voice = None

    stage = job["stage"] or "RESEARCHING"
    current_index = PIPELINE_ORDER.index(stage) if stage in PIPELINE_ORDER else 0
    if stage in ("READY", "PUBLISHED"):
        current_index += 1  # finished states are shown as done
    steps = []
    for index, name in enumerate(PIPELINE_ORDER):
        if index < current_index:
            state = "done"
        elif index == current_index:
            state = "current"
        else:
            state = "later"
        if name == "SCHEDULED" and state == "done":
            state = "skipped"  # uploaded straight away, not scheduled
        steps.append({"label": STAGE_LABELS.get(name, name), "state": state,
                      "built": name in BUILT_STAGES or name in ("READY", "PUBLISHED")})

    # Facts only count as checked once the fact-check stage has finished.
    facts_checked = PIPELINE_ORDER.index(stage) > PIPELINE_ORDER.index("FACT_CHECKING") \
        if stage in PIPELINE_ORDER else False
    working = stage in BUILT_STAGES and job["status"] not in ("CANCELLED", "EXCEPTION", "FAILED")
    waiting_for_build = stage not in BUILT_STAGES and job["status"] not in ("CANCELLED", "EXCEPTION", "FAILED")
    return render_template(
        "job.html", job=job, channel=channel, topic=topic, facts=facts, sources=sources,
        script=script, problems=problems, steps=steps, working=working, scenes=scenes,
        built_stages=BUILT_STAGES, voice=voice, video=video, qc=qc, metadata=metadata,
        posts=posts, youtube=connection_status(),
        waiting_for_build=waiting_for_build, stage_label=STAGE_LABELS.get(stage, stage),
        verified_count=sum(1 for f in facts if f["verified"]), facts_checked=facts_checked,
    )


@bp.post("/jobs/<int:job_id>/cancel")
def cancel(job_id):
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status = 'CANCELLED', updated_at = datetime('now') "
            "WHERE id = ? AND status NOT IN ('PUBLISHED', 'CANCELLED')", (job_id,))
    if cur.rowcount:
        log_event("job_cancelled", f"job {job_id}")
        flash("Job cancelled. Nothing more will happen to it.", "ok")
    return redirect(f"/jobs/{job_id}")


@bp.post("/jobs/<int:job_id>/retry")
def retry(job_id):
    with get_db() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            abort(404)
        if job["stage"] not in BUILT_STAGES:
            flash("This job is waiting for a stage that is not built yet. Nothing to retry.", "bad")
            return redirect(f"/jobs/{job_id}")
        conn.execute(
            "UPDATE jobs SET status = ?, retry_count = 0, next_attempt_at = NULL, last_error = '', "
            "updated_at = datetime('now') WHERE id = ?", (job["stage"], job_id))
        conn.execute("UPDATE exceptions SET resolved = 1 WHERE job_id = ?", (job_id,))
    log_event("job_retry_manual", f"job {job_id}")
    flash("Trying again now.", "ok")
    return redirect(f"/jobs/{job_id}")


@bp.route("/jobs/<int:job_id>/visual/<name>")
def visual(job_id, name):
    if not re.fullmatch(r"scene_\d{2}_916\.jpg", name):
        abort(404)
    return send_from_directory(job_dir(job_id) / "visuals", name, max_age=3600)


@bp.route("/jobs/<int:job_id>/audio/voice_preview.m4a")
def audio(job_id):
    return send_from_directory(job_dir(job_id) / "audio", "voice_preview.m4a", max_age=60)


@bp.route("/jobs/<int:job_id>/video/final.mp4")
def video_file(job_id):
    return send_from_directory(job_dir(job_id) / "video", "final.mp4", max_age=60)


@bp.post("/jobs/<int:job_id>/approve")
def approve(job_id):
    folder = job_dir(job_id)
    with get_db() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            abort(404)
        if job["status"] != "EXCEPTION" or job["stage"] != "QUALITY_CHECK" or not (folder / "video" / "final.mp4").exists():
            flash("Only a finished video that is waiting after a failed quality check can be approved.", "bad")
            return redirect(f"/jobs/{job_id}")
        report = load_json_file(folder / "qc_report.json", {})
        report["approved_by_owner"] = True
        (folder / "qc_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        conn.execute("UPDATE videos SET qc_passed = 1, qc_report = ? WHERE job_id = ?", (json.dumps(report), job_id))
        conn.execute("UPDATE jobs SET status = 'READY', stage = 'READY', last_error = '', "
                     "updated_at = datetime('now') WHERE id = ?", (job_id,))
        conn.execute("UPDATE exceptions SET resolved = 1 WHERE job_id = ?", (job_id,))
    log_event("job_approved_by_owner", f"job {job_id}")
    flash("Approved. The video is marked Ready.", "ok")
    return redirect(f"/jobs/{job_id}")


@bp.post("/jobs/<int:job_id>/publish/youtube")
def publish_youtube(job_id):
    with get_db() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            abort(404)
        if job["stage"] != "READY":
            flash("Only a video marked Ready can be uploaded.", "bad")
            return redirect(f"/jobs/{job_id}")
        if conn.execute("SELECT 1 FROM platform_posts WHERE job_id = ? AND platform = 'youtube' "
                        "AND status = 'published'", (job_id,)).fetchone():
            flash("This video is already on YouTube.", "bad")
            return redirect(f"/jobs/{job_id}")
        if connection_status().get("state") != "connected":
            flash("Connect YouTube first (YouTube page, on the Mac).", "bad")
            return redirect(f"/jobs/{job_id}")
        conn.execute("INSERT INTO platform_posts (job_id, platform, privacy, status) "
                     "VALUES (?, 'youtube', 'private', 'queued')", (job_id,))
        conn.execute("UPDATE jobs SET status = 'PUBLISHING', stage = 'PUBLISHING', retry_count = 0, "
                     "next_attempt_at = NULL, last_error = '', updated_at = datetime('now') WHERE id = ?", (job_id,))
    log_event("youtube_upload_requested", f"job {job_id} (private)")
    flash("Uploading to YouTube as PRIVATE. This page refreshes by itself.", "ok")
    return redirect(f"/jobs/{job_id}")
