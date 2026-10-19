"""
VideoFactory - the production line (background worker).

A "job" is one video moving through the factory. A background worker
inside the app picks up jobs and runs them stage by stage:

  RESEARCHING    -> find real Wikipedia articles, extract facts from them
  FACT_CHECKING  -> confirm every fact against the article text
  SCRIPTING      -> write the script using ONLY confirmed facts
  SCENE_PLANNING -> split the script into scenes and plan what each shows
  GENERATING_VISUALS -> find a legally usable picture for every scene
  GENERATING_VOICE   -> narrate the script and time every scene to the voice
  GENERATING_CAPTIONS -> make big, readable on-screen captions timed to the voice
  ASSEMBLING         -> build the real 1080x1920 MP4 video with FFmpeg
  QUALITY_CHECK      -> write title/description/hashtags, then check everything;
                        fix problems automatically by redoing the stage at fault
  READY              -> finished and saved
  PUBLISHING         -> upload to YouTube through the official API (started by you for now)
  PUBLISHED          -> uploaded and confirmed by YouTube

Safety rules built in:
  - Temporary problems (AI busy, internet hiccup) are retried automatically,
    waiting longer each time: 1 min, 5 min, 15 min. Then it stops.
  - Permanent problems (e.g. not enough reliable facts) are never retried
    blindly. They go to the exception queue with plain-language fix steps.
  - A cancelled job is never moved forward.
"""
import difflib
import json
import re
import shutil
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import DATA_DIR, get_db, log_event
from .llm import GeminiProvider, LLMError
from .research import ResearchError, WikipediaResearch
from .visuals import (PexelsImages, PixabayImages, VisualError, WikimediaImages,
                      download, make_vertical_frame)
from .render import (RenderError, assemble, caption_cues, probe_video,
                     render_scene_clip, write_captions)
from .qc import check_audio, check_captions, check_video_file
from .youtube import YouTubeError, upload_video, video_status, connection_status
from .voice import (DEFAULT_VOICE, VOICES, GeminiVoice, VoiceError, find_pauses,
                    finish_audio, probe_duration, scene_timings)
from .topic_engine import parse_json

JOBS_DIR = DATA_DIR / "jobs"

BUILT_STAGES = ("RESEARCHING", "FACT_CHECKING", "SCRIPTING", "SCENE_PLANNING", "GENERATING_VISUALS",
                "GENERATING_VOICE", "GENERATING_CAPTIONS", "ASSEMBLING", "QUALITY_CHECK", "PUBLISHING")
NEXT_STAGE = {
    "RESEARCHING": "FACT_CHECKING",
    "FACT_CHECKING": "SCRIPTING",
    "SCRIPTING": "SCENE_PLANNING",
    "SCENE_PLANNING": "GENERATING_VISUALS",
    "GENERATING_VISUALS": "GENERATING_VOICE",
    "GENERATING_VOICE": "GENERATING_CAPTIONS",
    "GENERATING_CAPTIONS": "ASSEMBLING",
    "ASSEMBLING": "QUALITY_CHECK",
    "QUALITY_CHECK": "READY",
    "PUBLISHING": "PUBLISHED",
}
STAGE_LABELS = {
    "QUEUED": "Waiting to start",
    "RESEARCHING": "Research",
    "FACT_CHECKING": "Fact-check",
    "SCRIPTING": "Script",
    "SCENE_PLANNING": "Scene plan",
    "GENERATING_VISUALS": "Visuals",
    "GENERATING_VOICE": "Voice",
    "GENERATING_CAPTIONS": "Captions",
    "ASSEMBLING": "Video assembly",
    "QUALITY_CHECK": "Quality check",
    "READY": "Ready",
    "SCHEDULED": "Scheduled",
    "PUBLISHING": "Publishing",
    "PUBLISHED": "Published",
}
PIPELINE_ORDER = ["RESEARCHING", "FACT_CHECKING", "SCRIPTING", "SCENE_PLANNING",
                  "GENERATING_VISUALS", "GENERATING_VOICE", "GENERATING_CAPTIONS",
                  "ASSEMBLING", "QUALITY_CHECK", "READY", "SCHEDULED", "PUBLISHING", "PUBLISHED"]

MAX_RETRIES = 3
BACKOFF_SECONDS = [60, 300, 900]
MIN_VERIFIED_FACTS = 4
SCRIPT_MIN_WORDS, SCRIPT_MAX_WORDS = 90, 170
WORDS_PER_SECOND = 2.5
SCRIPT_SECTIONS = ["hook", "curiosity", "context", "story", "revelation", "payoff", "cta"]
MIN_SCENES, MAX_SCENES = 4, 10
MOTIONS = ("zoom_in", "zoom_out", "pan_left", "pan_right")

PERMANENT_HINTS = ("rejected the key", "not allowed to use Gemini", "not available from your location",
                   "No Gemini key")


class PermanentError(Exception):
    """A problem that retrying will not fix."""


class RedoFrom(Exception):
    """Quality control found a fixable problem: go back to an earlier stage."""

    def __init__(self, stage, reason):
        super().__init__(reason)
        self.stage = stage


# Exact answer shapes we ask the AI for (Google "structured output").
QUERIES_SCHEMA = {"type": "OBJECT", "properties": {
    "queries": {"type": "ARRAY", "items": {"type": "STRING"}}}, "required": ["queries"]}
FACTS_SCHEMA = {"type": "OBJECT", "properties": {
    "facts": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "fact": {"type": "STRING"}, "source": {"type": "INTEGER"}, "type": {"type": "STRING"}},
        "required": ["fact", "source", "type"]}}}, "required": ["facts"]}
CHECKS_SCHEMA = {"type": "OBJECT", "properties": {
    "checks": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "id": {"type": "INTEGER"}, "verdict": {"type": "STRING"}, "quote": {"type": "STRING"}},
        "required": ["id", "verdict", "quote"]}}}, "required": ["checks"]}
SCRIPT_SCHEMA = {"type": "OBJECT", "properties": {
    "title": {"type": "STRING"},
    "sections": {"type": "OBJECT", "properties": {k: {"type": "STRING"} for k in SCRIPT_SECTIONS},
                 "required": ["hook", "story", "payoff"]},
    "facts_used": {"type": "ARRAY", "items": {"type": "INTEGER"}}},
    "required": ["title", "sections"]}


