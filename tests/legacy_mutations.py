#!/usr/bin/env python3
"""Compile one changed guard at a time and require its named behavioral failure.

The historical entry points delegate here. An unmodified consumer must pass
first; compilation failures, tracebacks, missing evidence, and unrelated failed
cases never count as detecting a regression.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
# family, mutation, live source, old, new, oracle, exact failing cases
VARIANTS = [
    ('schema', 'actual-document', 'internal/analysis/schema.go', 'compiled.Validate(value)', 'compiled.Validate(map[string]any{})', 'cd3_e2e.py', {'project-type', 'missing-value'}),
    ('domain', 'control-plane-namespace', 'internal/analysis/domain.go', 'if options.ControlPlaneNamespace == "" || literal(meta["namespace"]) == options.ControlPlaneNamespace {', 'if true {', 'domain_e2e.py', {'wrong-control-plane'}),
    ('appset', 'dynamic-reference-unknown', 'internal/analysis/domain.go', '&& !unknownRef {', '&& (!unknownRef || true) {', 'appset_e2e.py', {'dynamic-ref-propagates-unknown'}),
    ('workspace', 'explicit-target-precedence', 'internal/workspace/workspace.go', 'if override != "" {', 'if false {', 'workspace_e2e.py', {'override'}),
    ('lsp', 'stale-notification', 'internal/lsp/server.go', '!ok || p.TextDocument.Version <= old.version', '!ok', 'lsp_e2e.py', {'stale-notification-cannot-roll-back'}),
    ('fix', 'new-error-rejection', 'internal/analysis/fixes.go', 'if errors[key] < 0 {', 'if errors[key] < 0 && false {', 'lsp_e2e.py', {'fix-cannot-introduce-type-error'}),
    ('candidates', 'damaged-prefix-recovery', 'internal/analysis/reference_recovery.go', 'if damaged == nil {', 'if true {', 'candidates_e2e.py', {'damaged-reliable-project-and-foreign-recovery'}),
    ('candidates', 'literal-encoding', 'internal/analysis/candidates.go', 'if yaml.UnmarshalStrict([]byte(name), &value) != nil || value != name {', 'if (yaml.UnmarshalStrict([]byte(name), &value) != nil || value != name) && false {', 'candidates_e2e.py', {'yaml-literal-project-name-remains-string'}),
    ('candidates', 'source-prefix-span', 'internal/analysis/candidates.go', 'Start: start + 1, End: start + slash', 'Start: start, End: start + slash', 'candidates_e2e.py', {'source-ref-document-local-exact-prefix'}),
    ('pin', 'client-version', 'internal/lsp/pins.go', '"version": b.version', '"version": 0', 'pin_e2e.py', {'root-pin-open-version-bytes', 'scope-pin-insert-and-verified-default'}),
    ('pin', 'stale-revision', 'internal/lsp/pins.go', '(p.Data.Epoch != latest.editGeneration || p.Data.Revision != uint64(b.snapshot.Revision()))', 'false', 'pin_e2e.py', {'root-pin-open-version-bytes'}),
    ('pin-review', 'post-simulation-completeness', 'internal/lsp/pins.go', '[]completed{before, after}', '[]completed{before}', 'pin_review_e2e.py', {'dependency-damaged-between-action-and-resolve'}),
    ('pin-review', 'root-identity', 'internal/lsp/pins.go', 'if pin.Root {', 'if pin.Scope == "root" || pin.Scope == "unassigned" {', 'pin_review_e2e.py', {'named-root-scope-scalar-replacement'}),
]
COUNTS = {'cd3_e2e.py': 8, 'domain_e2e.py': 16, 'appset_e2e.py': 6,
          'workspace_e2e.py': 9, 'lsp_e2e.py': 25, 'candidates_e2e.py': 10,
          'pin_e2e.py': 3, 'pin_review_e2e.py': 13}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def valid_rows(rows):
    return bool(rows) and len({row.get('id') for row in rows}) == len(rows) and all(
        isinstance(row, dict) and type(row.get('passed')) is bool for row in rows)


def detected(script, control, result, wanted):
    """Fail closed on a consumer exception or a failure outside the named axis."""
    rows, baseline = result['cases'], control['cases']
    if type(result['exit']) is not int or result['exit'] != 1 or not valid_rows(rows):
        return False
    failed = {row['id'] for row in rows if not row['passed']}
    if failed != wanted:
        return False
    if script == 'lsp_e2e.py':
        # This ordered lifecycle consumer stops at its first failing assertion.
        index = next((i for i, row in enumerate(baseline) if row['id'] in wanted), -1)
        return (index > 0 and [row['id'] for row in rows] == [row['id'] for row in baseline[:index+1]]
                and all(row['passed'] for row in rows[:-1]) and rows[-1].get('errorType') == 'AssertionError')
    if [row['id'] for row in rows] != [row['id'] for row in baseline]:
        return False
    for row in rows:
        error = row.get('observed', {}).get('exception')
        if error and row.get('observed', {}).get('exceptionType') != 'AssertionError':
            return False
        if row.get('error') or any('Traceback' in str(value) for value in row.get('errors', [])):
            return False
    return any(row['passed'] for row in rows)


def invoke(command, cwd, env, folder, label, timeout=180):
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, timeout=timeout)
    (folder / (label + '.stdout')).write_bytes(result.stdout)
    (folder / (label + '.stderr')).write_bytes(result.stderr)
    return {'command': command, 'exit': result.returncode}


def oracle(binary, script, folder, env):
    evidence = folder / 'oracle'
    command = [sys.executable, str(ROOT / 'tests' / script)]
    if script not in ('candidates_e2e.py', 'pin_e2e.py', 'pin_review_e2e.py'):
        command.append(str(binary))
    if script == 'lsp_e2e.py':
        command.append(str(evidence))
    result = invoke(command, ROOT, dict(env, CD_AUDIT_BINARY=str(binary), CD_AUDIT_OUTPUT=str(evidence)), folder, 'oracle')
    path = evidence / ('report.json' if script in ('candidates_e2e.py', 'pin_e2e.py', 'pin_review_e2e.py') else 'results.json')
    report = json.loads(path.read_text())
    result['cases'] = report if isinstance(report, list) else report['cases']
    result['reportSHA256'] = sha(path)
    return result


def main(family=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--family', choices=sorted({item[0] for item in VARIANTS}), default=family)
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts' / ((family or 'legacy') + '-ablation'))
    args = parser.parse_args()
    selected = [item for item in VARIANTS if args.family is None or item[0] == args.family]
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, GOWORK='off', GOTOOLCHAIN='local', GOPROXY='off', CGO_ENABLED='1')
    go = env.get('GO') or (str(Path(env['GOROOT']) / 'bin/go') if env.get('GOROOT') else shutil.which('go'))
    report = {'runnerSHA256': sha(Path(__file__)), 'controls': {}, 'rows': [], 'passed': False}
    try:
        if not go:
            raise RuntimeError('Go is required through GO, GOROOT or PATH')
        control_dir = out / 'control'
        control_dir.mkdir()
        control_binary = control_dir / ('argocd-ls.exe' if os.name == 'nt' else 'argocd-ls')
        command = [go, 'build', '-mod=vendor', '-buildvcs=false', '-trimpath', '-o', str(control_binary), './cmd/argocd-ls']
        report['controlBuild'] = invoke(command, ROOT, env, control_dir, 'build')
        if report['controlBuild']['exit'] != 0:
            raise RuntimeError('Unmodified source did not compile')
        for script in sorted({item[5] for item in selected}):
            folder = control_dir / script.removesuffix('.py')
            folder.mkdir()
            result = oracle(control_binary, script, folder, env)
            report['controls'][script] = result
            if (result['exit'] != 0 or not valid_rows(result['cases']) or len(result['cases']) != COUNTS[script]
                    or not all(row['passed'] for row in result['cases'])):
                raise RuntimeError('Unmodified consumer did not pass every expected case: ' + script)
        for group, name, filename, before, after, script, wanted in selected:
            folder = out / (group + '-' + name)
            folder.mkdir()
            source = ROOT / filename
            text = source.read_text()
            if text.count(before) != 1:
                raise RuntimeError(name + ': mutation anchor must occur exactly once in current source')
            changed = folder / source.name
            changed.write_text(text.replace(before, after, 1))
            overlay = folder / 'overlay.json'
            overlay.write_text(json.dumps({'Replace': {str(source): str(changed)}}) + '\n')
            binary = folder / control_binary.name
            command = [go, 'build', '-mod=vendor', '-buildvcs=false', '-trimpath', '-overlay=' + str(overlay), '-o', str(binary), './cmd/argocd-ls']
            row = {'id': group + '-' + name, 'source': filename, 'before': before, 'after': after,
                   'sourceSHA256': sha(source), 'mutationSHA256': sha(changed), 'expectedFailures': sorted(wanted), 'passed': False}
            report['rows'].append(row)
            row['build'] = invoke(command, ROOT, env, folder, 'build')
            if row['build']['exit'] == 0:
                row['binarySHA256'] = sha(binary)
                row['oracle'] = oracle(binary, script, folder, env)
                row['passed'] = detected(script, report['controls'][script], row['oracle'], wanted)
            print(json.dumps({'id': row['id'], 'passed': row['passed'], 'failed': [r['id'] for r in row.get('oracle', {}).get('cases', []) if not r['passed']]}), flush=True)
        report['passed'] = len(report['rows']) == len(selected) and all(row['passed'] for row in report['rows'])
    except Exception as error:
        report['error'] = repr(error)
    (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'error': report.get('error')}), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
