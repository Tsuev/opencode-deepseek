// Run the actual controller's stall path against stale visible DOM, without sockets.
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const path = require('node:path');
const source = fs.readFileSync(process.argv[2] || path.join(__dirname, '../browser/opencode-controller.user.js'), 'utf8');
let section = source.slice(source.indexOf('    // Sites flagged stalls'), source.indexOf('  async function poll()'));
// Old versions defined DOM finishers here. Hoist them only in this fixture to
// reproduce their real runtime availability, then execute the same stall loop.
const helperAt = section.indexOf('  const geminiHtmlToMarkdown');
const helpers = helperAt < 0 ? '' : section.slice(helperAt);
section = helperAt < 0 ? section : section.slice(0, helperAt);
section = section.slice(0, section.lastIndexOf('\n  }'));
(async () => {
  for (const provider of ['mistral', 'claude']) {
    let clock = 0, finished = 0;
    const context = vm.createContext({
      site: {provider, stalls: true, path: /^\/chat\//}, dispatching: true,
      pending: {id: 'owned-job', sawCapture: true}, job: {id: 'owned-job', prompt: 'NEW_PROMPT'},
      deadline: 30000, performance: {now: () => (clock += 1000)},
      setTimeout: fn => {fn();}, TextEncoder, btoa,
      location: {pathname: '/chat/owned'},
      document: {querySelector: () => null, querySelectorAll: () => [{
        innerText: 'PREVIOUS_TOOL_CALL', closest: () => null, getAttribute: () => 'false',
      }]},
      finish: async () => {finished++;}, BridgeFailure: Error,
    });
    await assert.rejects(vm.runInContext(`(async () => { ${helpers}\n${section} })()`, context), /observation stalled/);
    assert.equal(finished, 0, 'old DOM must not finish a new request');
  }
  console.log('stale DOM cannot fabricate a terminal completion');
})().catch(error => {console.error(error); process.exitCode = 1;});
