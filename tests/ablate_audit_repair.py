#!/usr/bin/env python3
"""Isolated removals of behavior guards, after the original oracle passes."""
import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

root = pathlib.Path(__file__).resolve().parents[1]
variants = [
    ('cancel', 'internal/lsp/server.go', 'if request, ok := requests[key]; ok {',
     'if request, ok := requests[key]; ok && false {', 'budget'),
    ('root-owner', 'internal/workspace/workspace.go', 'len(r) > len(owner)', 'owner == ""', 'lsp'),
    ('buffer-first', 'internal/workspace/workspace.go', 'snap, opened := buffers[resolved]',
     'snap, opened := buffers[resolved]; opened = false', 'lsp'),
    ('excluded-isolation', 'internal/workspace/stream.go', 'isolated: excluded && opened',
     'isolated: false', 'lsp'),
    ('array-fields', 'internal/analysis/queries.go', 'case []any:\n\t\t\ti, err := strconv.Atoi(p)',
     'case []any:\n            if len(v) >= 0 { return nil }; i, err := strconv.Atoi(p)', 'lsp'),
]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--variant', choices=[item[0] for item in variants])
parser.add_argument('--output', type=pathlib.Path, default=root / 'artifacts/audit-repair-ablation')
args = parser.parse_args()
out = args.output.resolve()
out.mkdir(parents=True, exist_ok=False)
selected = [item for item in variants if args.variant is None or item[0] == args.variant]
env = dict(os.environ, GOWORK='off', GOTOOLCHAIN='local', GOPROXY='off', CGO_ENABLED='1')
go = env.get('GO') or (str(pathlib.Path(env['GOROOT']) / 'bin/go')
                       if env.get('GOROOT') else shutil.which('go'))
report = {'command': sys.argv, 'python': sys.version, 'cases': [], 'controls': {}, 'passed': False,
          'environment': {key: env.get(key) for key in ('GO', 'GOROOT', 'GOMODCACHE', 'GOCACHE')},
          'testSHA256': hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),
          'oracleSHA256': hashlib.sha256((root / 'tests/audit_repair_e2e.py').read_bytes()).hexdigest()}


def run(command, folder, name, cwd=root, environment=env):
    result = subprocess.run(command, cwd=cwd, env=environment, capture_output=True, timeout=180)
    (folder / (name + '.stdout')).write_bytes(result.stdout)
    (folder / (name + '.stderr')).write_bytes(result.stderr)
    return {'command': command, 'exit': result.returncode}


def build(tree, folder):
    binary = folder / ('argocd-ls.exe' if os.name == 'nt' else 'argocd-ls')
    result = run([go, 'build', '-mod=readonly', '-buildvcs=false', '-trimpath', '-o', str(binary),
                  './cmd/argocd-ls'], folder, 'build', cwd=tree)
    if result['exit'] != 0:
        raise RuntimeError('Variant did not compile: ' + str(folder))
    return binary, result


def oracle(binary, folder, mode):
    environment = dict(env, CD_AUDIT_BINARY=str(binary), CD_AUDIT_OUTPUT=str(folder / 'oracle'))
    result = run([sys.executable, str(root / 'tests/audit_repair_e2e.py'), mode],
                 folder, 'oracle', environment=environment)
    result['report'] = json.loads((folder / 'oracle' / (mode + '-report.json')).read_text())
    return result


try:
    if not go:
        raise RuntimeError('Go toolchain is required (GO, GOROOT or PATH)')
    control = out / 'control'
    control.mkdir()
    binary, report['controlBuild'] = build(root, control)
    for mode in sorted({item[4] for item in selected}):
        folder = control / mode
        folder.mkdir()
        result = oracle(binary, folder, mode)
        report['controls'][mode] = result
        cases = result['report']['cases']
        if result['exit'] != 0 or not cases or not all(case['passed'] for case in cases):
            raise RuntimeError('Original oracle did not pass the unmodified binary: ' + mode)
    for name, file, old, new, mode in selected:
        folder = out / name
        folder.mkdir()
        row = {'variant': name, 'mutation': {'file': file, 'from': old, 'to': new}, 'detected': False}
        report['cases'].append(row)
        with tempfile.TemporaryDirectory(prefix='cd-repair-') as temporary:
            tree = pathlib.Path(temporary)
            for part in ('go.mod', 'go.sum', 'cmd', 'internal'):
                source = root / part
                shutil.copytree(source, tree / part) if source.is_dir() else shutil.copy2(source, tree / part)
            target = tree / file
            text = target.read_text()
            if text.count(old) != 1:
                raise RuntimeError(name + ': mutation anchor must occur exactly once')
            target.write_text(text.replace(old, new, 1))
            (folder / 'mutated-source.txt').write_bytes(target.read_bytes())
            binary, row['build'] = build(tree, folder)
            row['binarySHA256'] = hashlib.sha256(binary.read_bytes()).hexdigest()
            row['mutatedSourceSHA256'] = hashlib.sha256(target.read_bytes()).hexdigest()
            row['oracle'] = oracle(binary, folder, mode)
            cases = row['oracle']['report']['cases']
            row['detected'] = (row['oracle']['exit'] == 1 and bool(cases)
                               and any(not case['passed'] for case in cases)
                               and not any(case['observed'].get('exception') for case in cases))
            if name == 'excluded-isolation':
                # The moved guard must lose exactly the excluded-project boundary;
                # nested root ownership and the other original controls still pass.
                row['detected'] = row['detected'] and all(
                    case['passed'] == (case['id'] != 'opened-excluded-project') for case in cases)
            print(name, row['detected'], flush=True)
    report['passed'] = len(report['cases']) == len(selected) and all(row['detected'] for row in report['cases'])
except Exception as error:
    report['error'] = repr(error)
finally:
    (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
sys.exit(0 if report['passed'] else 1)
