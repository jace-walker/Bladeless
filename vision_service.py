"""Lazy, serialized DeepFace inference. Frames never enter persistent storage."""
import threading
import time
import inspect
import math

class FaceEngine:
    def __init__(self,store):
        self.store=store;self.lock=threading.Lock();self.status='Not loaded';self.error=None;self.last_seq=-1
    def embed(self,jpeg,anti_spoofing=False):
        with self.lock:
            self.status='Loading / processing face model'
            try:
                import cv2
                import numpy as np
                from deepface import DeepFace
                frame=cv2.imdecode(np.frombuffer(jpeg,dtype=np.uint8),cv2.IMREAD_COLOR)
                if frame is None or max(frame.shape[:2])>1024:raise ValueError('Invalid or oversized image')
                if anti_spoofing and 'anti_spoofing' not in inspect.signature(DeepFace.represent).parameters:raise RuntimeError('Installed DeepFace does not support anti-spoofing. Update DeepFace to use this option.')
                opts=dict(model_name='Facenet512',detector_backend='opencv',max_faces=1,enforce_detection=True)
                if anti_spoofing:opts['anti_spoofing']=True
                faces=DeepFace.represent(frame,**opts);embedding=faces[0]['embedding']
                self.status='Ready';self.error=None
                return embedding
            except Exception as e:
                if type(e).__name__=='FaceNotDetected' or 'face could not be detected' in str(e).lower():
                    self.status='Ready';self.error=None;return None
                if type(e).__name__=='SpoofDetected' or 'spoof detected' in str(e).lower():
                    self.status='Ready';self.error=None;raise ValueError('Liveness check failed')
                self.status='Error';self.error=str(e)[:240];raise
    def recognize(self,jpeg,settings):
        embedding=self.embed(jpeg,settings['requireLiveness'])
        if embedding is None:return dict(status='idle')
        import numpy as np
        a=np.asarray(embedding,dtype=np.float32);norm=np.linalg.norm(a)
        if not np.isfinite(norm) or norm==0:raise ValueError('Invalid face embedding')
        best=None;distance=float('inf')
        with self.store.lock:
            faces=self.store.face_db()
            for m in self.store.members():
                if not m['enrolled']:continue
                for vector in faces.get(m['face_name'],[]):
                    b=np.asarray(vector,dtype=np.float32)
                    if b.shape!=(512,) or not np.all(np.isfinite(b)) or np.linalg.norm(b)==0:continue
                    d=float(1-np.dot(a,b)/(norm*np.linalg.norm(b)))
                    if d<distance:distance=d;best=m
        if not best or distance>settings['threshold']:return dict(status='unknown',method='face')
        # Recheck enrollment and the latest threshold before committing attendance.
        latest=self.store.settings()
        if not latest['running'] or latest['eventId']!=settings['eventId'] or latest['requireLiveness']!=settings['requireLiveness']:return dict(status='idle')
        if distance>latest['threshold']:return dict(status='unknown',method='face')
        return self.store.checkin(best['id'],latest['eventId'],'face',distance,best['epoch'])
    def run(self,get_frame,publish):
        while True:
            time.sleep(.25)
            settings=self.store.settings()
            if not settings['running']:continue
            jpeg,seq,age=get_frame()
            if age>3 or not jpeg:
                publish(dict(status='idle'));continue
            if seq==self.last_seq:continue
            self.last_seq=seq
            try:result=self.recognize(jpeg,settings)
            except ValueError as e:result=dict(status='liveness_failed' if str(e)=='Liveness check failed' else 'camera_error',method='face',message=str(e)[:200])
            except Exception as e:result=dict(status='camera_error',method='face',message=str(e)[:200])
            if self.store.settings()['running']:
                if time.monotonic()-seq>8:continue
                publish(result);self.store.record(result)
