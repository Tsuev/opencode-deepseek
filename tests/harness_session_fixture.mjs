import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {apply,openSessionSafari} from '../integrations/harness_budget.mjs';
const dir=await mkdtemp(join(tmpdir(),'session-command-'));
try {
  const tokenFile=join(dir,'token');await writeFile(tokenFile,'a'.repeat(64));
  const commands=new Map(), calls=[], opened=[];
  let fails=false;
  globalThis.fetch=async(url,options)=>{
    calls.push({url:String(url),options});
    return {ok:true,json:async()=>String(url).endsWith('/start') ? {nonce:'a'.repeat(43),expires_in:600} : {status:'saved'}};
  };
  apply({commands:{register:c=>commands.set(c.name,c)}},{tokenFile},
    {openSafari:async(data,url)=>{if(fails)throw Error('offline open failure');opened.push({data,url});}});
  assert.equal((await commands.get('deepseek-login').handler({})).kind,'success');
  assert.equal(calls.length,1);assert.equal(opened.length,1);
  assert.match(calls[0].url,/\/admin\/session\/deepseek\/start$/);
  assert.match((await commands.get('deepseek-login-status').handler({})).text,/Цель не возобновлялась/);
  fails=true;
  assert.equal((await commands.get('deepseek-login').handler({})).kind,'error');
  assert.match(calls.at(-1).url,/\/cancel$/);
  const processCalls=[];
  await openSessionSafari({nonce:'b'.repeat(43),expires_in:600},'http://127.0.0.1:8000/',async(...args)=>processCalls.push(args));
  assert.equal(processCalls[0][0],'/usr/bin/open');
  assert.deepEqual(processCalls[0][1].slice(0,2),['-a','Safari']);
  assert.match(processCalls[0][1][2],/^https:\/\/chat\.deepseek\.com\/#bridge_session=/);
  await assert.rejects(openSessionSafari({nonce:'unsafe'},'http://127.0.0.1:8000/',()=>{throw Error('must not run');}));
  assert(calls.every(x=>x.url.startsWith('http://127.0.0.1:8000/admin/session/deepseek/')));
  console.log('Harness session commands: local start/status/cancel; Safari opener mocked; no model or goal dispatch');
} finally {await rm(dir,{recursive:true,force:true});}
