import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {apply} from '../integrations/harness_budget.mjs';
const dir=await mkdtemp(join(tmpdir(),'budget-fixture-'));const tokenFile=join(dir,'token');await writeFile(tokenFile,'a'.repeat(64));
const commands=new Map(); const calls=[];
const data={conversation:{max_replies:200,reset_by_budget_reset:false},budgets:[{category:'generation',window_seconds:3600,used:49,limit:120,remaining:71}],revision:'b'.repeat(64),min_interval_seconds:30,paused:null,busy:false};
globalThis.fetch=async(url,options)=>{calls.push({url:String(url),...options});return {ok:true,json:async()=>data};};
try {
 apply({commands:{register:c=>commands.set(c.name,c)}},{tokenFile});
 const invocation=rawInput=>({rawInput,agent:{session:{id:'synthetic'}}});
 const status=(await commands.get('bridge-budget').handler(invocation(''))).text;
 assert.match(status,/49 \/ 120/);
 assert.match(status,/200 подтверждённых ответов/);
 assert.match(status,/этот лимит не сбрасывает/);
 await commands.get('bridge-budget-reset').handler(invocation('CONFIRM'));
 assert.equal(calls.length,1);
 await commands.get('bridge-budget-reset').handler(invocation(''));
 await commands.get('bridge-budget-reset').handler(invocation('CONFIRM'));
 assert.equal(calls.filter(c=>c.method==='POST').length,1);
 assert.equal(JSON.parse(calls.at(-1).body).revision,data.revision);
 await commands.get('bridge-budget-reset').handler(invocation('CONFIRM'));
 assert.equal(calls.length,3);
 console.log('Harness budget commands: read, preview, confirmation and no replay passed');
} finally {await rm(dir,{recursive:true,force:true});}
