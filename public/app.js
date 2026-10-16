'use strict';
// The check-in page: Check in (#kiosk), Add my face (#enroll) and Organizer (#admin).
const $ = id => document.getElementById(id);
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const firstName = m => (m?.name || '').trim().split(/\s+/)[0] || 'there';

let preview = false, initialized = false, refreshing = false;
let state = {members: [], events: [], attendance: [], recent: [], access: {},
  settings: {threshold: .3, eventId: 'intro-ai', running: false, requireLiveness: false},
  latestMatch: {status: 'idle'}, vision: {status: 'Not loaded'}};
const demoMembers = [
  {id: '1', name: 'Alex Rivera', displayId: 'KH-0003', paid: true, consent: false, samples: 0, enrolled: false, eventsAttended: 6},
  {id: '2', name: 'Sam Chen', displayId: 'KH-0001', paid: true, consent: false, samples: 0, enrolled: false, eventsAttended: 3},
  {id: '3', name: 'Jordan Lee', displayId: 'KH-0002', paid: false, consent: false, samples: 0, enrolled: false, eventsAttended: 1},
  {id: '4', name: 'Taylor Kim', displayId: 'KH-6659', paid: false, consent: false, samples: 0, enrolled: false, eventsAttended: 7},
];
// Organizer wording for each result
const labels = {idle: 'Waiting for a face', checked_in: 'Checked in', already_checked_in: 'Already checked in', unknown: 'Not recognized', dues_required: 'Dues required', role_required: 'Not eligible', not_enrolled: 'Face check-in not enabled', member_not_found: 'Member not found', liveness_failed: 'Liveness check failed', camera_error: 'Camera / model error'};
const audiences = {public: 'Everyone', dues: 'Paid members', roles: 'Role holders'};
const camera = {enroll: null, kiosk: null, qr: null};
const SEND_EVERY_MS = 500;  // a snapshot about every half second (the server checks at most one per 0.4 s)
let sending = false, starting = false, scanning = false, autoStart = false;

async function request(path, payload) {
  if (preview) throw Error('The check-in server isn’t reachable right now.');
  const post = payload !== undefined;
  const r = await fetch(path, {method: post ? 'POST' : 'GET', headers: post ? {'Content-Type': 'application/json'} : {}, body: post ? JSON.stringify(payload) : undefined, cache: 'no-store'});
  if (r.status === 403) { location.href = '/pair.html'; throw Error('Pair this device again.'); }
  const data = await r.json();
  if (!r.ok) throw Error(data.error || 'Request failed');
  return data;
}
const warmUp = () => request('/api/warmup', {});  // loads the face model so the first check doesn't wait

// ---------- camera ----------
function cameraError(e) {
  if (e?.name === 'NotAllowedError') return 'The camera is blocked. Allow it in your browser’s address bar, then try again.';
  if (e?.name === 'NotFoundError') return 'No camera found. Connect a camera, then try again.';
  return e?.message || 'The camera could not start.';
}
async function startCamera(kind) {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) throw Error('The camera only works on a secure (https://) page.');
  stopCamera(kind);
  const stream = await navigator.mediaDevices.getUserMedia({audio: false, video: {facingMode: 'user', width: {ideal: 640}, height: {ideal: 480}}});
  camera[kind] = stream;
  const v = $(kind + '-video');
  v.srcObject = stream; v.hidden = false;
  await v.play();
  if (kind === 'enroll') $('enroll-placeholder').hidden = true;
  return stream;
}
function stopCamera(kind) {
  if (camera[kind]) camera[kind].getTracks().forEach(t => t.stop());
  camera[kind] = null;
  const v = $(kind + '-video');
  v.srcObject = null;
  if (kind === 'enroll') { $('enroll-placeholder').hidden = false; $('enroll-placeholder').textContent = 'Camera off'; }
  else v.hidden = true;
}
async function snapshot(video) {
  if (video.readyState < 2) throw Error('The camera is still starting. Try again.');
  const canvas = document.createElement('canvas'), scale = Math.min(640 / video.videoWidth, 480 / video.videoHeight, 1);
  canvas.width = Math.round(video.videoWidth * scale);
  canvas.height = Math.round(video.videoHeight * scale);
  canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
  const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', .65));
  if (!blob) throw Error('Unable to take a picture');
  return {blob, width: canvas.width, height: canvas.height};
}
const toBase64 = blob => new Promise((resolve, reject) => {
  const reader = new FileReader();
  reader.onload = () => resolve(String(reader.result).split(',')[1]);
  reader.onerror = reject;
  reader.readAsDataURL(blob);
});

