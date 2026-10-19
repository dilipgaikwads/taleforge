"""
VideoFactory - topic pages.

  /channels/<id>/topics            list ideas for a channel, best first
  /channels/<id>/topics/discover   button: ask the AI for new scored ideas
  /topics/<id>/select              button: use this topic for the next video
  /topics/<id>/reject              button: never use this topic
"""
import json

from flask import Blueprint, abort, flash, redirect, render_template

from .db import get_db
from .topic_engine import (BLOCK_SIMILARITY, CRITERIA, TopicError, best_pick_id,
                           discover_topics, get_channel, set_topic_status)

bp = Blueprint("topics", __name__)

EVIDENCE_LABELS = {
    "well_documented": "Well documented",
    "partly_documented": "Partly documented",
    "mostly_legend": "Mostly legend",
}


@bp.route("/channels/<int:channel_id>/topics")
def list_topics(channel_id):
    try:
        channel = get_channel(channel_id)
    except TopicError:
        abort(404)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM topics WHERE channel_id = ? ORDER BY "
            "CASE status WHEN 'selected' THEN 0 WHEN 'candidate' THEN 1 WHEN 'used' THEN 2 ELSE 3 END, "
            "score DESC, id DESC",
            (channel_id,),
        ).fetchall()

    best_id = best_pick_id(channel_id)
    visible, hidden = [], 0
    for row in rows:
        topic = dict(row)
        topic["details"] = json.loads(topic["score_details"] or "{}")
        topic["evidence_label"] = EVIDENCE_LABELS.get(topic["details"].get("evidence"), "")
        topic["is_best"] = topic["id"] == best_id
        if topic["status"] == "rejected":
            hidden += 1
        else:
            visible.append(topic)

    return render_template(
        "topics.html", channel=channel, topics=visible, hidden=hidden,
        criteria=list(CRITERIA.keys()), block_similarity=int(BLOCK_SIMILARITY * 100),
    )


@bp.post("/channels/<int:channel_id>/topics/discover")
def discover(channel_id):
    try:
        result = discover_topics(channel_id)
    except TopicError as exc:
        flash(f"Topic discovery did not finish: {exc}", "bad")
        return redirect(f"/channels/{channel_id}/topics")
    message = f"The AI proposed {result['added']} new ideas."
    if result["duplicates"]:
        message += f" {result['duplicates']} were too similar to earlier topics and were set aside."
    flash(message, "ok")
    return redirect(f"/channels/{channel_id}/topics")


@bp.post("/topics/<int:topic_id>/select")
def select(topic_id):
    try:
        channel_id, title = set_topic_status(topic_id, "selected")
    except TopicError:
        abort(404)
    flash(f"\u201c{title}\u201d is now the selected topic for the next video.", "ok")
    return redirect(f"/channels/{channel_id}/topics")


@bp.post("/topics/<int:topic_id>/reject")
def reject(topic_id):
    try:
        channel_id, title = set_topic_status(topic_id, "rejected")
    except TopicError:
        abort(404)
    flash(f"\u201c{title}\u201d was rejected and will not be used.", "ok")
    return redirect(f"/channels/{channel_id}/topics")
