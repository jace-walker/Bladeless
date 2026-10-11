'use strict';
const $ = id => document.getElementById(id);
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let preview = false, initialized = false, refreshing = false, captureBusy = false;
let state = {members:[],events:[],attendance:[],recent:[],settings:{threshold:.3,eventId:'intro-ai',running:false,requireLiveness:false},latestMatch:{status:'idle'},vision:{status:'Not loaded'}};
const demoMembers = [
 {id:'stuart',name:'Alex Rivera',displayId:'KH-0003',paid:true,consent:false,samples:0,enrolled:false,eventsAttended:6},
 {id:'jace',name:'Sam Chen',displayId:'KH-0001',paid:true,consent:false,samples:0,enrolled:false,eventsAttended:3},
 {id:'aaron',name:'Jordan Lee',displayId:'KH-0002',paid:false,consent:false,samples:0,enrolled:false,eventsAttended:1},
 {id:'jack',name:'Taylor Kim',displayId:'KH-6659',paid:false,consent:false,samples:0,enrolled:false,eventsAttended:7}
];
const labels = {idle:'Waiting for a face',checked_in:'Checked in',already_checked_in:'Already checked in',unknown:'Not recognized',dues_required:'Dues required',role_required:'Not eligible',not_enrolled:'Face check-in not enabled',member_not_found:'Member not found',liveness_failed:'Liveness check failed',camera_error:'Camera / model error'};
const camera = {profile:null,kiosk:null};
let sending = false, remoteUrl = null, profileUrl = null, scanning = false;
async function request(path, payload) {
 if(preview) throw Error('This is a preview without the check-in server.');
 const r = await fetch(path,{method:payload===undefined?'GET':'POST',headers:payload===undefined?{}:{'Content-Type':'application/json'},body:payload===undefined?undefined:JSON.stringify(payload),cache:'no-store'});
 if(r.status===403){location.href='/pair.html';throw Error('Pair this device again.');}
 const data = await r.json();
 if(!r.ok) throw Error(data.error || 'Request failed');
 return data;
}
function selectedMember(){return state.members.find(m=>m.id===$('member').value) || state.members[0];}
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
function render(){
 const old=$('member').value,memberKey=state.members.map(m=>m.id+':'+m.name).join('|');
 if($('member').dataset.key!==memberKey){$('member').innerHTML=state.members.map(m=>`<option value="${escapeHtml(m.id)}">${escapeHtml(m.name)}</option>`).join('');$('member').dataset.key=memberKey;if(state.members.some(m=>m.id===old))$('member').value=old;}
 const eventKey=state.events.map(e=>e.id).join('|');
 if($('event').dataset.key!==eventKey){$('event').innerHTML=state.events.map(e=>`<option value="${escapeHtml(e.id)}">${escapeHtml(e.name)}</option>`).join('');$('event').dataset.key=eventKey;}
 const m=selectedMember();
 if(m){$('consent').checked=m.consent;$('capture-count').textContent=`${Math.min(m.samples,5)}/5 captured${m.samples>5?' ('+m.samples+' total)':''}`;$('enrollment').textContent=m.enrolled?'Opted in':m.samples?`${m.samples}/5 captured`:'Not enrolled';$('enrollment').className='pill'+(m.enrolled?' green':'');$('profile-id').textContent=`Member ID: ${m.displayId}`;$('profile-dues').textContent=`Dues: ${m.paid?'Paid':'Unpaid'}`;$('profile-events').textContent=`Events attended: ${m.eventsAttended}`;$('capture').disabled=preview||(!camera.profile&&$('profile-source').value!=='phone')||!m.consent||captureBusy||m.samples>=20;$('profile-start').disabled=preview||!m.consent;}
 if(document.activeElement!==$('threshold')){$('threshold').value=state.settings.threshold;$('threshold-value').textContent=Number(state.settings.threshold).toFixed(2);}
 $('liveness').checked=state.settings.requireLiveness;$('event').value=state.settings.eventId;
 if(!state.settings.running&&sending){sending=false;stopCamera('kiosk');}
 $('kiosk-start').disabled=state.settings.running || preview;$('kiosk-stop').disabled=!state.settings.running || preview;
 $('members-table').innerHTML=state.members.map(m=>`<tr><td>${escapeHtml(m.name)}</td><td>${escapeHtml(m.displayId)}</td><td><button data-action="dues" data-id="${escapeHtml(m.id)}" aria-label="Toggle dues for ${escapeHtml(m.name)}"><span class="pill ${m.paid?'green':'red'}">${m.paid?'Paid':'Unpaid'}</span></button></td><td>${m.eventsAttended}</td><td><span class="pill ${m.enrolled?'green':''}">${m.enrolled?'Opted in':m.samples?m.samples+'/5 captured':'Not enrolled'}</span></td><td>${m.samples||m.consent?`<button data-action="delete" data-id="${escapeHtml(m.id)}">Delete face data</button>`:''}</td></tr>`).join('');
 $('recent').innerHTML=state.recent.length?state.recent.map(r=>{const i=resultInfo(r),time=new Date(r.createdAt*1000).toLocaleTimeString();return `<div class="log"><span style="color:${i.color}">${i.symbol}</span> ${escapeHtml(time)} · ${escapeHtml(r.member?.name || 'Unknown face')} · ${escapeHtml(i.title)} via ${r.method==='face'?'Face':'Member ID'}</div>`}).join(''):'<p>No check-ins yet.</p>';
 renderResult();
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
 if($('profile-source').value==='phone'&&location.hash!=='#admin'&&location.hash!=='#kiosk')await updateProfilePreview();
 if(state.settings.running&&$('source').value==='phone'&&!$('hide-preview').checked&&location.hash==='#kiosk')await updateRemotePreview();
 }catch(e){
 if(!initialized){preview=true;state.members=demoMembers;state.events=[{id:'intro-ai',name:'Intro to Artificial Intelligence'},{id:'intro-github',name:'Intro to GitHub and GitHub Etiquette'}];$('connection').textContent='Can’t reach the check-in server ('+e.message+'). Showing a preview with sample members; see /api/health.';render();}
 else{$('connection').textContent='Connection lost. Retrying…';state.latestMatch={status:'camera_error',message:'Server connection lost'};renderResult();}
 }finally{refreshing=false;}
}
function route(){
 const view=['profile','kiosk','admin'].includes(location.hash.slice(1))?location.hash.slice(1):new URLSearchParams(location.search).has('admin')?'admin':'profile';
 for(const id of ['profile','kiosk','admin'])$(id).hidden=id!==view;
 document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('active',a.hash==='#'+view));
 if(view!=='profile')stopCamera('profile');
}
async function startCamera(kind){
 if(!window.isSecureContext||!navigator.mediaDevices?.getUserMedia)throw Error('Camera access requires localhost on this computer, or HTTPS on a phone.');
 stopCamera(kind);
 const stream=await navigator.mediaDevices.getUserMedia({audio:false,video:{facingMode:'user',width:{ideal:640},height:{ideal:480}}});camera[kind]=stream;
 const v=$(kind+'-video');v.srcObject=stream;v.hidden=false;await v.play();
 if(kind==='profile')$('profile-placeholder').hidden=true;
 return stream;
}
function stopCamera(kind){if(kind==='profile')$('profile-remote').hidden=true;if(camera[kind])camera[kind].getTracks().forEach(t=>t.stop());camera[kind]=null;const v=$(kind+'-video');v.srcObject=null;if(kind==='profile')$('profile-placeholder').hidden=false;else v.hidden=true;}
async function getJpeg(video){
 if(video.readyState<2)throw Error('Camera is still starting. Try again.');
 const canvas=document.createElement('canvas'),scale=Math.min(640/video.videoWidth,480/video.videoHeight,1);canvas.width=Math.round(video.videoWidth*scale);canvas.height=Math.round(video.videoHeight*scale);canvas.getContext('2d').drawImage(video,0,0,canvas.width,canvas.height);
 const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',.65));if(!blob)throw Error('Unable to capture frame');return blob;
}
function toBase64(blob){return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=reject;reader.readAsDataURL(blob);});}
$('profile-start').onclick=async()=>{try{if(preview)throw Error('Capturing faces needs the check-in server.');if($('profile-source').value==='phone'){$('profile-message').textContent='Start the paired iPhone camera, then capture its current frame.';await updateProfilePreview();render();return;}await startCamera('profile');$('profile-message').textContent='Face the camera. Capture five shots, changing your angle slightly.';render();}catch(e){$('profile-message').textContent=e.message;}};
$('member').onchange=()=>{stopCamera('profile');$('profile-message').textContent='Enable the camera to capture samples for this member.';$('profile-feedback').textContent='';render();};
$('consent').onchange=async()=>{const consent=$('consent').checked;try{await request('/api/members/consent',{memberId:selectedMember().id,consent});if(!consent)stopCamera('profile');await refresh();if(consent)$('profile-start').click();}catch(e){$('profile-feedback').textContent=e.message;render();}};
$('capture').onclick=async()=>{if(captureBusy)return;const m=selectedMember();captureBusy=true;render();$('profile-message').textContent='Processing face… First capture may download and load the model.';try{let blob;if($('profile-source').value==='phone'){const r=await fetch('/api/camera/frame',{cache:'no-store'});if(r.status===204)throw Error('No recent iPhone frame. Start its camera and keep Safari visible.');if(!r.ok)throw Error('Unable to read the iPhone camera.');blob=await r.blob();}else blob=await getJpeg($('profile-video'));const image=await toBase64(blob);const d=await request('/api/enroll',{memberId:m.id,image});$('profile-message').textContent=d.enrolled?'Enrollment ready. You can now use the kiosk.':`Captured ${d.samples}/5. Turn slightly for the next shot.`;await refresh();}catch(e){$('profile-message').textContent=e.message;}finally{captureBusy=false;render();}};
$('add-form').onsubmit=async e=>{e.preventDefault();try{const member=await request('/api/members/add',{name:$('new-member').value});await refresh();$('member').value=member.id;$('new-member').value='';$('member').onchange();}catch(e){$('profile-feedback').textContent=e.message;}};
async function deleteFace(id){if(!confirm('Delete this member’s saved face embeddings and opt-in? Attendance will remain.'))return;await request('/api/faces/delete',{memberId:id});stopCamera('profile');await refresh();}
$('delete-my-face').onclick=async()=>{try{await deleteFace(selectedMember().id);}catch(e){$('profile-feedback').textContent=e.message;}};
$('members-table').onclick=async e=>{const button=e.target.closest('button[data-action]');if(!button)return;try{const m=state.members.find(m=>m.id===button.dataset.id);if(button.dataset.action==='dues')await request('/api/members/dues',{memberId:m.id,paid:!m.paid});else await deleteFace(m.id);await refresh();}catch(e){$('admin-error').textContent=e.message;}};
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
$('profile-source').onchange=()=>{stopCamera('profile');render();};
async function updateProfilePreview(){try{const r=await fetch('/api/camera/frame',{cache:'no-store'});if(r.status===204){$('profile-remote').hidden=true;$('profile-placeholder').hidden=false;return;}if(!r.ok)return;const blob=await r.blob();if(profileUrl)URL.revokeObjectURL(profileUrl);profileUrl=URL.createObjectURL(blob);$('profile-remote').src=profileUrl;$('profile-remote').hidden=false;$('profile-video').hidden=true;$('profile-placeholder').hidden=true;}catch{}}
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
window.addEventListener('hashchange',route);window.addEventListener('pagehide',()=>{sending=false;scanning=false;stopCamera('profile');stopCamera('kiosk');if(remoteUrl)URL.revokeObjectURL(remoteUrl);if(profileUrl)URL.revokeObjectURL(profileUrl);});
route();refresh();setInterval(refresh,1000);
