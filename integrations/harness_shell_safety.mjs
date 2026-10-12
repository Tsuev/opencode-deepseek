import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const name = 'bridge-shell-safety';
export const contractVersion = 'harness-contract-v3';
export const inject = ['shell', 'tools', 'commands', 'goals'];

// This is execution policy, not a rewrite of the model's shell source. Bash
// retains its normal explicit if/! / && / || handling of expected failures.
const OWNER = Symbol.for('opencode-local-bridge.shell-safety');

// grep -q/--quiet/--silent exits 0 on a match even when another input failed
// (GNU and BSD). The wrapper cannot see that error, so quiet mode is refused.
// Options are scanned up to `--`; values of argument-taking options are skipped.
const optionalGrep = 'bridge_grep_optional() {\n'
  + '  local __bridge_rc=0 __bridge_arg __bridge_rest __bridge_name __bridge_skip=0\n'
  + '  for __bridge_arg in "$@"; do\n'
  + '    if [ "$__bridge_skip" = 1 ]; then __bridge_skip=0; continue; fi\n'
  + '    case "$__bridge_arg" in\n'
  + '      --) break ;;\n'
  + '      --*=*) __bridge_name=${__bridge_arg%%=*}; __bridge_name=${__bridge_name#--} ;;\n'
  + '      --?*) __bridge_name=${__bridge_arg#--}\n'
  + '        case "$__bridge_name" in\n'
  + '          regexp|regex|rege|reg|file|fil|max-count|max-coun|max-cou|max-co|max-c|max|context|after-context|before-context|include|exclude|exclude-dir|exclude-from|binary-files|devices|directories|label|group-separator) __bridge_skip=1 ;;\n'
  + '        esac ;;\n'
  + '      -?*) __bridge_name=; __bridge_rest=${__bridge_arg#-}\n'
  + '        while [ -n "$__bridge_rest" ]; do\n'
  + '          case "$__bridge_rest" in\n'
  + '            q*) __bridge_name=quiet; break ;;\n'
  + '            [efmABCdD]) __bridge_skip=1; break ;;\n'
  + '            [efmABCdD]*) break ;;\n'
  + '          esac\n'
  + '          __bridge_rest=${__bridge_rest#?}\n'
  + '        done ;;\n'
  + '      *) __bridge_name= ;;\n'
  + '    esac\n'
  + '    case "$__bridge_name" in\n'
  + '      q|qu|qui|quie|quiet|s|si|sil|sile|silen|silent)\n'
  + '        printf \'%s\\n\' "bridge_grep_optional: quiet mode can hide read errors; use -c or redirect output" >&2\n'
  + '        return 2 ;;\n'
  + '    esac\n'
  + '  done\n'
  + '  command grep "$@" || __bridge_rc=$?\n'
  + '  if [ "$__bridge_rc" -eq 1 ]; then return 0; fi\n'
  + '  return "$__bridge_rc"\n'
  + '}\n';

// This is execution policy, not a rewrite of the model's shell source. Bash
// retains its normal explicit if/! / && / || handling of expected failures.
export function hardenSpec(spec, cacheRoot) {
  // Explicit opt-in for searches where zero matches is an expected result.
  // Never normalize a whole pipeline's status: its producer may also exit 1.
  const helper = fileURLToPath(new URL('./harness_gh_checks.py', import.meta.url));
  const quote = value => "'" + value.replaceAll("'", "'\\''") + "'";
  const ghChecks = 'gh() {\n'
    + '  if [ "${1-}" = pr ] && [ "${2-}" = checks ]; then\n'
    + '    local gh_binary; gh_binary=$(type -P gh) || return $?\n'
    + '    shift 2\n'
    + '    command python3 ' + quote(helper) + ' "$gh_binary" "$@"\n'
    + '  else command gh "$@"; fi\n'
    + '}\n';
  // Executors run `bash -c`; refuse any other interpreter instead of letting
  // shell differences (e.g. zsh's read-only $status) turn errors into exit 0.
  const requireBash = '[ -n "${BASH_VERSION-}" ] || { echo "bridge shell safety requires bash" >&2; exit 2; }\n';
  return {...spec, command: 'set -e -o pipefail\n' + requireBash + optionalGrep + ghChecks + spec.command,
    env: {...spec.env, XDG_CACHE_HOME: cacheRoot}};
}

export function failedResult(exec, result) {
  if (result.isError) return true;
  if (exec.name !== 'bash' || result.value?.kind !== 'foreground') return false;
  const value = result.value;
  return value.exitCode !== 0 || value.signal != null || value.timedOut
    || value.aborted || value.sandbox?.denied === true;
}

export function apply(ctx) {
  // Random, owner-only cache; no HOME/GH_CONFIG_DIR changes or credential copy.
  // Keep it until OS temp cleanup: background readers must not lose their files.
  const cacheRoot = mkdtempSync(join(process.platform === 'linux' ? '/tmp' : tmpdir(), 'harness-gh-'));
  const shell = ctx.shell;
  ctx.effect(() => {
    // Cordis returns a fresh proxy on every service method read, so identity
    // comparison cannot detect our wrapper. Mark ownership on the function.
    const hadOwn = Object.getOwnPropertyDescriptor(shell, 'resolve') !== undefined;
    const original = shell.resolve;
    const marker = {active: true};
    function resolve(request) {
      const spec = original.call(this, request);
      return marker.active ? hardenSpec(spec, cacheRoot) : spec;
    }
    resolve[OWNER] = marker;
    shell.resolve = resolve;
    return () => {
      // A later wrapper may sit on top: become pass-through instead of
      // clobbering it. Restore only when this wrapper is still outermost.
      marker.active = false;
      if (shell.resolve?.[OWNER] === marker) {
        if (hadOwn) shell.resolve = original;
        else delete shell.resolve;
      }
    };
  });
  ctx.on('tools/result', (exec, result) => {
    if (failedResult(exec, result) && exec.agent?.status === 'running') {
      // Cancellation alone does not revoke an armed goal's continuation
      // authority. Disarm first, without changing its durable phase/revision.
      // Still cancel if disarm throws: result observers cannot propagate errors
      // into the scheduler, and an idle transition must not start another step.
      try { ctx.goals.disarm(exec.agent); }
      finally { exec.agent.cancel({kind: 'user'}, {keepInbox: true}); }
      // A native stop preserves completed tool results and does not ask the
      // provider to diagnose/retry. Keep queued user input, like the Stop button.
    }
  });
  ctx.commands.register({definitionId: 'local/bridge-shell-status', name: 'bridge-shell-status',
    description: 'Local shell pipeline protection and gh cache status', recordInput: false,
    handler: async () => {
      const spec = ctx.shell.resolve({command: ':'});
      const active = spec.command.startsWith('set -e -o pipefail\n') && spec.env.XDG_CACHE_HOME === cacheRoot
        && typeof ctx.goals.disarm === 'function';
      return {kind: active ? 'success' : 'error', text: active
        ? `Shell safety active (${contractVersion}): errexit + pipefail; goal disarm before native stop on tool failure; validated gh pr checks status; private temporary gh cache. No command or provider request was sent.`
        : 'Shell safety is not active in the current executor. Do not resume until the profile is reloaded.'};
    }});
}
