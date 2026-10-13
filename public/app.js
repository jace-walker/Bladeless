'use strict';
const $ = id => document.getElementById(id);
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let preview = false, initialized = false, refreshing = false;
let state = {members:[],events:[],attendance:[],recent:[],settings:{threshold:.3,eventId:'intro-ai',running:false,requireLiveness:false},latestMatch:{status:'idle'},vision:{status:'Not loaded'}};
const demoMembers = [
 {id:'1',name:'Alex Rivera',displayId:'KH-0003',paid:true,consent:false,samples:0,enrolled:false,eventsAttended:6},
 {id:'2',name:'Sam Chen',displayId:'KH-0001',paid:true,consent:false,samples:0,enrolled:false,eventsAttended:3},
 {id:'3',name:'Jordan Lee',displayId:'KH-0002',paid:false,consent:false,samples:0,enrolled:false,eventsAttended:1},
 {id:'4',name:'Taylor Kim',displayId:'KH-6659',paid:false,consent:false,samples:0,enrolled:false,eventsAttended:7}
];
const labels = {idle:'Waiting for a face',checked_in:'Checked in',already_checked_in:'Already checked in',unknown:'Not recognized',dues_required:'Dues required',role_required:'Not eligible',not_enrolled:'Face check-in not enabled',member_not_found:'Member not found',liveness_failed:'Liveness check failed',camera_error:'Camera / model error'};
const audiences = {public:'Everyone',dues:'Paid members',roles:'Role holders'};
const camera = {enroll:null,kiosk:null};
let sending = false, remoteUrl = null, scanning = false;

