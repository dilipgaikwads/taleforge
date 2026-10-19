"""
VideoFactory - topic engine (discover, score, de-duplicate, pick).

1. Ask the AI for several specific topic ideas for a channel, telling it
   which topics were already used or proposed.
2. The AI scores each idea 1-10 on nine criteria. The factory (not the AI)
   combines those into a 0-100 score using fixed weights.
3. The factory checks every idea against the channel's past topics with a
   text-similarity test. Close matches lose points; near-copies are blocked.
4. The best eligible idea is marked as the "best pick".

This module has no web code, so the future autopilot can use it directly.
"""
import difflib
import json
import re

from .db import get_db, log_event
from .llm import GeminiProvider, LLMError

IDEAS_PER_ROUND = 8

CRITERIA = {
    # name: (weight, what it means)
    "relevance": (0.15, "fits the niche and audience"),
    "curiosity": (0.15, "makes people want to know the answer"),
    "story_potential": (0.15, "has a clear story with a satisfying payoff"),
    "novelty": (0.12, "not widely known or overdone"),
    "audience_appeal": (0.12, "broad interest for this audience"),
    "visual_potential": (0.10, "easy to show with images or footage"),
    "educational_value": (0.10, "the viewer learns something true"),
    "factual_confidence": (0.06, "well documented by reliable sources (10 = very well)"),
    "low_competition": (0.05, "few popular existing videos on exactly this (10 = very few)"),
}

WARN_SIMILARITY = 0.35   # above this: score penalty
BLOCK_SIMILARITY = 0.60  # at or above this: treated as a duplicate, never picked
MIN_FACTUAL_CONFIDENCE = 4

STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "was", "were", "are", "how",
    "why", "what", "who", "when", "into", "about", "their", "they", "its", "his", "her",
    "story", "true", "real", "secret", "mystery", "ancient", "history", "little", "known",
}


class TopicError(Exception):
    """A plain-language problem with topic discovery."""


# ---------- similarity (duplicate prevention) ----------

def _words(text):
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in STOPWORDS]


def similarity(a_title, a_summary, b_title, b_summary):
    """0.0 (unrelated) to 1.0 (same topic), based on shared meaningful words."""
    ta, tb = set(_words(a_title)), set(_words(b_title))
    fa, fb = ta | set(_words(a_summary)), tb | set(_words(b_summary))
    title_overlap = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    full_overlap = len(fa & fb) / len(fa | fb) if fa and fb else 0.0
    title_seq = difflib.SequenceMatcher(None, " ".join(_words(a_title)), " ".join(_words(b_title))).ratio()
    return round(max(title_overlap, full_overlap * 1.2, title_seq * 0.9), 3) if (ta or fa) else 0.0


def most_similar(title, summary, others):
    best_score, best_title = 0.0, ""
    for other in others:
        s = min(1.0, similarity(title, summary, other["title"], other["summary"]))
        if s > best_score:
            best_score, best_title = s, other["title"]
    return best_score, best_title


# ---------- AI prompt and parsing ----------

def build_prompt(channel, previous_titles):
    criteria_lines = "\n".join(f"- {name}: {meaning}" for name, (_, meaning) in CRITERIA.items())
    previous = "\n".join(f"- {t}" for t in previous_titles) if previous_titles else "(none yet)"
    example_scores = ", ".join(f'"{name}": 7' for name in CRITERIA)
    return f"""You are the topic researcher for a faceless short-video channel (YouTube Shorts / Instagram Reels).

CHANNEL
Name: {channel['name']}
Niche: {channel['niche']}
Audience: {channel['audience'] or 'general audience'}
Language for titles and summaries: {channel['language'] or 'English'}
Content rules (must follow): {channel['content_rules'] or 'none'}

TOPICS ALREADY USED OR PROPOSED. Do NOT repeat these or close variations:
{previous}

TASK
Propose {IDEAS_PER_ROUND} distinct topic ideas for 45-60 second vertical videos in this niche.
Each idea must be ONE specific story, event, person, place or discovery (not a broad subject),
based on real, documented knowledge. If an idea depends on legend or disputed claims,
say so honestly in "evidence" and give it a low factual_confidence.

Score each idea from 1 (poor) to 10 (excellent) on:
{criteria_lines}
Be honest and critical. Do not give every idea high scores.

Return ONLY JSON in exactly this shape:
{{"topics": [{{"title": "short specific title",
  "summary": "2 sentences: what happened and why it is surprising",
  "angle": "one-line hook idea for the first 3 seconds",
  "evidence": "well_documented | partly_documented | mostly_legend",
  "scores": {{{example_scores}}}}}]}}"""


def parse_json(text):
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(cleaned[start:end + 1])


