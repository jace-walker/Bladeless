"""OpenCV-compatible camera that reads recent iPhone JPEGs from the local server."""
import os
import time
from urllib.request import Request, urlopen
from urllib.error import URLError

class PhoneCamera:
    def __init__(self):
        self.closed = False
        self.url = os.environ.get('BLADE_SERVER_URL', 'http://127.0.0.1:8000').rstrip('/') + '/api/camera/frame'
        self.key = os.environ.get('BLADE_ADMIN_KEY', '')
        self.last_warning = 0

    def isOpened(self):
        return not self.closed

    def read(self):
        import cv2
        import numpy as np
        if self.closed:
            return False, None
        time.sleep(0.08)
        try:
            request = Request(self.url, headers={'Authorization': 'Bearer ' + self.key})
            with urlopen(request, timeout=2) as response:
                raw = response.read(250001)
            if raw and len(raw) <= 250000:
                frame = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
                if frame is not None and frame.shape[0] <= 1024 and frame.shape[1] <= 1024:
                    return True, frame
        except (URLError, OSError, ValueError) as error:
            if time.monotonic() - self.last_warning > 5:
                print(f'Phone camera unavailable: {error}')
                self.last_warning = time.monotonic()
        # A blank frame lets the tracker clear a stale match instead of retaining it.
        return True, np.zeros((480, 640, 3), dtype=np.uint8)

    def release(self):
        self.closed = True

def open_selected_camera(fallback):
    if os.environ.get('BLADE_CAMERA', '').lower() == 'phone':
        print('Using iPhone browser camera. Start the camera on the attendee page.')
        return PhoneCamera()
    return fallback()
