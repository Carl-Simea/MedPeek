"""Everything stored in images.db: the chat history and the normal reference X-rays."""
import os
import sqlite3
import uuid
from contextlib import contextmanager

# images.db sits next to app.py. Set XRAY_DB to use a different file.
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("XRAY_DB", os.path.join(PROJECT_DIR, "images.db"))


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create the messages table if it isn't there yet."""
    with connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                   id INTEGER PRIMARY KEY,
                   chat_id TEXT NOT NULL,
                   sender TEXT NOT NULL,
                   text TEXT NOT NULL
               )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS messages_by_chat ON messages (chat_id)")


def new_chat_id():
    return uuid.uuid4().hex


def add_message(chat_id, sender, text):
    with connect() as conn:
        conn.execute(
            "INSERT INTO messages (chat_id, sender, text) VALUES (?, ?, ?)", (chat_id, sender, text)
        )


def get_messages(chat_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT sender, text FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def delete_chat(chat_id):
    with connect() as conn:
        conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))


def supported_parts():
    """Body parts that have normal reference X-rays in the images table."""
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT body_part FROM images WHERE body_part IS NOT NULL ORDER BY body_part"
            ).fetchall()
    except sqlite3.OperationalError:
        print("images table has no body_part column yet; run the ALTER TABLE step")
        return []
    return [row["body_part"] for row in rows]


def reference_images(body_part):
    """Normal X-rays of this body part, as (title, image bytes) pairs."""
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT title, image FROM images WHERE body_part = ?", (body_part,)
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(row["title"], row["image"]) for row in rows]
