const $ = id => document.getElementById(id);
const titles = {IDLE:'Gotowy do próby',STARTING:'Uruchamianie',OPENING_LOGIN:'Otwieranie logowania',FINDING_APP_LINK:'Przygotowanie potwierdzenia',WAITING_FOR_CONFIRMATION:'Potwierdź w mObywatelu',VERIFYING:'Weryfikacja sesji',VERIFIED:'Test zakończony powodzeniem',BROWSER_ONLY:'Częściowy wynik',UNVERIFIED:'Sesja niepotwierdzona',ERROR:'Próba nieudana',EXPIRED:'Czas próby minął',STOPPED:'Próba zakończona'};
const terminal = new Set(['IDLE','VERIFIED','BROWSER_ONLY','UNVERIFIED','ERROR','EXPIRED','STOPPED']);
function draw(s) {
  $('status-title').textContent = titles[s.state] || s.state;
  $('message').textContent = s.message;
  $('dot').className = s.state === 'VERIFIED' ? 'success' : terminal.has(s.state) ? '' : 'working';
  $('start').hidden = !terminal.has(s.state);
  $('stop').hidden = ['IDLE','STOPPED'].includes(s.state);
  $('open-app').hidden = !s.handoff_url;
  $('return-note').hidden = !s.handoff_url;
  if(s.handoff_url) $('open-app').href = s.handoff_url; else $('open-app').removeAttribute('href');
  $('browser-check').textContent = s.browser_verified ? 'Potwierdzono' : 'Nie sprawdzono';
  $('http-check').textContent = s.http_verified ? 'Potwierdzono' : 'Nie sprawdzono';
  $('verified-time').textContent = s.verified_at ? 'Sprawdzono: ' + new Date(s.verified_at).toLocaleString('pl-PL') + '. To wynik tej próby.' : 'Sukces pojawi się dopiero po odczycie API.';
  $('diagnostics').textContent = JSON.stringify(s.diagnostics || {}, null, 2);
}
async function update(){try{const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw Error();draw(await r.json())}catch{$('message').textContent='Brak połączenia z panelem. Odśwież stronę, jeśli problem się utrzymuje.'}}
async function action(path){$('start').disabled=true;try{const r=await fetch(path,{method:'POST',headers:{'X-Poc-Action':'1'}});if(!r.ok)throw Error();await update()}catch{$('message').textContent='Nie udało się wykonać polecenia. Sprawdź połączenie i adres panelu.'}finally{$('start').disabled=false}}
$('start').onclick=()=>action('/api/start');$('stop').onclick=()=>action('/api/stop');
document.addEventListener('visibilitychange',()=>{if(!document.hidden)update()});
update();setInterval(()=>{if(!document.hidden)update()},2000);
