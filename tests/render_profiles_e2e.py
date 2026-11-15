#!/usr/bin/env python3
"""Independent named-profile and ordered-values acceptance with real Helm."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import audit_repair_e2e as wire

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = '''apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: {{ .Release.Name }}-{{ .Values.environment }}
  annotations:
    kube: {{ .Capabilities.KubeVersion.Version | quote }}
    special: {{ .Capabilities.APIVersions.Has "example.org/v1/Widget" | quote }}
spec:
  project: default
  source: {repoURL: https://example.org/repo}
  destination: {server: https://kubernetes.default.svc, namespace: {{ .Release.Namespace }}}
'''


def config():
    profiles = {}
    for name, values in [('dev', ['env/common.yaml', 'env/dev.yaml']), ('prod', ['env/common.yaml', 'env/prod.yaml', 'env/common.yaml', 'env/prod.yaml']), ('reverse', ['env/dev.yaml', 'env/common.yaml'])]:
        profiles[name] = {'chart': 'charts/demo', 'values': values, 'releaseName': 'acceptance', 'namespace': 'testing', 'kubeVersion': '1.31.0', 'apiVersions': ['example.org/v1/Widget']}
    return {'schemaVersion': 2, 'targetVersion': '3.6.1', 'renderProfiles': profiles}


def setup(root, configuration=None):
    root.mkdir(parents=True, exist_ok=True)
    wire.write(root, '.argocd-ls.yaml', json.dumps(configuration or config(), indent=2) + '\n')
    wire.write(root, 'charts/demo/Chart.yaml', 'apiVersion: v2\nname: profiles\nversion: 0.1.0\n')
    wire.write(root, 'charts/demo/values.yaml', 'environment: default\n')
    wire.write(root, 'env/common.yaml', 'environment: base\n')
    wire.write(root, 'env/dev.yaml', 'environment: dev\n')
    wire.write(root, 'env/prod.yaml', 'environment: prod\n')
    return wire.write(root, 'charts/demo/templates/app.yaml', TEMPLATE)


def run(binary, helm, out):
    root = out / 'workspace'
    selected = setup(root)
    rows = []
    environment = dict(os.environ, XDG_CACHE_HOME=str(out / 'cache'))
    source_hashes = {str(p.relative_to(root)): wire.hash_file(p) for p in root.rglob('*') if p.is_file()}

    def record(name, checks, observed):
        rows.append({'id': name, 'assertions': checks, 'observed': observed, 'passed': bool(checks) and all(checks.values())})
        report = {'rows': rows, 'passed': all(x['passed'] for x in rows), 'binarySHA256': wire.hash_file(binary), 'helmSHA256': wire.hash_file(helm), 'runnerSHA256': wire.hash_file(Path(__file__)), 'command': sys.argv}
        (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(name, 'PASS' if rows[-1]['passed'] else 'FAIL', flush=True)

    def execute(name, arguments, source=root):
        folder = out / name
        folder.mkdir()
        command = [str(binary), 'render-check', '--root', str(source), '--file', str(source / 'charts/demo/templates/app.yaml'), '--helm-binary', str(helm), '--format', 'json'] + arguments
        process = subprocess.run(command, capture_output=True, timeout=45, env=environment)
        (folder / 'stdout').write_bytes(process.stdout)
        (folder / 'stderr').write_bytes(process.stderr)
        (folder / 'command.json').write_text(json.dumps(command))
        try: data = json.loads(process.stdout)
        except ValueError: data = {}
        renderer = data.get('renderer') or {}
        generated = Path(renderer['output']) if renderer.get('output') else None
        text = generated.read_text() if generated and generated.is_file() else ''
        return process, data, renderer, text

    identity = json.loads(subprocess.check_output([str(binary), 'version', '--json']))
    record('configuration-version-migration', {'advertisesBothVersions': identity.get('configSchemaVersions') == [1, 2]}, identity)

    for name, expected in [('dev', 'dev'), ('prod', 'prod'), ('reverse', 'base')]:
        process, data, renderer, text = execute('profile-' + name, ['--profile', name])
        paths = [str(root / value) for value in config()['renderProfiles'][name]['values']]
        values = renderer.get('valuesFiles', [])
        command = renderer.get('command', [])
        actual_order = [command[i+1] for i, value in enumerate(command[:-1]) if value == '--values']
        record('profile-' + name, {'realRenderComplete': process.returncode == 0 and data.get('analysisComplete') is True and data.get('resourcesAnalyzed') == 1, 'independentEnvironmentResult': 'name: acceptance-' + expected in text and 'namespace: testing' in text and 'special: "true"' in text, 'profileAndOrder': renderer.get('profile') == name and [v.get('path') for v in values] == paths and actual_order == paths, 'valueHashes': bool(values) and all(v.get('sha256') == wire.hash_file(Path(v['path'])) for v in values), 'helmIdentity': renderer.get('binarySHA256') == wire.hash_file(helm) and renderer.get('version', '').startswith(('v3.', 'v4.')), 'reportArtifact': bool(renderer.get('output')) and (Path(renderer['output']).parent / 'report.json').is_file()}, {'report': data, 'generated': text})

    process, data, renderer, text = execute('ordered-flags', ['--values', 'env/dev.yaml', '--values', 'env/common.yaml'])
    command = renderer.get('command', [])
    paths = [str(root / 'env/dev.yaml'), str(root / 'env/common.yaml')]
    record('ordered-values-flags', {'laterValuesWin': process.returncode == 0 and 'name: argo-ls-base' in text, 'bothArgumentsPreserved': [command[i+1] for i, v in enumerate(command[:-1]) if v == '--values'] == paths and [v.get('path') for v in renderer.get('valuesFiles', [])] == paths}, data)

    old_root = out / 'legacy-workspace'
    setup(old_root, {'schemaVersion': 1, 'targetVersion': '3.6.1'})
    process, data, renderer, text = execute('legacy-default', [], old_root)
    record('legacy-default-preserved', {'sameDefaults': process.returncode == 0 and 'name: argo-ls-default' in text and renderer.get('values') == '' and renderer.get('releaseName') == 'argo-ls' and renderer.get('namespace') == 'default', 'sourceUnchanged': (old_root / 'charts/demo/templates/app.yaml').read_text() == TEMPLATE}, data)

    refused = []
    for index, args in enumerate([['--chart', 'charts/demo'], ['--values', 'env/dev.yaml'], ['--release-name', 'other'], ['--namespace', 'other'], ['--kube-version', '1.30.0'], ['--api-version', 'other/v1']]):
        process, data, _, _ = execute('conflict-' + str(index), ['--profile', 'dev'] + args)
        refused.append({'args': args, 'exit': process.returncode, 'error': process.stderr.decode(errors='replace'), 'report': data})
    record('profile-overrides-explicitly-refused', {'allConflictsRejected': all(x['exit'] == 2 and 'profile' in x['error'].lower() and 'override' in x['error'].lower() for x in refused), 'sixIndependentOptions': len(refused) == 6}, refused)

    scoped_root = out / 'scoped-workspace'
    scoped_config = config()
    scoped_config.pop('targetVersion')
    scoped_config['scopes'] = [{'name': 'chart', 'include': ['charts/**'], 'targetVersion': '3.6.1'}, {'name': 'unrelated', 'include': ['other/**'], 'targetVersion': '2.0.0'}]
    setup(scoped_root, scoped_config)
    scope_observations = []
    for selected_file in [False, True]:
        command = [str(binary), 'render-check', '--root', str(scoped_root), '--profile', 'dev', '--scope', 'unrelated', '--helm-binary', str(helm), '--output', str(out / ('borrowed-' + str(selected_file))), '--format', 'json']
        if selected_file: command += ['--file', str(scoped_root / 'charts/demo/templates/app.yaml')]
        process = subprocess.run(command, capture_output=True, timeout=45, env=environment)
        (out / ('scope-' + str(selected_file) + '.stdout')).write_bytes(process.stdout)
        (out / ('scope-' + str(selected_file) + '.stderr')).write_bytes(process.stderr)
        scope_observations.append({'selectedFile': selected_file, 'exit': process.returncode, 'stderr': process.stderr.decode(errors='replace'), 'outputAbsent': not (out / ('borrowed-' + str(selected_file)) / 'generated.yaml').exists()})
    record('profile-cannot-borrow-scope', {'bothSelectionsRefused': all(x['exit'] == 2 and 'scope' in x['stderr'].lower() and x['outputAbsent'] for x in scope_observations)}, scope_observations)

    alias = out / 'output-alias'
    alias.symlink_to(old_root / 'charts/demo', target_is_directory=True)
    before = {str(p.relative_to(old_root)): wire.hash_file(p) for p in old_root.rglob('*') if p.is_file()}
    process, data, _, _ = execute('source-output-link', ['--output', str(alias)], old_root)
    record('custom-output-cannot-overwrite-chart', {'canonicalOutputRefused': process.returncode == 2 and 'output' in process.stderr.decode(errors='replace').lower(), 'chartBytesPreserved': before == {str(p.relative_to(old_root)): wire.hash_file(p) for p in old_root.rglob('*') if p.is_file()}}, {'report': data, 'stderr': process.stderr.decode(errors='replace')})

    collision_results = []
    for kind in ['hardlink', 'chosen-values']:
        collision_root = out / ('collision-' + kind)
        collision_file = setup(collision_root, {'schemaVersion': 1, 'targetVersion': '3.6.1'})
        output = out / ('collision-output-' + kind)
        output.mkdir()
        flags = ['--output', str(output)]
        if kind == 'hardlink': os.link(collision_file, output / 'generated.yaml')
        else:
            output = collision_root / 'env'
            (output / 'generated.yaml').write_text('environment: dev\n')
            flags = ['--output', str(output), '--values', 'env/generated.yaml']
        before = {str(p.relative_to(collision_root)): wire.hash_file(p) for p in collision_root.rglob('*') if p.is_file()}
        process, data, _, _ = execute('collision-result-' + kind, flags, collision_root)
        collision_results.append({'kind': kind, 'refused': process.returncode == 2, 'inputsPreserved': before == {str(p.relative_to(collision_root)): wire.hash_file(p) for p in collision_root.rglob('*') if p.is_file()}, 'stderr': process.stderr.decode(errors='replace')})
    record('custom-output-cannot-overwrite-inputs', {'inputCollisionsRefused': all(x['refused'] for x in collision_results), 'inputBytesPreserved': all(x['inputsPreserved'] for x in collision_results)}, collision_results)

    rotation_root = out / 'rotation-workspace'
    rotation_file = setup(rotation_root)
    rotation_output = out / 'rotation-output'
    rotation_output.mkdir()
    held = out / 'rotation-held'
    release_file = out / 'rotation-release'
    wrapper = out / 'rotation-helm'
    wrapper.write_text('#!' + sys.executable + '\nimport os,pathlib,sys,time\nif sys.argv[1]=="template":\n pathlib.Path(' + repr(str(held)) + ').write_text(str(os.getpid()))\n while not pathlib.Path(' + repr(str(release_file)) + ').exists():time.sleep(.01)\nos.execv(' + repr(str(helm)) + ', [' + repr(str(helm)) + '] + sys.argv[1:])\n')
    wrapper.chmod(0o700)
    before = {str(p.relative_to(rotation_root)): wire.hash_file(p) for p in rotation_root.rglob('*') if p.is_file()}
    command = [str(binary), 'render-check', '--root', str(rotation_root), '--file', str(rotation_file), '--profile', 'dev', '--output', str(rotation_output), '--helm-binary', str(wrapper), '--format', 'json']
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
    stdout = stderr = b''
    try:
        deadline = time.monotonic() + 8
        while not held.exists() and process.poll() is None and time.monotonic() < deadline: time.sleep(.01)
        started = held.exists()
        rotation_output.rename(out / 'rotation-original-output')
        rotation_output.symlink_to(rotation_root / 'charts/demo', target_is_directory=True)
        release_file.write_text('go')
        stdout, stderr = process.communicate(timeout=45)
        record('custom-output-rotation-is-refused', {'realRendererStarted': started, 'obsoleteDestinationRefused': process.returncode == 2, 'sourceChartUnchanged': before == {str(p.relative_to(rotation_root)): wire.hash_file(p) for p in rotation_root.rglob('*') if p.is_file()}}, {'command': command, 'exit': process.returncode, 'stdout': stdout.decode(errors='replace'), 'stderr': stderr.decode(errors='replace')})
    finally:
        release_file.write_text('go')
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        (out / 'rotation.stdout').write_bytes(stdout)
        (out / 'rotation.stderr').write_bytes(stderr)

    malformed = []
    fixtures = [('v1-empty', 'schemaVersion: 1\nrenderProfiles: {}\n', 'schemaVersion'), ('v1-null', 'schemaVersion: 1\nrenderProfiles: null\n', 'schemaVersion'), ('unknown-profile-key', 'schemaVersion: 2\nrenderProfiles: {dev: {chart: charts/demo, shell: arbitrary}}\n', 'shell'), ('duplicate-profile', 'schemaVersion: 2\nrenderProfiles:\n  dev: {chart: charts/demo}\n  dev: {chart: charts/demo}\n', 'dev'), ('escape', 'schemaVersion: 2\nrenderProfiles: {dev: {chart: ../outside}}\n', 'relative'), ('flag-release', 'schemaVersion: 2\nrenderProfiles: {dev: {chart: charts/demo, releaseName: "--post-renderer=anything"}}\n', 'release')]
    for name, configuration, expected in fixtures:
        invalid = out / ('invalid-' + name)
        setup(invalid)
        (invalid / '.argocd-ls.yaml').write_text(configuration)
        process, data, _, _ = execute('invalid-result-' + name, ['--profile', 'dev'], invalid)
        malformed.append({'id': name, 'exit': process.returncode, 'expectedReason': expected, 'stderr': process.stderr.decode(errors='replace'), 'report': data})
    record('strict-profile-configuration', {'declaredMigrationEnforced': all(x['exit'] == 2 and x['expectedReason'] in x['stderr'] for x in malformed), 'noSourceChanges': source_hashes == {str(p.relative_to(root)): wire.hash_file(p) for p in root.rglob('*') if p.is_file()}}, malformed)
    return all(row['passed'] for row in rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('binary', type=Path)
    parser.add_argument('helm', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.resolve().mkdir(parents=True, exist_ok=False)
    sys.exit(0 if run(args.binary.resolve(), args.helm.resolve(), args.output.resolve()) else 1)
