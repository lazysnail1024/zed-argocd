#!/usr/bin/env python3
"""Compile two isolated semantic ablations and replay the complete consumer.

Only a successful build, a naturally completed real LSP consumer, and exactly
the declared lost guard count as a killed mutant. The product tree is read-only.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from reference_candidates_e2e import CASE_CHECKS

ROOT = Path(__file__).resolve().parents[1]
CONSUMER = 'tests/reference_candidates_e2e.py'
SOURCE = 'internal/lsp/reference_candidates.go'
MUTATIONS = [
    {
        'id': 'index-truncation',
        'needle': '\treturn result, proof, event, complete, matches.Truncated || !matches.Complete, nil',
        'replacement': '\treturn result, proof, event, complete, false, nil',
        'failures': {'query-limit-is-explicit': ['notClaimedComplete', 'truncated']},
    },
    {
        'id': 'target-identity',
        'needle': '\t\tif !reflect.DeepEqual(current, params.Argument) {',
        # Keep selection of the requested Candidate and all source/configuration
        # guards. Only Target's hash/open/version/revision proof is removed.
        'replacement': '\t\tif !reflect.DeepEqual(current.Source, params.Argument.Source) || current.SourceHash != params.Argument.SourceHash || current.Root != params.Argument.Root || current.RootEvent != params.Argument.RootEvent || current.Override != params.Argument.Override || !reflect.DeepEqual(current.InputTarget, params.Argument.InputTarget) || !reflect.DeepEqual(current.Candidate, params.Argument.Candidate) {',
        'failures': {'target-change-refuses-old-action': ['noLateOpen', 'oldExecuteIsStale', 'oldResolveIsStale']},
    },
]
ASSERTIONS = ['sourceBoundaryMatched', 'buildSucceeded', 'consumerCompletedNormally', 'onlyNamedChecksFailed', 'specificCounterexample', 'sourceUnchanged']


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def complete_consumer(report, binary_sha):
    rows = report.get('rows', [])
    return (report.get('binarySHA256') == binary_sha
            and report.get('runnerSHA256') == sha(ROOT / CONSUMER)
            and [row.get('id') for row in rows] == list(CASE_CHECKS)
            and all(set(row.get('assertions', {})) == set(CASE_CHECKS[row['id']]) | {'cleanExit', 'diskUnchanged'}
                    and all(type(value) is bool for value in row['assertions'].values())
                    and row['assertions'].get('cleanExit') is True
                    and row['assertions'].get('diskUnchanged') is True
                    and not row.get('observed', {}).get('exception')
                    and not row.get('observed', {}).get('closeException') for row in rows))


def failed_checks(report):
    return {row['id']: sorted(key for key, value in row['assertions'].items() if value is not True)
            for row in report.get('rows', []) if any(value is not True for value in row.get('assertions', {}).values())}


def specific_counterexample(report, mutation):
    rows = {row['id']: row for row in report.get('rows', [])}
    if mutation['id'] == 'index-truncation':
        row = rows.get('query-limit-is-explicit', {})
        current = (row.get('observed', {}).get('status', {}).get('result') or {}).get('referenceCandidates') or {}
        return (row.get('assertions', {}).get('singleExactItemBelowDisplayLimit') is True
                and len(current.get('items', [])) == 1 and current.get('scopeComplete') is True
                and current.get('truncated') is False and current.get('complete') is True)
    row = rows.get('target-change-refuses-old-action', {})
    observed = row.get('observed', {})
    resolved, executed = observed.get('oldResolve', {}), observed.get('oldExecute', {})
    opened = (executed.get('result') or {}).get('location')
    requests = observed.get('hostRequests', [])
    return ('result' in resolved and 'error' not in resolved and opened is not None and 'error' not in executed
            and len(requests) == 1 and requests[0].get('method') == 'window/showDocument'
            and requests[0].get('params', {}).get('uri') == opened.get('uri')
            and requests[0].get('params', {}).get('selection') == opened.get('range'))


def mutation_evidence(report, mutation, binary_sha):
    return (complete_consumer(report, binary_sha) and report.get('passed') is False
            and failed_checks(report) == mutation['failures'] and specific_counterexample(report, mutation)
            and all(row.get('passed') is (row['id'] not in mutation['failures']) for row in report['rows']))


def copy_sources(destination):
    listed = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT)
    paths = sorted(set(name.decode('utf-8') for name in listed.split(b'\0') if name))
    inventory = {}
    for name in paths:
        source, target = ROOT / name, destination / name
        if not source.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        inventory[name] = sha(target)
    if SOURCE not in inventory or CONSUMER not in inventory:
        raise ValueError('isolated source snapshot is missing product or consumer input')
    return inventory


def run_command(folder, label, command, cwd, timeout):
    result = {'command': command, 'exitCode': None, 'finished': False}
    try:
        process = subprocess.run(command, cwd=cwd, capture_output=True, timeout=timeout)
        (folder / (label + '.stdout')).write_bytes(process.stdout)
        (folder / (label + '.stderr')).write_bytes(process.stderr)
        result.update(exitCode=process.returncode, finished=True)
    except subprocess.TimeoutExpired as error:
        (folder / (label + '.stdout')).write_bytes(error.stdout or b'')
        (folder / (label + '.stderr')).write_bytes(error.stderr or b'')
        result['error'] = 'timeout; not a semantic counterexample'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {'runnerSHA256': sha(Path(__file__)), 'consumerSHA256': sha(ROOT / CONSUMER), 'rows': [], 'passed': False}
    live_source_sha = sha(ROOT / SOURCE)
    go = os.environ.get('GO') or (str(Path(os.environ['GOROOT']) / 'bin/go') if 'GOROOT' in os.environ else 'go')

    def save():
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')

    try:
        snapshot = output / 'source'
        inventory = copy_sources(snapshot)
        (output / 'source-inputs.json').write_text(json.dumps(inventory, indent=2) + '\n')
        report['sourceInventorySHA256'] = sha(output / 'source-inputs.json')
        original = (snapshot / SOURCE).read_text()
        report['sourceSHA256'] = inventory[SOURCE]
        baseline = output / 'baseline'
        baseline.mkdir()
        binary = baseline / ('argocd-ls.exe' if os.name == 'nt' else 'argocd-ls')
        command = [go, 'build', '-mod=vendor', '-buildvcs=false', '-o', str(binary), './cmd/argocd-ls']
        report['baselineBuild'] = run_command(baseline, 'build', command, snapshot, 180)
        if report['baselineBuild']['exitCode'] != 0:
            raise ValueError('unmodified isolated baseline must compile')
        report['baselineBinarySHA256'] = sha(binary)
        command = [sys.executable, str(snapshot / CONSUMER), str(binary), str(baseline / 'consumer')]
        report['baselineConsumer'] = run_command(baseline, 'consumer', command, snapshot, 120)
        control = json.loads((baseline / 'consumer/report.json').read_text())
        report['baselinePassed'] = (report['baselineConsumer']['exitCode'] == 0 and complete_consumer(control, report['baselineBinarySHA256'])
                                    and control.get('passed') is True and not failed_checks(control))
        if not report['baselinePassed']:
            raise ValueError('the complete identical consumer must pass before ablation')
        print(json.dumps({'stage': 'baseline', 'passed': True}), flush=True)
        save()
        for mutation in MUTATIONS:
            folder = output / mutation['id']
            folder.mkdir()
            row = {'id': mutation['id'], 'assertions': {name: False for name in ASSERTIONS}, 'passed': False}
            report['rows'].append(row)
            row['assertions']['sourceBoundaryMatched'] = original.count(mutation['needle']) == 1
            if not row['assertions']['sourceBoundaryMatched']:
                raise ValueError('mutation boundary changed; review ' + mutation['id'])
            changed = folder / 'reference_candidates.go'
            changed.write_text(original.replace(mutation['needle'], mutation['replacement'], 1))
            row['mutatedSourceSHA256'] = sha(changed)
            overlay = folder / 'overlay.json'
            overlay.write_text(json.dumps({'Replace': {str(snapshot / SOURCE): str(changed)}}, indent=2) + '\n')
            binary = folder / ('argocd-ls.exe' if os.name == 'nt' else 'argocd-ls')
            command = [go, 'build', '-mod=vendor', '-buildvcs=false', '-overlay=' + str(overlay), '-o', str(binary), './cmd/argocd-ls']
            row['build'] = run_command(folder, 'build', command, snapshot, 180)
            row['assertions']['buildSucceeded'] = row['build']['exitCode'] == 0
            if row['assertions']['buildSucceeded']:
                row['mutantBinarySHA256'] = sha(binary)
                command = [sys.executable, str(snapshot / CONSUMER), str(binary), str(folder / 'consumer')]
                row['consumer'] = run_command(folder, 'consumer', command, snapshot, 120)
                child = json.loads((folder / 'consumer/report.json').read_text())
                row['failedChecks'] = failed_checks(child)
                row['assertions']['consumerCompletedNormally'] = row['consumer']['finished'] is True and row['consumer']['exitCode'] == 1 and complete_consumer(child, row['mutantBinarySHA256'])
                row['assertions']['onlyNamedChecksFailed'] = row['failedChecks'] == mutation['failures']
                row['assertions']['specificCounterexample'] = specific_counterexample(child, mutation)
            row['assertions']['sourceUnchanged'] = sha(ROOT / SOURCE) == live_source_sha and sha(snapshot / SOURCE) == inventory[SOURCE] and sha(ROOT / CONSUMER) == report['consumerSHA256']
            row['passed'] = all(row['assertions'].values())
            print(json.dumps({'id': row['id'], 'passed': row['passed'], 'failedChecks': row.get('failedChecks')}), flush=True)
            save()
        report['passed'] = report['baselinePassed'] and len(report['rows']) == len(MUTATIONS) and all(row['passed'] for row in report['rows'])
    except Exception as error:
        report['error'] = repr(error)
    save()
    print(json.dumps({'passed': report['passed'], 'error': report.get('error')}), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
