"""Demo members, settings, attendance, consent, and embedding storage."""
import json
import math
import sqlite3
import threading
import time
import uuid
from pathlib import Path

DEFAULT_SETTINGS = dict(eventId='intro-ai', threshold=0.30, requireLiveness=False, running=False)
SEED = [('stuart','Stuart Palmer','Stuart','KH-0003',1,6),('jace','Jace','Jace','KH-0001',1,3),('aaron','Aaron','Aaron','KH-0002',0,1),('jack','Jack','Jack','KH-6659',0,7)]

class DemoStore:
    def __init__(self, root, events):
        self.root=Path(root);self.db=self.root/'attendance.sqlite3';self.faces=self.root/'faces.json';self.events=events;self.lock=threading.RLock()
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS attendance(member_id TEXT NOT NULL,event_id TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), UNIQUE(member_id,event_id))")
            db.execute('CREATE TABLE IF NOT EXISTS demo_members(id TEXT PRIMARY KEY,name TEXT UNIQUE COLLATE NOCASE,face_name TEXT UNIQUE,display_id TEXT UNIQUE,paid INTEGER,base_events INTEGER,consent INTEGER DEFAULT 0,epoch INTEGER DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS demo_settings(id INTEGER PRIMARY KEY,value TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS demo_log(id INTEGER PRIMARY KEY AUTOINCREMENT,value TEXT,created_at REAL)')
            for row in SEED:db.execute('INSERT OR IGNORE INTO demo_members(id,name,face_name,display_id,paid,base_events) VALUES (?,?,?,?,?,?)',row)
            db.execute('INSERT OR IGNORE INTO demo_settings(id,value) VALUES (1,?)',(json.dumps(DEFAULT_SETTINGS),))
        self.last_log={}
    def connect(self):
        db=sqlite3.connect(self.db,timeout=10);db.row_factory=sqlite3.Row;return db
    def face_db(self):
        if not self.faces.exists():return {}
        value=json.loads(self.faces.read_text())
        if not isinstance(value,dict):raise ValueError('faces.json must contain a name-to-embeddings object')
        return value
    def write_faces(self, value):
        path=self.faces.with_suffix('.json.tmp');path.write_text(json.dumps(value));path.replace(self.faces)
    def members(self):
        with self.lock,self.connect() as db:
            faces=self.face_db();rows=db.execute('SELECT * FROM demo_members ORDER BY rowid').fetchall()
            result=[]
            for r in rows:
                n=len(faces.get(r['face_name'],[]));count=db.execute('SELECT count(*) FROM attendance WHERE member_id=?',(r['id'],)).fetchone()[0]
                result.append(dict(id=r['id'],name=r['name'],face_name=r['face_name'],displayId=r['display_id'],paid=bool(r['paid']),consent=bool(r['consent']),samples=n,enrolled=bool(r['consent']) and n>=5,eventsAttended=r['base_events']+count,roleIds=[],epoch=r['epoch']))
            return result
    def settings(self):
        with self.connect() as db:return json.loads(db.execute('SELECT value FROM demo_settings WHERE id=1').fetchone()[0])
    def update_settings(self,p):
        with self.lock:
            if set(p)-set(DEFAULT_SETTINGS):raise ValueError('Unknown setting')
            s=self.settings();s.update(p)
            if s['eventId'] not in {e['id'] for e in self.events}:raise ValueError('Unknown event')
            if isinstance(s['threshold'],bool) or not isinstance(s['threshold'],(int,float)) or not .1<=s['threshold']<=.6:raise ValueError('Threshold must be between 0.10 and 0.60')
            if any(type(s[k]) is not bool for k in ('running','requireLiveness')):raise ValueError('Invalid settings')
            with self.connect() as db:db.execute('UPDATE demo_settings SET value=? WHERE id=1',(json.dumps(s),))
            return s
    def member_row(self,ident):
        with self.connect() as db:r=db.execute('SELECT * FROM demo_members WHERE id=? OR display_id=?',(ident,ident)).fetchone()
        if not r:raise ValueError('Member not found')
        return dict(r)
    def add_member(self,name):
        if not isinstance(name,str) or not 1<=len(name.strip())<=80:raise ValueError('Enter a name of 1–80 characters')
        name=name.strip()
        with self.lock,self.connect() as db:
            count=db.execute('SELECT count(*) FROM demo_members').fetchone()[0]
            if count>=200:raise ValueError('Demo member limit reached')
            code='KH-'+uuid.uuid4().hex[:6].upper()
            try:db.execute('INSERT INTO demo_members(id,name,face_name,display_id,paid,base_events) VALUES (?,?,?,?,0,0)',(str(uuid.uuid4()),name,name,code))
            except sqlite3.IntegrityError:raise ValueError('That name already exists')
        return next(m for m in self.members() if m['name']==name)
    def consent(self,ident,value):
        if type(value) is not bool:raise ValueError('Consent must be true or false')
        row=self.member_row(ident)
        with self.lock,self.connect() as db:db.execute('UPDATE demo_members SET consent=?,epoch=epoch+1 WHERE id=?',(int(value),row['id']))
        return dict(status='saved')
    def paid(self,ident,value):
        if type(value) is not bool:raise ValueError('Paid must be true or false')
        row=self.member_row(ident)
        with self.connect() as db:db.execute('UPDATE demo_members SET paid=? WHERE id=?',(int(value),row['id']))
        return dict(status='saved')
    def delete_face(self,ident):
        row=self.member_row(ident)
        with self.lock,self.connect() as db:
            faces=self.face_db();faces.pop(row['face_name'],None);self.write_faces(faces)
            db.execute('UPDATE demo_members SET consent=0,epoch=epoch+1 WHERE id=?',(row['id'],))
        return dict(status='deleted')
    def enroll(self,ident,embedding,epoch):
        if len(embedding)!=512 or not all(math.isfinite(float(v)) for v in embedding):raise ValueError('Invalid embedding')
        with self.lock:
            row=self.member_row(ident)
            if not row['consent'] or row['epoch']!=epoch:raise ValueError('Consent changed; capture again')
            faces=self.face_db();vectors=faces.setdefault(row['face_name'],[])
            if len(vectors)>=20:raise ValueError('20-sample limit reached; delete face data to re-enroll')
            vectors.append([float(v) for v in embedding]);self.write_faces(faces)
            return dict(status='captured',samples=len(vectors),enrolled=len(vectors)>=5)
    def record(self,data):
        if data.get('status')=='idle':return
        key=(data.get('status'),data.get('member',{}).get('id'),data.get('eventId'))
        now=time.time()
        if now-self.last_log.get(key,0)<10:return
        self.last_log[key]=now
        with self.connect() as db:
            db.execute('INSERT INTO demo_log(value,created_at) VALUES (?,?)',(json.dumps(data),now));db.execute('DELETE FROM demo_log WHERE id NOT IN (SELECT id FROM demo_log ORDER BY id DESC LIMIT 100)')
    def checkin(self,ident,event_id,method='manual',distance=None,epoch=None):
        event=next((e for e in self.events if e['id']==event_id),None)
        if not event:raise ValueError('Event not found')
        try:member=self.member_row(ident)
        except ValueError:return dict(status='member_not_found',method=method,eventId=event_id)
        summary=dict(id=member['id'],name=member['name'],displayId=member['display_id'],paid=bool(member['paid']))
        data=dict(member=summary,method=method,eventId=event_id)
        if distance is not None:data['distance']=round(distance,3)
        with self.lock,self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current=dict(db.execute('SELECT * FROM demo_members WHERE id=?',(member['id'],)).fetchone())
            if method=='face' and (not self.settings()['running'] or self.settings()['eventId']!=event_id):data['status']='idle'
            elif method=='face' and (not current['consent'] or (epoch is not None and current['epoch']!=epoch)):data['status']='not_enrolled'
            elif db.execute('SELECT 1 FROM attendance WHERE member_id=? AND event_id=?',(member['id'],event_id)).fetchone():data['status']='already_checked_in'
            elif event['audience']=='dues' and not current['paid']:data['status']='dues_required'
            elif event['audience']=='roles':data['status']='role_required'
            else:
                db.execute('INSERT INTO attendance(member_id,event_id) VALUES (?,?)',(member['id'],event_id));data.update(status='checked_in',pointsAwarded=event['points'],additionalAttendance=False)
        self.record(data);return data
    def reset_attendance(self):
        with self.lock:
            self.update_settings({'running':False})
            with self.connect() as db:db.execute('DELETE FROM attendance');db.execute('DELETE FROM demo_log')
            self.last_log.clear()
        return dict(status='reset')
    def state(self):
        with self.connect() as db:
            rows=db.execute('SELECT member_id,event_id,created_at FROM attendance ORDER BY created_at DESC').fetchall()
            logs=[dict(json.loads(r['value']),createdAt=r['created_at']) for r in db.execute('SELECT value,created_at FROM demo_log ORDER BY id DESC LIMIT 50')]
        return dict(members=self.members(),events=self.events,attendance=[dict(r) for r in rows],recent=logs,settings=self.settings())
