"""Install local Harness shell policy without touching sessions or accounts."""
import argparse
from pathlib import Path
from .harness import default_home, install_plugin_copy, profile_lock, render, write_private, yaml_codec

IDENTITY = 'local-bridge-shell-safety'
SOURCES = (Path(__file__).with_suffix('.mjs'), Path(__file__).with_name('harness_gh_checks.py'))


def patch_document(document, entry=None):
    if not isinstance(document, list):
        raise ValueError('Unsupported Harness profile')
    entry = str(Path(__file__).with_suffix('.mjs').resolve()) if entry is None else str(entry)
    row = {'insert': [{'id': IDENTITY, 'name': entry, 'disabled': False}]}
    matches = [i for i, item in enumerate(document) if isinstance(item, dict) and
               (item.get('id') == IDENTITY or any(isinstance(child, dict) and
                child.get('id') == IDENTITY for child in item.get('insert', []) or []))]
    if len(matches) > 1:
        raise ValueError('Duplicate shell safety entries')
    result = list(document)
    if matches:
        old = result[matches[0]]
        if 'insert' in old and len(old['insert']) != 1:
            raise ValueError('Shell safety shares an insert block; preserve it manually')
        result[matches[0]] = row
    else:
        result.append(row)
    return result


def install(home, profile):
    with profile_lock(home, profile):
        path = Path(home) / 'profiles' / profile / 'cordis.patch.yml'
        if path.is_symlink():
            raise ValueError('Refusing to replace a symlinked Harness profile')
        original = path.read_bytes()
        entry = install_plugin_copy(home, IDENTITY, SOURCES)
        updated = render(patch_document(yaml_codec().load(original.decode()), entry))
        write_private(path, updated, original)
        return path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=default_home())
    parser.add_argument('--profile', default='desktop', choices=['desktop', 'web', 'headless'])
    args = parser.parse_args()
    print(install(args.home, args.profile))
