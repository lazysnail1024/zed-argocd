#!/usr/bin/env python3
"""Complement native platform coverage with independent Linux semantic axes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from native_ci import start_command

ROOT = Path(__file__).resolve().parents[1]
# IDs are disjoint. Controls inside mutation suites intentionally replay the
# unmodified consumer before compiling a fault; that repetition proves the oracle.
CASES = {
    'contracts': [
        ('catalog-integrity', 'tests/catalog_e2e.py', [], 'results.json', 30),
        ('alias-provenance', 'tests/alias_provenance_edges_e2e.py', ['{binary}', '{case}'], 'report.json', 60),
        ('config-input', 'tests/config_input_e2e.py', ['{binary}', '{case}'], 'report.json', 60),
        ('candidate-values', 'tests/candidate_review_e2e.py', [], 'report.json', 90),
        ('parser-depth', 'tests/dependency_depth_e2e.py', [], 'deep-budget-and-recovery/report.json', 60),
        ('scope-globs', 'tests/scope_glob_e2e.py', ['{binary}', '{case}'], 'report.json', 60),
        ('scope-refresh', 'tests/scope_refresh_e2e.py', ['{binary}', '{case}'], 'report.json', 90),
        ('schema-repair-queries', 'tests/schema_queries_e2e.py', ['{binary}', '{case}', '--cases', '{root}/tests/schema-query-repair-cases.json'], 'report.json', 120),
        ('schema-boundary-queries', 'tests/schema_queries_e2e.py', ['{binary}', '{case}', '--cases', '{root}/tests/schema-query-boundary-cases.json'], 'report.json', 120),
        ('schema-comment-replay', 'tests/schema_query_comment_e2e.py', ['{binary}', '{output}/schema-boundary-queries/report.json', '{case}'], 'report.json', 60),
        ('schema-fixture', 'tests/schema_fixture_e2e.py', ['{case}'], 'report.json', 240),
        ('schema-repair-fixture', 'tests/schema_fixture_e2e.py', ['{case}', '--fixture', 'schema-query-repair-fixture.json', '--cases', 'schema-query-repair-fixture-cases.json'], 'report.json', 240),
        ('schema-boundary-fixture', 'tests/schema_fixture_e2e.py', ['{case}', '--fixture', 'schema-query-boundary-fixture.json', '--cases', 'schema-query-boundary-fixture-cases.json'], 'report.json', 240),
        ('domain-boundaries', 'tests/domain_matrix_e2e.py', ['{binary}', '--versions', '3.0.0', '3.4.12', '3.5.6', '--output', '{case}'], 'report.json', 180),
        ('domain-transitions', 'tests/domain_matrix_lsp_e2e.py', [], 'report.json', 180),
        ('scope-isolation', 'tests/audit_repair_e2e.py', ['cli'], 'cli-report.json', 60),
        ('snippets', 'editors/zed/tests/snippets_e2e.py', ['{binary}', '{case}'], 'results.json', 60),
    ],
    'boundaries': [
        ('render-defaults-helm3', 'tests/render_defaults_e2e.py', ['--helm', '{helm}'], 'report.json', 120),
        ('render-defaults-helm4', 'tests/render_defaults_e2e.py', ['--helm', '{helm4}'], 'report.json', 120),
        ('render-selected-scope', 'tests/render_scope_e2e.py', ['--helm', '{helm}'], 'report.json', 120),
        ('completed-work', 'tests/bounded_accounting_e2e.py', ['{binary}', '{case}'], 'report.json', 60),
        ('renderer-input', 'tests/renderer_input_e2e.py', ['{binary}', '{case}'], 'report.json', 60),
        ('renderer-process-tree', 'tests/bounded_process_e2e.py', ['{binary}', '{case}'], 'report.json', 180),
        ('renderer-acquisition', 'tests/render_acquisition_e2e.py', ['{binary}', '{case}'], 'report.json', 90),
        ('configuration-terminal', 'tests/explain_terminal_e2e.py', ['{binary}', '{case}'], 'report.json', 60),
        ('real-helm', 'tests/helm_e2e.py', ['{binary}', '{helm}'], 'results.json', 90),
        ('render-parity', 'tests/readiness_render_e2e.py', ['{binary}', '{helm}', '{case}'], 'report.json', 180),
        ('output-failures', 'tests/output_failure_e2e.py', ['{binary}', '{helm}', '{case}'], 'report.json', 180),
    ],
    'mutations': [
        ('config-helm-guards', 'tests/ablate_config_helm.py', ['--run', '--binary', '{binary}', '--helm', '{helm}', '--output', '{case}'], 'report.json', 600),
        ('consumer-oracles', 'tests/oracle_repair_mutation.py', ['{binary}', '{case}'], 'report.json', 180),
        ('named-guards', 'tests/legacy_mutations.py', ['--output', '{case}'], 'report.json', 600),
        ('schema-constraints', 'tests/schema_constraints_ablation.py', ['--output', '{case}'], 'report.json', 300),
        ('query-guards', 'tests/ablate_schema_queries.py', ['{case}'], 'report.json', 300),
        ('query-boundaries', 'tests/ablate_schema_boundaries.py', ['{case}'], 'report.json', 300),
        ('lifecycle-facts', 'tests/shared_lifecycle_mutations.py', ['{case}'], 'report.json', 300),
        ('workspace-guards', 'tests/ablate_audit_repair.py', ['--output', '{case}'], 'report.json', 600),
    ],
}


def evidence_passed(value):
    if not isinstance(value, dict):
        return False
    if 'passed' in value and value['passed'] is not True:
        return False
    if value.get('error') or value.get('runnerError') or value.get('exception'):
        return False
    rows = value.get('rows', value.get('cases', value.get('results')))
    if rows is not None:
        return isinstance(rows, list) and bool(rows) and all(row.get('passed', row.get('detected', row.get('caughtExpectedRegression'))) is True for row in rows)
    return value.get('passed') is True


def verify_consumer_identity(result, folder, binary_sha, helm_sha=None):
    """Accept the consumer's actual invocation receipt, not just our argv."""
    receipt = result
    if 'binarySHA256' not in receipt:
        receipt = json.loads((folder / 'manifest.json').read_text())
    if receipt.get('binarySHA256') != binary_sha:
        raise ValueError('Consumer evidence identifies a different native binary')
    if helm_sha is not None and receipt.get('helmSHA256') != helm_sha:
        raise ValueError('Consumer evidence identifies a different Helm executable')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('suite', choices=CASES)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--helm', type=Path)
    parser.add_argument('--helm4', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    binary = args.binary.resolve()
    report = {'suite': args.suite, 'binarySHA256': hashlib.sha256(binary.read_bytes()).hexdigest(),
              'runnerSHA256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'schemaVersion':2, 'workflowRun':{key:os.environ.get(key) for key in ('GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT')},
              'producerReceipt':json.loads((binary.parent/'receipt.json').read_text()) if (binary.parent/'receipt.json').is_file() else None,
              'expectedCases': [case[0] for case in CASES[args.suite]], 'rows': [], 'passed': False}
    for ident, script, arguments, report_path, timeout in CASES[args.suite]:
        folder = out / ident
        substitutions = {'root': str(ROOT), 'output': str(out), 'binary': str(binary), 'case': str(folder),
                         'helm': str(args.helm.resolve()) if args.helm else '', 'helm4':str(args.helm4.resolve()) if args.helm4 else ''}
        command = [sys.executable, str(ROOT / script), *[item.format(**substitutions) for item in arguments]]
        environment = dict(os.environ, CD_AUDIT_BINARY=str(binary), CD_AUDIT_OUTPUT=str(folder), CD_MATRIX_LSP_OUT=str(folder), PYTHONUTF8='1')
        row = {'id': ident, 'command': command, 'passed': False}
        started = time.monotonic()
        process = close = None
        try:
            if '{helm4}' in arguments and not args.helm4:
                raise ValueError('The pinned real Helm4 executable is required')
            if '{helm}' in arguments and not args.helm:
                raise ValueError('The pinned real Helm executable is required for renderer acceptance')
            process, close = start_command(command, environment)
            stdout, stderr = process.communicate(timeout=timeout)
            (out / (ident + '.stdout')).write_bytes(stdout)
            (out / (ident + '.stderr')).write_bytes(stderr)
            row['exit'] = process.returncode
            evidence = folder / report_path
            result = json.loads(evidence.read_text())
            row['reportSHA256'] = hashlib.sha256(evidence.read_bytes()).hexdigest()
            if args.suite != 'mutations' and ident != 'catalog-integrity' and script != 'tests/schema_fixture_e2e.py':
                selected_helm=args.helm4 if '{helm4}' in arguments else args.helm if '{helm}' in arguments else None
                helm_sha=hashlib.sha256(selected_helm.resolve().read_bytes()).hexdigest() if selected_helm else None
                verify_consumer_identity(result, folder, report['binarySHA256'], helm_sha)
            row['passed'] = process.returncode == 0 and evidence_passed(result)
        except Exception as error:
            row['error'] = repr(error)
        finally:
            if close:
                close()
            if process and process.poll() is None:
                process.kill()
                process.communicate(timeout=3)
            row['elapsedSeconds'] = time.monotonic() - started
            report['rows'].append(row)
            (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(row), flush=True)
    report['passed'] = [row['id'] for row in report['rows']] == report['expectedCases'] and all(row['passed'] for row in report['rows'])
    (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