async function request(path, payload) {
 if(preview) throw Error('This is a preview without the check-in server.');
 const r = await fetch(path,{method:payload===undefined?'GET':'POST',headers:payload===undefined?{}:{'Content-Type':'application/json'},body:payload===undefined?undefined:JSON.stringify(payload),cache:'no-store'});
 if(r.status===403){location.href='/pair.html';throw Error('Pair this device again.');}
 const data = await r.json();
 if(!r.ok) throw Error(data.error || 'Request failed');
 return data;
}
function resultInfo(r) {
 const good = ['checked_in','already_checked_in'].includes(r.status), idle=r.status==='idle';
 return {symbol:idle?'—':good?'✓':'✕',color:idle?'#aaa2c5':good?'#24d7ad':'#ff6a7b',title:labels[r.status] || 'Unable to check in'};
}
function renderResult(){
 const r=state.latestMatch || {status:'idle'},info=resultInfo(r);
 for(const id of ['admin-symbol','kiosk-symbol']){$(id).textContent=info.symbol;$(id).style.color=info.color;}
 $('admin-title').textContent=r.member?`${info.title}: ${r.member.name}`:info.title;
 $('admin-member-id').textContent=r.member?`Member ID: ${r.member.displayId || r.member.id}`:'';
 $('admin-dues').textContent=r.member?`Dues: ${r.member.paid?'Paid':'Not paid'}`:'';
 $('admin-dues').style.color=r.member?.paid?'#24d7ad':'#ff6a7b';
 $('admin-method').textContent=r.member?`${r.method==='face'?'Face':'Member ID'}${r.distance!==undefined?' · distance '+r.distance.toFixed(3):''}${r.pointsAwarded!==undefined?' · +'+r.pointsAwarded+' points':''}`:'';
 $('admin-error').textContent=r.message || '';
 $('kiosk-result-label').textContent=r.status==='idle'?'Ready for check-in':info.symbol==='✓'?'Checked in':'Please see the organizer';
 if(state.settings.running)$('kiosk-message').textContent=r.message || (r.status==='idle'?`Waiting for a face · Model: ${state.vision.status}`:r.member?`${info.title}: ${r.member.name}${r.distance!==undefined?' (distance '+r.distance.toFixed(3)+')':''}`:info.title);
}
// Only rebuild a table when its contents change, so keyboard focus isn't lost every second.
function setRows(id, html){if($(id).dataset.html!==html){$(id).innerHTML=html;$(id).dataset.html=html;}}
function render(){
 const eventKey=state.events.map(e=>e.id+':'+e.name).join('|');
 if($('event').dataset.key!==eventKey){$('event').innerHTML=state.events.map(e=>`<option value="${escapeHtml(e.id)}">${escapeHtml(e.name)}</option>`).join('');$('event').dataset.key=eventKey;}
 if(document.activeElement!==$('threshold')){$('threshold').value=state.settings.threshold;$('threshold-value').textContent=Number(state.settings.threshold).toFixed(2);}
 $('liveness').checked=state.settings.requireLiveness;$('event').value=state.settings.eventId;
 if(!state.settings.running&&sending){sending=false;stopCamera('kiosk');}
 $('kiosk-start').disabled=state.settings.running || preview;$('kiosk-stop').disabled=!state.settings.running || preview;
 setRows('members-table',state.members.map(m=>`<tr><td>${escapeHtml(m.name)}</td><td>${escapeHtml(m.displayId)}</td><td><button data-action="dues" data-id="${escapeHtml(m.id)}" aria-label="Toggle dues for ${escapeHtml(m.name)}"><span class="pill ${m.paid?'green':'red'}">${m.paid?'Paid':'Unpaid'}</span></button></td><td>${m.eventsAttended}</td><td><span class="pill ${m.enrolled?'green':''}">${memberStatus(m).text}</span></td><td><div class="row-actions">${m.samples||m.consent?`<button data-action="delete" data-id="${escapeHtml(m.id)}">Delete face data</button>`:''}<button class="danger" data-action="remove" data-id="${escapeHtml(m.id)}" aria-label="Remove ${escapeHtml(m.name)}">Remove</button></div></td></tr>`).join(''));
 setRows('events-table',state.events.length?state.events.map(e=>`<tr><td>${escapeHtml(e.name)}</td><td>${escapeHtml(e.date)}</td><td>${escapeHtml(e.location||'—')}</td><td>${escapeHtml(e.points)}</td><td>${escapeHtml(audiences[e.audience]||e.audience)}</td><td>${escapeHtml(e.checkins??0)}</td><td><div class="row-actions">${(e.checkins??0)===0?`<button class="danger" data-action="delete-event" data-id="${escapeHtml(e.id)}" aria-label="Delete ${escapeHtml(e.name)}">Delete</button>`:''}</div></td></tr>`).join(''):'<tr><td colspan="7" class="muted">No events yet. Create one below.</td></tr>');
 $('recent').innerHTML=state.recent.length?state.recent.map(r=>{const i=resultInfo(r),time=new Date(r.createdAt*1000).toLocaleTimeString();return `<div class="log"><span style="color:${i.color}">${i.symbol}</span> ${escapeHtml(time)} · ${escapeHtml(r.member?.name || 'Unknown face')} · ${escapeHtml(i.title)} via ${r.method==='face'?'Face':'Member ID'}</div>`}).join(''):'<p>No check-ins yet.</p>';
 renderResult();
 renderEnroll();
}
async function refresh(){
 if(refreshing || preview)return;
 refreshing=true;
 try{
 const r=await fetch('/api/state',{cache:'no-store'});
 if(r.status===403){location.href='/pair.html';return;}
 if(!r.ok){let m='Server unavailable';try{m=(await r.json()).error||m}catch{}throw Error(m)}
 const next=await r.json();if(!Array.isArray(next.members)||!next.settings)throw Error('Local backend required');
 state=next;initialized=true;$('connection').textContent='● Connected · Face model: '+state.vision.status;render();
 // Phone check-in is off unless the server turns it on (PHONE_CHECKIN=on)
 const phoneOn=!!(state.access&&state.access.phoneCheckin),phoneOption=$('source').querySelector('option[value="phone"]');
 phoneOption.hidden=phoneOption.disabled=!phoneOn;if(!phoneOn&&$('source').value==='phone')$('source').value='computer';
 if(state.settings.running&&$('source').value==='phone'&&!$('hide-preview').checked&&location.hash==='#kiosk')await updateRemotePreview();
 }catch(e){
 if(!initialized){preview=true;state.members=demoMembers;state.events=[{id:'intro-ai',name:'Intro to Artificial Intelligence'},{id:'intro-github',name:'Intro to GitHub and GitHub Etiquette'}];$('connection').textContent='Can’t reach the check-in server ('+e.message+'). Showing a preview with sample members; see /api/health.';render();}
 else{$('connection').textContent='Connection lost. Retrying…';state.latestMatch={status:'camera_error',message:'Server connection lost'};renderResult();}
 }finally{refreshing=false;}
}
function route(){
 let hash=location.hash.slice(1);
 if(hash==='profile')hash='enroll'; // old links
 const view=['enroll','kiosk','admin'].includes(hash)?hash:new URLSearchParams(location.search).has('admin')?'admin':'enroll';
 for(const id of ['enroll','kiosk','admin'])$(id).hidden=id!==view;
 document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('active',a.hash==='#'+view));
 if(view!=='enroll')stopCamera('enroll');
 else if(enrollFlow.step==='capture'&&!camera.enroll)startCapture({keepBase:true});
}
async function startCamera(kind){
 if(!window.isSecureContext||!navigator.mediaDevices?.getUserMedia)throw Error('Camera access requires localhost on this computer, or HTTPS on a phone.');
 stopCamera(kind);
 const stream=await navigator.mediaDevices.getUserMedia({audio:false,video:{facingMode:'user',width:{ideal:640},height:{ideal:480}}});camera[kind]=stream;
 const v=$(kind+'-video');v.srcObject=stream;v.hidden=false;await v.play();
 if(kind==='enroll')$('enroll-placeholder').hidden=true;
 return stream;
}
function stopCamera(kind){
 if(camera[kind])camera[kind].getTracks().forEach(t=>t.stop());
 camera[kind]=null;const v=$(kind+'-video');v.srcObject=null;
 if(kind==='enroll'){$('enroll-placeholder').hidden=false;$('enroll-placeholder').textContent='Camera off';}else v.hidden=true;
}
async function getJpeg(video){
 if(video.readyState<2)throw Error('Camera is still starting. Try again.');
 const canvas=document.createElement('canvas'),scale=Math.min(640/video.videoWidth,480/video.videoHeight,1);canvas.width=Math.round(video.videoWidth*scale);canvas.height=Math.round(video.videoHeight*scale);canvas.getContext('2d').drawImage(video,0,0,canvas.width,canvas.height);
 const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',.65));if(!blob)throw Error('Unable to capture frame');return blob;
}
function toBase64(blob){return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=reject;reader.readAsDataURL(blob);});}

