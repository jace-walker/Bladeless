"""Local demo only. Run: python3 server.py (no dependencies)."""
import base64
from urllib.parse import urlsplit
from demo_service import DemoStore
from vision_service import FaceEngine
import os
import secrets
from http.cookies import SimpleCookie
import argparse
import threading
import time
import json
import sqlite3
from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

ROOT = Path(__file__).parent
DB = ROOT / 'attendance.sqlite3'
EVENTS = [dict(id='intro-ai', name='Intro to Artificial Intelligence', date='Oct 13, 2026, 5:00 PM', location='HEC 101', points=10, audience='public', roleIds=[]), dict(id='intro-github', name='Intro to GitHub and GitHub Etiquette (SASE X KH)', date='Oct 20, 2026, 5:00 PM', location='HEC 101', points=10, audience='dues', roleIds=[])]
MEMBERS = [dict(id='stuart', name='Stuart Palmer', face_name='Stuart', paid=True, roleIds=[]), dict(id='jace', name='Jace', face_name='Jace', paid=False, roleIds=[])]

STORE = None
ENGINE = None
SECURE = False
ADMIN_KEY = ''
PHONE_KEY = ''
FRAME_LOCK = threading.Lock()
FRAME = b''
FRAME_AT = 0
DISPLAY_LOCK = threading.Lock()
LATEST = {'status': 'idle', 'updatedAt': 0}

def current_display(private=False):
    with DISPLAY_LOCK:
        value = dict(LATEST)
    if time.time() - value['updatedAt'] > 8:
        return dict(status='idle')
    if private:
        return value
    return dict(status=value['status'])

def publish_result(data):
    with DISPLAY_LOCK:
        LATEST.clear()
        LATEST.update(data, updatedAt=time.time())

