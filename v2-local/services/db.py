"""Everything MedPeek stores lives in images.db: users, their chats, chat messages,
and the normal reference X-rays."""
import json
import os
import sqlite3
from contextlib import contextmanager

# images.db sits next to app.py. Set MEDPEEK_DB to use a different file.
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("MEDPEEK_DB", os.path.join(PROJECT_DIR, "images.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    first_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT 'New chat',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS chats_by_user ON chats (user_id, updated_at);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    sender TEXT NOT NULL,          -- 'user' or 'bot'
    kind TEXT NOT NULL,            -- 'text' or 'analysis'
    text TEXT NOT NULL DEFAULT '',
    data TEXT,                     -- JSON for analysis results
    image BLOB,                    -- the uploaded X-ray, for user messages
    filename TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS messages_by_chat ON messages (chat_id, id);

CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    body_part TEXT NOT NULL,
    image BLOB NOT NULL,
    source TEXT
);
"""

MESSAGE_COLUMNS = "id, sender, kind, text, data, filename, image IS NOT NULL AS has_image"


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def init_db():
    """Create the tables, replacing older versions of messages/images if they're still around."""
    with connect() as conn:
        message_cols = columns(conn, "messages")
        if message_cols and "kind" not in message_cols:
            conn.execute("DROP TABLE messages")  # old chat format from before logins
        image_cols = columns(conn, "images")
        if image_cols and "body_part" not in image_cols:
            conn.execute("ALTER TABLE images ADD COLUMN body_part TEXT NOT NULL DEFAULT 'chest'")
        if image_cols and "source" not in image_cols:
            conn.execute("ALTER TABLE images ADD COLUMN source TEXT")
        conn.executescript(SCHEMA)


# ---------- users ----------

def create_user(username, first_name, password_hash):
    """Return the new user's id, or None if the username is taken."""
    try:
        with connect() as conn:
            return conn.execute(
                "INSERT INTO users (username, first_name, password_hash) VALUES (?, ?, ?)",
                (username, first_name, password_hash),
            ).lastrowid
    except sqlite3.IntegrityError:
        return None


def get_user_by_username(username):
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return dict(row) if row else None


def get_user(user_id):
    with connect() as conn:
        row = conn.execute("SELECT id, username, first_name FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


# ---------- chats ----------

def create_chat(user_id):
    with connect() as conn:
        return conn.execute("INSERT INTO chats (user_id) VALUES (?)", (user_id,)).lastrowid


def get_chat(user_id, chat_id):
    """The chat, only if it belongs to this user."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM chats WHERE id = ? AND user_id = ?", (chat_id, user_id)).fetchone()
    return dict(row) if row else None


def list_chats(user_id):
    """The user's chats, most recently used first, with how many X-rays each has."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT c.id, c.title, c.updated_at,
                      (SELECT count(*) FROM messages m WHERE m.chat_id = c.id AND m.image IS NOT NULL) AS xrays
               FROM chats c WHERE c.user_id = ? ORDER BY c.updated_at DESC, c.id DESC""",
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def latest_chat_id(user_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM chats WHERE user_id = ? ORDER BY updated_at DESC, id DESC LIMIT 1", (user_id,)
        ).fetchone()
    return row["id"] if row else None


def set_chat_title(chat_id, title):
    with connect() as conn:
        conn.execute("UPDATE chats SET title = ? WHERE id = ?", (title[:60], chat_id))


def delete_chat(user_id, chat_id):
    with connect() as conn:
        conn.execute("DELETE FROM chats WHERE id = ? AND user_id = ?", (chat_id, user_id))


# ---------- messages ----------

def add_message(chat_id, sender, text="", kind="text", data=None, image=None, filename=None):
    with connect() as conn:
        message_id = conn.execute(
            "INSERT INTO messages (chat_id, sender, kind, text, data, image, filename) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, sender, kind, text, json.dumps(data) if data is not None else None, image, filename),
        ).lastrowid
        conn.execute("UPDATE chats SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (chat_id,))
        return message_id


def _message(row):
    message = dict(row)
    message["data"] = json.loads(message["data"]) if message["data"] else None
    return message


def get_messages(chat_id, after_id=0):
    with connect() as conn:
        rows = conn.execute(
            f"SELECT {MESSAGE_COLUMNS} FROM messages WHERE chat_id = ? AND id > ? ORDER BY id", (chat_id, after_id)
        ).fetchall()
    return [_message(row) for row in rows]


def recent_messages(chat_id, limit=8):
    with connect() as conn:
        rows = conn.execute(
            f"SELECT {MESSAGE_COLUMNS} FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?", (chat_id, limit)
        ).fetchall()
    return [_message(row) for row in reversed(rows)]


def message_image(user_id, message_id):
    """An uploaded X-ray, only if it belongs to one of this user's chats."""
    with connect() as conn:
        row = conn.execute(
            """SELECT m.image FROM messages m JOIN chats c ON c.id = m.chat_id
               WHERE m.id = ? AND c.user_id = ? AND m.image IS NOT NULL""",
            (message_id, user_id),
        ).fetchone()
    return row["image"] if row else None


def image_count(chat_id):
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) FROM messages WHERE chat_id = ? AND image IS NOT NULL", (chat_id,)
        ).fetchone()[0]


def user_message_count(chat_id):
    with connect() as conn:
        return conn.execute("SELECT count(*) FROM messages WHERE chat_id = ? AND sender = 'user'", (chat_id,)).fetchone()[0]


# ---------- reference X-rays ----------

def supported_parts():
    """Body parts that have normal reference X-rays."""
    with connect() as conn:
        rows = conn.execute("SELECT DISTINCT body_part FROM images ORDER BY body_part").fetchall()
    return [row["body_part"] for row in rows]


def reference_images(body_part):
    """Normal X-rays of this body part, as (title, image bytes) pairs."""
    with connect() as conn:
        rows = conn.execute("SELECT title, image FROM images WHERE body_part = ?", (body_part,)).fetchall()
    return [(row["title"], row["image"]) for row in rows]