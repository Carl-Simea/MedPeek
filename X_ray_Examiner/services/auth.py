"""Local accounts and revocable sessions. No patient data is stored here."""
from contextlib import contextmanager
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from flask import request, session, g, redirect, url_for, render_template, jsonify
from werkzeug.security import generate_password_hash, check_password_hash


def init_auth(app):
    directory = Path(os.environ.get('MEDPEEK_AUTH_DIR', Path(app.root_path) / 'instance'))
    directory.mkdir(parents=True, exist_ok=True)
    key_file = directory / 'session.key'
    if not key_file.exists():
        try:
            with key_file.open('x') as f:
                f.write(secrets.token_hex(32))
        except FileExistsError:
            pass
    app.secret_key = os.environ.get('FLASK_SECRET_KEY') or key_file.read_text().strip()
    app.config['AUTH_DB'] = str(directory / 'accounts.sqlite3')
    app.config['SESSION_COOKIE_SECURE'] = os.environ.get('MEDPEEK_HTTPS') == '1'
    @contextmanager
    def db():
        connection = sqlite3.connect(app.config['AUTH_DB'], timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS attempts(address TEXT NOT NULL, created REAL NOT NULL);
        """)
    dummy_hash = generate_password_hash(secrets.token_hex(32))
    def token_hash(token):
        return hashlib.sha256(token.encode()).hexdigest()
    def revoke():
        if session.get('auth_token'):
            with db() as conn:
                conn.execute('DELETE FROM sessions WHERE token_hash=?', (token_hash(session['auth_token']),))
    def csrf():
        if 'csrf' not in session:
            session['csrf'] = secrets.token_urlsafe(32)
        return session['csrf']
    app.jinja_env.globals['csrf_token'] = csrf

    @app.before_request
    def authenticate():
        g.user = None
        token = session.get('auth_token')
        if token:
            with db() as conn:
                g.user = conn.execute('SELECT users.id, users.username FROM sessions JOIN users ON users.id=sessions.user_id WHERE token_hash=? AND expires>?', (token_hash(token), time.time())).fetchone()
            if g.user is None:
                session.clear()
        public = {'home', 'login', 'register', 'static'}
        if request.endpoint not in public and not g.user:
            if request.path.startswith('/api/') or request.path == '/clear':
                return jsonify(error='Please sign in to continue.'), 401
            return redirect(url_for('login'))
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            supplied = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token', '')
            expected = session.get('csrf', '')
            if not expected or not hmac.compare_digest(supplied, expected):
                return jsonify(error='Your session changed. Refresh the page and try again.'), 403

    @app.after_request
    def no_cache(response):
        if request.endpoint != 'static':
            response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'DENY'
        return response

    def account_page(registering=False):
        if g.user:
            return redirect(url_for('results'))
        error = None
        if request.method == 'POST':
            username = request.form.get('username', '').strip().lower()
            password = request.form.get('password', '')
            now = time.time()
            with db() as conn:
                conn.execute('DELETE FROM attempts WHERE created<?', (now-900,))
                conn.execute('DELETE FROM sessions WHERE expires<?', (now,))
                count = conn.execute('SELECT count(*) FROM attempts WHERE address=?', (request.remote_addr,)).fetchone()[0]
                if count >= 10:
                    return render_template('auth.html', registering=registering, error='Too many attempts. Try again in 15 minutes.'), 429
                conn.execute('INSERT INTO attempts VALUES (?,?)', (request.remote_addr,now))
            if registering:
                if not re.fullmatch(r'[a-z0-9_]{3,32}', username):
                    error = 'Use 3–32 letters, numbers, or underscores for your username.'
                elif not 12 <= len(password) <= 128:
                    error = 'Choose a password between 12 and 128 characters.'
                elif password != request.form.get('confirm_password'):
                    error = 'Your passwords do not match.'
                else:
                    hashed = generate_password_hash(password)
                    try:
                        with db() as conn:
                            cursor = conn.execute('INSERT INTO users(username,password_hash) VALUES (?,?)', (username,hashed))
                            user_id = cursor.lastrowid
                    except sqlite3.IntegrityError:
                        error = 'That username is already taken.'
            else:
                with db() as conn:
                    user = conn.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
                valid = check_password_hash(user['password_hash'] if user else dummy_hash, password[:129])
                if not user or not valid or len(password)>128:
                    error = 'Incorrect username or password.'
                else:
                    user_id = user['id']
            if not error:
                revoke()
                session.clear()
                token = secrets.token_urlsafe(32)
                with db() as conn:
                    conn.execute('INSERT INTO sessions VALUES (?,?,?)', (token_hash(token), user_id, now+43200))
                session['auth_token'] = token
                csrf()
                return redirect(url_for('results'))
        return render_template('auth.html', registering=registering, error=error)

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        return account_page()

    @app.route('/register', methods=['GET', 'POST'])
    def register():
        return account_page(True)

    @app.post('/logout')
    def logout():
        revoke()
        session.clear()
        return redirect(url_for('login'))

