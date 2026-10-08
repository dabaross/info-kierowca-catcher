'use strict';
const $ = id => document.getElementById(id);
const days = ['Pn','Wt','Śr','Cz','Pt','Sb','Nd'];
let state, loaded = false, dirty = false, profileSignature = '', reg, pushKey, timer, polling = false;
const fmt = (value, options={}) => value ? new Date(typeof value === 'number' ? value*1000 : value).toLocaleString('pl-PL',{timeZone:'Europe/Warsaw',...options}) : '—';
const clock = value => fmt(value,{hour:'2-digit',minute:'2-digit'});
function el(tag, text, cls) { const node=document.createElement(tag); if(text!==undefined)node.textContent=text; if(cls)node.className=cls; return node; }
function toast(message) { $('toast').textContent=message; $('toast').hidden=false; clearTimeout(timer); timer=setTimeout(()=>$('toast').hidden=true,6500); }
function markDirty() { dirty=true; $('dirty').hidden=false; }
async function api(path, method='GET', data) {
  const response=await fetch('/api/'+path,{method,credentials:'same-origin',cache:'no-store',headers:method==='GET'?{}:{'Content-Type':'application/json','X-Catcher-Action':'1'},body:data===undefined?undefined:JSON.stringify(data)});
  let result; try { result=await response.json(); } catch (_) { result={}; }
  if(response.status===401 && path!=='auth/login') showLogin();
  if(!response.ok) throw new Error(typeof result.detail==='string'?result.detail:`Błąd serwera (${response.status}).`);
  return result;
}
function showLogin() { $('loginPanel').hidden=false; $('dashboard').hidden=true; $('logout').hidden=true; loaded=false; }
function action(id, handler) { $(id).addEventListener('click',async event=>{ event.preventDefault(); const button=$(id); button.disabled=true; try { await handler(); } catch(e) { toast(e.message); } finally { button.disabled=false; } }); }
function addRange(value) {
  if($('ranges').children.length>=10) return toast('Możesz dodać do 10 przedziałów.');
  const fallback=state?.monitor.config.ranges[0];
  value=value||fallback;
  const box=el('fieldset',undefined,'range');
  const head=el('div',undefined,'range-head'); head.append(el('span','Przedział '+($('ranges').children.length+1)));
  const remove=el('button','Usuń','quiet'); remove.type='button'; remove.addEventListener('click',()=>{ if($('ranges').children.length===1)return toast('Pozostaw przynajmniej jeden przedział.');box.remove();renumber();markDirty(); }); head.append(remove);box.append(head);
  const fields=el('div',undefined,'fields');
  for(const [key,label,type] of [['date_from','Od dnia','date'],['date_to','Do dnia','date'],['time_from','Od godziny','time'],['time_to','Do godziny','time']]) {
    const wrap=el('label',label),input=el('input');input.type=type;input.name=key;input.required=true;input.value=type==='time'?value[key].slice(0,5):value[key];wrap.append(input);fields.append(wrap);
  }
  box.append(fields);
  const week=el('div',undefined,'weekdays');
  days.forEach((name,i)=>{const label=el('label'), input=el('input');input.type='checkbox';input.name='weekday';input.value=i;input.checked=value.weekdays.includes(i);label.append(input,el('span',name));week.append(label);});
  box.append(week);$('ranges').append(box);
}
function renumber(){[...$('ranges').children].forEach((box,i)=>box.querySelector('.range-head span').textContent='Przedział '+(i+1));}
function readConfig(){
  return {center_id:Number($('center').value),profile_id:$('profile').value,interval_seconds:Number($('interval').value),ranges:[...$('ranges').children].map(box=>{
    const value={};for(const key of ['date_from','date_to','time_from','time_to'])value[key]=box.querySelector(`[name=${key}]`).value;
    value.weekdays=[...box.querySelectorAll('[name=weekday]:checked')].map(i=>Number(i.value));return value;
  })};
}
async function save(){
  if(!$('configForm').reportValidity())throw new Error('Uzupełnij daty i godziny.');
  if(Number($('center').value)!==43)throw new Error('W tej wersji wybierz PORD Gdańsk, aby zapisać konfigurację.');
  const config=await api('config','PUT',readConfig());dirty=false;$('dirty').hidden=true;state.monitor.config=config;toast('Preferencje zapisane.');
}
const statusLabels={PAUSED:'Wstrzymany',NEEDS_LOGIN:'Czeka na login',NEEDS_PROFILE:'Wybierz profil',NEEDS_CENTER:'Wybierz PORD Gdańsk',WAITING:'Oczekuje',CHECKING:'Sprawdzamy…',WATCHING:'Aktywny',FINISHED:'Zakończony',RATE_LIMITED:'Limit zapytań',NETWORK:'Brak odpowiedzi',UPSTREAM:'Błąd portalu',HTTP_400:'HTTP 400 · próby wstrzymane',SCHEMA:'Zmiana API',ERROR:'Błąd monitora'};
async function refresh(){
  if(polling)return;polling=true;
  try {
    const data=await api('status');state=data;
    if(!loaded){
      const centers=await api('centers'),placeholder=el('option','Wybierz ośrodek');placeholder.value='';placeholder.disabled=true;
      $('center').replaceChildren(placeholder,...centers.map(c=>{const option=el('option',c.name);option.value=c.id;return option;}));
      $('center').value=String(data.monitor.config.center_id);
      if($('center').value!=='43'){$('center').value='';$('centerNotice').textContent='Zapisana konfiguracja wskazuje inny ośrodek. Ustaw PORD Gdańsk i zapisz, aby wznowić monitoring; dotychczasowe ustawienia i dane pozostają zachowane.';$('centerNotice').hidden=false;}
      else {$('centerNotice').hidden=true;}
      $('center').addEventListener('change',()=>{if(Number($('center').value)===43)$('centerNotice').hidden=true;});
      $('center').addEventListener('change',()=>{if(Number($('center').value)===43)$('centerNotice').hidden=true;});
      $('interval').value=data.monitor.config.interval_seconds;
      $('ranges').replaceChildren();data.monitor.config.ranges.forEach(addRange);dirty=false;$('dirty').hidden=true;profileSignature='';loaded=true;
    }
    $('loginPanel').hidden=true;$('dashboard').hidden=false;$('logout').hidden=false;$('connection').hidden=true;
    const {session:s,monitor:m,login:l,push:p}=data;
    $('sessionStatus').textContent=s.active?'Aktywna':'Potrzebny login';
    $('sessionBadge').textContent=s.active?'Połączono':'Wymaga logowania';
    $('sessionAge').textContent=s.active?`Od logowania: ${Math.max(0,Math.floor((Date.now()/1000-s.connected_at)/60))} min`:'Potwierdź na telefonie';
    $('sessionMessage').textContent=s.message;
    $('loginStart').textContent=s.active?'Odśwież sesję':'Zaloguj przez mObywatela';
    $('disconnect').hidden=!s.active;
    const pending=l&&['STARTING','OPENING_LOGIN','FINDING_APP_LINK','WAITING_FOR_CONFIRMATION','VERIFYING'].includes(l.state);
    $('loginStart').disabled=!!pending;$('cancelLogin').hidden=!pending;
    $('loginAttempt').hidden=!l;
    if(l){$('attemptMessage').textContent=l.message;$('handoff').hidden=!l.handoff_url;if(l.handoff_url && l.handoff_url.startsWith('mobywatel:'))$('handoff').href=l.handoff_url;else $('handoff').removeAttribute('href');}
    const signature=JSON.stringify(s.profiles);
    if(signature!==profileSignature){
      const selection=$('profile').value||m.config.profile_id;
      $('profile').replaceChildren(...s.profiles.map(p=>{const opt=el('option',p.label);opt.value=p.id;return opt;}));
      if(!s.profiles.length){const opt=el('option',s.active?'Brak obsługiwanych profili PKK':'Najpierw połącz konto');opt.value=m.config.profile_id||'';$('profile').append(opt);}
      else if(s.profiles.some(p=>p.id===selection))$('profile').value=selection;
      profileSignature=signature;
    }
    $('monitorStatus').textContent=statusLabels[m.state]||m.state;
    $('nextCheck').textContent=m.state==='HTTP_400'?'Automatyczne próby wstrzymane':m.state==='NEEDS_LOGIN'?'Wymaga ponownego logowania':m.state==='NEEDS_PROFILE'?'Wymaga wyboru profilu PKK':m.state==='NEEDS_CENTER'?'Wymaga wyboru PORD Gdańsk':m.enabled&&s.active?`Kolejna próba: ${m.next_check>Date.now()/1000?clock(m.next_check):'wkrótce'}`:'Ustawienia zapisane na serwerze';
    $('slotCount').textContent=m.slots.length;
    $('lastCheck').textContent=m.last_check?'Ostatni udany odczyt: '+clock(m.last_check):'Brak zarejestrowanego udanego odczytu';
    $('monitorMessage').textContent=m.message;
    $('monitorToggle').textContent=m.enabled?'Wstrzymaj monitoring':'Uruchom monitoring';
    const slots=m.slots.map(slot=>{const card=el('article',undefined,'slot'),left=el('div');left.append(el('strong',fmt(slot.start,{day:'numeric',month:'long',weekday:'short'})),el('small',`Miejsca: ${slot.places} · odczyt ${clock(slot.checked_at)}`));const t=el('time',clock(slot.start));t.dateTime=slot.start;card.append(left,t);return card;});
    if(!slots.length){const empty=el('div',undefined,'empty'),emptyMessage=m.state==='HTTP_400'?'Odczyt nie powiódł się (HTTP 400); wyników nie zaktualizowano.':m.last_check?'Brak pasujących terminów w odczytanych oknach.':'Terminy pojawią się po pierwszym odczycie.';empty.append(el('span','⌁'),el('div',emptyMessage));slots.push(empty);}
    $('slots').replaceChildren(...slots);
    $('coverage').textContent=`Odczytane okna: ${m.windows.length}/${m.windows_total}. `+m.windows.map(w=>`${w.from}–${w.to} (${clock(w.at)})`).join(' · ');
    $('events').replaceChildren(...data.events.slice(0,15).map(e=>{const li=el('li');li.append(el('time',fmt(e.at,{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})),el('span',e.message));return li;}));
    $('diagnostics').textContent=JSON.stringify({monitor:m.state,http:m.http_status,validation:m.diagnostic||null,last_successful_schedule_at:m.last_check,next_schedule_attempt_at:m.next_check||null,request_policy:m.request_policy,last_verified:s.last_verified,login:l?.diagnostics||{},push:p.last_result},null,2);
    pushKey=p.public_key;
    $('enablePush').disabled=!reg||!('PushManager' in window);
    $('pushState').textContent=(!('PushManager' in window)?'Ta przeglądarka nie udostępnia Web Push. Na iPhonie otwórz aplikację z ekranu początkowego. ': '')+`Zapisane urządzenia: ${p.devices}.`+(p.last_result?` Ostatnia wysyłka: ${p.last_result.accepted?'przyjęta przez serwer push':'nieudana'}.`:'');
  }catch(e){if(!$('dashboard').hidden)$('connection').hidden=false;}finally{polling=false;}
}
$('authForm').addEventListener('submit',async e=>{e.preventDefault();$('authError').textContent='';try{await api('auth/login','POST',{password:$('password').value});$('password').value='';await refresh();}catch(error){$('authError').textContent=error.message;}});
$('configForm').addEventListener('input',markDirty);
$('configForm').addEventListener('submit',async e=>{e.preventDefault();try{await save();await refresh();}catch(error){toast(error.message);}});
action('addRange',()=>{addRange();markDirty();});
action('logout',async()=>{await api('auth/logout','POST');showLogin();});
action('loginStart',async()=>{await api('login/start','POST');await refresh();});
action('cancelLogin',async()=>{await api('login/cancel','POST');await refresh();});
action('disconnect',async()=>{if(confirm('Rozłączyć sesję Info-Kierowca i wstrzymać monitoring?')){await api('session/disconnect','POST');await refresh();}});
action('monitorToggle',async()=>{if(state.monitor.enabled)await api('monitor/stop','POST');else{await save();await api('monitor/start','POST');}await refresh();});
action('testPush',async()=>{await api('push/test','POST');toast('Test jest w kolejce. Sprawdź powiadomienie na telefonie.');});
action('enablePush',async()=>{
  if(!reg||!pushKey)throw new Error('Poczekaj na przygotowanie powiadomień.');
  const raw=atob(pushKey.replace(/-/g,'+').replace(/_/g,'/'));
  // subscribe is invoked directly during the button gesture, before any network await.
  const sub=await reg.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:Uint8Array.from(raw,c=>c.charCodeAt(0))});
  await api('push/subscribe','POST',sub.toJSON());$('disablePush').hidden=false;toast('Powiadomienia włączone. Wyślij test.');await refresh();
});
action('disablePush',async()=>{const sub=await reg?.pushManager.getSubscription();if(sub){await api('push/unsubscribe','POST',{endpoint:sub.endpoint});await sub.unsubscribe();}$('disablePush').hidden=true;toast('Powiadomienia wyłączone na tym urządzeniu.');await refresh();});
if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js').then(()=>navigator.serviceWorker.ready).then(async r=>{reg=r;if('PushManager' in window)$('disablePush').hidden=!(await reg.pushManager.getSubscription());}).catch(()=>{});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
refresh();setInterval(()=>{if(!document.hidden&&!$('dashboard').hidden)refresh();},4000);
