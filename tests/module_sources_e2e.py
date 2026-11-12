#!/usr/bin/env python3
"""Exercise clean module acquisition and rejected source/cache substitutions."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TOOLKIT = 'github.com/lazysnail1024/k8s-crd-toolkit'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--platform', required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = output / 'source'
    source.mkdir()
    cache = output / '.module-cache'  # Go ./... must not traverse downloaded modules.
    cache.mkdir()
    rows = []
    env = dict(os.environ, GOMODCACHE=str(cache), GOWORK='off', GOTOOLCHAIN='local',
               GOFLAGS='', GIT_AUTHOR_NAME='Module fixture', GIT_AUTHOR_EMAIL='fixture@example.invalid',
               GIT_COMMITTER_NAME='Module fixture', GIT_COMMITTER_EMAIL='fixture@example.invalid')

    def run(name, expected=0, reason=None):
        build = output / name
        command = [sys.executable, str(source / 'tools/native.py'), 'build', '--platform',
                   args.platform, '--output', str(build)]
        process = subprocess.run(command, cwd=source, env=env, capture_output=True, timeout=240)
        (output / (name + '.stdout')).write_bytes(process.stdout)
        (output / (name + '.stderr')).write_bytes(process.stderr)
        result = json.loads(process.stdout)
        checks = {'expectedExit': process.returncode == expected, 'reported': result['passed'] == (expected == 0)}
        if reason:
            checks['specificFailure'] = any(text in result.get('error', '').lower() for text in reason)
            checks['refusedBeforeCompile'] = not any(row['command'][1:2] == ['build'] for row in result['steps'])
        row = {'id': name, 'command': command, 'checks': checks, 'passed': all(checks.values())}
        rows.append(row)
        if not row['passed']:
            raise AssertionError(name + ': ' + result.get('error', 'unexpected acceptance'))
        return json.loads((build / 'manifest.json').read_text()) if expected == 0 else result

    try:
        names = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT).decode().split('\0')
        for name in sorted(set(names) - {''}):
            path = ROOT / name
            if not path.exists():
                continue
            if name.startswith('vendor/') or path.is_symlink() or not path.is_file():
                raise ValueError('Build fixture must contain only project sources, without vendor')
            destination = source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        for command in (['git', 'init'], ['git', '-c', 'core.autocrlf=false', 'add', '.'],
                        ['git', '-c', 'commit.gpgsign=false', 'commit', '-m', 'Synthetic module source fixture']):
            subprocess.run(command, cwd=source, env=env, capture_output=True, check=True, timeout=30)
        before = {name: (source / name).read_bytes() for name in ('go.mod', 'go.sum')}
        assert not list(cache.iterdir()), 'cold acquisition requires an empty module cache'
        first = run('cold-cache')
        second = run('warm-cache')
        lock_path = source / 'contracts/toolkit.lock.json'
        lock_bytes = lock_path.read_bytes()
        lock = json.loads(lock_bytes)
        checks = {
            'noVendoredInputs': not any(name.startswith('vendor/') for name in first['sourceInputs']),
            'declarationsUnchanged': all((source / name).read_bytes() == data for name, data in before.items()),
            'pinnedVersion': first['moduleInputs'][TOOLKIT]['Version'] == lock['implementationVersion'],
            'verifiedChecksums': all(row['Sum'].startswith('h1:') and row['GoModSum'].startswith('h1:') for row in first['moduleInputs'].values()),
            'identicalCachedBytes': first['binarySHA256'] == second['binarySHA256'],
        }
        rows.append({'id': 'module-provenance', 'checks': checks, 'passed': all(checks.values())})

        checksum = first['moduleInputs'][TOOLKIT]['Sum']
        try:
            (source / 'go.sum').write_bytes(before['go.sum'].replace(checksum.encode(), b'h1:' + b'A' * 43 + b'=', 1))
            run('wrong-module-checksum', 2, ('checksum mismatch', 'not pinned by go.sum'))
        finally:
            (source / 'go.sum').write_bytes(before['go.sum'])
        try:
            (source / 'go.mod').write_bytes(before['go.mod'] + ('\nreplace ' + TOOLKIT + ' => ./unexpected-replacement\n').encode())
            run('local-module-replacement', 2, ('must not use replace',))
        finally:
            (source / 'go.mod').write_bytes(before['go.mod'])
        try:
            lock['implementationCommit'] = 'f' * 40
            lock_path.write_text(json.dumps(lock))
            run('wrong-source-commit', 2, ('source commit', 'source lock'))
        finally:
            lock_path.write_bytes(lock_bytes)

        contract = source / 'contracts' / json.loads(lock_bytes)['contractFile']
        original_contract = contract.read_bytes()
        try:
            contract.write_bytes(original_contract + b'\nChanged contract.\n')
            run('wrong-contract', 2, ('contract differs',))
        finally:
            contract.write_bytes(original_contract)

        module = cache / (TOOLKIT + '@' + json.loads(lock_bytes)['implementationVersion'])
        victim = module / 'index/index.go'
        original, mode = victim.read_bytes(), stat.S_IMODE(victim.stat().st_mode)
        try:
            victim.chmod(mode | stat.S_IWUSR)
            victim.write_bytes(original + b'\n// Deliberately corrupted isolated test cache.\n')
            run('modified-module-cache', 2, ('dir has been modified',))
        finally:
            victim.write_bytes(original)
            victim.chmod(mode)
        assert hashlib.sha256(victim.read_bytes()).digest() == hashlib.sha256(original).digest()
    except Exception as error:
        rows.append({'id': 'runner', 'passed': False, 'error': repr(error)})
    report = {'scope': 'synthetic source and isolated module cache; no shared-cache edits',
              'platform': args.platform, 'rows': rows,
              'passed': len(rows) == 8 and all(row['passed'] for row in rows)}
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'rows': len(rows), 'passed': report['passed']}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
