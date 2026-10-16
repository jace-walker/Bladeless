'use strict';
// The Blade-style homepage: Check-in works; the other sections are placeholders.

// Links from before the homepage existed (/#kiosk, /?admin=1) go to the check-in page.
const oldLinks = {'#kiosk': '#kiosk', '#enroll': '#enroll', '#profile': '#enroll', '#admin': '#admin'};
function redirectOldLink() {
  const target = oldLinks[location.hash] || (new URLSearchParams(location.search).has('admin') ? '#admin' : null);
  if (target) location.replace('/checkin.html' + target);
  return !!target;
}

// What each section is for in the real Blade
const placeholders = {
  dashboard: {name: 'Dashboard', text: 'In the real Blade, this is where members see their points, membership status and upcoming events at a glance.'},
  events: {name: 'Events', text: 'In the real Blade, this is the club calendar, where members find events and look back at the ones they went to.'},
  dues: {name: 'Dues', text: 'In the real Blade, this is where members pay their club dues.'},
  forms: {name: 'Forms', text: 'In the real Blade, this is where members fill in club sign-ups, applications and feedback forms.'},
  settings: {name: 'Settings', text: 'In the real Blade, this is where members edit their profile and résumé and connect their Discord account.'},
};

function show(moveFocus) {
  const id = location.hash.slice(1);
  const view = placeholders[id] ? id : 'checkin';
  const panel = placeholders[view] ? 'placeholder' : view;
  for (const section of document.querySelectorAll('.panel')) section.hidden = section.id !== panel;
  if (placeholders[view]) {
    document.getElementById('ph-title').textContent = placeholders[view].name;
    document.getElementById('ph-text').textContent = placeholders[view].text;
  }
  for (const link of document.querySelectorAll('.side-link')) {
    if (link.dataset.view === view) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  const name = placeholders[view]?.name || 'Check-in';
  document.title = `${name} — Blade demo`;
  if (moveFocus) document.getElementById(panel).querySelector('h1').focus();
}

window.addEventListener('hashchange', () => { if (!redirectOldLink()) show(true); });
if (!redirectOldLink()) show(false);