// ---------- Enroll: choose member → consent → photos → done ----------
const SHOTS = 5;
const PROMPTS = ['Look straight at the camera','Turn your head slightly to the left','Turn your head slightly to the right','Tilt your chin up a little','Tilt your chin down a little'];
const STEP_CARDS = {choose:'step-choose',consent:'step-consent',enrolled:'step-enrolled',capture:'step-capture',done:'step-done'};
const enrollFlow = {step:'choose',memberId:null,base:0,busy:false,firstPhoto:true,listKey:''};
function enrollMember(){return state.members.find(m=>m.id===enrollFlow.memberId);}
function memberStatus(m){return m.enrolled?{text:'Enrolled',cls:'green'}:m.samples?{text:`${Math.min(m.samples,SHOTS)} of ${SHOTS} photos`,cls:''}:{text:'Not enrolled',cls:''};}
function showStep(step,{focus=true}={}){
 enrollFlow.step=step;
 for(const [name,id] of Object.entries(STEP_CARDS))$(id).hidden=name!==step;
 const order=['choose','consent','photos'];
 const current={choose:'choose',consent:'consent',enrolled:'consent',capture:'photos',done:'done'}[step];
 order.forEach((s,i)=>{const li=$('step-label-'+s);li.classList.toggle('done',current==='done'||i<order.indexOf(current));if(s===current)li.setAttribute('aria-current','step');else li.removeAttribute('aria-current');});
 if(step!=='capture')stopCamera('enroll');
 renderEnroll();
 if(focus&&!$('enroll').hidden){if(step==='choose')$('member-search').focus();else $(STEP_CARDS[step]).querySelector('h2').focus();}
}
function renderResults(){
 if(!initialized&&!preview)return; // members haven't loaded yet
 const q=$('member-search').value.trim(),ql=q.toLowerCase();
 const matches=state.members.filter(m=>!ql||m.name.toLowerCase().includes(ql)||(m.displayId||'').toLowerCase().includes(ql));
 const exact=state.members.some(m=>m.name.toLowerCase()===ql);
 const key=JSON.stringify([q,matches.map(m=>[m.id,m.name,m.displayId,m.enrolled,m.samples])]);
 if(key===enrollFlow.listKey)return;
 enrollFlow.listKey=key;
 const rows=matches.map(m=>{const s=memberStatus(m);return `<button type="button" class="member-row" data-id="${escapeHtml(m.id)}"><span class="name">${escapeHtml(m.name)}</span><span class="code">${escapeHtml(m.displayId)}</span><span class="pill ${s.cls}">${s.text}</span></button>`;});
 if(q&&!exact)rows.push(`<button type="button" class="member-row add" data-add="1"><span class="name">Add “${escapeHtml(q)}” as a new member</span></button>`);
 $('member-results').innerHTML=rows.length?rows.join(''):(!q?'<p class="empty">No members yet. Type a name to add the first one.</p>':'');
}
function renderEnroll(){
 const m=enrollMember();
 if(enrollFlow.step!=='choose'&&!m){enrollFlow.memberId=null;showStep('choose',{focus:false});$('choose-message').textContent='That member was removed. Choose someone else.';return;}
 if(enrollFlow.step==='choose')renderResults();
 if(!m)return;
 const taken=Math.max(0,Math.min(m.samples-enrollFlow.base,SHOTS));
 $('consent-title').textContent=`Ask ${m.name} to read this`;
 $('enrolled-title').textContent=`${m.name} is already enrolled`;
 $('enrolled-text').textContent=`${m.samples} photo${m.samples===1?'':'s'} saved, so they can check in at the kiosk now. Take more photos if the kiosk has trouble recognizing them.`;
 $('capture-title').textContent=`Photos of ${m.name}`;
 $('shot-dots').innerHTML=Array.from({length:SHOTS},(_,i)=>`<li class="${i<taken?'taken':i===taken?'current':''}"></li>`).join('');
 $('shot-dots').setAttribute('aria-label',`${taken} of ${SHOTS} photos taken`);
 $('shot-prompt').textContent=taken<SHOTS?PROMPTS[taken]:'All photos taken';
 $('capture').disabled=preview||!camera.enroll||enrollFlow.busy;
 $('capture').textContent=enrollFlow.busy?'Saving…':'Take photo';
 $('done-title').textContent=`${m.name} is enrolled`;
}
function chooseMember(id){
 enrollFlow.memberId=id;const m=enrollMember();if(!m)return;
 for(const msg of ['consent-message','enrolled-message','capture-message'])$(msg).textContent='';
 if(!m.consent)showStep('consent');else if(m.samples>=SHOTS)showStep('enrolled');else startCapture();
}
function backToChoose(){
 enrollFlow.memberId=null;enrollFlow.listKey='';$('member-search').value='';
 for(const msg of ['choose-message','consent-message','enrolled-message','capture-message'])$(msg).textContent='';
 showStep('choose');
}
function cameraError(e){
 if(e?.name==='NotAllowedError')return 'Camera access is blocked. Allow the camera in your browser’s address bar, then try again.';
 if(e?.name==='NotFoundError')return 'No camera found. Connect a camera, then try again.';
 return e?.message||'The camera could not start.';
}
async function startCapture({keepBase=false}={}){
 const m=enrollMember();if(!m)return;
 if(!keepBase)enrollFlow.base=m.samples>=SHOTS?m.samples:0; // already enrolled: count 5 more
 enrollFlow.busy=false;$('camera-retry').hidden=true;
 showStep('capture');
 $('enroll-placeholder').textContent='Starting camera…';
 try{
  if(preview)throw Error('Taking photos needs the check-in server.');
  await startCamera('enroll');
  if(enrollFlow.step!=='capture'||$('enroll').hidden)stopCamera('enroll'); // left while the camera was starting
 }catch(e){$('enroll-placeholder').textContent='Camera off';$('capture-message').textContent=cameraError(e);$('camera-retry').hidden=preview;}
 renderEnroll();
}
async function takePhoto(){
 const m=enrollMember();if(!m||enrollFlow.busy||!camera.enroll)return;
 enrollFlow.busy=true;renderEnroll();
 $('capture-message').textContent=enrollFlow.firstPhoto?'Saving… The first photo can take up to 20 seconds while the face model loads.':'Saving…';
 try{
  const image=await toBase64(await getJpeg($('enroll-video')));
  const d=await request('/api/enroll',{memberId:m.id,image});
  enrollFlow.firstPhoto=false;
  await refresh();
  const taken=Math.min(d.samples-enrollFlow.base,SHOTS);
  if(taken>=SHOTS){enrollFlow.busy=false;showStep('done');return;}
  $('capture-message').textContent=`Photo ${taken} of ${SHOTS} saved.`;
 }catch(e){
  $('capture-message').textContent=e.message.startsWith('No face found')?'No face found. Move closer, face the light, and try again.':e.message;
 }finally{enrollFlow.busy=false;renderEnroll();}
}
$('member-search').oninput=()=>{$('choose-message').textContent='';renderResults();};
$('member-search').onkeydown=e=>{
 const rows=[...$('member-results').querySelectorAll('.member-row')];
 if(e.key==='Enter'){
  e.preventDefault();
  const members=rows.filter(r=>!r.dataset.add);
  if(members.length===1)members[0].click();          // exactly one match: pick it
  else if(!members.length&&rows.length)rows[0].click(); // no match: add the new member
  else rows[0]?.focus();                              // several: let them choose
 }
 if(e.key==='ArrowDown'&&rows.length){e.preventDefault();rows[0].focus();}
};
$('member-results').onkeydown=e=>{
 const rows=[...$('member-results').querySelectorAll('.member-row')],i=rows.indexOf(document.activeElement);
 if(e.key==='ArrowDown'&&i<rows.length-1){e.preventDefault();rows[i+1].focus();}
 if(e.key==='ArrowUp'){e.preventDefault();(i>0?rows[i-1]:$('member-search')).focus();}
};
$('member-results').onclick=async e=>{
 const row=e.target.closest('.member-row');if(!row)return;
 $('choose-message').textContent='';
 try{
  if(row.dataset.add){const m=await request('/api/members/add',{name:$('member-search').value.trim()});await refresh();chooseMember(m.id);}
  else chooseMember(row.dataset.id);
 }catch(err){$('choose-message').textContent=err.message;}
};
$('consent-agree').onclick=async()=>{
 const m=enrollMember();if(!m)return;
 $('consent-agree').disabled=true;
 try{await request('/api/members/consent',{memberId:m.id,consent:true});await refresh();if(enrollMember()?.samples>=SHOTS)showStep('enrolled');else await startCapture();}
 catch(e){$('consent-message').textContent=e.message;}
 finally{$('consent-agree').disabled=false;}
};
$('enrolled-more').onclick=()=>startCapture();
$('enrolled-delete').onclick=async()=>{
 const m=enrollMember();if(!m||!confirm(`Delete ${m.name}’s face data? They won’t be recognized at the kiosk until they enroll again. Their attendance history stays.`))return;
 try{await request('/api/faces/delete',{memberId:m.id});await refresh();backToChoose();$('choose-message').textContent=`Deleted ${m.name}’s face data.`;}
 catch(e){$('enrolled-message').textContent=e.message;}
};
for(const id of ['consent-cancel','enrolled-back','capture-back','done-another'])$(id).onclick=backToChoose;
$('camera-retry').onclick=()=>{$('capture-message').textContent='';startCapture({keepBase:true});};
$('capture').onclick=takePhoto;
document.addEventListener('keydown',e=>{
 if(e.code!=='Space'||enrollFlow.step!=='capture'||$('enroll').hidden)return;
 if(['INPUT','TEXTAREA','SELECT','BUTTON','A'].includes(document.activeElement?.tagName))return; // let those handle Space themselves
 e.preventDefault();takePhoto();
});