def clean_idea(raw):
    """Check one AI idea. Returns a tidy dict, or None if it is unusable."""
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title", "")).strip()[:150]
    summary = str(raw.get("summary", "")).strip()[:600]
    if len(title) < 5 or len(summary) < 20:
        return None
    scores = {}
    raw_scores = raw.get("scores") if isinstance(raw.get("scores"), dict) else {}
    for name in CRITERIA:
        try:
            scores[name] = max(1, min(10, int(round(float(raw_scores.get(name, 0))))))
        except (TypeError, ValueError):
            return None
    evidence = str(raw.get("evidence", "")).strip().lower()
    if evidence not in ("well_documented", "partly_documented", "mostly_legend"):
        evidence = "partly_documented"
    return {
        "title": title,
        "summary": summary,
        "angle": str(raw.get("angle", "")).strip()[:300],
        "evidence": evidence,
        "scores": scores,
    }


def weighted_score(scores):
    total = sum(scores[name] * weight for name, (weight, _) in CRITERIA.items())
    return round(total * 10, 1)  # 1-10 scale -> 10-100


# ---------- main entry points ----------

def get_channel(channel_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if row is None:
        raise TopicError("That channel no longer exists.")
    return dict(row)


def discover_topics(channel_id, ai=None):
    """Ask the AI for new ideas, score them, check duplicates, save them.

    Returns {'added': n, 'duplicates': n, 'best_id': id or None}.
    """
    ai = ai or GeminiProvider()
    channel = get_channel(channel_id)

    with get_db() as conn:
        rows = conn.execute(
            "SELECT title, summary FROM topics WHERE channel_id = ? ORDER BY id DESC LIMIT 300",
            (channel_id,),
        ).fetchall()
    previous = [dict(r) for r in rows]

    prompt = build_prompt(channel, [p["title"] for p in previous[:120]])
    ideas, last_problem = [], ""
    for _attempt in range(2):  # one retry if the AI answer is malformed
        try:
            reply = ai.generate(prompt, temperature=0.9, json_mode=True)
        except LLMError as exc:
            raise TopicError(str(exc))
        try:
            data = parse_json(reply["text"])
            raw_list = data.get("topics", []) if isinstance(data, dict) else []
            ideas = [i for i in (clean_idea(r) for r in raw_list) if i]
        except (json.JSONDecodeError, AttributeError):
            last_problem = "The AI answer was not in the expected format."
            ideas = []
        if len(ideas) >= 3:
            break
        last_problem = last_problem or "The AI returned too few usable ideas."
    if len(ideas) < 3:
        raise TopicError(last_problem + " Please tap the button again in a minute.")

    added, duplicates = 0, 0
    compare_against = list(previous)
    with get_db() as conn:
        for idea in ideas:
            sim, similar_to = most_similar(idea["title"], idea["summary"], compare_against)
            base = weighted_score(idea["scores"])
            penalty = 0.0
            if sim >= WARN_SIMILARITY:
                penalty = round(min(1.0, (sim - WARN_SIMILARITY) / (BLOCK_SIMILARITY - WARN_SIMILARITY)) * 30, 1)
            duplicate = sim >= BLOCK_SIMILARITY
            low_facts = idea["scores"]["factual_confidence"] < MIN_FACTUAL_CONFIDENCE
            final = max(0.0, round(base - penalty, 1))
            details = {
                "scores": idea["scores"],
                "base_score": base,
                "duplicate_penalty": penalty,
                "similarity": sim,
                "similar_to": similar_to if sim >= WARN_SIMILARITY else "",
                "duplicate": duplicate,
                "low_factual_confidence": low_facts,
                "eligible": not duplicate and not low_facts,
                "evidence": idea["evidence"],
                "angle": idea["angle"],
                "model": reply["model"],
            }
            conn.execute(
                "INSERT INTO topics (channel_id, title, summary, score, score_details, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (channel_id, idea["title"], idea["summary"], final, json.dumps(details),
                 "rejected" if duplicate else "candidate"),
            )
            added += 1
            duplicates += int(duplicate)
            compare_against.append({"title": idea["title"], "summary": idea["summary"]})

    best_id = best_pick_id(channel_id)
    log_event("topics_discovered", f"channel {channel_id}: {added} ideas, {duplicates} duplicates")
    return {"added": added, "duplicates": duplicates, "best_id": best_id}


def best_pick_id(channel_id):
    """Highest-scoring candidate that is not a duplicate and is well enough documented."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, score_details FROM topics WHERE channel_id = ? AND status = 'candidate' "
            "ORDER BY score DESC, id ASC",
            (channel_id,),
        ).fetchall()
    for row in rows:
        if json.loads(row["score_details"] or "{}").get("eligible", True):
            return row["id"]
    return None


def set_topic_status(topic_id, status):
    """Mark a topic selected/rejected/candidate. Only one topic per channel is 'selected'."""
    with get_db() as conn:
        row = conn.execute("SELECT id, channel_id, title FROM topics WHERE id = ?", (topic_id,)).fetchone()
        if row is None:
            raise TopicError("That topic no longer exists.")
        if status == "selected":
            conn.execute(
                "UPDATE topics SET status = 'candidate' WHERE channel_id = ? AND status = 'selected'",
                (row["channel_id"],),
            )
        conn.execute("UPDATE topics SET status = ? WHERE id = ?", (status, topic_id))
    log_event(f"topic_{status}", f"{topic_id}: {row['title']}")
    return row["channel_id"], row["title"]
