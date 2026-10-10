"""Import into face_check.py; sends member IDs, never images or embeddings."""
import os
import json
import time
from urllib.request import Request, urlopen
from urllib.error import URLError

FACE_TO_MEMBER = {'Stuart': 'stuart', 'Jace': 'jace'}
EVENT_ID = 'intro-ai'
API_URL = 'http://127.0.0.1:8000/api/checkins'
_retry_after = {}

def record_match(name):
    member_id = FACE_TO_MEMBER.get(name)
    key = (member_id, EVENT_ID)
    if not member_id:
        print(f'No demo member linked to {name!r}; edit FACE_TO_MEMBER.')
        return
    if time.monotonic() < _retry_after.get(key, 0):
        return
    body = json.dumps(dict(memberId=member_id, eventId=EVENT_ID)).encode()
    try:
        request = Request(API_URL, data=body, headers={'Content-Type':'application/json', 'Authorization':'Bearer ' + os.environ.get('BLADE_ADMIN_KEY', '')}, method='POST')
        with urlopen(request, timeout=2) as response:
            result = json.load(response)
        if result.get('status') in ('checked_in', 'already_checked_in'):
            _retry_after[key] = time.monotonic() + 2
            print(f"Check-in: {result['member']['name']} — {result['status']}")
        else:
            _retry_after[key] = time.monotonic() + 5
            print(f"Check-in rejected: {result.get('status', 'unknown')}")
    except (URLError, TimeoutError, ValueError, OSError) as error:
        _retry_after[key] = time.monotonic() + 5
        print(f'Check-in server unavailable: {error}. Retrying in 5 seconds.')


def record_unknown():
    """Call when a detected face fails the match threshold."""
    _record_status('unknown')

def record_no_face():
    """Call when embed(frame) returns no face; clears the attendee screen."""
    _record_status('idle')

def _record_status(status):
    key = ('display', status)
    if time.monotonic() < _retry_after.get(key, 0):
        return
    _retry_after[key] = time.monotonic() + 2
    try:
        request = Request(API_URL.rsplit('/', 1)[0] + '/recognition',
                          data=json.dumps({'status': status}).encode(),
                          headers={'Content-Type': 'application/json', 'Authorization':'Bearer ' + os.environ.get('BLADE_ADMIN_KEY', '')}, method='POST')
        with urlopen(request, timeout=2) as response:
            response.read()
    except (URLError, TimeoutError, OSError) as error:
        _retry_after[key] = time.monotonic() + 5
        print(f'Display server unavailable: {error}')
