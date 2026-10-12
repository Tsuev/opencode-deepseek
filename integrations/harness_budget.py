"""Install human-only budget commands into an existing Harness profile, offline."""
import argparse
import os
from pathlib import Path
from .harness import default_home, install_plugin_copy, profile_lock, render, write_private, yaml_codec


def install(home, profile, token_file, base_url='http://127.0.0.1:8000'):
    with profile_lock(home, profile):
        return _install_unlocked(home, profile, token_file, base_url)


def _install_unlocked(home, profile, token_file, base_url):
    path = Path(home) / 'profiles' / profile / 'cordis.patch.yml'
    if path.is_symlink():
        raise ValueError('Refusing to replace a symlinked Harness profile')
    if not path.is_file():
        raise ValueError('Initialize the Harness profile first')
    original = path.read_bytes()
    document = yaml_codec().load(original.decode('utf-8'))
    if not isinstance(document, list):
        raise ValueError('Unsupported Harness profile')
    identity = 'local-bridge-budget'
    matches = [row for row in document if isinstance(row, dict) and (row.get('id') == identity
               or (isinstance(row.get('insert'), list) and len(row['insert']) == 1
                   and isinstance(row['insert'][0], dict) and row['insert'][0].get('id') == identity))]
    if len(matches) > 1:
        raise ValueError('Duplicate budget plugin entries')
    entry = install_plugin_copy(home, identity, (Path(__file__).with_name('harness_budget.mjs'),))
    row = {'id':identity, 'name':str(entry),
           'config':{'baseURL':base_url, 'tokenFile':str(Path(token_file).resolve())}, 'disabled':False}
    if matches:
        document[document.index(matches[0])] = {'insert':[row]}
    else:
        document.append({'insert':[row]})
    write_private(path, render(document), original)
    return path


def _valid_port(text):
    return 1 <= len(text) <= 5 and text.isascii() and text.isdecimal() and 1 <= int(text) <= 65535


def base_url_argument(value):
    """Literal loopback HTTP origin, the same rule harness_budget.mjs enforces."""
    from urllib.parse import urlsplit
    parts = urlsplit(value)
    port = value.rsplit(':', 1)[-1].rstrip('/') if parts.port is not None else ''
    if (parts.scheme != 'http' or parts.hostname not in ('127.0.0.1', '::1') or parts.username
            or parts.password or parts.query or parts.fragment or parts.path not in ('', '/')
            or (parts.port is not None and not _valid_port(port))):
        raise argparse.ArgumentTypeError('must be a literal loopback HTTP origin, e.g. http://127.0.0.1:8000')
    return value.rstrip('/')


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', type=Path, default=default_home())
    parser.add_argument('--profile', default='desktop', choices=['desktop','web','headless'])
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--base-url', type=base_url_argument, default=None,
                        help='Bridge origin; defaults to PORT from the environment or .env (8000)')
    args = parser.parse_args(argv)
    base_url = args.base_url
    if base_url is None:
        # Only the default depends on PORT: an explicit --base-url wins even
        # when PORT is broken. Same settings source as app.py and the exporter.
        from settings import load_environment
        load_environment()
        port = os.getenv('PORT', '8000').strip()
        if not _valid_port(port):
            parser.error('PORT must be a TCP port number; pass --base-url explicitly')
        base_url = 'http://127.0.0.1:' + port
    print(install(args.home, args.profile, args.token_file, base_url))


if __name__ == '__main__': main()