// ---------- loading state ----------
function setOffline(message) { $('offline').hidden = !message; $('offline').textContent = message || ''; }
// Only rebuild a table when its contents change, so keyboard focus isn't lost every second.
function setRows(id, html) { if ($(id).dataset.html !== html) { $(id).innerHTML = html; $(id).dataset.html = html; } }

function render() {
  const eventKey = state.events.map(e => e.id + ':' + e.name).join('|');
  if ($('event').dataset.key !== eventKey) {
    $('event').innerHTML = state.events.map(e => `<option value="${escapeHtml(e.id)}">${escapeHtml(e.name)}</option>`).join('');
    $('event').dataset.key = eventKey;
  }
  if (document.activeElement !== $('threshold')) {
    $('threshold').value = state.settings.threshold;
    $('threshold-value').textContent = Number(state.settings.threshold).toFixed(2);
  }
  $('liveness').checked = state.settings.requireLiveness;
  $('event').value = state.settings.eventId;
  const event = state.events.find(e => e.id === state.settings.eventId);
  $('kiosk-event').textContent = event ? event.name : 'No event chosen yet';
  $('kiosk-state').textContent = state.settings.running ? 'The check-in camera is on.' : 'The check-in camera is off. Start it on the Check in page.';

  // Phone check-in is off unless the server turns it on (PHONE_CHECKIN=on)
  const phoneOn = !!state.access?.phoneCheckin, phoneOption = $('source').querySelector('option[value="phone"]');
  phoneOption.hidden = phoneOption.disabled = !phoneOn;
  if (!phoneOn && $('source').value === 'phone') $('source').value = 'computer';
  const nightly = !!state.access?.nightlyReset;
  $('reset-note').hidden = !nightly;
  $('consent-nightly').hidden = !nightly;

  // Someone turned the check-in off from another screen
  if (!state.settings.running && sending) {
    stopKioskCamera();
    $('kiosk-cover-text').textContent = 'The camera was turned off from another screen. Press Start to begin again.';
  }

  setRows('members-table', state.members.map(m => `<tr><td>${escapeHtml(m.name)}</td><td>${escapeHtml(m.displayId)}</td><td><button data-action="dues" data-id="${escapeHtml(m.id)}" aria-label="Toggle dues for ${escapeHtml(m.name)}"><span class="pill ${m.paid ? 'green' : 'red'}">${m.paid ? 'Paid' : 'Unpaid'}</span></button></td><td>${m.eventsAttended}</td><td><span class="pill ${m.enrolled ? 'green' : ''}">${memberStatus(m)}</span></td><td><div class="row-actions">${m.samples || m.consent ? `<button data-action="delete" data-id="${escapeHtml(m.id)}">Delete face data</button>` : ''}<button class="danger" data-action="remove" data-id="${escapeHtml(m.id)}" aria-label="Remove ${escapeHtml(m.name)}">Remove</button></div></td></tr>`).join(''));
  setRows('events-table', state.events.length ? state.events.map(e => `<tr><td>${escapeHtml(e.name)}</td><td>${escapeHtml(e.date)}</td><td>${escapeHtml(e.location || '—')}</td><td>${escapeHtml(e.points)}</td><td>${escapeHtml(audiences[e.audience] || e.audience)}</td><td>${escapeHtml(e.checkins ?? 0)}</td><td><div class="row-actions">${(e.checkins ?? 0) === 0 ? `<button class="danger" data-action="delete-event" data-id="${escapeHtml(e.id)}" aria-label="Delete ${escapeHtml(e.name)}">Delete</button>` : ''}</div></td></tr>`).join('') : '<tr><td colspan="7" class="muted">No events yet. Create one below.</td></tr>');
  $('recent').innerHTML = state.recent.length ? state.recent.map(r => {
    const i = resultInfo(r), time = new Date(r.createdAt * 1000).toLocaleTimeString();
    return `<div class="log"><span style="color:${i.color}">${i.symbol}</span> ${escapeHtml(time)} · ${escapeHtml(r.member?.name || 'Unknown face')} · ${escapeHtml(i.title)} via ${r.method === 'face' ? 'Face' : 'Member ID'}</div>`;
  }).join('') : '<p>No check-ins yet.</p>';
  renderAdminResult();
  renderKiosk();
  renderEnroll();
}