def connect():
    return sqlite3.connect(DB, timeout=10)

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / 'dist'), **kwargs)

    def role(self):
        if not SECURE:
            return 'admin'
        supplied = self.headers.get('Authorization', '').removeprefix('Bearer ')
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
            supplied = supplied or (cookie['blade_key'].value if 'blade_key' in cookie else '')
        except Exception:
            return None
        if secrets.compare_digest(supplied, ADMIN_KEY):
            return 'admin'
        if secrets.compare_digest(supplied, PHONE_KEY):
            return 'phone'
        return None

    def authorized(self):
        role = self.role()
        if role == 'admin':
            return True
        if role == 'phone' and self.path in ('/guest.html', '/api/display', '/api/camera'):
            return True
        self.reply(403, dict(error='Pair this device first' if not role else 'Admin access required'))
        return False

    def reply(self, status, data):
        if self.path == '/api/checkins' and 'status' in data:
            publish_result(data)
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.path = urlsplit(self.path).path
        if self.path == '/pair.html':
            return super().do_GET()
        if SECURE and not self.role() and not self.path.startswith('/api/'):
            self.send_response(302)
            self.send_header('Location', '/pair.html')
            self.end_headers()
            return
        if not self.authorized():
            return
        if self.path == '/api/camera/frame':
            with FRAME_LOCK:
                body = FRAME if time.monotonic() - FRAME_AT < 3 else b''
            self.send_response(200 if body else 204)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == '/api/state':
            data=STORE.state()
            data.update(latestMatch=current_display(private=True),vision=dict(status=ENGINE.status,error=ENGINE.error))
            self.reply(200,data)
        elif self.path == '/api/display':
            self.reply(200, current_display())
        elif self.path.startswith('/api/'):
            self.reply(404, dict(error='Endpoint not found'))
        else:
            super().do_GET()

    def do_POST(self):
        self.path = urlsplit(self.path).path
        global FRAME, FRAME_AT
        origin = self.headers.get('Origin')
        if origin and origin not in ('http://' + self.headers.get('Host', ''), 'https://' + self.headers.get('Host', '')):
            return self.reply(403, dict(error='Cross-origin requests are not allowed'))
        if self.path == '/api/pair':
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 4096:
                    raise ValueError()
                payload = json.loads(self.rfile.read(size))
                key = payload.get('key', '')
                if not isinstance(key, str):
                    raise ValueError()
            except (ValueError, AttributeError):
                return self.reply(400, dict(error='Invalid request'))
            role = 'admin' if secrets.compare_digest(key, ADMIN_KEY) else 'phone' if secrets.compare_digest(key, PHONE_KEY) else None
            if not SECURE or not role:
                return self.reply(403, dict(error='Invalid pairing key'))
            self.send_response(200)
            secure_cookie = '; Secure' if self.headers.get('X-Forwarded-Proto') == 'https' else ''
            self.send_header('Set-Cookie', f'blade_key={key}; HttpOnly; SameSite=Strict; Path=/' + secure_cookie)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            body = json.dumps(dict(target='/?admin=1' if role == 'admin' else '/guest.html')).encode()
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.authorized():
            return
        if self.path == '/api/camera':
            if self.headers.get('Content-Type') != 'image/jpeg':
                return self.reply(400, dict(error='Expected JPEG'))
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 250000:
                    raise ValueError()
            except ValueError:
                return self.reply(413, dict(error='Frame too large'))
            body = self.rfile.read(size)
            if len(body) != size or not body.startswith(b'\xff\xd8') or not body.endswith(b'\xff\xd9'):
                return self.reply(400, dict(error='Invalid JPEG'))
            with FRAME_LOCK:
                FRAME, FRAME_AT = body, time.monotonic()
            return self.reply(200, dict(status='received'))
        if self.path not in ('/api/checkins', '/api/recognition', '/api/members/add', '/api/members/consent', '/api/members/dues', '/api/faces/delete', '/api/enroll', '/api/settings', '/api/reset'):
            return self.reply(404, dict(error='Endpoint not found'))
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= (350000 if self.path == '/api/enroll' else 4096):
                return self.reply(400, dict(error='Invalid request size'))
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError()
        except (ValueError, json.JSONDecodeError):
            return self.reply(400, dict(error='Invalid JSON'))
        if self.path == '/api/recognition':
            if payload not in ({'status': 'unknown'}, {'status': 'idle'}):
                return self.reply(400, dict(error='Expected unknown or idle status'))
            publish_result(payload)
            return self.reply(200, payload)
        try:
            if self.path == '/api/members/add':
                result=STORE.add_member(payload.get('name'))
            elif self.path == '/api/members/consent':
                result=STORE.consent(payload.get('memberId'),payload.get('consent'))
            elif self.path == '/api/members/dues':
                result=STORE.paid(payload.get('memberId'),payload.get('paid'))
            elif self.path == '/api/faces/delete':
                result=STORE.delete_face(payload.get('memberId'));publish_result(dict(status='idle'))
            elif self.path == '/api/settings':
                result=STORE.update_settings(payload)
                if not result['running']:publish_result(dict(status='idle'))
            elif self.path == '/api/reset':
                if payload != {'confirm': True}:raise ValueError('Reset confirmation required')
                result=STORE.reset_attendance();publish_result(dict(status='idle'))
            elif self.path == '/api/enroll':
                row=STORE.member_row(payload.get('memberId'))
                if not row['consent']:raise ValueError('Consent is required before capturing')
                raw=base64.b64decode(payload.get('image',''),validate=True)
                if not 0<len(raw)<=250000 or not raw.startswith(b'\xff\xd8'):raise ValueError('Invalid image')
                embedding=ENGINE.embed(raw)
                if embedding is None:raise ValueError('No face found. Face the camera and try again.')
                result=STORE.enroll(row['id'],embedding,row['epoch'])
            else:
                if set(payload) != {'memberId','eventId'}:raise ValueError('Expected memberId and eventId')
                result=STORE.checkin(payload['memberId'],payload['eventId'])
            return self.reply(200,result)
        except (ValueError,TypeError) as e:
            return self.reply(400,dict(error=str(e)))
        except Exception as e:
            return self.reply(503,dict(error='Face service unavailable: '+str(e)[:200]))


if __name__ == '__main__':
    with connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS attendance(member_id TEXT NOT NULL,event_id TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), UNIQUE(member_id,event_id))")
    STORE=DemoStore(ROOT,EVENTS)
    ENGINE=FaceEngine(STORE)
    def get_frame():
        with FRAME_LOCK:
            return FRAME,FRAME_AT,time.monotonic()-FRAME_AT
    # Settings are operator-controlled; never restart automatic recognition silently.
    STORE.update_settings({'running': False})
    threading.Thread(target=ENGINE.run,args=(get_frame,publish_result),daemon=True).start()
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--secure', action='store_true', help='Require separate admin and phone pairing keys')
    args = parser.parse_args()
    SECURE = args.secure
    if SECURE:
        ADMIN_KEY = secrets.token_urlsafe(8)
        PHONE_KEY = secrets.token_urlsafe(8)
        print(f'Admin pairing key: {ADMIN_KEY}')
        print(f'Phone pairing key: {PHONE_KEY}')
        print('Keys change each time the server restarts. Keep the admin key on your computer.')
    print(f'Admin: http://127.0.0.1:{args.port}/?admin=1')
    print(f'Attendee: http://127.0.0.1:{args.port}/guest.html')
    if args.host == '0.0.0.0' and not SECURE:
        print('LAN demo enabled. On a trusted network only: this prototype has no authentication.')
    ThreadingHTTPServer((args.host,args.port), Handler).serve_forever()