// ---------- Admin: members and events ----------
async function deleteFace(m){if(!confirm(`Delete ${m.name}’s face data? They won’t be recognized at the kiosk until they enroll again. Their attendance history stays.`))return;await request('/api/faces/delete',{memberId:m.id});}
$('members-table').onclick=async e=>{
 const button=e.target.closest('button[data-action]');if(!button)return;
 const m=state.members.find(m=>m.id===button.dataset.id);if(!m)return;
 try{
  if(button.dataset.action==='dues')await request('/api/members/dues',{memberId:m.id,paid:!m.paid});
  else if(button.dataset.action==='delete')await deleteFace(m);
  else if(button.dataset.action==='remove'){if(!confirm(`Remove ${m.name}? This deletes their face data and attendance history, and can’t be undone.`))return;await request('/api/members/remove',{memberId:m.id});}
  await refresh();
 }catch(e){$('admin-error').textContent=e.message;}
};
$('event-form').onsubmit=async e=>{
 e.preventDefault();
 const body={name:$('event-name').value,date:$('event-date').value,points:Number($('event-points').value),duesOnly:$('event-dues').checked};
 if($('event-time').value)body.time=$('event-time').value;
 if($('event-location').value.trim())body.location=$('event-location').value;
 try{const created=await request('/api/events/add',body);$('event-form').reset();$('event-message').textContent=`Created “${created.name}”. It’s now in the kiosk’s event list.`;await refresh();}
 catch(err){$('event-message').textContent=err.message;}
};
$('events-table').onclick=async e=>{
 const button=e.target.closest('button[data-action="delete-event"]');if(!button)return;
 const ev=state.events.find(x=>x.id===button.dataset.id);if(!ev||!confirm(`Delete “${ev.name}”?`))return;
 try{await request('/api/events/delete',{eventId:ev.id});$('event-message').textContent=`Deleted “${ev.name}”.`;await refresh();}
 catch(err){$('event-message').textContent=err.message;}
};