SCENES_SCHEMA = {"type": "OBJECT", "properties": {
    "scenes": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "sentences": {"type": "ARRAY", "items": {"type": "INTEGER"}},
        "visual_description": {"type": "STRING"},
        "source": {"type": "STRING"},
        "archive_query": {"type": "STRING"},
        "stock_queries": {"type": "ARRAY", "items": {"type": "STRING"}},
        "motion": {"type": "STRING"}},
        "required": ["sentences", "visual_description", "source", "archive_query", "stock_queries", "motion"]}}},
    "required": ["scenes"]}


METADATA_SCHEMA = {"type": "OBJECT", "properties": {
    "youtube_title": {"type": "STRING"},
    "summary": {"type": "STRING"},
    "youtube_hashtags": {"type": "ARRAY", "items": {"type": "STRING"}},
    "instagram_caption": {"type": "STRING"},
    "instagram_hashtags": {"type": "ARRAY", "items": {"type": "STRING"}}},
    "required": ["youtube_title", "summary", "youtube_hashtags", "instagram_caption", "instagram_hashtags"]}
CLAIMS_SCHEMA = {"type": "OBJECT", "properties": {
    "problems": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "sentence": {"type": "STRING"}, "issue": {"type": "STRING"}},
        "required": ["sentence", "issue"]}}}, "required": ["problems"]}


# ---------- helpers ----------

