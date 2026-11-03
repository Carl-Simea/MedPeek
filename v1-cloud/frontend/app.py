"""MedPeek web app: accounts, conversations and the patient-facing UI for the MedPeek backend.

Run:  python app.py              (http://127.0.0.1:5000)
Env:  XRAY_BACKEND_URL           backend base URL (default: the team's Codespace)
      PORT                       web port (default 5000)
      MEDPEEK_HTTPS=1            when served over HTTPS (secure cookies)
"""
import os
import re
import secrets
import threading

import requests
from flask import Flask, Response, abort, g, jsonify, redirect, render_template, request, session, url_for

from services import history
from services.auth import init_auth
from services.backend_client import (HEADERS, api_url, chat_turn, get_options, health, nearby_care, poll_analysis,
                                     start_analysis, zip_lookup)

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or secrets.token_hex(32)
app.config.update(MAX_CONTENT_LENGTH=26 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')

init_auth(app)
history.init_history(app)
threading.Thread(target=history.collect, args=(app,), daemon=True).start()

ZIP_RE = re.compile(r'^\d{5}$')


def clean_zip(value):
    digits = re.sub(r'\D', '', str(value or ''))[:5]
    return digits if ZIP_RE.match(digits) else ''


# ---------------------------------------------------------------- pages
@app.get('/')
def home():
    return render_template('home.html', backend=health())


@app.get('/chat')
def chat_page():
    options, error = get_options()
    return render_template('chat.html', options=options, error_message=error)


@app.get('/results')
def results():
    return redirect(url_for('chat_page', **request.args))


# ---------------------------------------------------------------- conversations
@app.get('/api/chats')
def chats():
    return jsonify(chats=history.listing())


@app.post('/api/chats')
def new_chat():
    return jsonify(id=history.create()), 201


@app.get('/api/chats/<chat_id>')
def chat(chat_id):
    return jsonify(history.read(chat_id))


@app.delete('/api/chats/<chat_id>')
def delete_chat(chat_id):
    history.delete(chat_id)
    return jsonify(ok=True)


@app.post('/api/chats/<chat_id>/zip')
def set_zip(chat_id):
    """Save the patient's ZIP code and look up nearby care for the latest report (or general care)."""
    data = request.get_json(silent=True) or {}
    zip_code = clean_zip(data.get('zip'))
    if not zip_code:
        return jsonify(error='Please enter a 5-digit US ZIP code.'), 400
    report = history.latest_report(chat_id)
    specialist = (report or {}).get('referral', {}).get('specialist', '') if report else ''
    urgency = (report or {}).get('urgency', 'soon') if report else 'soon'
    try:
        place = zip_lookup(zip_code)
        care = nearby_care(zip_code, specialist, urgency)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except requests.RequestException:
        return jsonify(error='The care finder is not reachable right now. Please try again in a moment.'), 502
    history.set_zip(chat_id, zip_code)
    result = {'type': 'care', **care}
    text = f"Nearest places that can help near {place['city']}, {place['state']} {zip_code}."
    history.add_bot(chat_id, text, result)
    return jsonify(chat_id=chat_id, zip=zip_code, city=place['city'], state=place['state'], care=result)


@app.post('/api/submit')
def submit():
    """One turn: a text message (answered by the MedPeek chat) and/or an image (sent for analysis)."""
    chat_id = request.form.get('chat_id', '')
    file = request.files.get('xray_image')
    text = request.form.get('user_message', '').strip()[:1000]
    if not text and not (file and file.filename):
        return jsonify(error='Write a message or attach an image.'), 400

    if file and file.filename:
        message_id = history.begin(chat_id, text, file.filename)
        try:
            data = start_analysis(file, request.form.get('body_part', 'auto'), request.form.get('modality', 'xray'), text)
            history.update(message_id, 'queued', 'Received by MedPeek', job_id=data['job_id'])
            jobs = session.get('jobs', [])
            session['jobs'] = (jobs + [data['job_id']])[-50:]
            return jsonify(chat_id=chat_id, job_id=data['job_id'], status=data['status'],
                           image_count=history.read(chat_id)['image_count']), 202
        except ValueError as exc:
            error = str(exc)
        except requests.Timeout:
            error = 'Upload confirmation timed out. The backend may have received it; check before resending.'
        except (requests.RequestException, KeyError):
            error = 'MedPeek could not accept this upload. Check the connection and file format.'
        history.update(message_id, 'error', error)
        return jsonify(error=error), 502

    # Text only: ask the MedPeek chat, with this conversation's context.
    message_id = history.begin(chat_id, text, None, pending_text='Thinking…')
    chat_row = history.read(chat_id)
    zip_code = clean_zip(chat_row.get('zip')) or clean_zip(request.form.get('zip'))
    messages = history.transcript(chat_id)
    if not messages or messages[-1]['role'] != 'user' or messages[-1]['content'] != text[:4000]:
        messages.append({'role': 'user', 'content': text})
    try:
        reply = chat_turn(messages, zip_code, history.latest_report(chat_id))
    except requests.Timeout:
        history.update(message_id, 'error', 'MedPeek took too long to answer. Please try again.')
        return jsonify(error='MedPeek took too long to answer. Please try again.'), 504
    except (requests.RequestException, ValueError):
        history.update(message_id, 'error', 'MedPeek could not answer right now. Please try again in a moment.')
        return jsonify(error='MedPeek could not answer right now. Please try again in a moment.'), 502
    if reply.get('zip') and not zip_code:
        history.set_zip(chat_id, clean_zip(reply['zip']))
    result = {'type': 'chat', **reply}
    history.update(message_id, 'done', reply.get('reply', ''), result=result)
    return jsonify(chat_id=chat_id, message_id=message_id, result=result), 200


@app.get('/api/jobs/<job_id>')
def job(job_id):
    if job_id not in session.get('jobs', []):
        return jsonify(error='This job does not belong to this chat.'), 404
    try:
        return jsonify(poll_analysis(job_id))
    except (requests.RequestException, ValueError, KeyError):
        return jsonify(error='Unable to check the analysis right now.'), 502


@app.post('/api/report-pdf')
def report_pdf():
    """Ask the MedPeek backend to turn a finished report into a PDF the patient can download."""
    result = request.get_json(silent=True)
    if not isinstance(result, dict) or not result.get('summary'):
        return jsonify(error='There is no finished report to save yet.'), 400
    try:
        response = requests.post(api_url() + '/api/report/pdf', json=result, headers=HEADERS, timeout=(10, 60))
        response.raise_for_status()
    except requests.Timeout:
        return jsonify(error='The PDF service took too long. Please try downloading again.'), 504
    except requests.RequestException:
        return jsonify(error='The PDF service is unavailable. Please try again shortly.'), 502
    if 'application/pdf' not in response.headers.get('Content-Type', '').lower() or not response.content.startswith(b'%PDF-'):
        return jsonify(error='The backend returned an invalid PDF. Please check the backend connection.'), 502
    report_id = re.sub(r'[^A-Za-z0-9_-]', '', str(result.get('id', 'report')))[:40] or 'report'
    return Response(response.content, mimetype='application/pdf',
                    headers={'Content-Disposition': f'attachment; filename="MedPeek-report-{report_id}.pdf"'})


@app.post('/api/report-issue')
def report_issue():
    """Send a 'report a mistake, bug or issue' message to the MedPeek backend."""
    data = request.get_json(silent=True) or {}
    message = str(data.get('message', '')).strip()
    if len(message) < 3:
        return jsonify(error='Please tell us a little more.'), 400
    payload = {'kind': str(data.get('kind', 'issue'))[:20], 'message': message[:2000],
               'report_id': str(data.get('report_id', ''))[:64], 'contact': str(data.get('contact', ''))[:200],
               'page': 'medpeek-app'}
    try:
        response = requests.post(api_url() + '/api/feedback', json=payload, headers=HEADERS, timeout=(10, 20))
        response.raise_for_status()
        return jsonify(response.json()), 201
    except (requests.RequestException, ValueError):
        return jsonify(error='Your report could not be sent right now. Please try again.'), 502


@app.post('/clear')
def clear():
    session.pop('jobs', None)
    return jsonify(ok=True)


@app.errorhandler(413)
def too_large(error):
    return jsonify(error='Please choose an image smaller than 25 MB.'), 413


@app.errorhandler(404)
def not_found(error):
    if request.path.startswith('/api/'):
        return jsonify(error='Not found.'), 404
    return render_template('home.html', backend=None, not_found=True), 404


if __name__ == '__main__':
    app.run(host=os.environ.get('HOST', '127.0.0.1'), port=int(os.environ.get('PORT', '5000')), debug=False)
