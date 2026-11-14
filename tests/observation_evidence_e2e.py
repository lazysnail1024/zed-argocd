#!/usr/bin/env python3
"""Replay frozen, real positive evidence and minimally malformed copies."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--contracts', type=Path)
parser.add_argument('--mutations', type=Path)
parser.add_argument('--retained', type=Path)
parser.add_argument('--resources', type=Path)
parser.add_argument('--verifier-path', type=Path, default=ROOT/'tests')
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
OUT = args.output.resolve()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replay(name, source, consumer, mutation=None, script=None, strict=True, folder=None):
    source = ROOT / source
    original = json.loads(source.read_text())
    value = copy.deepcopy(original)
    if mutation:
        mutation(value)
    destination = OUT / name
    destination.mkdir(parents=True, exist_ok=False)
    report = destination / 'input.json'
    report.write_text(json.dumps(value, indent=2) + '\n')
    if script is None:
        code = "import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from resource_ci import load_json;from orthogonal_ci import evidence_passed,verify_consumer_identity;v=load_json(Path(sys.argv[2]).read_text());"
        if 'binarySHA256' in original:
            code += "verify_consumer_identity(v,Path(sys.argv[3]),sys.argv[4]);"
        code += "assert evidence_passed(v,sys.argv[5],Path(sys.argv[3]));print('ACCEPTED')"
        argv = [sys.executable, '-c', code, str(args.verifier_path.resolve()), str(report), str(folder or source.parent), str(original.get('binarySHA256')), consumer]
    else:
        loader = "load_json(Path(sys.argv[2]).read_text())" if strict else "json.loads(Path(sys.argv[2]).read_text())"
        code = "import sys,json;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from resource_ci import load_json,validate_report;v=" + loader + ";print('ACCEPTED',validate_report(v,sys.argv[3],sys.argv[4],sys.argv[5],sys.argv[6]))"
        argv = [sys.executable, '-c', code, str(args.verifier_path.resolve()), str(report), consumer, script, original['binarySHA256'], original.get('runnerSHA256', original.get('testSHA256'))]
    argv[2]=argv[2].replace('sys.path.insert(0,sys.argv[1]);', 'sys.path.insert(0,'+repr(str(ROOT/'tests'))+');sys.path.insert(0,sys.argv[1]);')
    process = subprocess.run(argv, cwd=ROOT, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), capture_output=True)
    (destination / 'stdout').write_bytes(process.stdout)
    (destination / 'stderr').write_bytes(process.stderr)
    row = {'id': name, 'source': str(source), 'sourceSHA256': digest(source), 'inputSHA256': digest(report), 'command': argv, 'exit': process.returncode, 'accepted': process.returncode == 0, 'expectedAccepted': mutation is None, 'strictJSON': strict, 'rejection':process.stderr.decode('utf-8',errors='replace')[-1000:] if process.returncode else None}
    row['expectationMet'] = row['accepted'] == row['expectedAccepted']
    (destination / 'receipt.json').write_text(json.dumps(row, indent=2) + '\n')
    return row


def main():
    OUT.mkdir(parents=True, exist_ok=False)
    rows = []
    orthogonal = [
        ('parser-depth', 'parser-depth/deep-budget-and-recovery/report.json', 'abnormal-exit', lambda v: v['cases'][0].update(exit=-9)),
        ('candidate-values', 'candidate-values/report.json', 'missing-observed', lambda v: v['cases'][0].pop('observed')),
        ('scope-globs', 'scope-globs/report.json', 'wrong-actual', lambda v: v['rows'][0].update(actual=[])),
        ('domain-transitions', 'domain-transitions/report.json', 'wrong-actual', lambda v: v['cases'][0].update(actual=[])),
    ]
    for consumer, relative, suffix, mutation in (orthogonal if args.contracts else []):
        source = args.contracts / relative
        rows.append(replay(consumer + '-original', source, consumer))
        rows.append(replay(consumer + '-' + suffix, source, consumer, mutation))
    if args.mutations:
        source = args.mutations / 'lifecycle-facts/report.json'
        rows.append(replay('lifecycle-facts-original', source, 'lifecycle-facts'))
        rows.append(replay('lifecycle-facts-boolean-control-exit', source, 'lifecycle-facts', lambda v: v['control'].update(exit=False)))
        raw_missing = OUT / 'empty-lifecycle-folder'
        raw_missing.mkdir()
        rows.append(replay('lifecycle-facts-missing-raw', source, 'lifecycle-facts', lambda v: None, folder=raw_missing))
        source = args.mutations / 'workspace-guards/report.json'
        rows.append(replay('workspace-guards-original', source, 'workspace-guards'))
        rows.append(replay('workspace-guards-missing-positive-controls', source, 'workspace-guards', lambda v: v.pop('controls')))
        rows.append(replay('workspace-guards-missing-mutant-observation', source, 'workspace-guards', lambda v: v['cases'][0]['oracle']['report']['cases'][0].pop('observed')))
        rows.append(replay('workspace-guards-no-behavior-failure', source, 'workspace-guards', lambda v: v['cases'][0]['oracle']['report']['cases'][0]['observed']['assertions'].update(cancellationObservedWithin200ms=True)))
    if args.retained:
        source = args.retained
        rows.append(replay('retained-budget-original', source, 'retained-boundary-multiroot', script='retained_budget_e2e.py'))
        for suffix, mutation in [
            ('zero-rss', lambda v: v['rows'][0]['observed'].update(rss=0)),
            ('boolean-rss', lambda v: v['rows'][0]['observed'].update(rss=True)),
            ('missing-rss', lambda v: v['rows'][0]['observed'].pop('rss')),
            ('zero-samples', lambda v: v['metadata'].update(replacementRSS=[0] * 5)),
            ('missing-samples', lambda v: v['metadata'].pop('replacementRSS')),
            ('boolean-samples', lambda v: v['metadata'].update(replacementRSS=[True] * 5)),
            ('nan-samples-direct', lambda v: v['metadata'].update(replacementRSS=[float('nan')] * 5)),
        ]:
            rows.append(replay('retained-budget-' + suffix, source, 'retained-boundary-multiroot', mutation, script='retained_budget_e2e.py', strict=suffix != 'nan-samples-direct'))
    if args.resources:
        source = args.resources / 'full-cli/report.json'
        rows.append(replay('full-cli-original', source, 'full-cli', script='bounded_cli_e2e.py'))
        rows.append(replay('full-cli-process-over-budget', source, 'full-cli', lambda v: v['rows'][5].update(processElapsedSeconds=1000), script='bounded_cli_e2e.py'))
        def slow_consistent(value):
            row=value['rows'][5]
            extra=6-row['processElapsedSeconds']
            row['processElapsedSeconds']=6
            for key in ('elapsedSeconds','totalElapsedSeconds'):
                row[key]+=extra
            for key in ('processObservedEnd','decoded','verified'):
                row['phaseSeconds'][key]+=extra
        rows.append(replay('full-cli-consistent-process-over-budget', source, 'full-cli', slow_consistent, script='bounded_cli_e2e.py'))
    report = {'scope':'Production observation acceptance controls, not product execution', 'python':sys.version,
              'replayScriptSHA256':digest(Path(__file__)), 'verifierSHA256':{name:digest(args.verifier_path/name) for name in ('orthogonal_ci.py','orthogonal_contracts.py','resource_ci.py')}, 'rows':rows,
              'passed': bool(rows) and all(row['expectationMet'] for row in rows)}
    (OUT / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