def job_dir(job_id):
    path = JOBS_DIR / str(job_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_json_file(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def json_list(text, key):
    """Read a list from the AI's JSON answer, accepting {"key": [...]} or a bare [...]."""
    data = parse_json(text)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        value = data.get(key, [])
        return value if isinstance(value, list) else []
    return []


def save_raw_answer(job_id, name, text):
    """Keep the AI's exact answer when it could not be read, so problems can be diagnosed."""
    (job_dir(job_id) / f"debug_{name}.txt").write_text(text or "", encoding="utf-8")


def _load(job):
    with get_db() as conn:
        topic = conn.execute("SELECT * FROM topics WHERE id = ?", (job["topic_id"],)).fetchone()
        channel = conn.execute("SELECT * FROM channels WHERE id = ?", (job["channel_id"],)).fetchone()
    if topic is None or channel is None:
        raise PermanentError("The topic or channel for this job was deleted.")
    return dict(topic), dict(channel)


def _normalise(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def quote_found(quote, source_text):
    """True if the quote (or at least 85% of it, in one piece) appears in the source text."""
    q, s = _normalise(quote), _normalise(source_text)
    if len(q) < 15:
        return False
    if q in s:
        return True
    match = difflib.SequenceMatcher(None, s, q, autojunk=False).find_longest_match(0, len(s), 0, len(q))
    return match.size >= 0.85 * len(q)


# ---------- stage 1: research ----------

def stage_research(job, ai, research):
    topic, channel = _load(job)

    reply = ai.generate(
        "Give 3 short English Wikipedia search queries that would find the main articles "
        "about this topic. Use names of the people, places, objects or events involved.\n"
        f"Topic: {topic['title']}\nSummary: {topic['summary']}\n"
        'Return ONLY JSON: {"queries": ["...", "...", "..."]}',
        job_id=job["id"], temperature=0.2, json_mode=True, schema=QUERIES_SCHEMA)
    try:
        queries = [str(q) for q in json_list(reply["text"], "queries")][:3]
    except (json.JSONDecodeError, ValueError):
        queries = []
    queries.append(topic["title"])

    sources = research.collect(queries, max_articles=4)
    if not sources:
        raise PermanentError("No suitable Wikipedia articles were found for this topic, "
                             "so its facts cannot be checked.")
    for number, source in enumerate(sources, start=1):
        source["n"] = number
    (job_dir(job["id"]) / "sources.json").write_text(json.dumps(sources, indent=2), encoding="utf-8")

    source_block = "\n\n".join(f"SOURCE {s['n']}: {s['title']}\n{s['text']}" for s in sources)
    facts_prompt = (
        f"""You are a careful researcher. Using ONLY the sources below (no outside knowledge),
list up to 12 facts that are useful for a short video about this topic:
Topic: {topic['title']}
Summary: {topic['summary']}

For each fact give the source number it came from, and its type:
"fact" = stated as established, "disputed" = sources say it is uncertain or debated,
"legend" = sources describe it as myth, legend or tradition.
Write each fact in your own words, one sentence, with specific names, dates and numbers.

{source_block}

Return ONLY JSON: {{"facts": [{{"fact": "...", "source": 1, "type": "fact"}}]}}""")
    raw_facts = None
    for attempt in range(2):
        reply = ai.generate(facts_prompt, job_id=job["id"], temperature=0.2, json_mode=True, schema=FACTS_SCHEMA)
        try:
            raw_facts = json_list(reply["text"], "facts")
            break
        except (json.JSONDecodeError, ValueError):
            save_raw_answer(job["id"], f"research_{attempt + 1}", reply["text"])
    if raw_facts is None:
        raise LLMError("The AI's research answer was not in the expected format (saved for diagnosis).")

    by_number = {s["n"]: s for s in sources}
    rows = []
    for item in raw_facts:
        if not isinstance(item, dict):
            continue
        text = str(item.get("fact", "")).strip()[:500]
        try:
            source = by_number.get(int(item.get("source", 0)))
        except (TypeError, ValueError):
            source = None
        kind = str(item.get("type", "fact")).strip().lower()
        if len(text) < 15 or source is None:
            continue
        if kind not in ("fact", "disputed", "legend"):
            kind = "fact"
        rows.append((topic["id"], text, kind, source["title"], source["url"]))

    if len(rows) < MIN_VERIFIED_FACTS:
        raise PermanentError(f"Only {len(rows)} usable facts could be found in the sources "
                             f"(at least {MIN_VERIFIED_FACTS} are needed).")
    with get_db() as conn:
        conn.execute("DELETE FROM research_sources WHERE topic_id = ?", (topic["id"],))
        conn.executemany(
            "INSERT INTO research_sources (topic_id, claim, claim_type, source_title, source_url, verified) "
            "VALUES (?, ?, ?, ?, ?, 0)", rows)


# ---------- stage 2: fact-check ----------

def _check_against_source(job, ai, source, group):
    """Check a small group of facts against ONE article. Returns {fact_id: (verdict, quote)}."""
    fact_block = "\n".join(f"{f['id']}. {f['claim']}" for f in group)
    prompt = f"""You are a strict fact-checker. Check each numbered claim against the SOURCE ONLY.

Verdicts:
"supported"   = the source clearly states it
"disputed"    = the source says it is uncertain or debated
"legend"      = the source presents it as legend, myth or tradition
"unsupported" = the source does not clearly state it, or it is wrong

For every verdict except "unsupported", copy an EXACT quote (10 to 40 words) from the
source text that proves it. Copy the words exactly; do not paraphrase. For "unsupported"
use an empty quote.

CLAIMS:
{fact_block}

SOURCE: {source['title']}
{source['text']}

Return ONLY JSON: {{"checks": [{{"id": 1, "verdict": "supported", "quote": "..."}}]}}"""

    for attempt in range(2):
        reply = ai.generate(prompt, job_id=job["id"], temperature=0.0, json_mode=True, schema=CHECKS_SCHEMA)
        try:
            checks = json_list(reply["text"], "checks")
        except (json.JSONDecodeError, ValueError):
            save_raw_answer(job["id"], f"factcheck_{source['n']}_{attempt + 1}", reply["text"])
            continue
        results = {}
        for check in checks:
            if not isinstance(check, dict):
                continue
            try:
                fact_id = int(check.get("id"))
            except (TypeError, ValueError):
                continue
            results[fact_id] = (str(check.get("verdict", "")).strip().lower(), str(check.get("quote", "")))
        return results
    raise LLMError("The AI's fact-check answer was not in the expected format (saved for diagnosis).")


def stage_fact_check(job, ai, research):
    topic, _channel = _load(job)
    sources = load_json_file(job_dir(job["id"]) / "sources.json", [])
    if not sources:
        raise PermanentError("The research sources for this job are missing. Retry from research.")
    with get_db() as conn:
        facts = [dict(r) for r in conn.execute(
            "SELECT * FROM research_sources WHERE topic_id = ? ORDER BY id", (topic["id"],)).fetchall()]
    if not facts:
        raise PermanentError("No researched facts were found to check.")

    # Check facts in small groups, one article at a time: shorter, clearer AI tasks.
    by_title = {s["title"]: s for s in sources}
    groups = {}
    for fact in facts:
        groups.setdefault(fact["source_title"], []).append(fact)

    outcome = {}
    for title, group in groups.items():
        source = by_title.get(title)
        if source is None:
            continue
        for fact_id, (verdict, quote) in _check_against_source(job, ai, source, group).items():
            # The quote must really exist in the article. This catches invented evidence.
            confirmed = verdict in ("supported", "disputed", "legend") and quote_found(quote, source["text"])
            outcome[fact_id] = (confirmed, verdict)

    verified = 0
    with get_db() as conn:
        for fact in facts:
            confirmed, verdict = outcome.get(fact["id"], (False, "unsupported"))
            kind = {"supported": "fact", "disputed": "disputed", "legend": "legend"}.get(verdict, fact["claim_type"])
            conn.execute("UPDATE research_sources SET verified = ?, claim_type = ? WHERE id = ?",
                         (1 if confirmed else 0, kind, fact["id"]))
            verified += int(confirmed)

    if verified < MIN_VERIFIED_FACTS:
        raise PermanentError(f"Only {verified} facts could be confirmed against the sources "
                             f"(at least {MIN_VERIFIED_FACTS} are needed for an honest video).")


# ---------- stage 3: script ----------

def stage_script(job, ai, research):
    topic, channel = _load(job)
    with get_db() as conn:
        facts = [dict(r) for r in conn.execute(
            "SELECT * FROM research_sources WHERE topic_id = ? AND verified = 1 ORDER BY id",
            (topic["id"],)).fetchall()]
    if len(facts) < MIN_VERIFIED_FACTS:
        raise PermanentError("Not enough confirmed facts to write an honest script.")

    angle = json.loads(topic["score_details"] or "{}").get("angle", "")
    fact_block = "\n".join(
        f"{f['id']}. [{f['claim_type'].upper()}] {f['claim']}" for f in facts)
    base_prompt = f"""Write the narration script for a 45-55 second faceless vertical video.

CHANNEL: {channel['name']}. Niche: {channel['niche']}
AUDIENCE: {channel['audience'] or 'general audience'}
LANGUAGE: {channel['language'] or 'English'}
CONTENT RULES (must follow): {channel['content_rules'] or 'none'}
TOPIC: {topic['title']}
HOOK IDEA (optional): {angle}

CONFIRMED FACTS. Use ONLY these. Do not add any other facts, numbers, names or dates:
{fact_block}

RULES
- Total length {SCRIPT_MIN_WORDS + 20}-{SCRIPT_MAX_WORDS - 20} words.
- Facts marked [DISPUTED] must be narrated as uncertain ("historians still debate...").
- Facts marked [LEGEND] must be narrated as legend ("according to legend...").
- Structure: hook (first 3 seconds, makes the viewer stay), curiosity, context, story,
  revelation, payoff, and an optional short cta (e.g. "Follow for more lost history").
- No greetings, no "in this video", no filler, no repetition, no clickbait lies.
- Short, spoken sentences. Write your own words; do not copy the sources' wording.

Return ONLY JSON:
{{"title": "working title", "sections": {{"hook": "...", "curiosity": "...", "context": "...",
"story": "...", "revelation": "...", "payoff": "...", "cta": "..."}}, "facts_used": [1, 2]}}"""

    qc_notes = load_json_file(job_dir(job["id"]) / "qc_state.json", {}).get("script_feedback", [])
    if qc_notes:
        base_prompt += ("\n\nA previous version was rejected by the fact-checker for these sentences. "
                        "Do not repeat these problems:\n" + "\n".join(f"- {n}" for n in qc_notes))
    feedback, script = "", None
    for _attempt in range(2):
        reply = ai.generate(base_prompt + feedback, job_id=job["id"], temperature=0.8,
                            json_mode=True, schema=SCRIPT_SCHEMA)
        try:
            data = parse_json(reply["text"])
            if isinstance(data, list) and data:
                data = data[0]
            sections = {k: " ".join(str(data.get("sections", {}).get(k, "")).split()) for k in SCRIPT_SECTIONS}
        except (json.JSONDecodeError, ValueError, AttributeError):
            save_raw_answer(job["id"], f"script_{_attempt + 1}", reply["text"])
            feedback = "\n\nYour previous answer was not valid JSON. Return ONLY the JSON."
            continue
        full_text = " ".join(sections[k] for k in SCRIPT_SECTIONS if sections[k])
        words = len(full_text.split())
        if not sections["hook"] or not (SCRIPT_MIN_WORDS <= words <= SCRIPT_MAX_WORDS):
            feedback = (f"\n\nYour previous script had {words} words and "
                        f"{'no hook' if not sections['hook'] else 'the wrong length'}. "
                        f"Fix this: {SCRIPT_MIN_WORDS + 20}-{SCRIPT_MAX_WORDS - 20} words, with a hook.")
            continue
        valid_ids = {f["id"] for f in facts}
        used = [i for i in (data.get("facts_used") or []) if isinstance(i, int) and i in valid_ids]
        script = {"title": str(data.get("title", topic["title"]))[:150], "sections": sections,
                  "full_text": full_text, "word_count": words, "facts_used": used,
                  "estimated_seconds": round(words / WORDS_PER_SECOND), "model": reply["model"]}
        break
    if script is None:
        raise LLMError("The AI could not produce a script of the right length and format.")

    (job_dir(job["id"]) / "script.json").write_text(json.dumps(script, indent=2), encoding="utf-8")
    with get_db() as conn:
        conn.execute("DELETE FROM scripts WHERE job_id = ?", (job["id"],))
        conn.execute("INSERT INTO scripts (job_id, hook, full_text, word_count) VALUES (?, ?, ?, ?)",
                     (job["id"], script["sections"]["hook"], script["full_text"], script["word_count"]))


# ---------- stage 4: scene plan ----------

def split_sentences(text):
    """Split narration into sentences; very short bits are joined to the next one."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]
    sentences, carry = [], ""
    for part in parts:
        part = (carry + " " + part).strip() if carry else part
        if len(part.split()) < 4:
            carry = part
            continue
        sentences.append(part)
        carry = ""
    if carry:
        if sentences:
            sentences[-1] = sentences[-1] + " " + carry
        else:
            sentences.append(carry)
    return sentences


def stage_scene_plan(job, ai, research):
    topic, channel = _load(job)
    script = load_json_file(job_dir(job["id"]) / "script.json", None)
    if not script:
        raise PermanentError("The script for this job is missing. Retry from the script stage.")
    sentences = split_sentences(script["full_text"])
    if len(sentences) < 2:
        raise PermanentError("The script is too short to split into scenes.")

    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(sentences, start=1))
    base_prompt = f"""You are planning the pictures for a vertical (9:16) short video.

TOPIC: {topic['title']}
CHANNEL VISUAL STYLE: {channel['visual_style'] or 'cinematic, realistic'}
CONTENT RULES: {channel['content_rules'] or 'none'}

The narration is split into numbered sentences:
{numbered}

Group CONSECUTIVE sentences into {MIN_SCENES} to {min(MAX_SCENES, len(sentences))} scenes. Every sentence number
from 1 to {len(sentences)} must appear exactly once, in order. Each scene should last about 3-9 seconds
(about 2.5 spoken words per second).

For each scene give:
- sentences: the sentence numbers in this scene
- visual_description: one sentence describing what the viewer sees
- source: "archive" if it needs a REAL historical place, artifact, artwork, map or document;
  "stock" if a general atmospheric picture works (sea, sky, smoke, ruins, landscape)
- archive_query: 2-5 words to search Wikimedia Commons, using specific names (e.g. "Akrotiri fresco")
- stock_queries: 2 short generic photo searches without proper names (e.g. "volcano eruption night")
- motion: one of zoom_in, zoom_out, pan_left, pan_right (vary them)
Never ask for gore, violence against people, or identifiable modern people.

Return ONLY JSON: {{"scenes": [{{"sentences": [1, 2], "visual_description": "...", "source": "archive",
"archive_query": "...", "stock_queries": ["...", "..."], "motion": "zoom_in"}}]}}"""

    feedback, plan = "", None
    for attempt in range(2):
        reply = ai.generate(base_prompt + feedback, job_id=job["id"], temperature=0.4,
                            json_mode=True, schema=SCENES_SCHEMA)
        try:
            scenes = json_list(reply["text"], "scenes")
        except (json.JSONDecodeError, ValueError):
            save_raw_answer(job["id"], f"scenes_{attempt + 1}", reply["text"])
            feedback = "\n\nYour previous answer was not valid JSON. Return ONLY the JSON."
            continue
        cleaned = []
        for scene in scenes:
            if not isinstance(scene, dict):
                continue
            numbers = [int(n) for n in (scene.get("sentences") or []) if str(n).strip().isdigit()]
            if numbers:
                cleaned.append({**scene, "sentences": numbers})
        order = [n for scene in cleaned for n in scene["sentences"]]
        if order == list(range(1, len(sentences) + 1)) and MIN_SCENES <= len(cleaned) <= MAX_SCENES:
            plan = cleaned
            break
        scenes = cleaned
        save_raw_answer(job["id"], f"scenes_{attempt + 1}", reply["text"])
        feedback = (f"\n\nYour previous plan used the sentence numbers {order} in {len(scenes)} scenes. "
                    f"It must use exactly 1 to {len(sentences)}, each once, in order, in "
                    f"{MIN_SCENES}-{MAX_SCENES} scenes.")
    if plan is None:
        raise LLMError("The AI could not produce a valid scene plan.")

    rows = []
    for number, scene in enumerate(plan, start=1):
        narration = " ".join(sentences[n - 1] for n in scene["sentences"])
        motion = str(scene.get("motion", "")).strip().lower()
        source = str(scene.get("source", "")).strip().lower()
        details = {
            "source": source if source in ("archive", "stock") else "stock",
            "archive_query": str(scene.get("archive_query", ""))[:80],
            "stock_queries": [str(q)[:60] for q in (scene.get("stock_queries") or []) if str(q).strip()][:3],
        }
        rows.append((job["id"], number, round(len(narration.split()) / WORDS_PER_SECOND, 1), narration,
                     str(scene.get("visual_description", ""))[:400], json.dumps(details),
                     motion if motion in MOTIONS else MOTIONS[number % len(MOTIONS)]))
    with get_db() as conn:
        conn.execute("DELETE FROM scenes WHERE job_id = ?", (job["id"],))
        conn.executemany(
            "INSERT INTO scenes (job_id, scene_number, duration_sec, narration, visual_description, "
            "visual_prompt, motion) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)


# ---------- stage 5: visuals ----------

def _find_picture(scene, plan, providers, used_urls, folder):
    """Try the scene's preferred source first, then the other one. Returns asset info or None."""
    commons, stock_provider = providers
    archive = ("archive", commons, [plan.get("archive_query", "")])
    # With no stock-photo key, Wikimedia Commons also handles the general searches.
    stock = ("stock", stock_provider or commons, plan.get("stock_queries", []))
    order = [archive, stock] if plan.get("source") == "archive" else [stock, archive]
    raw = folder / f"scene_{scene['scene_number']:02d}_original"
    frame = folder / f"scene_{scene['scene_number']:02d}_916.jpg"
    for _kind, provider, queries in order:
        if provider is None or not provider.available():
            continue
        for query in queries:
            if not query.strip():
                continue
            for candidate in provider.search(query):
                if candidate["url"] in used_urls:
                    continue
                if not (download(candidate["url"], raw) or download(candidate.get("fallback_url"), raw)):
                    continue
                if not make_vertical_frame(raw, frame):
                    continue
                used_urls.add(candidate["url"])
                return {**candidate, "file": frame, "query": query}
    return None


def stage_visuals(job, ai, research):
    with get_db() as conn:
        scenes = [dict(r) for r in conn.execute(
            "SELECT * FROM scenes WHERE job_id = ? ORDER BY scene_number", (job["id"],)).fetchall()]
    if not scenes:
        raise PermanentError("This job has no scene plan. Retry from the scene plan stage.")

    folder = job_dir(job["id"]) / "visuals"
    folder.mkdir(parents=True, exist_ok=True)
    stock_provider = next((p for p in (PexelsImages(), PixabayImages()) if p.available()), None)
    providers = (WikimediaImages(), stock_provider)
    used_urls, found, missing = set(), {}, []
    for scene in scenes:
        plan = json.loads(scene["visual_prompt"] or "{}")
        result = _find_picture(scene, plan, providers, used_urls, folder)  # VisualError -> retry later
        if result:
            found[scene["scene_number"]] = result
        else:
            missing.append(scene["scene_number"])

    # One gap can be covered by re-using the neighbouring scene's picture; more cannot.
    if len(missing) > 1 or not found:
        hint = "" if stock_provider else " Adding a free Pixabay key gives the factory many more pictures."
        raise PermanentError(f"No suitable, freely licensed pictures were found for scenes "
                             f"{', '.join(map(str, missing))}.{hint}")
    for number in missing:
        neighbour = found.get(number - 1) or found.get(number + 1) or next(iter(found.values()))
        frame = folder / f"scene_{number:02d}_916.jpg"
        frame.write_bytes(Path(neighbour["file"]).read_bytes())
        found[number] = {**neighbour, "file": frame, "reused": True}

    credits = []
    with get_db() as conn:
        conn.execute("DELETE FROM assets WHERE job_id = ? AND kind = 'image'", (job["id"],))
        for scene in scenes:
            item = found[scene["scene_number"]]
            conn.execute(
                "INSERT INTO assets (job_id, scene_id, kind, file_path, provider, license, source_url) "
                "VALUES (?, ?, 'image', ?, ?, ?, ?)",
                (job["id"], scene["id"], str(item["file"]), item["provider"],
                 item["credit"] + (" (re-used for a second scene)" if item.get("reused") else ""),
                 item["page_url"]))
            if not item.get("reused"):
                credits.append(f"{item['credit']}: {item['page_url']}")
    (job_dir(job["id"]) / "credits.txt").write_text("\n".join(credits) + "\n", encoding="utf-8")


# ---------- stage 6: voice ----------

def stage_voice(job, ai, research):
    _topic, channel = _load(job)
    script = load_json_file(job_dir(job["id"]) / "script.json", None)
    with get_db() as conn:
        scenes = [dict(r) for r in conn.execute(
            "SELECT * FROM scenes WHERE job_id = ? ORDER BY scene_number", (job["id"],)).fetchall()]
    if not script or not scenes:
        raise PermanentError("The script or scene plan for this job is missing.")

    # Narrate exactly the scene texts, in order, so pictures and voice stay matched.
    text = " ".join(scene["narration"] for scene in scenes)
    voice = channel["voice"] if channel["voice"] in VOICES else DEFAULT_VOICE
    folder = job_dir(job["id"]) / "audio"
    folder.mkdir(parents=True, exist_ok=True)
    raw, master, preview = folder / "voice_raw.wav", folder / "voice.wav", folder / "voice_preview.m4a"

    details = GeminiVoice().synthesize(text, voice, raw, job_id=job["id"])
    finish_audio(raw, master, preview)
    seconds = probe_duration(master)

    words = len(text.split())
    expected = words / WORDS_PER_SECOND
    if not (0.5 * expected <= seconds <= 2.0 * expected):
        raise VoiceError(f"The narration is {seconds:.0f} seconds long, but about {expected:.0f} were "
                         f"expected for {words} words. It may be cut off or garbled.")

    timings = scene_timings([len(sc["narration"].split()) for sc in scenes], seconds, find_pauses(master))
    with get_db() as conn:
        for scene, timing in zip(scenes, timings):
            conn.execute("UPDATE scenes SET duration_sec = ? WHERE id = ?", (timing["duration"], scene["id"]))
        conn.execute("DELETE FROM assets WHERE job_id = ? AND kind = 'voice'", (job["id"],))
        conn.execute(
            "INSERT INTO assets (job_id, kind, file_path, provider, license) VALUES (?, 'voice', ?, ?, ?)",
            (job["id"], str(master), f"gemini-tts:{details['model']}",
             f"Generated with Google Gemini text-to-speech (voice: {details['voice']})"))
    (job_dir(job["id"]) / "voice.json").write_text(json.dumps({
        **details, "seconds": seconds, "words": words, "requested_voice": voice,
        "scenes": [{"scene": sc["scene_number"], **t} for sc, t in zip(scenes, timings)],
    }, indent=2), encoding="utf-8")


# ---------- stage 7: captions ----------

def _scenes_and_timings(job):
    with get_db() as conn:
        scenes = [dict(r) for r in conn.execute(
            "SELECT * FROM scenes WHERE job_id = ? ORDER BY scene_number", (job["id"],)).fetchall()]
    voice = load_json_file(job_dir(job["id"]) / "voice.json", None)
    if not scenes or not voice or len(voice.get("scenes", [])) != len(scenes):
        raise PermanentError("The voice timing for this job is missing or out of date. Retry from Voice.")
    return scenes, voice


def stage_captions(job, ai, research):
    scenes, voice = _scenes_and_timings(job)
    folder = job_dir(job["id"])
    cues = caption_cues(scenes, voice["scenes"])
    if not cues:
        raise PermanentError("No caption text could be made from the narration.")
    write_captions(cues, folder / "captions.ass", folder / "captions.srt")
    with get_db() as conn:
        conn.execute("DELETE FROM assets WHERE job_id = ? AND kind = 'captions'", (job["id"],))
        conn.execute("INSERT INTO assets (job_id, kind, file_path, provider) VALUES (?, 'captions', ?, 'videofactory')",
                     (job["id"], str(folder / "captions.srt")))


# ---------- stage 8: assemble the video ----------

def stage_assemble(job, ai, research):
    scenes, voice = _scenes_and_timings(job)
    folder = job_dir(job["id"])
    work = folder / "render"
    work.mkdir(parents=True, exist_ok=True)
    clips = []
    for scene, timing in zip(scenes, voice["scenes"]):
        picture = folder / "visuals" / f"scene_{scene['scene_number']:02d}_916.jpg"
        if not picture.exists():
            raise PermanentError(f"The picture for scene {scene['scene_number']} is missing. Retry from Visuals.")
        clip = work / f"clip_{scene['scene_number']:02d}.mp4"
        render_scene_clip(picture, scene["motion"], timing["duration"], clip)
        clips.append(clip)

    shutil.copyfile(folder / "captions.ass", work / "captions.ass")
    output_dir = folder / "video"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "final.mp4"
    assemble(work, clips, folder / "audio" / "voice.wav", "captions.ass", voice["seconds"], output)

    info = probe_video(output)
    if info.get("width") != 1080 or info.get("height") != 1920 or not info.get("has_audio"):
        raise RenderError(f"The finished video is not right (found {info}).")
    with get_db() as conn:
        conn.execute("DELETE FROM videos WHERE job_id = ?", (job["id"],))
        conn.execute(
            "INSERT INTO videos (job_id, file_path, duration_sec, width, height) VALUES (?, ?, ?, ?, ?)",
            (job["id"], str(output), round(info["duration"], 2), info["width"], info["height"]))
    for leftover in work.glob("clip_*.mp4"):
        leftover.unlink()
    (work / "joined.mp4").unlink(missing_ok=True)


# ---------- stage 9: metadata + quality check ----------

MAX_QC_FIX_ROUNDS = 2


def _clean_tags(tags, limit):
    cleaned = []
    for tag in tags or []:
        word = re.sub(r"[^\w]", "", str(tag).replace(" ", ""), flags=re.UNICODE)
        if word and word.lower() not in [t.lower() for t in cleaned]:
            cleaned.append(word)
    return ["#" + t for t in cleaned[:limit]]


def make_metadata(job, ai, topic, channel, script):
    folder = job_dir(job["id"])
    reply = ai.generate(
        f"""Write publishing text for a short vertical video.
CHANNEL: {channel['name']} ({channel['niche']})
LANGUAGE: {channel['language'] or 'English'}
TOPIC: {topic['title']}
SCRIPT: {script['full_text']}

Rules: honest, no clickbait lies, no ALL CAPS, no emoji spam, no promises the video does not keep.
- youtube_title: under 70 characters, curiosity-driven but accurate
- summary: 2-3 sentences describing the video for the YouTube description
- youtube_hashtags: 3 to 5 relevant hashtags (words only)
- instagram_caption: 1-3 short sentences for Instagram Reels
- instagram_hashtags: 4 to 8 relevant hashtags (words only)
Return ONLY JSON with exactly these fields.""",
        job_id=job["id"], temperature=0.6, json_mode=True, schema=METADATA_SCHEMA)
    try:
        data = parse_json(reply["text"])
        data = data[0] if isinstance(data, list) else data
    except (json.JSONDecodeError, ValueError):
        save_raw_answer(job["id"], "metadata", reply["text"])
        raise LLMError("The AI's title/description answer was not in the expected format.")

    title = " ".join(str(data.get("youtube_title", "")).split())[:95] or script.get("title", topic["title"])[:95]
    yt_tags = _clean_tags(data.get("youtube_hashtags"), 5)
    ig_tags = _clean_tags(data.get("instagram_hashtags"), 8)
    sources = load_json_file(folder / "sources.json", [])
    credits_file = folder / "credits.txt"
    credits = credits_file.read_text(encoding="utf-8").strip() if credits_file.exists() else ""

    parts = [" ".join(str(data.get("summary", "")).split())]
    if sources:
        parts.append("Sources:\n" + "\n".join(f"- {s['title']} (Wikipedia): {s['url']}" for s in sources))
    if credits:
        parts.append("Image credits:\n" + credits)
    parts.append("Narration: AI-generated voice (Google Gemini). Facts checked against the sources above.")
    parts.append(" ".join(yt_tags))
    description = "\n\n".join(p for p in parts if p.strip())[:4900]

    metadata = {
        "youtube": {"title": title, "description": description, "hashtags": yt_tags},
        "instagram": {"caption": (" ".join(str(data.get("instagram_caption", "")).split()) + "\n\n"
                                  + " ".join(ig_tags)).strip()[:2100], "hashtags": ig_tags},
    }
    (folder / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    with get_db() as conn:
        conn.execute("UPDATE videos SET title = ?, description = ?, hashtags = ? WHERE job_id = ?",
                     (title, description, " ".join(yt_tags), job["id"]))
    return metadata


def check_script_claims(job, ai, topic, script):
    """Ask the AI whether the final script states anything the confirmed facts don't support."""
    with get_db() as conn:
        facts = [r["claim"] for r in conn.execute(
            "SELECT claim FROM research_sources WHERE topic_id = ? AND verified = 1", (topic["id"],)).fetchall()]
    reply = ai.generate(
        f"""You are a strict fact-checker for a short video script.
CONFIRMED FACTS (the only allowed factual basis):
{chr(10).join('- ' + f for f in facts)}

SCRIPT:
{script['full_text']}

List every sentence that states a factual claim (name, date, number, event, cause) that is NOT
supported by the confirmed facts, or that presents a disputed/legendary point as certain.
Ignore questions, rhetorical hooks, opinions, and the call to action.
If everything is supported, return an empty list.
Return ONLY JSON: {{"problems": [{{"sentence": "...", "issue": "..."}}]}}""",
        job_id=job["id"], temperature=0.0, json_mode=True, schema=CLAIMS_SCHEMA)
    try:
        problems = [p for p in json_list(reply["text"], "problems") if isinstance(p, dict)]
    except (json.JSONDecodeError, ValueError):
        save_raw_answer(job["id"], "claims", reply["text"])
        raise LLMError("The AI's script check answer was not in the expected format.")
    return problems


def stage_quality_check(job, ai, research):
    topic, channel = _load(job)
    folder = job_dir(job["id"])
    script = load_json_file(folder / "script.json", None)
    video_path = folder / "video" / "final.mp4"
    if not script or not video_path.exists():
        raise PermanentError("The script or finished video is missing. Retry from Video assembly.")
    with get_db() as conn:
        scenes = [dict(r) for r in conn.execute(
            "SELECT * FROM scenes WHERE job_id = ? ORDER BY scene_number", (job["id"],)).fetchall()]
    voice = load_json_file(folder / "voice.json", {})
    qc_state = load_json_file(folder / "qc_state.json", {"rounds": 0})

    metadata = make_metadata(job, ai, topic, channel, script)
    checks, info = check_video_file(video_path, len(scenes), len(voice.get("scenes", [])))
    checks += check_audio(video_path, info["duration"])
    checks += check_captions(folder / "captions.srt", " ".join(s["narration"] for s in scenes), info["duration"])

    problems = check_script_claims(job, ai, topic, script)
    checks.append({"name": "Script only states confirmed facts", "status": "fail" if problems else "pass",
                   "detail": "all claims supported" if not problems else
                   "; ".join(f"\u201c{p.get('sentence', '')[:80]}\u201d: {p.get('issue', '')[:100]}" for p in problems[:3]),
                   "fix": "SCRIPTING" if problems else None})
    yt = metadata["youtube"]
    checks.append({"name": "Title, description and hashtags", "status":
                   "pass" if yt["title"] and len(yt["description"]) > 40 and 3 <= len(yt["hashtags"]) <= 5 else "fail",
                   "detail": f"title {len(yt['title'])} chars, {len(yt['hashtags'])} hashtags",
                   "fix": None})

    failures = [c for c in checks if c["status"] == "fail"]
    report = {"passed": not failures, "rounds": qc_state["rounds"], "checks": checks}
    (folder / "qc_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    with get_db() as conn:
        conn.execute("UPDATE videos SET qc_passed = ?, qc_report = ? WHERE job_id = ?",
                     (0 if failures else 1, json.dumps(report), job["id"]))
    if not failures:
        return

    summary = "; ".join(f"{c['name']}: {c['detail']}" for c in failures)[:400]
    fixes = [c["fix"] for c in failures if c["fix"]]
    if fixes and qc_state["rounds"] < MAX_QC_FIX_ROUNDS:
        # Redo the EARLIEST stage at fault; everything after it is rebuilt automatically.
        earliest = min(fixes, key=PIPELINE_ORDER.index)
        qc_state["rounds"] += 1
        (folder / "qc_state.json").write_text(json.dumps(qc_state), encoding="utf-8")
        if earliest == "SCRIPTING":
            qc_state["script_feedback"] = [f"{p.get('sentence', '')} -> {p.get('issue', '')}" for p in problems][:6]
            (folder / "qc_state.json").write_text(json.dumps(qc_state), encoding="utf-8")
        raise RedoFrom(earliest, f"Quality check failed ({summary}). Automatic fix: redoing "
                                 f"{STAGE_LABELS.get(earliest, earliest)} (round {qc_state['rounds']} of {MAX_QC_FIX_ROUNDS}).")
    raise PermanentError(f"Quality check still fails after automatic fixes: {summary}")


# ---------- stage 10: publish to YouTube ----------

def stage_publish(job, ai, research):
    folder = job_dir(job["id"])
    with get_db() as conn:
        done = conn.execute("SELECT id FROM platform_posts WHERE job_id = ? AND platform = 'youtube' "
                            "AND status = 'published'", (job["id"],)).fetchone()
        post = conn.execute("SELECT * FROM platform_posts WHERE job_id = ? AND platform = 'youtube' "
                            "AND status IN ('queued', 'uploading', 'failed') ORDER BY id DESC LIMIT 1",
                            (job["id"],)).fetchone()
    if done:
        return  # already uploaded: never upload the same video twice
    if post is None:
        raise PermanentError("No YouTube upload was requested for this video.")
    if connection_status().get("state") != "connected":
        raise YouTubeError("YouTube is not connected (or Google ended the connection). Reconnect YouTube on the Mac.")
    metadata = load_json_file(folder / "metadata.json", None)
    video_path = folder / "video" / "final.mp4"
    if not metadata or not video_path.exists():
        raise PermanentError("The finished video or its title/description is missing.")

    with get_db() as conn:
        conn.execute("UPDATE platform_posts SET status = 'uploading', updated_at = datetime('now') WHERE id = ?",
                     (post["id"],))
    yt = metadata["youtube"]
    try:
        result = upload_video(video_path, yt["title"], yt["description"], yt["hashtags"], privacy=post["privacy"])
    except YouTubeError as exc:
        with get_db() as conn:
            conn.execute("UPDATE platform_posts SET status = 'failed', error = ?, updated_at = datetime('now') "
                         "WHERE id = ?", (str(exc)[:500], post["id"]))
        raise

    # Verify with YouTube that the video really exists.
    try:
        check = video_status(result["id"])
    except YouTubeError:
        check = {"found": None}
    with get_db() as conn:
        conn.execute(
            "UPDATE platform_posts SET status = 'published', post_id = ?, url = ?, privacy = ?, "
            "account_id = ?, published_at = datetime('now'), details = ?, error = '', "
            "updated_at = datetime('now') WHERE id = ?",
            (result["id"], result["url"], result["privacy"], connection_status().get("channel_id", ""),
             json.dumps(check), post["id"]))
    log_event("youtube_uploaded", f"job {job['id']}: {result['url']} ({result['privacy']})")


STAGE_FUNCTIONS = {
    "RESEARCHING": stage_research,
    "FACT_CHECKING": stage_fact_check,
    "SCRIPTING": stage_script,
    "SCENE_PLANNING": stage_scene_plan,
    "GENERATING_VISUALS": stage_visuals,
    "GENERATING_VOICE": stage_voice,
    "GENERATING_CAPTIONS": stage_captions,
    "ASSEMBLING": stage_assemble,
    "QUALITY_CHECK": stage_quality_check,
    "PUBLISHING": stage_publish,
}

FIX_STEPS_NEW_TOPIC = ("1. Tap Back to dashboard, then Topics for this channel.\n"
                       "2. Tap Use this topic on a different, well-documented idea.\n"
                       "3. Tap Start video.")


# ---------- failure handling ----------

def handle_failure(job, stage, message, permanent):
    label = STAGE_LABELS.get(stage, stage)
    if not permanent and job["retry_count"] < MAX_RETRIES:
        delay = BACKOFF_SECONDS[min(job["retry_count"], len(BACKOFF_SECONDS) - 1)]
        when = (datetime.now(timezone.utc) + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S")
        with get_db() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'RETRYING', retry_count = retry_count + 1, last_error = ?, "
                "next_attempt_at = ?, updated_at = datetime('now') WHERE id = ? AND status != 'CANCELLED'",
                (message[:500], when, job["id"]))
        log_event("job_retry_scheduled", f"job {job['id']} {stage}: {message[:200]}")
        return

    tries = job["retry_count"] + 1
    lowered = message.lower()
    if permanent and "reconnect youtube" in lowered:
        recommendation = "Reconnect YouTube on the Mac, then tap Retry."
        steps = ("1. On the Mac, open http://127.0.0.1:8000/youtube in Chrome (log in if asked).\n"
                 "2. Tap Reconnect YouTube and approve on Google's page.\n"
                 "3. Open this job on your phone and tap Retry.")
    elif permanent and "quality check" in lowered:
        recommendation = "Watch the video and read the quality report. If it looks fine, tap Approve anyway; otherwise tap Retry or cancel."
        steps = ("1. Scroll up and watch the video.\n2. Read the Quality check list (red items).\n"
                 "3. Tap Approve anyway if you are happy with it, or Retry to run the check again.")
    elif permanent and "pictures" in lowered:
        recommendation = "Add a free Pixabay key (or pick a more visual topic), then tap Retry."
        steps = ("1. Follow the Pixabay key instructions from Step 15 on the Mac.\n"
                 "2. Restart the app.\n3. Open this job and tap Retry.")
    elif permanent and ("facts" in lowered or "articles" in lowered):
        recommendation = "Choose a different, better-documented topic for this video."
        steps = FIX_STEPS_NEW_TOPIC
    elif "key" in lowered:
        recommendation = "Fix the Gemini key, then tap Retry."
        steps = "1. On the Mac, redo Step 9 to save a new Gemini key.\n2. Restart the app.\n3. Open this job and tap Retry."
    else:
        recommendation = "Wait a while (free AI limits reset daily), then tap Retry."
        steps = "1. Wait at least an hour, or until tomorrow if the free daily limit was reached.\n2. Open this job and tap Retry."

    with get_db() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'EXCEPTION', last_error = ?, next_attempt_at = NULL, "
            "updated_at = datetime('now') WHERE id = ? AND status != 'CANCELLED'",
            (message[:500], job["id"]))
        conn.execute(
            "INSERT INTO exceptions (job_id, what_happened, why, what_was_tried, recommendation, fix_steps) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (job["id"], f"The {label} stage could not finish.", message,
             "Not retried, because retrying would not fix this." if permanent
             else f"Tried {tries} time(s), waiting longer between attempts.",
             recommendation, steps))
    log_event("job_exception", f"job {job['id']} {stage}: {message[:200]}")


# ---------- the worker ----------

def run_one(ai, research):
    """Run the next stage of the oldest job that is ready. Returns True if work was done."""
    placeholders = ",".join("?" for _ in BUILT_STAGES)
    with get_db() as conn:
        row = conn.execute(
            f"SELECT * FROM jobs WHERE stage IN ({placeholders}) "
            "AND status NOT IN ('CANCELLED', 'EXCEPTION', 'FAILED') "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= datetime('now')) "
            "ORDER BY id LIMIT 1", BUILT_STAGES).fetchone()
        if row is None:
            return False
        job = dict(row)
        stage = job["stage"]
        conn.execute("UPDATE jobs SET status = ?, updated_at = datetime('now') "
                     "WHERE id = ? AND status != 'CANCELLED'", (stage, job["id"]))

    try:
        STAGE_FUNCTIONS[stage](job, ai, research)
    except RedoFrom as redo:
        with get_db() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, stage = ?, retry_count = 0, next_attempt_at = NULL, "
                "last_error = ?, updated_at = datetime('now') WHERE id = ? AND status = ?",
                (redo.stage, redo.stage, str(redo)[:500], job["id"], stage))
        log_event("job_qc_redo", f"job {job['id']}: {redo}"[:300])
        return True
    except PermanentError as exc:
        handle_failure(job, stage, str(exc), permanent=True)
        return True
    except YouTubeError as exc:
        handle_failure(job, stage, str(exc), permanent=not exc.retryable)
        return True
    except (LLMError, ResearchError, VisualError, VoiceError, RenderError) as exc:
        message = str(exc)
        handle_failure(job, stage, message, permanent=any(h in message for h in PERMANENT_HINTS))
        return True
    except Exception as exc:  # unexpected bug: retry a few times, then ask the owner
        traceback.print_exc()
        handle_failure(job, stage, f"Unexpected problem: {exc}", permanent=False)
        return True

    nxt = NEXT_STAGE[stage]
    with get_db() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, stage = ?, retry_count = 0, next_attempt_at = NULL, "
            "last_error = '', output_path = ?, updated_at = datetime('now') WHERE id = ? AND status = ?",
            (nxt, nxt, str(job_dir(job["id"])), job["id"], stage))
    log_event("job_stage_done", f"job {job['id']}: {stage} -> {nxt}")
    return True


_worker = {"thread": None, "last_beat": 0.0}


def _loop():
    ai, research = GeminiProvider(), WikipediaResearch()
    while True:
        _worker["last_beat"] = time.time()
        try:
            worked = run_one(ai, research)
        except Exception:
            traceback.print_exc()
            worked = False
        time.sleep(1 if worked else 5)


def start_worker():
    if _worker["thread"] is None:
        thread = threading.Thread(target=_loop, name="videofactory-worker", daemon=True)
        thread.start()
        _worker["thread"] = thread


def worker_alive():
    thread = _worker["thread"]
    return bool(thread and thread.is_alive() and time.time() - _worker["last_beat"] < 600)
