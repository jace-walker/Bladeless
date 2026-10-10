# Blade face check-in — v5

A local integration prototype styled after the supplied three screenshots. Includes My profile, Check-in kiosk, Admin, and the paired iPhone attendee page. It does not connect to the real Blade portal, Discord, or a payment provider.

## What is new

- Browser enrollment: consent, member selection/addition, five real embedding captures, and face-data deletion.
- Kiosk: start/stop, computer or paired iPhone camera, strictness slider, optional DeepFace anti-spoofing, hidden preview, event selection, and manual member-ID/QR fallback.
- Admin: live green/red result, member details, dues toggles, sample past-event counts plus new attendance, opt-in status, recent results, and one-click attendance reset.
- Recognition now runs inside the Python server. You do not need to run `face_check.py` simultaneously. The same Facenet512/OpenCV pipeline is used.

## Prepare your Windows folder

Extract the ZIP. Copy your existing `faces.json` and `attendance.sqlite3` into the new `blade-demo` folder if you want to retain them. Do not copy older server/UI files over the new ones. Existing face samples are read, but each member starts with consent disabled; use the profile checkbox to explicitly opt them in. Existing face names `Stuart` and `Jace` map to those demo members. If your face data uses other names, add the exact name as a member.

The SQLite schema adds tables without dropping old attendance. Stop old servers and trackers before copying the database. No face data or attendance database is bundled in this download.

## Start with your existing face-recognition environment

Open Command Prompt in the extracted v5 `blade-demo` folder. Run:

```bat
"C:\Users\Owner\Downloads\blade-demo-v2\blade-demo\.venv\Scripts\python.exe" server.py --secure
```

Use your actual installed Python environment path if it differs. `py server.py` can serve member management, but enrollment and recognition require DeepFace, NumPy, and OpenCV installed in the Python interpreter running the server. You previously installed these in the v2 environment.

If creating a fresh environment:

```bat
py -m venv .venv
.venv\Scripts\python.exe -m pip install deepface opencv-contrib-python
.venv\Scripts\python.exe server.py --secure
```

The server prints separate admin and phone pairing keys. They change after every server restart. Open http://127.0.0.1:8000/pair.html on your computer, enter the admin key, and keep it private. The server binds to loopback by default. Do not expose an unprotected server through a public tunnel.

## My profile

Open My profile. The profile selector is a demo identity selector for the organizer, not production member authentication. Select the correct person or add their name, obtain their consent, and tick the checkbox. This enables capture. For a computer camera, allow browser camera access. Capture five shots with small angle changes. First capture may take a while as the model loads/downloads. A failed/no-face capture does not increment the counter.

To enroll from the iPhone, select Paired iPhone, start its camera on its paired HTTPS attendee page, then Capture on the COMPUTER. The profile preview shows its current image. The phone key still cannot browse profiles or member details. Avoid another person's face entering the frame during enrollment.

Only embeddings are saved in `faces.json`; captured photos are processed in memory. If you already have five samples under the member’s exact enrolled name, giving consent makes those samples eligible without recapture. Unticking consent disables face recognition while retaining embeddings until you click Delete face data. Deletion also revokes consent, but preserves attendance/history. Manual member-ID check-in remains available.

## Check-in kiosk

Select an event and a camera source, then Start kiosk. For This computer, your browser supplies the video. For Paired iPhone, the attendee browser supplies it and you do not need the old tracker process. Only people with explicit consent and at least five embeddings are candidates.

Match strictness is the maximum cosine distance (0.10–0.60): lower is stricter. Start at the default 0.30. Changes affect subsequent checks. Require liveness passes `anti_spoofing=True` to DeepFace. It may need additional model downloads/dependencies and is not a guarantee against impersonation. If your installed DeepFace lacks support or the model cannot load, a model error is shown; the app does not silently bypass this option. No new attendance is awarded for a liveness failure.

Hide camera preview only hides the organizer preview; it does not stop capture or matching. Stop kiosk pauses recognition. Phone streaming can be stopped separately on the phone.

The manual fallback accepts the displayed KH member code or the internal member ID. QR scanning works only when the browser provides the native BarcodeDetector QR format; otherwise the typed input remains available. Demo QR contents must be the member ID, KH code, or `member:ID`. Real Blade account QR payloads are not interchangeable with these demo member codes.

## iPhone camera and attendee result

Safari camera access on a phone requires HTTPS. Start the protected server first. In a separate terminal, use the existing Cloudflare executable:

```bat
"C:\Users\Owner\Downloads\cloudflared.exe" tunnel --url http://127.0.0.1:8000
```

Open the new exact HTTPS `.trycloudflare.com` URL on the iPhone, pair with the PHONE key, then tap Start iPhone camera and allow access. Keep Safari visible and the phone unlocked. On the COMPUTER kiosk page select Paired iPhone and Start kiosk. Do not run `face_check.py` as well.

Camera JPEGs pass through Cloudflare to your computer. No microphone is requested. The app keeps only the most recent frame in RAM and does not persist it; stale frames are not supplied to recognition. Embeddings remain local in `faces.json`. Get consent for this processing and relay before starting camera capture. Phone/admin access separation is enforced by the pairing keys: the phone cannot browse member data, retrieve frames, or record attendance.

The phone shows green for a successful or duplicate check-in, red for unknown/ineligible/liveness/model-error results, and neutral when no face is visible. It does not show names or dues details. The admin sees details and a rejection reason. Stop the tunnel and server with Ctrl+C when finished.

## Admin

Click a Paid/Unpaid button to toggle that demo member’s dues. This is a local test flag, not an actual payment. The AI event is public; the GitHub event is dues-only in this demo. These are sample policies, not claims about the real events.

Initial past-event counts are sample data. Real new attendance is stored locally and counted once per member/event. Duplicate matches do not award more points. Recent results are bounded and repeated identical detections are limited to one log entry per ten seconds.

Reset attendance clears newly recorded attendance and recent results, and stops recognition to avoid immediately recreating attendance. It preserves members, dues settings, and face embeddings. Initial sample past-event counts remain.

## Hosted UI preview

The published Site shows the three views, sample members, and camera controls. It is a STATIC UI preview. Member writes, enrollment, QR/ID check-in, and real recognition require your local Python server; the preview explicitly marks these unavailable rather than pretending to recognize faces.

## Storage and scope

`attendance.sqlite3`: members, consent flags, settings, attendance, bounded check-in log. `faces.json`: only embeddings keyed by enrollment name. Camera frames stay in memory. Copy these files intentionally when changing folders; never commit them to GitHub.

This is a single-station development demo. Multiple simultaneous stations/users, production identity verification, formal biometric retention controls, and real Blade authentication are outside this version. A lower threshold and optional anti-spoofing do not turn the prototype into a guaranteed identity check.

## Verification

Run `python -m unittest discover -s tests -q`. Backend tests cover persistence, consent, enrollment/deletion, late capture after deletion, concurrent duplicate prevention, dues checks, reset preservation, mocked-embedding matching, and phone/admin permission boundaries. The embedding tests deliberately mock model output; they do not validate real facial accuracy. JavaScript syntax was checked. Live DeepFace inference and iPhone Safari camera behavior must be tested on your installed environment; DeepFace/OpenCV are not installed in the build environment. No browser visual QA was available.
