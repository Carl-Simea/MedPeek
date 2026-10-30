import os
import secrets
import requests
from flask import Flask, render_template, request, session, jsonify
from services import history
import threading
from services.auth import init_auth
from services.backend_client import get_options, start_analysis, poll_analysis

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or secrets.token_hex(32)
app.config.update(MAX_CONTENT_LENGTH=26 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')

init_auth(app)
history.init_history(app)
threading.Thread(target=history.collect,args=(app,),daemon=True).start()

@app.get('/')
def home():
    return render_template('home.html')

@app.get('/results')
def results():
    options, error = get_options()
    return render_template('results.html', options=options, error_message=error, image_count=session.get('image_count', 0))

@app.get('/api/chats')
def chats():
    return jsonify(chats=history.listing())

@app.post('/api/chats')
def new_chat():
    return jsonify(id=history.create()),201

@app.get('/api/chats/<chat_id>')
def chat(chat_id):
    return jsonify(history.read(chat_id))

@app.post('/api/submit')
def submit():
    chat_id=request.form.get('chat_id','')
    file=request.files.get('xray_image')
    text=request.form.get('user_message','').strip()[:500]
    if not text and not (file and file.filename):
        return jsonify(error='Write a message or attach an image.'),400
    if file and not request.form.get('body_part'):
        return jsonify(error='Choose the body area.'),400
    message_id=history.begin(chat_id,text,file.filename if file else None)
    if not file:
        return jsonify(chat_id=chat_id),200
    try:
        data=start_analysis(file,request.form['body_part'],request.form.get('modality','xray'),text)
        history.update(message_id,'queued','Received by the backend',job_id=data['job_id'])
        return jsonify(chat_id=chat_id,job_id=data['job_id'],status=data['status'],image_count=history.read(chat_id)['image_count']),202
    except requests.Timeout:
        error='Upload confirmation timed out. The backend may have received it; check before resending.'
    except (requests.RequestException,ValueError,KeyError):
        error='The backend could not confirm this upload. Check the connection and file format.'
    history.update(message_id,'error',error)
    return jsonify(error=error),502

@app.get('/api/jobs/<job_id>')
def job(job_id):
    if job_id not in session.get('jobs', []):
        return jsonify(error='This job does not belong to this chat.'), 404
    try:
        return jsonify(poll_analysis(job_id))
    except (requests.RequestException, ValueError, KeyError):
        return jsonify(error='Unable to check the analysis right now.'), 502

@app.post('/clear')
def clear():
    session.pop('jobs', None)
    session.pop('image_count', None)
    return jsonify(ok=True)

@app.errorhandler(413)
def too_large(error):
    return jsonify(error='Please choose an image smaller than 25 MB.'), 413

if __name__ == '__main__':
    app.run(port=5000, debug=False)
