#!/usr/bin/env python3
"""Run predeclared synthetic value sets through the real CLI validator."""
from pathlib import Path
import argparse, hashlib, json, os, subprocess, sys, time

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser();parser.add_argument('binary',type=Path);parser.add_argument('output',type=Path);parser.add_argument('--adjacent',action='store_true');args=parser.parse_args()
OUT=args.output.resolve();BINARY=args.binary.resolve()

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    rows = []
    # Independent set algebra: (a|b)&(b|c) | d = {b,d};
    # (a|b|c) & (b|d) = {b}. This is not computed from production facets.
    expected = {'nestedConjunction': {'b', 'd'}, 'baseAndAlternatives': {'b'}}
    if args.adjacent:expected={'exclusiveChoice':{'a','c'},'inclusiveChoice':{'a','b','c'},'duplicateExclusive':set(),'referencedExclusive':{'a','c'},'duplicateInclusive':{'a','b'}}
    (OUT/'expected.json').write_text(json.dumps({k: sorted(v) for k,v in expected.items()}, indent=2)+'\n')
    for target in ['3.0.0', '3.5.6']:
        for field, allowed in expected.items():
            for value in ['a', 'b', 'c', 'd']:
                name = f'{target}-{field}-{value}'
                folder = OUT/name
                work = folder/'workspace'
                work.mkdir(parents=True, exist_ok=True)
                (work/'.argocd-ls.yaml').write_text(f'schemaVersion: 1\ntargetVersion: "{target}"\n')
                (work/'input.yaml').write_text('apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata: {name: example}\nspec:\n  '+field+': '+json.dumps({'value':value} if args.adjacent else value)+'\n')
                command = [str(BINARY), 'check', '--root', str(work), '--format', 'json']
                row = dict(id=name, command=command, expectedSchemaAccepted=value in allowed, passed=False)
                start = time.monotonic()
                try:
                    process = subprocess.run(command, capture_output=True, timeout=10)
                    (folder/'stdout').write_bytes(process.stdout)
                    (folder/'stderr').write_bytes(process.stderr)
                    observed = json.loads(process.stdout)
                    constraints = [d for d in observed['diagnostics'] if d['code']=='SCHEMA_CONSTRAINT']
                    checks = dict(exit=process.returncode==(0 if value in allowed else 1), readiness=observed['analysisComplete'] is True,
                                  resource=observed['resourcesAnalyzed']==1,
                                  schemaDecision=(not constraints)==(value in allowed),
                                  configuration=observed['configurationErrors']==[])
                    row.update(exit=process.returncode, checks=checks, passed=all(checks.values()), diagnostics=observed['diagnostics'])
                except Exception as error:
                    row['error'] = repr(error)
                row['seconds'] = time.monotonic()-start
                rows.append(row)
    report = dict(scope='synthetic consumer and real CLI validator consistency, never historical upstream semantics',
                  binarySHA256=sha(BINARY), fixtureSHA256=sha(ROOT/'tests'/('schema-query-boundary-fixture.json' if args.adjacent else 'schema-query-repair-fixture.json')), runnerSHA256=sha(Path(__file__)),
                  rows=rows, validatorPassed=len(rows)==len(expected)*8 and all(r['passed'] for r in rows))
    (OUT/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    (OUT/'hashes.json').write_text(json.dumps({str(p.relative_to(OUT)):sha(p) for p in OUT.rglob('*') if p.is_file() and p.name!='hashes.json'},indent=2)+'\n')
    print(json.dumps(dict(validatorPassed=report['validatorPassed'], rows=len(rows))))
    return 0 if report['validatorPassed'] else 1

if __name__=='__main__':
    sys.exit(main())
