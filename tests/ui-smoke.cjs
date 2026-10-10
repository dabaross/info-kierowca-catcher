// Optional DOM integration check: npm install --no-save jsdom; node tests/ui-smoke.cjs
const {JSDOM}=require(process.env.JSDOM_PATH||'jsdom');
const fs=require('node:fs');const assert=require('node:assert/strict');const path=require('node:path');
const root=path.resolve(__dirname,'../poc/static');
const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'https://panel.example/',runScripts:'outside-only',pretendToBeVisual:true});
const w=dom.window;let signed=false, offline=false, saved;
const range={date_from:'2026-10-12',date_to:'2026-10-16',time_from:'07:00',time_to:'09:00',weekdays:[0,1,2,3,4]};
const state={session:{active:true,generation:1,connected_at:Date.now()/1000,message:'Sesja potwierdzona.',profiles:[{id:'profile',label:'Kategoria B · PKK …1234'}]},login:null,
  monitor:{config:{center_id:43,profile_id:'profile',ranges:[range],interval_seconds:1200},enabled:false,state:'PAUSED',slots:[],calendar:null,next_check:0,message:'Wstrzymany',last_check:null,http_status:null},events:[],push:{public_key:'abc',devices:0}};
w.fetch=async(url,opts)=>{
 if(offline)throw new Error('offline');
 let status=200,data={ok:true};
 if(url.endsWith('auth/login'))signed=true;
 else if(!signed){status=401;data={detail:'Zaloguj się'};}
 else if(url.endsWith('centers'))data=[{id:43,name:'PORD Gdańsk'}];
 else if(url.endsWith('config')){saved=JSON.parse(opts.body);state.monitor.config=saved;data=saved;}
 else if(url.endsWith('monitor/start')){state.monitor.enabled=true;state.monitor.state='WATCHING';}
 else if(url.endsWith('status'))data=state;
 return {ok:status===200,status,json:async()=>structuredClone(data)};
};
w.confirm=()=>true;
const pause=()=>new Promise(r=>setTimeout(r,20));
async function main(){
 w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8')+'\nwindow.testRefresh=refresh;');await pause();
 assert.equal(w.document.getElementById('loginPanel').hidden,false);
 w.document.getElementById('password').value='testpassword';
 w.document.getElementById('authForm').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await pause();
 assert.equal(w.document.getElementById('dashboard').hidden,false);
 assert.deepEqual([...w.document.getElementById('interval').options].map(o=>Number(o.value)),[900,1200,1800,3600]);
 assert.equal(w.document.getElementById('interval').value,'1200');
 assert.equal(w.document.querySelectorAll('.range').length,1);
 w.document.getElementById('addRange').click();await pause();
 const ranges=w.document.querySelectorAll('.range');assert.equal(ranges.length,2);
 ranges[1].querySelector('[name=time_from]').value='16:00';ranges[1].querySelector('[name=time_to]').value='18:00';
 w.document.getElementById('configForm').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await pause();
 assert.equal(saved.ranges.length,2);assert.equal(saved.ranges[1].time_from,'16:00');assert.equal(saved.profile_id,'profile');
 w.document.getElementById('monitorToggle').click();await pause();assert.equal(state.monitor.enabled,true);
 ranges[1].querySelector('.range-head button').click();await pause();assert.equal(w.document.querySelectorAll('.range').length,1);
 assert.equal(w.document.getElementById('dirty').hidden,false);
 await w.testRefresh();assert.equal(w.document.querySelectorAll('.range').length,1); // polling must preserve edits
 state.monitor.state='HTTP_400';state.monitor.http_status=400;state.monitor.message='HTTP 400. Próby wstrzymane.';
 await w.testRefresh();
 assert.match(w.document.getElementById('monitorStatus').textContent,/HTTP 400/);
 assert.match(w.document.getElementById('nextCheck').textContent,/wstrzymane/i);
 assert.match(w.document.getElementById('slots').textContent,/HTTP 400/);
 offline=true;await w.testRefresh();assert.equal(w.document.getElementById('connection').hidden,false);
 console.log('UI DOM: login, interval choices/default, multiple ranges, save, start, removal, unsaved edits and offline state: PASS');
 dom.window.close();
}
main().catch(e=>{console.error(e);dom.window.close();process.exitCode=1;});