async function refresh() {
  if (refreshing || preview) return;
  refreshing = true;
  try {
    const r = await fetch('/api/state', {cache: 'no-store'});
    if (r.status === 403) { location.href = '/pair.html'; return; }
    if (!r.ok) { let m = 'Server unavailable'; try { m = (await r.json()).error || m; } catch {} throw Error(m); }
    const next = await r.json();
    if (!Array.isArray(next.members) || !next.settings) throw Error('Unexpected response');
    state = next; initialized = true;
    setOffline('');
    $('connection').textContent = '● Connected · Face model: ' + state.vision.status;
    render();
  } catch (e) {
    if (!initialized) {
      preview = true;
      state.members = demoMembers;
      state.events = [{id: 'intro-ai', name: 'Intro to Artificial Intelligence'}, {id: 'intro-github', name: 'Intro to GitHub and GitHub Etiquette'}];
      setOffline('Can’t reach the check-in server right now, so this is a preview with sample members.');
      $('connection').textContent = 'Can’t reach the check-in server (' + e.message + '). See /api/health.';
      render();
    } else {
      setOffline('Lost the connection to the check-in server. Trying again…');
    }
  } finally { refreshing = false; }
}

function route() {
  let hash = location.hash.slice(1);
  if (hash === 'profile') hash = 'enroll';  // old links
  const view = ['enroll', 'kiosk', 'admin'].includes(hash) ? hash : new URLSearchParams(location.search).has('admin') ? 'admin' : 'kiosk';
  for (const id of ['enroll', 'kiosk', 'admin']) $(id).hidden = id !== view;
  for (const a of document.querySelectorAll('.app-header nav a')) {
    a.classList.toggle('active', a.hash === '#' + view);
    if (a.hash === '#' + view) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  document.title = {kiosk: 'Check in', enroll: 'Add my face', admin: 'Organizer'}[view] + ' — Blade demo';
  if (view === 'enroll') {
    if (camera.kiosk) stopKiosk();  // so nobody gets checked in while adding their face
    if (enrollFlow.step === 'capture' && !camera.enroll) startCapture({keepBase: true});
  } else stopCamera('enroll');
  if (view !== 'admin' && scanning) { scanning = false; stopCamera('qr'); }
  if (view === 'kiosk' && autoStart) { autoStart = false; if (!camera.kiosk && !starting) startKiosk(); }
}

// ---------- Check in ----------
const shortStatus = {checked_in: 'Checked in', already_checked_in: 'Already checked in', unknown: 'Not recognized', dues_required: 'Dues needed', role_required: 'Not eligible', not_enrolled: 'Face check-in off', member_not_found: 'Not found', idle: '…'};
const toneOf = status => ['checked_in', 'already_checked_in'].includes(status) ? 'good' : status === 'idle' ? 'idle' : 'bad';

// What the person at the camera reads, in plain words
function friendly(r) {
  const n = firstName(r.member);
  switch (r.status) {
    case 'checked_in': return {tone: 'good', title: `Hi, ${n}! You're checked in.`, sub: r.pointsAwarded ? `+${r.pointsAwarded} points` : ''};
    case 'already_checked_in': return {tone: 'good', title: `Hi, ${n}! You're already checked in.`};
    case 'unknown': return {tone: 'bad', title: 'I don’t know you yet.', sub: 'New here? Tap “Add my face” below.'};
    case 'dues_required': return {tone: 'bad', title: `${n}, this event is for members who paid dues.`, sub: 'Please see an organizer.'};
    case 'role_required': return {tone: 'bad', title: 'This event is only for certain members.', sub: 'Please see an organizer.'};
    case 'not_enrolled': return {tone: 'bad', title: `${n}, face check-in is turned off for you.`, sub: 'Add your face again to turn it back on.'};
    case 'member_not_found': return {tone: 'bad', title: 'I couldn’t find that member.', sub: 'Please see an organizer.'};
    case 'liveness_failed': return {tone: 'bad', title: 'That looked like a photo, not a real face.', sub: 'Please look at the camera yourself.'};
    case 'camera_error': return {tone: 'bad', title: 'Something went wrong.', sub: r.message || 'Please try again.'};
    default: return {tone: 'idle', title: 'Look at the camera.'};
  }
}
function setKiosk({tone, title, sub = ''}) {
  $('kiosk-result').dataset.tone = tone;
  $('kiosk-mark').textContent = tone === 'good' ? '✓' : tone === 'bad' ? '!' : '';
  $('kiosk-title').textContent = title;
  $('kiosk-sub').textContent = sub;
}
function renderOthers(faces) {
  const list = $('kiosk-others');
  list.hidden = faces.length < 2;
  list.innerHTML = faces.length < 2 ? '' : faces.map(f => `<li data-tone="${toneOf(f.status)}"><span class="mark" aria-hidden="true">${toneOf(f.status) === 'good' ? '✓' : '!'}</span><span class="who">${escapeHtml(f.member ? f.member.name : 'Someone new')}</span><span class="what">${escapeHtml(shortStatus[f.status] || 'See an organizer')}</span></li>`).join('');
}
// Keep each result on screen long enough to read, so "You're checked in" doesn't flip
// to "already checked in" on the next snapshot or vanish the moment someone looks away.
let shown = {result: null, until: 0};
function showKioskResult(r) {
  const now = Date.now(), held = shown.result && now < shown.until;
  const sameWelcome = r.status === 'already_checked_in' && shown.result?.status === 'checked_in' && r.member?.id === shown.result.member?.id;
  if (held && (r.status === 'idle' || sameWelcome)) return;
  if (r.status === 'idle') { shown = {result: null, until: 0}; setKiosk(friendly(r)); renderOthers([]); return; }
  shown = {result: r, until: now + 4000};
  const faces = r.faces || [];
  if (faces.length > 1 && faces.every(f => toneOf(f.status) === 'good')) {
    setKiosk({tone: 'good', title: `Hi, ${faces.map(f => firstName(f.member)).join(' and ')}! You're all checked in.`});
  } else setKiosk(friendly(r));
  renderOthers(faces);
}
// A box and first name over each face. The picture is mirrored, so the boxes are too.
let boxTimer = null;
function drawFaces(faces, frameWidth) {
  const layer = $('face-boxes'), video = $('kiosk-video');
  clearTimeout(boxTimer);
  if (!faces.length || !video.videoWidth) { layer.innerHTML = ''; return; }
  const stage = $('kiosk-wrap').getBoundingClientRect(), vw = video.videoWidth, vh = video.videoHeight;
  const scale = Math.min(stage.width / vw, stage.height / vh);
  const offX = (stage.width - vw * scale) / 2, offY = (stage.height - vh * scale) / 2, k = vw / frameWidth;
  layer.innerHTML = faces.map(f => {
    const b = f.box, x = (vw - (b.x + b.w) * k) * scale + offX, y = b.y * k * scale + offY;
    return `<div class="face-box" data-tone="${toneOf(f.status)}" style="left:${x}px;top:${y}px;width:${b.w * k * scale}px;height:${b.h * k * scale}px"><span>${escapeHtml(f.member ? firstName(f.member) : '?')}</span></div>`;
  }).join('');
  boxTimer = setTimeout(() => { layer.innerHTML = ''; }, 1500);
}
async function sendFrames() {
  if (!sending || !camera.kiosk) return;
  const started = Date.now();
  try {
    const shot = await snapshot($('kiosk-video'));
    const r = await fetch('/api/camera', {method: 'POST', headers: {'Content-Type': 'image/jpeg'}, body: shot.blob});
    if (r.status === 403) { location.href = '/pair.html'; return; }
    if (!r.ok) throw Error('The check-in server didn’t answer. Trying again…');
    const d = await r.json();
    if (d.result && sending) { drawFaces(d.result.faces || [], shot.width); showKioskResult(d.result); }
  } catch (e) {
    if (sending) setKiosk({tone: 'bad', title: 'Having trouble…', sub: e.message});
  }
  if (sending) setTimeout(sendFrames, Math.max(50, SEND_EVERY_MS - (Date.now() - started)));
}
function renderKiosk() {
  const phoneCamera = !camera.kiosk && !starting && state.settings.running && $('source').value === 'phone';
  $('kiosk-cover').hidden = !!camera.kiosk || starting;
  $('kiosk-result').hidden = !camera.kiosk && !starting && !phoneCamera;  // the Start button says what to do
  if (camera.kiosk || starting) $('kiosk-cover-text').textContent = 'Then just look at the camera.';
  $('kiosk-stop').hidden = !camera.kiosk;
  $('kiosk-start').disabled = starting || preview;
  // When a paired phone is the camera, show its results here
  if (phoneCamera) showKioskResult(state.latestMatch || {status: 'idle'});
}
function stopKioskCamera() {
  sending = false;
  stopCamera('kiosk');
  $('face-boxes').innerHTML = '';
  renderOthers([]);
  renderKiosk();
}
async function startKiosk() {
  if (preview) { $('kiosk-cover-text').textContent = 'The check-in server isn’t reachable right now. Try again in a minute.'; return; }
  starting = true; renderKiosk();
  try {
    if ($('source').value === 'phone') {
      await request('/api/settings', {running: true});
      setKiosk({tone: 'idle', title: 'Start the camera on the paired phone.'});
      return;
    }
    await startCamera('kiosk');
    $('kiosk-ready').hidden = false;
    setKiosk({tone: 'idle', title: 'Getting ready…', sub: 'The first time can take about 15 seconds.'});
    await request('/api/settings', {running: true});
    await warmUp();
    sending = true; shown = {result: null, until: 0};
    setKiosk({tone: 'idle', title: 'Look at the camera.'});
    sendFrames();
  } catch (e) {
    stopKioskCamera();
    $('kiosk-cover-text').textContent = 'The camera couldn’t start. ' + cameraError(e);
  } finally {
    starting = false;
    $('kiosk-ready').hidden = true;
    renderKiosk();
    refresh();
  }
}
async function stopKiosk() {
  stopKioskCamera();
  try { await request('/api/settings', {running: false}); await refresh(); }
  catch (e) { $('kiosk-cover-text').textContent = 'Couldn’t turn check-in off: ' + e.message; }
}
$('kiosk-start').onclick = startKiosk;
$('kiosk-stop').onclick = stopKiosk;
document.querySelectorAll('[data-autostart]').forEach(a => a.addEventListener('click', () => { autoStart = true; }));

// ---------- Add my face: name → OK → 5 pictures → done ----------
const SHOTS = 5;
const PROMPTS = [
  {icon: '◎', text: 'Look straight at the camera'},
  {icon: '←', text: 'Turn your head a little to your left'},
  {icon: '→', text: 'Turn your head a little to your right'},
  {icon: '↑', text: 'Lift your chin up a little'},
  {icon: '↓', text: 'Tip your chin down a little'},
];
const STEP_CARDS = {choose: 'step-choose', consent: 'step-consent', enrolled: 'step-enrolled', capture: 'step-capture', done: 'step-done'};
const enrollFlow = {step: 'choose', memberId: null, base: 0, busy: false, listKey: ''};
const enrollMember = () => state.members.find(m => m.id === enrollFlow.memberId);
const memberStatus = m => m.enrolled ? 'Enrolled' : m.samples ? `${Math.min(m.samples, SHOTS)} of ${SHOTS} photos` : 'Not enrolled';
const faceStatus = m => m.enrolled ? 'Face added' : m.samples ? `${Math.min(m.samples, SHOTS)} of ${SHOTS} pictures` : 'No face yet';

function showStep(step, {focus = true} = {}) {
  enrollFlow.step = step;
  for (const [name, id] of Object.entries(STEP_CARDS)) $(id).hidden = name !== step;
  const order = ['choose', 'consent', 'photos'];
  const current = {choose: 'choose', consent: 'consent', enrolled: 'consent', capture: 'photos', done: 'done'}[step];
  order.forEach((s, i) => {
    const li = $('step-label-' + s);
    li.classList.toggle('done', current === 'done' || i < order.indexOf(current));
    if (s === current) li.setAttribute('aria-current', 'step'); else li.removeAttribute('aria-current');
  });
  if (step !== 'capture') stopCamera('enroll');
  renderEnroll();
  if (focus && !$('enroll').hidden) { if (step === 'choose') $('member-search').focus(); else $(STEP_CARDS[step]).querySelector('h2').focus(); }
}
function renderResults() {
  if (!initialized && !preview) return;  // members haven't loaded yet
  const q = $('member-search').value.trim(), ql = q.toLowerCase();
  const matches = ql ? state.members.filter(m => m.name.toLowerCase().includes(ql) || (m.displayId || '').toLowerCase() === ql).slice(0, 6) : [];
  const exact = state.members.some(m => m.name.toLowerCase() === ql);
  const key = JSON.stringify([q, matches.map(m => [m.id, m.name, m.enrolled, m.samples])]);
  if (key === enrollFlow.listKey) return;
  enrollFlow.listKey = key;
  const rows = matches.map(m => `<button type="button" class="member-row" data-id="${escapeHtml(m.id)}"><span class="name">${escapeHtml(m.name)}</span><span class="pill ${m.enrolled ? 'green' : ''}">${faceStatus(m)}</span><span class="row-cta">That’s me</span></button>`);
  if (q && !exact) rows.push(`<button type="button" class="member-row add" data-add="1"><span class="name">I’m new: add me as “${escapeHtml(q)}”</span></button>`);
  $('member-results').innerHTML = rows.length ? (matches.length ? '<p class="results-label">Is this you?</p>' : '') + rows.join('') : '';
}
function renderEnroll() {
  const m = enrollMember();
  if (enrollFlow.step !== 'choose' && !m) {
    enrollFlow.memberId = null;
    showStep('choose', {focus: false});
    $('choose-message').textContent = 'That name was removed. Type your name again.';
    return;
  }
  if (enrollFlow.step === 'choose') renderResults();
  if (!m) return;
  const n = firstName(m), taken = Math.max(0, Math.min(m.samples - enrollFlow.base, SHOTS));
  $('consent-title').textContent = `${n}, is it OK to save your face?`;
  $('enrolled-title').textContent = `You're already set up, ${n}!`;
  $('enrolled-text').textContent = `We have ${m.samples} pictures of your face, so you can check in now.`;
  $('capture-title').textContent = taken < SHOTS ? `Picture ${taken + 1} of ${SHOTS}` : `All ${SHOTS} pictures taken`;
  $('shot-dots').innerHTML = Array.from({length: SHOTS}, (_, i) => `<li class="${i < taken ? 'taken' : i === taken ? 'current' : ''}">${i + 1}</li>`).join('');
  $('shot-dots').setAttribute('aria-label', `${taken} of ${SHOTS} pictures taken`);
  const prompt = PROMPTS[Math.min(taken, SHOTS - 1)];
  $('prompt-icon').textContent = prompt.icon;
  $('shot-prompt').textContent = prompt.text;
  $('capture').disabled = preview || !camera.enroll || enrollFlow.busy;
  $('capture-label').textContent = enrollFlow.busy ? 'Saving…' : 'Take picture';
  $('done-title').textContent = `All done, ${n}!`;
}
function chooseMember(id) {
  enrollFlow.memberId = id;
  const m = enrollMember();
  if (!m) return;
  for (const msg of ['consent-message', 'enrolled-message', 'capture-message']) $(msg).textContent = '';
  if (!m.consent) showStep('consent');
  else if (m.samples >= SHOTS) showStep('enrolled');
  else startCapture();
}
async function addAndChoose(name) {
  try { const m = await request('/api/members/add', {name}); await refresh(); chooseMember(m.id); }
  catch (e) { $('choose-message').textContent = e.message; }
}
// The Next button (and Enter): an exact name picks it, a partial match asks, no match adds.
async function chooseFromInput() {
  const q = $('member-search').value.trim();
  $('choose-message').textContent = '';
  if (!q) { $('choose-message').textContent = 'Type your name first.'; $('member-search').focus(); return; }
  const exact = state.members.find(m => m.name.toLowerCase() === q.toLowerCase());
  if (exact) { chooseMember(exact.id); return; }
  renderResults();
  const first = $('member-results').querySelector('.member-row:not(.add)');
  if (first) { $('choose-message').textContent = 'Tap your name, or tap “I’m new”.'; first.focus(); return; }
  await addAndChoose(q);
}
function backToChoose() {
  enrollFlow.memberId = null; enrollFlow.listKey = '';
  $('member-search').value = '';
  for (const msg of ['choose-message', 'consent-message', 'enrolled-message', 'capture-message']) $(msg).textContent = '';
  showStep('choose');
}
async function startCapture({keepBase = false} = {}) {
  const m = enrollMember();
  if (!m) return;
  if (!keepBase) enrollFlow.base = m.samples >= SHOTS ? m.samples : 0;  // already set up: count 5 more
  enrollFlow.busy = false;
  $('camera-retry').hidden = true;
  showStep('capture');
  $('enroll-placeholder').textContent = 'Starting camera…';
  try {
    if (preview) throw Error('Taking pictures needs the check-in server.');
    await startCamera('enroll');
    if (enrollFlow.step !== 'capture' || $('enroll').hidden) stopCamera('enroll');  // left while the camera was starting
    else warmUp().catch(() => {});  // load the face model while they get ready
  } catch (e) {
    $('enroll-placeholder').textContent = 'Camera off';
    $('capture-message').textContent = cameraError(e);
    $('camera-retry').hidden = preview;
  }
  renderEnroll();
}
async function takePhoto() {
  const m = enrollMember();
  if (!m || enrollFlow.busy || !camera.enroll) return;
  enrollFlow.busy = true; renderEnroll();
  $('capture-message').textContent = 'Saving…';
  try {
    const image = await toBase64((await snapshot($('enroll-video'))).blob);
    const d = await request('/api/enroll', {memberId: m.id, image});
    await refresh();
    const left = SHOTS - Math.min(d.samples - enrollFlow.base, SHOTS);
    if (left <= 0) { enrollFlow.busy = false; showStep('done'); return; }
    $('capture-message').textContent = left === 1 ? 'Great! One more to go.' : `Great! ${left} more to go.`;
  } catch (e) {
    $('capture-message').textContent = e.message.startsWith('No face found') ? 'I can’t see your face. Move closer and face the light, then try again.' : e.message;
  } finally { enrollFlow.busy = false; renderEnroll(); }
}
$('member-search').oninput = () => { $('choose-message').textContent = ''; renderResults(); };
$('member-search').onkeydown = e => {
  if (e.key === 'Enter') { e.preventDefault(); chooseFromInput(); }
  const rows = [...$('member-results').querySelectorAll('.member-row')];
  if (e.key === 'ArrowDown' && rows.length) { e.preventDefault(); rows[0].focus(); }
};
$('choose-next').onclick = chooseFromInput;
$('member-results').onkeydown = e => {
  const rows = [...$('member-results').querySelectorAll('.member-row')], i = rows.indexOf(document.activeElement);
  if (e.key === 'ArrowDown' && i < rows.length - 1) { e.preventDefault(); rows[i + 1].focus(); }
  if (e.key === 'ArrowUp') { e.preventDefault(); (i > 0 ? rows[i - 1] : $('member-search')).focus(); }
};
$('member-results').onclick = async e => {
  const row = e.target.closest('.member-row');
  if (!row) return;
  $('choose-message').textContent = '';
  if (row.dataset.add) await addAndChoose($('member-search').value.trim());
  else chooseMember(row.dataset.id);
};
$('consent-agree').onclick = async () => {
  const m = enrollMember();
  if (!m) return;
  $('consent-agree').disabled = true;
  try {
    await request('/api/members/consent', {memberId: m.id, consent: true});
    await refresh();
    if (enrollMember()?.samples >= SHOTS) showStep('enrolled'); else await startCapture();
  } catch (e) { $('consent-message').textContent = e.message; }
  finally { $('consent-agree').disabled = false; }
};
$('enrolled-more').onclick = () => startCapture();
$('enrolled-delete').onclick = async () => {
  const m = enrollMember();
  if (!m || !confirm(`Delete ${m.name}'s face? You won't be recognized until you add it again.`)) return;
  try {
    await request('/api/faces/delete', {memberId: m.id});
    await refresh();
    backToChoose();
    $('choose-message').textContent = `${m.name}'s face was deleted.`;
  } catch (e) { $('enrolled-message').textContent = e.message; }
};
for (const id of ['consent-cancel', 'enrolled-back', 'capture-back', 'done-another']) $(id).onclick = backToChoose;
$('camera-retry').onclick = () => { $('capture-message').textContent = ''; startCapture({keepBase: true}); };
$('capture').onclick = takePhoto;
document.addEventListener('keydown', e => {
  if (e.code !== 'Space' || enrollFlow.step !== 'capture' || $('enroll').hidden) return;
  if (['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON', 'A'].includes(document.activeElement?.tagName)) return;  // they handle Space themselves
  e.preventDefault();
  takePhoto();
});

// ---------- Organizer ----------
function resultInfo(r) {
  const good = ['checked_in', 'already_checked_in'].includes(r.status), idle = r.status === 'idle';
  return {symbol: idle ? '—' : good ? '✓' : '✕', color: idle ? '#aaa2c5' : good ? '#24d7ad' : '#ff6a7b', title: labels[r.status] || 'Unable to check in'};
}
function renderAdminResult() {
  const r = state.latestMatch || {status: 'idle'}, info = resultInfo(r);
  $('admin-symbol').textContent = info.symbol;
  $('admin-symbol').style.color = info.color;
  $('admin-title').textContent = r.member ? `${info.title}: ${r.member.name}` : info.title;
  $('admin-member-id').textContent = r.member ? `Member ID: ${r.member.displayId || r.member.id}` : '';
  $('admin-dues').textContent = r.member ? `Dues: ${r.member.paid ? 'Paid' : 'Not paid'}` : '';
  $('admin-dues').style.color = r.member?.paid ? '#24d7ad' : '#ff6a7b';
  const others = (r.faces?.length || 1) - 1;
  $('admin-method').textContent = r.member ? `${r.method === 'face' ? 'Face' : 'Member ID'}${r.distance !== undefined ? ' · distance ' + r.distance.toFixed(3) : ''}${r.pointsAwarded !== undefined ? ' · +' + r.pointsAwarded + ' points' : ''}${others ? ` · ${others} more ${others === 1 ? 'person' : 'people'} in view` : ''}` : '';
  $('admin-error').textContent = r.message || '';
}
async function settings(patch) { await request('/api/settings', patch); await refresh(); }
const settingsError = e => { $('manual-message').textContent = e.message; };
$('threshold').oninput = () => { $('threshold-value').textContent = Number($('threshold').value).toFixed(2); };
$('threshold').onchange = () => settings({threshold: Number($('threshold').value)}).catch(settingsError);
$('liveness').onchange = () => settings({requireLiveness: $('liveness').checked}).catch(settingsError);
$('event').onchange = () => settings({eventId: $('event').value}).catch(settingsError);
$('source').onchange = () => { if (camera.kiosk || state.settings.running) stopKiosk(); };
$('hide-preview').onchange = () => $('kiosk-wrap').classList.toggle('preview-hidden', $('hide-preview').checked);
async function deleteFace(m) {
  if (!confirm(`Delete ${m.name}’s face data? They won’t be recognized until they add their face again. Their attendance history stays.`)) return;
  await request('/api/faces/delete', {memberId: m.id});
}
$('members-table').onclick = async e => {
  const button = e.target.closest('button[data-action]');
  if (!button) return;
  const m = state.members.find(x => x.id === button.dataset.id);
  if (!m) return;
  try {
    if (button.dataset.action === 'dues') await request('/api/members/dues', {memberId: m.id, paid: !m.paid});
    else if (button.dataset.action === 'delete') await deleteFace(m);
    else if (button.dataset.action === 'remove') {
      if (!confirm(`Remove ${m.name}? This deletes their face data and attendance history, and can’t be undone.`)) return;
      await request('/api/members/remove', {memberId: m.id});
    }
    await refresh();
  } catch (err) { $('admin-error').textContent = err.message; }
};
$('event-form').onsubmit = async e => {
  e.preventDefault();
  const body = {name: $('event-name').value, date: $('event-date').value, points: Number($('event-points').value), duesOnly: $('event-dues').checked};
  if ($('event-time').value) body.time = $('event-time').value;
  if ($('event-location').value.trim()) body.location = $('event-location').value;
  try {
    const created = await request('/api/events/add', body);
    $('event-form').reset();
    $('event-message').textContent = `Created “${created.name}”. It’s now in the event list.`;
    await refresh();
  } catch (err) { $('event-message').textContent = err.message; }
};
$('events-table').onclick = async e => {
  const button = e.target.closest('button[data-action="delete-event"]');
  if (!button) return;
  const ev = state.events.find(x => x.id === button.dataset.id);
  if (!ev || !confirm(`Delete “${ev.name}”?`)) return;
  try { await request('/api/events/delete', {eventId: ev.id}); $('event-message').textContent = `Deleted “${ev.name}”.`; await refresh(); }
  catch (err) { $('event-message').textContent = err.message; }
};
async function manualCheckin(id) {
  const d = await request('/api/checkins', {memberId: id.trim().replace(/^member:/, ''), eventId: state.settings.eventId});
  $('manual-message').textContent = (labels[d.status] || d.status) + (d.member ? ': ' + d.member.name : '');
  await refresh();
}
$('manual-form').onsubmit = async e => { e.preventDefault(); try { await manualCheckin($('manual-id').value); } catch (err) { $('manual-message').textContent = err.message; } };
$('scan-qr').onclick = async () => {
  try {
    if (preview) throw Error('QR check-in needs the check-in server.');
    if (!window.BarcodeDetector) throw Error('QR scanning isn’t available in this browser. Type the member ID instead.');
    if (!(await BarcodeDetector.getSupportedFormats()).includes('qr_code')) throw Error('This browser can’t scan QR codes. Type the member ID instead.');
    await startCamera('qr');
    const detector = new BarcodeDetector({formats: ['qr_code']}), began = Date.now();
    scanning = true;
    $('manual-message').textContent = 'Hold the QR code up to the camera.';
    const stopScan = () => { scanning = false; stopCamera('qr'); };
    const scan = async () => {
      if (!scanning) return;
      try {
        const codes = await detector.detect($('qr-video'));
        if (codes.length) { stopScan(); $('manual-id').value = codes[0].rawValue; await manualCheckin(codes[0].rawValue); return; }
      } catch (err) { stopScan(); $('manual-message').textContent = err.message; return; }
      if (Date.now() - began > 30000) { stopScan(); $('manual-message').textContent = 'No QR code found. Try again, or type the member ID.'; return; }
      setTimeout(scan, 300);
    };
    scan();
  } catch (err) { $('manual-message').textContent = err.message; }
};
$('reset').onclick = async () => {
  if (!confirm('Clear all recorded attendance and recent check-in results? Members and face data will remain.')) return;
  try { await request('/api/reset', {confirm: true}); await refresh(); } catch (err) { $('admin-error').textContent = err.message; }
};

window.addEventListener('hashchange', route);
window.addEventListener('pagehide', () => { sending = false; scanning = false; stopCamera('enroll'); stopCamera('kiosk'); stopCamera('qr'); });
showStep('choose', {focus: false});
route();
refresh();
setInterval(refresh, 1000);
