"""Independent, loopback-only backend checker: python diagnostics.py (port 5001)."""
import secrets
import struct
import threading
import time
import zlib
import requests
from flask import Flask, jsonify, render_template, request
from services.backend_client import api_url, HEADERS

app = Flask(__name__)
TOKEN = secrets.token_urlsafe(32)
lock = threading.Lock()
active_job = None

def remote(method, path, **kwargs):
    r = requests.request(method, api_url()+path, headers=HEADERS, timeout=(10,30), **kwargs)
    if r.status_code in (401,403):
        raise ValueError('GitHub access denied. Check forwarded port 8000 permissions.')
    r.raise_for_status()
    if 'application/json' not in r.headers.get('Content-Type',''):
        raise ValueError('Backend returned a webpage instead of JSON; check Codespaces access.')
    return r.json()

@app.before_request
def guard():
    if request.host.split(':')[0] not in ('127.0.0.1','localhost'):
        return jsonify(error='Local access only.'),403
    if request.path.startswith('/check/') and not secrets.compare_digest(request.headers.get('X-Check-Token',''),TOKEN):
        return jsonify(error='Refresh this checker page.'),403

@app.after_request
def headers(response):
    response.headers['Cache-Control']='no-store'
    response.headers['X-Frame-Options']='DENY'
    return response

@app.get('/')
def index():
    return render_template('diagnostics.html',token=TOKEN,backend=api_url())

@app.get('/check/health')
def health():
    start=time.monotonic()
    try:
        data=remote('GET','/api/health')
        return jsonify(health=data,latency_ms=round((time.monotonic()-start)*1000))
    except (requests.RequestException,ValueError) as exc:
        return jsonify(error=str(exc)),502

def test_image():
    def chunk(kind,data):
        return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
    # A plain gray synthetic image, never a patient's scan.
    pixels=(b'\x00'+b'\x88\x88\x88'*128)*128
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',128,128,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(pixels))+chunk(b'IEND',b'')

@app.post('/check/test')
def test():
    global active_job
    with lock:
        if active_job:
            return jsonify(job_id=active_job,reused=True)
        try:
            result=remote('POST','/api/analyze',files={'image':('synthetic-check.png',test_image(),'image/png')},data={'body_part':'whole_body','modality':'xray','question':'Connectivity test: this is a plain gray synthetic image, not a medical scan. Identify it as non-medical.'})
            active_job=result['job_id']
            return jsonify(job_id=active_job),202
        except (requests.RequestException,ValueError,KeyError) as exc:
            return jsonify(error=str(exc)),502

@app.get('/check/job')
def job():
    global active_job
    with lock:
        if not active_job:
            return jsonify(error='No test is running.'),404
        try:
            data=remote('GET','/api/jobs/'+active_job)
            if data.get('status') in ('done','error'):
                active_job=None
            return jsonify(data)
        except (requests.RequestException,ValueError) as exc:
            return jsonify(error=str(exc)),502

if __name__=='__main__':
    app.run(host='127.0.0.1',port=5001,debug=False)