// ---------- Kiosk ----------
async function settings(p){await request('/api/settings',p);await refresh();}
$('threshold').oninput=()=>{$('threshold-value').textContent=Number($('threshold').value).toFixed(2);};
$('threshold').onchange=async()=>{try{await settings({threshold:Number($('threshold').value)});}catch(e){$('kiosk-message').textContent=e.message;}};
$('liveness').onchange=async()=>{try{await settings({requireLiveness:$('liveness').checked});}catch(e){$('kiosk-message').textContent=e.message;}};
$('event').onchange=async()=>{try{await settings({eventId:$('event').value});}catch(e){$('kiosk-message').textContent=e.message;}};
$('hide-preview').onchange=()=>$('kiosk-wrap').classList.toggle('preview-hidden',$('hide-preview').checked);
async function sendFrames(){
 if(!sending||!camera.kiosk)return;
 try{const blob=await getJpeg($('kiosk-video'));const r=await fetch('/api/camera',{method:'POST',headers:{'Content-Type':'image/jpeg'},body:blob});if(!r.ok)throw Error('Frame upload failed');}catch(e){$('kiosk-message').textContent=e.message;}
 if(sending)setTimeout(sendFrames,900);
}
async function updateRemotePreview(){
 try{const r=await fetch('/api/camera/frame',{cache:'no-store'});if(r.status===204){$('remote-preview').hidden=true;return;}if(!r.ok)return;const blob=await r.blob();if(remoteUrl)URL.revokeObjectURL(remoteUrl);remoteUrl=URL.createObjectURL(blob);$('remote-preview').src=remoteUrl;$('remote-preview').hidden=false;}catch{}
}
$('source').onchange=async()=>{if(state.settings.running)$('kiosk-stop').click();};
$('kiosk-start').onclick=async()=>{try{if(preview)throw Error('Recognition needs the check-in server.');if($('source').value==='computer'){await startCamera('kiosk');sending=true;sendFrames();}else{stopCamera('kiosk');$('kiosk-message').textContent='Start the iPhone camera on its paired attendee page.';}await settings({running:true});}catch(e){sending=false;stopCamera('kiosk');$('kiosk-message').textContent=e.message;}};
$('kiosk-stop').onclick=async()=>{sending=false;scanning=false;stopCamera('kiosk');$('remote-preview').hidden=true;try{await settings({running:false});$('kiosk-message').textContent='Kiosk stopped.';}catch(e){$('kiosk-message').textContent=e.message;}};
async function manualCheckin(id){const d=await request('/api/checkins',{memberId:id.trim().replace(/^member:/,''),eventId:state.settings.eventId});$('manual-message').textContent=(labels[d.status]||d.status)+(d.member?': '+d.member.name:'');await refresh();}
$('manual-form').onsubmit=async e=>{e.preventDefault();try{await manualCheckin($('manual-id').value);}catch(e){$('manual-message').textContent=e.message;}};
$('scan-qr').onclick=async()=>{try{if(preview)throw Error('QR check-in needs the check-in server.');if(!window.BarcodeDetector)throw Error('QR scanning is unavailable in this browser. Type the member ID instead.');const formats=await BarcodeDetector.getSupportedFormats();if(!formats.includes('qr_code'))throw Error('This browser does not support QR scanning. Type the member ID instead.');if(!camera.kiosk)await startCamera('kiosk');const detector=new BarcodeDetector({formats:['qr_code']});scanning=true;$('manual-message').textContent='Show a demo member-ID QR to the camera.';async function scan(){if(!scanning)return;try{const codes=await detector.detect($('kiosk-video'));if(codes.length){scanning=false;$('manual-id').value=codes[0].rawValue;await manualCheckin(codes[0].rawValue);if(!state.settings.running)stopCamera('kiosk');return;}}catch(e){scanning=false;$('manual-message').textContent=e.message;}if(scanning)setTimeout(scan,300);}scan();}catch(e){$('manual-message').textContent=e.message;}};
$('reset').onclick=async()=>{if(!confirm('Clear all recorded attendance and recent check-in results? Members and face data will remain.'))return;try{await request('/api/reset',{confirm:true});await refresh();}catch(e){$('admin-error').textContent=e.message;}};

window.addEventListener('hashchange',route);
window.addEventListener('pagehide',()=>{sending=false;scanning=false;stopCamera('enroll');stopCamera('kiosk');if(remoteUrl)URL.revokeObjectURL(remoteUrl);});
showStep('choose',{focus:false});route();refresh();setInterval(refresh,1000);
