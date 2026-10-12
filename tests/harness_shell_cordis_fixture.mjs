// Opt-in: node tests/harness_shell_cordis_fixture.mjs <path to Harness cordis/lib/index.js>
// Uses the real cordis service proxies from an extracted Harness build.
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import * as safety from '../integrations/harness_shell_safety.mjs';
const { Context, Service } = await import(pathToFileURL(process.argv[2]).href);
const tick = () => new Promise(resolve => setTimeout(resolve, 30));
class Shell extends Service { constructor(ctx) { super(ctx, 'shell'); } resolve(request) { return {...request, env: {}}; } }
for (const name of ['tools', 'commands', 'goals']) {
  globalThis[name] = class extends Service { constructor(ctx) { super(ctx, name); } register() {} disarm() {} };
}
const root = new Context();
root.plugin(Shell); for (const name of ['tools', 'commands', 'goals']) root.plugin(globalThis[name]);
await tick();
let tool; root.inject(['shell'], ctx => { tool = ctx; });
const plugin = {name: safety.name, inject: safety.inject, apply: safety.apply};
const fiber = root.plugin(plugin); await tick();
assert.ok(tool.shell.resolve({command: 'X'}).command.startsWith('set -e -o pipefail'));
await fiber.dispose(); await tick();
assert.equal(tool.shell.resolve({command: 'X'}).command, 'X');
root.plugin(plugin); await tick(); root.plugin(plugin); await tick();
assert.equal(tool.shell.resolve({command: 'X'}).command.split('set -e -o pipefail').length - 1, 2);
console.log('real cordis: hardening reaches other plugins; dispose restores; independent instances do not leak');
