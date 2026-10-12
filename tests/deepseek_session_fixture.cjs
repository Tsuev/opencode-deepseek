const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const source=fs.readFileSync(require('node:path').join(__dirname,'../browser/deepseek-session-transfer.user.js'),'utf8');
async function run({statuses=['saved'],hash=true,confirm=true}={}) {
  const storage=new Map(), session=new Map(), requests=[], labels=[];let tick;
  let token='a'.repeat(64);
  function element(){return {style:{},isConnected:false,append(){},attachShadow(){return element();},set textContent(v){labels.push(v);}};}
  const window={confirm:()=>confirm,addEventListener(){}};window.top=window;
  const context={window,location:{origin:'https://chat.deepseek.com',pathname:'/',search:'',hash:hash?'#bridge_session='+'x'.repeat(43)+'&expires='+(Date.now()+600000):''},
    history:{replaceState(){}},URLSearchParams,Date,TextEncoder,crypto:webcrypto,
    document:{cookie:'ds_cookie_preference=all',createElement:element,documentElement:{append(){}}},navigator:{userAgent:'Safari fixture'},
    localStorage:{getItem:key=>JSON.stringify({value:key==='userToken'?token:{id:'same-account'}})},
    sessionStorage:{getItem:k=>session.get(k),setItem:(k,v)=>session.set(k,v),removeItem:k=>session.delete(k)},
    setInterval:f=>{tick=f;},GM:{getValue:async k=>storage.get(k),setValue:async(k,v)=>storage.set(k,structuredClone(v)),deleteValue:async k=>storage.delete(k),
      xmlHttpRequest:async r=>{requests.push(r);assert.equal(r.url,'http://127.0.0.1:8000/session/deepseek/capture');return {status:200,responseText:JSON.stringify({status:statuses.shift()})};}}};
  await vm.runInNewContext(source,context);
  return {requests,storage,labels,tick,changeToken(){token='b'.repeat(64);}};
}
(async()=>{
 let x=await run({hash:false});await x.tick();assert.equal(x.requests.length,0);
 x=await run();assert.equal(x.requests.length,1);await x.tick();assert.equal(x.requests.length,1);assert.equal(x.storage.size,0);
 x=await run({statuses:['confirmation_required','saved']});assert.equal(x.requests.length,2);assert.equal(JSON.parse(x.requests[1].data).same_account,true);
 x=await run({statuses:['confirmation_required'],confirm:false});assert.equal(x.requests.length,1);assert.equal(x.storage.size,0);
 x=await run({statuses:['new_login_required','saved']});await x.tick();assert.equal(x.requests.length,1);x.changeToken();await x.tick();assert.equal(x.requests.length,2);
 assert(x.labels.some(v=>v.includes('Цель в Harness не запускалась')));
 console.log('Safari transfer: unarmed page inert; one-use capture; explicit first-account confirmation; waits locally for changed token; no model requests');
})().catch(e=>{console.error(e);process.exitCode=1;});
