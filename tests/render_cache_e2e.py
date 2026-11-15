#!/usr/bin/env python3
"""Explicit cleanup owns exact render artifacts, never neighboring user files."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import audit_repair_e2e as wire
from render_profiles_e2e import setup, TEMPLATE


def run(binary, helm, out):
    rows = []
    cache = out / 'cache'
    managed = cache / 'argocd-ls/render'
    environment = dict(os.environ, XDG_CACHE_HOME=str(cache))
    root = out / 'workspace'
    file = setup(root, {'schemaVersion': 1, 'targetVersion': '3.6.1'})

    def record(name, checks, observed):
        rows.append({'id': name, 'assertions': checks, 'observed': observed, 'passed': bool(checks) and all(checks.values())})
        (out / 'report.json').write_text(json.dumps({'rows': rows, 'passed': all(r['passed'] for r in rows), 'binarySHA256': wire.hash_file(binary), 'helmSHA256': wire.hash_file(helm), 'runnerSHA256': wire.hash_file(Path(__file__)), 'command': sys.argv}, indent=2) + '\n')
        print(name, 'PASS' if rows[-1]['passed'] else 'FAIL', flush=True)

    def command(name, args):
        process = subprocess.run([str(binary)] + args, capture_output=True, timeout=45, env=environment)
        (out / (name + '.stdout')).write_bytes(process.stdout)
        (out / (name + '.stderr')).write_bytes(process.stderr)
        try: data = json.loads(process.stdout)
        except ValueError: data = {}
        return process, data

    def render(name, output=None):
        args = ['render-check', '--root', str(root), '--file', str(file), '--helm-binary', str(helm), '--format', 'json']
        if output: args += ['--output', str(output)]
        process, data = command(name, args)
        renderer = data.get('renderer') or {}
        path = Path(renderer.get('directory') or (str(Path(renderer['output']).parent) if renderer.get('output') else str(out / ('absent-' + name))))
        return process, data, path

    process, data, path = render('owned')
    listed, listing = command('list', ['render-cache'])
    record('owned-cache-listed-without-deletion', {'managedDirectory': process.returncode == 0 and path.parent == managed and (path / 'cache-manifest.json').is_file(), 'verifiedEntryListed': listed.returncode == 0 and any(x.get('directory') == str(path) and x.get('owned') is True for x in listing.get('entries', [])), 'listingIsReadOnly': (path / 'generated.yaml').is_file()}, {'render': data, 'list': listing})
    removed, deletion = command('remove-owned', ['render-cache', '--remove', str(path)])
    record('explicit-owned-removal', {'onlyExplicitEntryRemoved': removed.returncode == 0 and not path.exists(), 'sourcePreserved': file.read_text() == TEMPLATE and managed.is_dir()}, deletion)

    for name in ['modified-file', 'extra-user-file', 'copied-marker', 'nested-symlink']:
        _, data, artifact = render(name)
        target = artifact
        sentinel = out / (name + '-sentinel')
        sentinel.write_text('user content must survive\n')
        if artifact.is_dir():
            if name == 'modified-file': (artifact / 'generated.yaml').write_text('user modified output\n')
            elif name == 'extra-user-file': (artifact / 'user.txt').write_text(sentinel.read_text())
            elif name == 'copied-marker':
                target = managed / 'chart-copied-marker'
                target.mkdir(parents=True)
                for child in artifact.iterdir():
                    if child.is_file(): shutil.copyfile(child, target / child.name)
                (target / 'user.txt').write_text(sentinel.read_text())
            else:
                (artifact / 'generated.yaml').unlink()
                (artifact / 'generated.yaml').symlink_to(sentinel)
        rejected, response = command('remove-' + name, ['render-cache', '--remove', str(target)])
        record(name, {'unverifiedOwnershipRefused': rejected.returncode == 2 and target.is_dir(), 'userDataPreserved': sentinel.read_text() == 'user content must survive\n' and (name != 'extra-user-file' or (target / 'user.txt').read_text() == sentinel.read_text())}, {'response': response, 'stderr': rejected.stderr.decode(errors='replace'), 'artifact': str(target)})

    outside = out / 'outside-directory'
    outside.mkdir()
    (outside / 'sentinel').write_text('outside\n')
    managed.mkdir(parents=True, exist_ok=True)
    alias = managed / 'chart-root-link'
    alias.symlink_to(outside, target_is_directory=True)
    rejected, data = command('remove-root-symlink', ['render-cache', '--remove', str(alias)])
    record('artifact-root-symlink', {'rootLinkRefused': rejected.returncode == 2 and alias.is_symlink(), 'outsideSentinelPreserved': (outside / 'sentinel').read_text() == 'outside\n'}, data)

    custom = managed / 'user-output'
    process, data, artifact = render('custom-output', custom)
    rejected, removal = command('remove-custom', ['render-cache', '--remove', str(custom)])
    record('custom-output-never-owned', {'customRenderStillWorks': process.returncode == 0 and artifact == custom and (custom / 'generated.yaml').is_file(), 'noOwnershipMarker': not (custom / 'cache-manifest.json').exists(), 'cleanupRefusesUserOutput': rejected.returncode == 2 and (custom / 'generated.yaml').is_file()}, {'render': data, 'remove': removal})

    original_cache = environment['XDG_CACHE_HOME']
    before = {str(p.relative_to(root)): wire.hash_file(p) for p in root.rglob('*') if p.is_file()}
    marker = out / 'cache-placement-helm-called'
    wrapper = out / 'placement-helm'
    wrapper.write_text('#!' + sys.executable + '\nimport os,pathlib,sys\npathlib.Path(' + repr(str(marker)) + ').write_text("called")\nos.execv(' + repr(str(helm)) + ', [' + repr(str(helm)) + '] + sys.argv[1:])\n')
    wrapper.chmod(0o700)
    environment['XDG_CACHE_HOME'] = str(root / 'charts/demo/cache')
    process, data = command('managed-under-chart', ['render-check', '--root', str(root), '--file', str(file), '--helm-binary', str(wrapper), '--format', 'json'])
    record('managed-cache-cannot-enter-chart', {'preflightRefusesBeforeHelm': process.returncode == 2 and not marker.exists(), 'sourceTreeUnchanged': before == {str(p.relative_to(root)): wire.hash_file(p) for p in root.rglob('*') if p.is_file()} and not (root / 'charts/demo/cache').exists()}, {'report': data, 'stderr': process.stderr.decode(errors='replace')})
    environment['XDG_CACHE_HOME'] = original_cache

    file.write_text('{{ fail "deliberate render failure" }}\n')
    failed, data, artifact = render('failed-render')
    renderer = data.get('renderer') or {}
    evidence = (artifact / 'helm.stderr').read_text() if (artifact / 'helm.stderr').is_file() else ''
    retained = (artifact / 'report.json').is_file() and (artifact / 'cache-manifest.json').is_file()
    removed, removal = command('remove-failed', ['render-cache', '--remove', str(artifact)])
    record('failed-render-evidence-owned-and-cleanable', {'failureEvidenceRetained': failed.returncode == 2 and renderer.get('exit', 0) != 0 and 'deliberate render failure' in evidence and retained, 'explicitFailureCleanup': removed.returncode == 0 and not artifact.exists()}, {'render': data, 'remove': removal})
    return all(row['passed'] for row in rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('binary', type=Path)
    parser.add_argument('helm', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.resolve().mkdir(parents=True, exist_ok=False)
    sys.exit(0 if run(args.binary.resolve(), args.helm.resolve(), args.output.resolve()) else 1)
