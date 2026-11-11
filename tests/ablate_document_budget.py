#!/usr/bin/env python3
"""Require a passing baseline and a compiled document-lane budget counterexample."""
import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('binary', type=pathlib.Path)
    parser.add_argument('output', type=pathlib.Path)
    args = parser.parse_args()
    root = pathlib.Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    binary = args.binary.resolve()
    source = root / 'internal/lsp/server.go'
    consumer = root / 'tests/projection_aggregate_e2e.py'
    sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    inputs = [consumer, root / 'tests/audit_repair_e2e.py', root / 'tests/alias-projection-cases.json']
    report = {'passed': False, 'baselineBinarySHA256': sha(binary),
              'inputSHA256': {str(path.relative_to(root)): sha(path) for path in inputs},
              'expectedRow': 'False-publication-lifecycle',
              'expectedCheck': 'noFalseBudgetInAnyPhase'}

    def run(name, command, timeout):
        result = subprocess.run(command, cwd=root, capture_output=True, timeout=timeout)
        (output / (name + '.stdout')).write_bytes(result.stdout)
        (output / (name + '.stderr')).write_bytes(result.stderr)
        report[name] = {'command': command, 'exitCode': result.returncode}
        return result.returncode

    try:
        baseline_exit = run('baseline', [sys.executable, str(consumer), str(binary), str(output / 'baseline')], 180)
        baseline = json.loads((output / 'baseline/report.json').read_text())
        if baseline_exit != 0 or not baseline.get('passed') or len(baseline.get('rows', [])) != 15:
            raise ValueError('the identical consumer must pass all 15 baseline cases before mutation')
        if baseline.get('binarySHA256') != report['baselineBinarySHA256']:
            raise ValueError('baseline binary identity changed')

        original = source.read_text()
        needle = '\t\t\t\tif documentOnly {\n\t\t\t\t\tanalyzer.BeginFile()\n\t\t\t\t}'
        if original.count(needle) != 1:
            raise ValueError('document-lane budget boundary changed; review the ablation')
        mutant = output / 'server.go'
        mutant.write_text(original.replace(needle, '\t\t\t\t// Per-file budget reset deliberately removed.', 1))
        overlay = output / 'overlay.json'
        overlay.write_text(json.dumps({'Replace': {str(source): str(mutant)}}, indent=2) + '\n')
        report['originalSHA256'], report['mutantSHA256'] = sha(source), sha(mutant)
        go = str(pathlib.Path(os.environ['GOROOT']) / 'bin/go') if 'GOROOT' in os.environ else 'go'
        mutated_binary = output / ('argocd-ls.exe' if os.name == 'nt' else 'argocd-ls')
        command = [go, 'build', '-mod=vendor', '-buildvcs=false', '-overlay=' + str(overlay), '-o', str(mutated_binary), './cmd/argocd-ls']
        if run('build', command, 180) != 0:
            raise ValueError('a compilation failure is not a killed semantic mutant')
        report['mutantBinarySHA256'] = sha(mutated_binary)
        mutant_exit = run('mutant', [sys.executable, str(consumer), str(mutated_binary), str(output / 'mutant')], 180)
        observed = json.loads((output / 'mutant/report.json').read_text())
        rows = observed.get('rows', [])
        failures = [row['id'] for row in rows if not row.get('passed')]
        lifecycle = next((row for row in rows if row['id'] == report['expectedRow']), {})
        false_document_budgets = [event for event in lifecycle.get('observed', {}).get('unexpectedBudget', [])
                                 if event['message']['params'].get('x-argocd-analysis-scope') == 'document']
        report['failedRows'] = failures
        report['falseDocumentBudgets'] = false_document_budgets
        report['passed'] = (mutant_exit == 1 and len(rows) == 15 and
                            lifecycle.get('checks', {}).get(report['expectedCheck']) is False and
                            lifecycle.get('checks', {}).get('exit') is True and
                            bool(false_document_budgets) and
                            set(failures) <= {'False-publication-lifecycle', 'True-publication-lifecycle'} and
                            all(sha(path) == report['inputSHA256'][str(path.relative_to(root))] for path in inputs))
    except Exception as error:
        report['error'] = repr(error)
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: report.get(key) for key in ['passed', 'failedRows', 'error']}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
