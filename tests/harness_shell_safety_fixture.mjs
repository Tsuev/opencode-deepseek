import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, statSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { hardenSpec, failedResult, apply } from '../integrations/harness_shell_safety.mjs';

const root = mkdtempSync(join(tmpdir(), 'shell-safety-test-'));
try {
  function run(command) {
    const original = {command, workdir: root, env: {ORIGINAL: 'preserved'}, sandboxPolicy: {mode: 'workspace-write'}};
    const spec = hardenSpec(original, root);
    assert.equal(original.command, command);
    assert.equal(spec.sandboxPolicy, original.sandboxPolicy);
    return spawnSync('bash', ['-c', spec.command], {cwd: spec.workdir,
      env: {...process.env, ...spec.env}, encoding: 'utf8'});
  }
  const fakeGh = join(root, 'gh');
  const payload = JSON.stringify([{name: 'CI', bucket: 'fail', state: 'FAILURE', link: '', workflow: ''}]);
  writeFileSync(fakeGh, '#!/bin/bash\nprintf \'%s\\n\' \'' + payload + '\'\nexit 1\n', {mode: 0o700});
  const oldPath = process.env.PATH;
  process.env.PATH = root + ':' + oldPath;
  let check = run('gh pr checks 69 2>&1 | sed -n \'1,40p\'; printf CONTINUED');
  assert.equal(check.status, 0, check.stderr);
  assert.match(check.stdout, /"bucket": "fail"/);
  assert.match(check.stdout, /CONTINUED/);
  assert.equal(run('gh pr checks 69 | (exit 17); printf UNREACHABLE').status, 17);
  const pending = JSON.stringify([{name: 'CI', bucket: 'pending', state: 'IN_PROGRESS', link: '', workflow: ''}]);
  writeFileSync(fakeGh, '#!/bin/bash\nprintf \'%s\\n\' \'' + pending + '\'\nexit 0\n');
  check = run('gh pr checks 80 2>&1 | sed -n \'1,40p\'; printf CONTINUED');
  assert.equal(check.status, 0, check.stderr);
  assert.match(check.stdout, /"bucket": "pending"/);
  assert.match(check.stdout, /CONTINUED/);
  assert.equal(run('gh pr checks 80 | (exit 17); printf UNREACHABLE').status, 17);
  writeFileSync(fakeGh, '#!/bin/bash\nprintf "auth failed\\n" >&2\nexit 1\n');
  check = run('gh pr checks 69 2>&1 | sed -n \'1,40p\'; printf UNREACHABLE');
  assert.equal(check.status, 1);
  assert.match(check.stdout, /auth failed/);
  assert.doesNotMatch(check.stdout, /UNREACHABLE/);
  assert.equal(run('gh pr list; printf UNREACHABLE').status, 1);
  process.env.PATH = oldPath;
  let result = run('printf before; (exit 17) | tail -60; printf UNREACHABLE');
  assert.equal(result.status, 17);
  assert.equal(result.stdout, 'before');
  assert.equal(run('printf ok | tail -1').stdout, 'ok');
  assert.equal(run('false; printf UNREACHABLE').status, 1);
  // Regression: probing a branch for an optional platform fix must reach the
  // next inspection when markers are absent, while mandatory assertions fail.
  assert.equal(run("printf 'unrelated source\\n' | bridge_grep_optional -n -E 'requires_bytes_plan|pytestmark|CAPABILITY_UNAVAILABLE' | sed -n '1,20p'; printf NEXT_FILE").stdout, 'NEXT_FILE');
  assert.equal(run("printf 'pytestmark = fixture\\n' | bridge_grep_optional -n -E 'requires_bytes_plan|pytestmark|CAPABILITY_UNAVAILABLE' | sed -n '1,20p'").stdout, '1:pytestmark = fixture\n');
  assert.equal(run("(exit 128) | bridge_grep_optional -n -E 'requires_bytes_plan|pytestmark|CAPABILITY_UNAVAILABLE' | sed -n '1,20p'; printf UNREACHABLE").status, 128);
  assert.equal(run('printf plain | bridge_grep_optional missing | sed -n \'1,40p\'; printf CONTINUED').stdout, 'CONTINUED');
  assert.equal(run('printf plain | grep missing; printf UNREACHABLE').status, 1);
  assert.equal(run('(exit 17) | bridge_grep_optional missing | cat; printf UNREACHABLE').status, 17);
  assert.equal(run('(exit 1) | bridge_grep_optional missing | cat; printf UNREACHABLE').status, 1);
  assert.equal(run('printf plain | bridge_grep_optional "["; printf UNREACHABLE').status, 2);
  assert.equal(run('bridge_grep_optional x missing-file; printf UNREACHABLE').status, 2);
  assert.equal(run('printf "match\\nother\\n" | bridge_grep_optional -n match').stdout, '1:match\n');
  assert.equal(run('seq 1 20000 | bridge_grep_optional . | sed -n \'1,2p\'').status, 0);
  // N1: quiet mode returns 0 on a match even when another input failed.
  writeFileSync(join(root, 'f1'), 'x\n');
  for (const flags of ['-q', '-qn', '-nq', '--quiet', '--silent', '--qui', '-c -q']) {
    const quiet = run(`bridge_grep_optional ${flags} x f1 missing-file; printf UNREACHABLE`);
    assert.equal(quiet.status, 2, flags);
    assert.match(quiet.stderr, /quiet mode can hide read errors/);
    assert.doesNotMatch(quiet.stdout, /UNREACHABLE/);
  }
  assert.equal(run('bridge_grep_optional -c x f1 missing-file; printf UNREACHABLE').status, 2);
  assert.equal(run('bridge_grep_optional -s x missing-file; printf UNREACHABLE').status, 2);
  assert.equal(run("printf -- '-q\\n' | bridge_grep_optional -e -q; printf NEXT").stdout, '-q\nNEXT');
  assert.equal(run("printf -- '-q\\n' | bridge_grep_optional -- -q; printf NEXT").stdout, '-q\nNEXT');
  assert.equal(run("printf 'aq\\n' | bridge_grep_optional -eq; printf NEXT").stdout, 'aq\nNEXT');
  assert.equal(run('printf a | bridge_grep_optional --regexp -q; printf NEXT').stdout, 'NEXT');
  // P2-4: a non-bash interpreter is refused instead of exiting 0 silently.
  const zsh = spawnSync('/bin/zsh', ['-c', hardenSpec({command: 'printf RAN', env: {}}, root).command], {encoding: 'utf8'});
  if (zsh.error?.code !== 'ENOENT') {
    assert.equal(zsh.status, 2);
    assert.doesNotMatch(zsh.stdout, /RAN/);
  }
  assert.equal(run('if false; then printf BAD; fi; false || printf handled').stdout, 'handled');
  result = run('printf "%s" "$ORIGINAL" > "$XDG_CACHE_HOME/owned"');
  assert.equal(result.status, 0, result.stderr);
  assert.equal(readFileSync(join(root, 'owned'), 'utf8'), 'preserved');
  const exec = {name: 'bash'};
  assert.equal(failedResult(exec, {value: {kind: 'foreground', exitCode: 0, signal: null}}), false);
  assert.equal(failedResult(exec, {value: {kind: 'foreground', exitCode: 17}}), true);
  assert.equal(failedResult(exec, {value: {kind: 'foreground', exitCode: 0, sandbox: {denied: true}}}), true);
  assert.equal(failedResult({name: 'read'}, {isError: true}), true);
  assert.equal(failedResult(exec, {value: {kind: 'background'}}), false);
  const cancelled = [], disposers = [], handlers = {}, events = [];
  const goals = {disarm: agent => { agent.activation = 'disarmed'; events.push('disarm'); }};
  const resolve = request => request;
  const commands = [];
  const ctx = {goals, shell: {resolve}, commands: {register: command => commands.push(command)},
    effect: fn => disposers.push(fn()), on: (event, fn) => {handlers[event] = fn;}};
  apply(ctx);
  const hardened = ctx.shell.resolve({command: 'true'});
  assert.equal(statSync(hardened.env.XDG_CACHE_HOME).mode & 0o777, 0o700);
  handlers['tools/result']({name: 'bash', agent: {status: 'running', cancel: (...args) => { events.push('cancel'); cancelled.push(args); }}},
    {value: {kind: 'foreground', exitCode: 3}});
  assert.deepEqual(cancelled, [[{kind: 'user'}, {keepInbox: true}]]);
  assert.deepEqual(events, ['disarm', 'cancel']);
  // A manual turn may have an armed goal without a driver-owned attempt.
  // The idle callback must observe disarmed before considering a followup.
  let continuations = 0;
  const agent = {status: 'running', activation: 'armed', phase: 'active', revision: 9,
    cancel() { this.status = 'idle'; if (this.activation === 'armed') continuations++; }};
  handlers['tools/result']({name: 'read', agent}, Object.freeze({isError: true}));
  assert.equal(continuations, 0);
  assert.equal(agent.phase, 'active');
  assert.equal(agent.revision, 9);
  let stopped = false;
  goals.disarm = () => { throw new Error('disarm failed'); };
  assert.throws(() => handlers['tools/result']({name: 'read', agent: {status: 'running',
    cancel() { stopped = true; }}}, {isError: true}), /disarm failed/);
  assert.equal(stopped, true);
  assert.equal((await commands[0].handler()).kind, 'success');
  assert.match((await commands[0].handler()).text, /harness-contract-v3/);
  disposers.forEach(fn => fn());
  assert.equal(ctx.shell.resolve, resolve);
  // P3-11: cordis returns a fresh proxy for every method read. Teardown must
  // still restore, must not clobber a later wrapper, and reload must not stack.
  {
    const service = new (class { resolve(request) { return {...request, env: {}}; } })();
    const proxied = new Proxy(service, {get(target, prop, receiver) {
      const value = Reflect.get(target, prop, receiver);
      return typeof value === 'function' ? new Proxy(value, {}) : value;
    }});
    const cleanups = [];
    const pctx = {...ctx, shell: proxied, effect: fn => cleanups.push(fn())};
    apply(pctx);
    assert.ok(proxied.resolve({command: 'true'}).command.startsWith('set -e -o pipefail'));
    cleanups.pop()();
    assert.equal(proxied.resolve({command: 'true'}).command, 'true');
    assert.equal(Object.hasOwn(service, 'resolve'), false);
    apply(pctx); apply(pctx);
    assert.equal(proxied.resolve({command: 'X'}).command.split('set -e -o pipefail').length - 1, 2);
    const later = proxied.resolve;
    function outer(request) { return {...later.call(this, request), outer: true}; }
    proxied.resolve = outer;
    cleanups.pop()();
    assert.equal(proxied.resolve({command: 'X'}).outer, true);
    assert.equal(proxied.resolve({command: 'X'}).command.split('set -e -o pipefail').length - 1, 1);
  }
  rmSync(hardened.env.XDG_CACHE_HOME, {recursive: true});
  console.log('shell safety: pipeline, early exit, expected failure, cache, structured stop, teardown passed');
} finally { rmSync(root, {recursive: true, force: true}); }
