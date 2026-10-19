"""
VideoFactory - database (the app's long-term memory).

Uses SQLite: a single file at VideoFactory/data/factory.db.
Free, built into Python, no server needed.

Upgrades ("migrations") are applied automatically and safely at start-up,
so your existing data is always kept.
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "factory.db"
SCHEMA_VERSION = 3

REQUIRED_TABLES = (
    "schema_version", "settings", "channels", "topics", "research_sources",
    "jobs", "scripts", "scenes", "assets", "videos",
    "provider_usage", "exceptions", "audit_logs", "social_accounts", "platform_posts",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE,
    niche          TEXT NOT NULL,
    audience       TEXT NOT NULL DEFAULT '',
    language       TEXT NOT NULL DEFAULT 'en',
    voice          TEXT NOT NULL DEFAULT '',
    visual_style   TEXT NOT NULL DEFAULT '',
    caption_style  TEXT NOT NULL DEFAULT 'default',
    content_rules  TEXT NOT NULL DEFAULT '',
    videos_per_day INTEGER NOT NULL DEFAULT 1,
    timezone       TEXT NOT NULL DEFAULT 'UTC',
    is_paused      INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS topics (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id    INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    title         TEXT NOT NULL,
    summary       TEXT NOT NULL DEFAULT '',
    score         REAL,
    score_details TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'candidate'
                  CHECK (status IN ('candidate','selected','rejected','used')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_topics_channel ON topics(channel_id, status);

CREATE TABLE IF NOT EXISTS research_sources (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id     INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    claim        TEXT NOT NULL,
    claim_type   TEXT NOT NULL DEFAULT 'fact'
                 CHECK (claim_type IN ('fact','inference','legend','disputed','creative')),
    source_title TEXT NOT NULL DEFAULT '',
    source_url   TEXT NOT NULL DEFAULT '',
    verified     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    topic_id    INTEGER REFERENCES topics(id) ON DELETE SET NULL,
    status      TEXT NOT NULL DEFAULT 'QUEUED'
                CHECK (status IN ('QUEUED','RESEARCHING','TOPIC_SELECTED','FACT_CHECKING',
                                  'SCRIPTING','SCENE_PLANNING','GENERATING_VISUALS',
                                  'GENERATING_VOICE','GENERATING_CAPTIONS','ASSEMBLING',
                                  'QUALITY_CHECK','READY','SCHEDULED','PUBLISHING',
                                  'PUBLISHED','FAILED','RETRYING','EXCEPTION','CANCELLED')),
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT NOT NULL DEFAULT '',
    output_path TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_jobs_channel_status ON jobs(channel_id, status);

CREATE TABLE IF NOT EXISTS scripts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    hook       TEXT NOT NULL DEFAULT '',
    full_text  TEXT NOT NULL,
    word_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scenes (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id             INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    scene_number       INTEGER NOT NULL,
    duration_sec       REAL NOT NULL DEFAULT 0,
    narration          TEXT NOT NULL DEFAULT '',
    visual_description TEXT NOT NULL DEFAULT '',
    visual_prompt      TEXT NOT NULL DEFAULT '',
    motion             TEXT NOT NULL DEFAULT '',
    caption            TEXT NOT NULL DEFAULT '',
    sfx                TEXT NOT NULL DEFAULT '',
    transition         TEXT NOT NULL DEFAULT 'cut',
    UNIQUE (job_id, scene_number)
);

CREATE TABLE IF NOT EXISTS assets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    scene_id   INTEGER REFERENCES scenes(id) ON DELETE SET NULL,
    kind       TEXT NOT NULL
               CHECK (kind IN ('image','video','voice','music','sfx','captions','other')),
    file_path  TEXT NOT NULL,
    provider   TEXT NOT NULL DEFAULT '',
    license    TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS videos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
    file_path    TEXT NOT NULL,
    duration_sec REAL,
    width        INTEGER,
    height       INTEGER,
    qc_passed    INTEGER NOT NULL DEFAULT 0,
    qc_report    TEXT NOT NULL DEFAULT '{}',
    title        TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',
    hashtags     TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS provider_usage (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id             INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    provider           TEXT NOT NULL,
    category           TEXT NOT NULL,
    units              REAL NOT NULL DEFAULT 0,
    unit_name          TEXT NOT NULL DEFAULT '',
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS exceptions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id         INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    what_happened  TEXT NOT NULL,
    why            TEXT NOT NULL DEFAULT '',
    what_was_tried TEXT NOT NULL DEFAULT '',
    recommendation TEXT NOT NULL DEFAULT '',
    fix_steps      TEXT NOT NULL DEFAULT '',
    resolved       INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event      TEXT NOT NULL,
    details    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Each upgrade runs once, in order. Never edit an old one; only add new ones.
MIGRATIONS = {
    2: [
        # Which pipeline stage a job is on (kept even while it is RETRYING).
        "ALTER TABLE jobs ADD COLUMN stage TEXT NOT NULL DEFAULT ''",
        # When a RETRYING job may try again (UTC, 'YYYY-MM-DD HH:MM:SS').
        "ALTER TABLE jobs ADD COLUMN next_attempt_at TEXT",
    ],
    3: [
        # Connected social media accounts (never passwords; permission tokens live in secrets/).
        """CREATE TABLE IF NOT EXISTS social_accounts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            platform     TEXT NOT NULL,
            account_id   TEXT NOT NULL,
            account_name TEXT NOT NULL DEFAULT '',
            status       TEXT NOT NULL DEFAULT 'connected'
                         CHECK (status IN ('connected', 'needs_reconnect', 'disconnected')),
            scopes       TEXT NOT NULL DEFAULT '',
            connected_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (platform, account_id))""",
        # Every attempt to put a video on a platform, and its result.
        """CREATE TABLE IF NOT EXISTS platform_posts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id        INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            platform      TEXT NOT NULL,
            account_id    TEXT NOT NULL DEFAULT '',
            post_id       TEXT NOT NULL DEFAULT '',
            url           TEXT NOT NULL DEFAULT '',
            privacy       TEXT NOT NULL DEFAULT 'private',
            status        TEXT NOT NULL DEFAULT 'queued'
                          CHECK (status IN ('queued', 'uploading', 'published', 'failed')),
            scheduled_for TEXT,
            published_at  TEXT,
            details       TEXT NOT NULL DEFAULT '',
            error         TEXT NOT NULL DEFAULT '',
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at    TEXT NOT NULL DEFAULT (datetime('now')))""",
        "CREATE INDEX IF NOT EXISTS idx_posts_job ON platform_posts(job_id, platform)",
    ],
}


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def get_db():
    """Open the database, save changes if all went well, undo them if not."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create the database file and all tables, then apply any upgrades."""
    with get_db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_version (version) VALUES (1)")
            current = 1
        else:
            current = row["version"]

    for version in sorted(MIGRATIONS):
        if version <= current:
            continue
        with get_db() as conn:
            for statement in MIGRATIONS[version]:
                conn.execute(statement)
            conn.execute("UPDATE schema_version SET version = ?", (version,))
        log_event("database_upgraded", f"to version {version}")


def log_event(event, details=""):
    with get_db() as conn:
        conn.execute("INSERT INTO audit_logs (event, details) VALUES (?, ?)", (event, details))
