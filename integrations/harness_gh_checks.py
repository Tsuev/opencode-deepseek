"""One-shot gh check-status adapter. No retry and no credentials handling."""
import json
import subprocess
import sys

FIELDS = 'name,state,bucket,link,workflow'
STATES = {
    'pass': {'SUCCESS', 'SUCCESSFUL'},
    'fail': {'FAILURE', 'ERROR', 'TIMED_OUT', 'ACTION_REQUIRED', 'STARTUP_FAILURE', 'STALE'},
    'pending': {'PENDING', 'QUEUED', 'IN_PROGRESS', 'WAITING', 'REQUESTED', 'EXPECTED'},
    'cancel': {'CANCELLED'},
    'skipping': {'SKIPPED', 'NEUTRAL'},
}


def supported(args):
    """Only plain, single-snapshot checks; other CLI modes retain native behavior."""
    positional = 0
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ('--repo', '-R'):
            i += 1
            if i >= len(args) or args[i].startswith('-'):
                return False
        elif arg.startswith('--repo=') and arg != '--repo=':
            pass
        elif arg == '--required':
            pass
        elif arg.startswith('-'):
            return False
        else:
            positional += 1
            if positional > 1:
                return False
        i += 1
    return True


def decode_checks(text):
    rows = json.loads(text)
    if not isinstance(rows, list) or not rows:
        raise ValueError('Expected nonempty check list')
    for row in rows:
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str) for k in FIELDS.split(',')):
            raise ValueError('Invalid check schema')
        if not row['name'] or row['state'] not in STATES.get(row['bucket'], set()):
            raise ValueError('Invalid check state')
    return rows


def main(argv=None):
    executable, *args = sys.argv[1:] if argv is None else argv
    if not supported(args):
        return subprocess.call([executable, 'pr', 'checks', *args])
    result = subprocess.run([executable, 'pr', 'checks', *args, '--json', FIELDS],
                            capture_output=True, text=True)
    sys.stderr.write(result.stderr)
    try:
        rows = decode_checks(result.stdout)
        buckets = {row['bucket'] for row in rows}
        # A status code alone is ambiguous. Require a complete matching payload
        # and no stderr before interpreting a nonzero exit as check-status data.
        expected = 1 if 'fail' in buckets else 8 if 'pending' in buckets else 0
        # JSON export can succeed (0) while the checks themselves are pending
        # or failing. Their validated buckets remain visible to the caller.
        # Only nonzero status codes must match the check-state exit convention.
        valid = result.returncode in (0, expected) and not result.stderr.strip()
    except (ValueError, TypeError):
        valid = False
    if not valid:
        sys.stdout.write(result.stdout)
        if result.returncode == 0:
            sys.stderr.write('Invalid gh checks status payload; continuation stopped.\n')
        return result.returncode if result.returncode > 0 else 1
    sys.stdout.write(json.dumps(rows, ensure_ascii=False) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
