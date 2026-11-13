#!/usr/bin/env python3
"""Prove CLI and mutation oracles reject false success and unrelated failures.

Wrappers alter real server output one dimension at a time. Their reports are
oracle controls; they are not evidence that a modified server was executed.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from legacy_mutations import detected

ROOT = Path(__file__).resolve().parents[1]


def classifier_controls():
    baseline = {'exit': 0, 'cases': [{'id': 'changed', 'passed': True}, {'id': 'control', 'passed': True}]}
    failure = {'exit': 1, 'cases': [{'id': 'changed', 'passed': False}, {'id': 'control', 'passed': True}]}
    variants = [('named-assertion', failure, True)]
    for name in ('unrelated-failure', 'timeout', 'missing-positive-control', 'missing-evidence', 'boolean-exit', 'all-passed'):
        result = copy.deepcopy(failure)
        if name == 'unrelated-failure': result['cases'][1]['passed'] = False
        elif name == 'timeout': result['cases'][0]['observed'] = {'exception': 'TimeoutError()', 'exceptionType': 'TimeoutError'}
        elif name == 'missing-positive-control': result['cases'].pop()
        elif name == 'missing-evidence': result['cases'] = []
        elif name == 'boolean-exit': result['exit'] = True
        elif name == 'all-passed': result['cases'][0]['passed'] = True
        variants.append((name, result, False))
    return [{'id': 'classifier-' + name, 'expectedDetection': expected,
             'passed': detected('candidates_e2e.py', baseline, result, {'changed'}) is expected}
            for name, result, expected in variants]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    binary, out = args.binary.resolve(), args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    rows = classifier_controls()
    mutations = {'baseline': '', 'wrong-exit': 'exit_code = 0',
                 'unexpected-error': "report['diagnostics'].append({'uri':'file:///injected', 'range':{'start':{'line':0,'character':0},'end':{'line':0,'character':1}},'code':'INJECTED_ERROR','source':'argocd-ls','severity':1,'message':'oracle negative control'})",
                 'drop-diagnostics': "report['diagnostics'] = []", 'false-incomplete': "report['analysisComplete'] = False"}
    try:
        for script, count in [('appset_e2e.py', 6), ('domain_e2e.py', 16)]:
            for name, mutation in mutations.items():
                folder = out / (script.removesuffix('.py') + '-' + name)
                folder.mkdir()
                wrapper = folder / 'report_wrapper.py'
                wrapper.write_text('#!/usr/bin/env python3\nimport json,subprocess,sys\np=subprocess.run(' + repr([str(binary)]) + '+sys.argv[1:],capture_output=True,timeout=20)\nexit_code=p.returncode\nreport=json.loads(p.stdout)\n' + mutation + '\nsys.stdout.write(json.dumps(report)+"\\n")\nsys.stderr.buffer.write(p.stderr)\nsys.exit(exit_code)\n')
                wrapper.chmod(0o755)
                evidence = folder / 'oracle'
                command = [sys.executable, str(ROOT / 'tests' / script), str(wrapper)]
                result = subprocess.run(command, cwd=ROOT, env=dict(os.environ, CD_AUDIT_OUTPUT=str(evidence)), capture_output=True, timeout=90)
                (folder / 'stdout').write_bytes(result.stdout)
                (folder / 'stderr').write_bytes(result.stderr)
                cases = json.loads((evidence / 'results.json').read_text())
                failed = [row for row in cases if not row['passed']]
                checks = {'exit': result.returncode == (0 if name == 'baseline' else 1),
                          'allCasesExecuted': len(cases) == count and len({row['id'] for row in cases}) == count,
                          'structuredAssertions': all(isinstance(row.get('errors'), list) for row in cases),
                          'outcome': not failed if name == 'baseline' else bool(failed),
                          'noHarnessCrash': b'Traceback' not in result.stderr}
                if name in ('wrong-exit', 'false-incomplete'):
                    checks['specificFailure'] = any(any('exit' in error or 'severity exit' in error for error in row['errors']) for row in failed)
                if name == 'unexpected-error':
                    checks['specificFailure'] = all(any('Error' in error for error in row['errors']) for row in failed)
                if name == 'drop-diagnostics':
                    checks['specificFailure'] = any(any('missing ' in error for error in row['errors']) for row in failed)
                rows.append({'id': script + '-' + name, 'command': command, 'checks': checks,
                             'failedCases': [row['id'] for row in failed], 'passed': all(checks.values())})
    except Exception as error:
        rows.append({'id': 'runner', 'passed': False, 'error': repr(error)})
    report = {'scope': 'oracle controls, not product mutation execution', 'binarySHA256': hashlib.sha256(binary.read_bytes()).hexdigest(),
              'runnerSHA256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'rows': rows,
              'passed': len(rows) == 17 and all(row['passed'] for row in rows)}
    (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'failed': [row['id'] for row in rows if not row['passed']]}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
