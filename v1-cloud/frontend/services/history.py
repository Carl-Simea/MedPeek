"""Account-owned local conversations and durable analysis results."""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from flask import current_app, g, abort

@contextmanager
def db():
    conn=sqlite3.connect(current_app.config['AUTH_DB'],timeout=15)
    conn.row_factory=sqlite3.Row
    try:
        with conn: yield conn
    finally: conn.close()

def init_history(app):
    with app.app_context(), db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS chats(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,title TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,image_count INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,chat_id TEXT NOT NULL,role TEXT NOT NULL,text TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'done',job_id TEXT,result TEXT,created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS chats_owner ON chats(user_id,updated);
        CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id,id);
        """)
        conn.execute("UPDATE messages SET status='error',text='The upload was interrupted before its receipt was saved. Check the backend before resending.' WHERE status='sending'")

def owned(conn,chat_id):
    row=conn.execute('SELECT * FROM chats WHERE id=? AND user_id=?',(chat_id,g.user['id'])).fetchone()
    if not row: abort(404)
    return row

def create():
    chat_id=uuid.uuid4().hex
    with db() as conn: conn.execute('INSERT INTO chats(id,user_id,title,created,updated) VALUES (?,?,?,?,?)',(chat_id,g.user['id'],'New conversation',time.time(),time.time()))
    return chat_id

def listing():
    with db() as conn: return [dict(r) for r in conn.execute('SELECT * FROM chats WHERE user_id=? ORDER BY updated DESC',(g.user['id'],))]

def read(chat_id):
    with db() as conn:
        chat=dict(owned(conn,chat_id))
        chat['messages']=[dict(r) for r in conn.execute('SELECT * FROM messages WHERE chat_id=? ORDER BY id',(chat_id,))]
        for message in chat['messages']:
            message['result']=json.loads(message['result']) if message['result'] else None
        return chat

def begin(chat_id,text,filename):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        chat=owned(conn,chat_id)
        if filename and chat['image_count']>=20: abort(400,description='This conversation has reached its 20-image limit.')
        user_text=text+('\nAttached: '+filename if filename else '')
        conn.execute('INSERT INTO messages(chat_id,role,text,created) VALUES (?,?,?,?)',(chat_id,'user',user_text,time.time()))
        cursor=conn.execute('INSERT INTO messages(chat_id,role,text,status,created) VALUES (?,?,?,?,?)',(chat_id,'bot','Sending image…' if filename else 'Please attach a scan and choose its body area so I can help explain it.','sending' if filename else 'done',time.time()))
        title=chat['title'] if chat['title']!='New conversation' else (text or filename or 'Conversation')[:65]
        conn.execute('UPDATE chats SET title=?,updated=?,image_count=image_count+? WHERE id=?',(title,time.time(),int(bool(filename)),chat_id))
        return cursor.lastrowid

def update(message_id,status,text='',job_id=None,result=None):
    with db() as conn:
        conn.execute('UPDATE messages SET status=?,text=?,job_id=COALESCE(?,job_id),result=? WHERE id=?',(status,text,job_id,json.dumps(result) if result else None,message_id))
        conn.execute('UPDATE chats SET updated=? WHERE id=(SELECT chat_id FROM messages WHERE id=?)',(time.time(),message_id))

def refresh(message):
    from services.backend_client import poll_analysis
    import requests
    try:
        data=poll_analysis(message['job_id'])
        state=data.get('status')
        if state in ('done','error'):
            update(message['id'],state,data.get('text') or data.get('error') or '',result=data.get('result'))
        return data
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code==404:
            update(message['id'],'error','This analysis is no longer available on the backend. Please submit the image again.')
        return None
    except (requests.RequestException,ValueError,KeyError): return None

def collect(app):
    # Save finished jobs even if the browser is closed.
    while True:
        try:
            with app.app_context():
                with db() as conn: pending=[dict(r) for r in conn.execute("SELECT * FROM messages WHERE status IN ('queued','running') AND job_id IS NOT NULL")]
                for message in pending: refresh(message)
        except Exception:
            app.logger.exception('History result collection failed')
        time.sleep(5)
