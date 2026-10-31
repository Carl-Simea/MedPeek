"""MedPeek: upload a hand X-ray, Gemma compares it with normal X-rays and looks for breaks."""
import os
import re
import secrets
from functools import wraps

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from services import db, ollama_client
from services.analyzer import analyze_xray, chat_reply

MAX_PER_SEND = 5
MAX_PER_CHAT = 20
USERNAME = re.compile(r"[A-Za-z0-9_.-]{3,30}")
FIRST_NAME = re.compile(r"[^\W\d_]+(?:['-][^\W\d_]+)*")


def secret_key():
    """Use FLASK_SECRET_KEY if set; otherwise make a random key once and keep it in .secret_key."""
    if os.environ.get("FLASK_SECRET_KEY"):
        return os.environ["FLASK_SECRET_KEY"]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app = Flask(__name__)
app.secret_key = secret_key()
app.config.update(
    MAX_CONTENT_LENGTH=15 * 1024 * 1024,  # one X-ray per request, up to 15 MB
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

db.init_db()
ollama_client.start_in_background()  # ollama serve + ollama pull + load Gemma, while the site starts


# ---------- security helpers ----------

def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def check_csrf():
    if request.method == "POST":
        sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Your session expired. Reload the page and try again.")


@app.before_request
def load_user():
    g.user = db.get_user(session["user_id"]) if "user_id" in session else None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if request.path.startswith("/api/"):
                return jsonify(error="Please sign in again."), 401
            return redirect(url_for("home"))
        return view(*args, **kwargs)
    return wrapped


def sign_in(user):
    session.clear()
    session["user_id"] = user["id"]
    csrf_token()


# ---------- pages ----------

@app.route("/")
def home():
    if g.user:
        return redirect(url_for("results"))
    return render_template("home.html", tab=request.args.get("tab", "login"))


@app.post("/register")
def register():
    first_name = request.form.get("first_name", "").strip()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    error = None
    if not FIRST_NAME.fullmatch(first_name) or len(first_name) > 30:
        error = "Type just your first name, using letters."
    elif not USERNAME.fullmatch(username):
        error = "Usernames are 3 to 30 letters, numbers, dots, dashes or underscores."
    elif len(password) < 8:
        error = "Use a password of at least 8 characters."
    elif password != request.form.get("confirm", ""):
        error = "The two passwords don't match."
    if error is None:
        first_name = first_name[0].upper() + first_name[1:]
        user_id = db.create_user(username, first_name, generate_password_hash(password))
        if user_id is None:
            error = "That username is taken. Try another one."
        else:
            sign_in(db.get_user(user_id))
            return redirect(url_for("results"))
    flash(error, "register")
    return redirect(url_for("home", tab="register"))


@app.post("/login")
def login():
    user = db.get_user_by_username(request.form.get("username", "").strip())
    if user is None or not check_password_hash(user["password_hash"], request.form.get("password", "")):
        flash("Wrong username or password.", "login")
        return redirect(url_for("home", tab="login"))
    sign_in(user)
    return redirect(url_for("results"))


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


def greeting(first_name):
    parts = db.supported_parts()
    what = f"a {' or '.join(parts)} X-ray" if parts else "an X-ray"
    return (f"Hi {first_name}, welcome to MedPeek! Attach {what} with the paperclip or drag it here. "
            f"I'll compare it with normal X-rays and look for breaks. You can also add a note, "
            f"like how you got hurt, or ask me a question.")


def new_chat(user):
    chat_id = db.create_chat(user["id"])
    db.add_message(chat_id, "bot", greeting(user["first_name"]))
    return chat_id


def chat_title(text, filename):
    """Name a chat after its first message."""
    title = " ".join(text.split()) or f"X-ray: {filename}"
    return title if len(title) <= 40 else title[:39].rstrip() + "…"


@app.route("/results")
@login_required
def results():
    """Open the most recent chat, or start the first one."""
    chat_id = db.latest_chat_id(g.user["id"]) or new_chat(g.user)
    return redirect(url_for("chat", chat_id=chat_id))


@app.route("/chat/<int:chat_id>")
@login_required
def chat(chat_id):
    current = db.get_chat(g.user["id"], chat_id)
    if current is None:
        return redirect(url_for("results"))
    return render_template(
        "results.html",
        user=g.user,
        chat=current,
        chats=db.list_chats(g.user["id"]),
        messages=db.get_messages(chat_id),
        image_count=db.image_count(chat_id),
        max_images=MAX_PER_CHAT,
        max_per_send=MAX_PER_SEND,
    )


@app.post("/chat/new")
@login_required
def create_chat():
    return redirect(url_for("chat", chat_id=new_chat(g.user)))


@app.post("/chat/<int:chat_id>/delete")
@login_required
def delete_chat(chat_id):
    db.delete_chat(g.user["id"], chat_id)
    return redirect(url_for("results"))


# ---------- API used by script.js ----------

@app.post("/api/message")
@login_required
def api_message():
    """One request per X-ray (or one for a typed message), so answers show up one by one."""
    name = g.user["first_name"]
    current = db.get_chat(g.user["id"], request.form.get("chat_id", type=int, default=0))
    if current is None:
        return jsonify(error="That chat doesn't exist anymore. Start a new one."), 404
    chat_id = current["id"]
    text = request.form.get("text", "").strip()[:2000]
    file = request.files.get("xray")
    last_id = request.form.get("after", type=int, default=0)
    first_message = db.user_message_count(chat_id) == 0

    if file and file.filename:
        if db.image_count(chat_id) >= MAX_PER_CHAT:
            return jsonify(error=f"This chat has all {MAX_PER_CHAT} uploads. Start a new chat for more."), 400
        data = file.read()
        db.add_message(chat_id, "user", text, image=data, filename=file.filename[:120])
        reply = analyze_xray(data, name, note=text)
    elif text:
        history = db.recent_messages(chat_id)
        db.add_message(chat_id, "user", text)
        reply = chat_reply(name, text, history)
    else:
        return jsonify(error="Type a message or attach an X-ray."), 400

    title = None
    if first_message:
        title = chat_title(text, file.filename if file else "")
        db.set_chat_title(chat_id, title)
    db.add_message(chat_id, "bot", reply["text"], kind=reply["kind"], data=reply["data"])
    return jsonify(
        html=render_template("_messages.html", messages=db.get_messages(chat_id, after_id=last_id)),
        image_count=db.image_count(chat_id),
        title=title,
    )


@app.get("/api/status")
def api_status():
    return jsonify(ollama_client.STATUS)


@app.post("/api/status/retry")
def api_status_retry():
    ollama_client.retry()
    return jsonify(ollama_client.STATUS)


@app.get("/image/<int:message_id>")
@login_required
def message_image(message_id):
    data = db.message_image(g.user["id"], message_id)
    if data is None:
        abort(404)
    mimetype = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return Response(data, mimetype=mimetype, headers={"Cache-Control": "private, max-age=86400"})


@app.errorhandler(400)
def bad_request(error):
    if request.path.startswith("/api/"):
        return jsonify(error=error.description), 400
    return error


@app.errorhandler(413)
def too_large(_):
    return jsonify(error="That file is over 15 MB. Please upload a smaller X-ray."), 413


if __name__ == "__main__":
    # Port 5000 is often taken (another Flask app, or AirPlay Receiver on a Mac), so MedPeek uses 5050.
    # Pick another one with:  PORT=5060 python app.py
    port = int(os.environ.get("PORT", 5050))
    print(f"MedPeek is running at http://127.0.0.1:{port}")
    # use_reloader=False so Ollama is only started once
    app.run(host="127.0.0.1", port=port, debug=True, use_reloader=False)