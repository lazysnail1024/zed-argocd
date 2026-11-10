#!/usr/bin/env python3
"""Compile a shared-query ablation and require named real LSP failures."""
import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=pathlib.Path)
    args = parser.parse_args()
    root = pathlib.Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = root / 'vendor/github.com/lazysnail1024/k8s-crd-toolkit/schemaquery/query.go'
    original = source.read_text()
    needle = '\tw := newWalk(ctx, limits)\n\tnodes, _ := w.nodes(root, path, value, 0)'
    assert original.count(needle) == 1, 'shared Values implementation changed; review ablation'
    mutant = output / 'query.go'
    mutant.write_text(original.replace(needle, '\tif true { return nil, nil }\n' + needle, 1))
    overlay = output / 'overlay.json'
    overlay.write_text(json.dumps({'Replace': {str(source): str(mutant)}}, indent=2) + '\n')
    go = str(pathlib.Path(os.environ['GOROOT']) / 'bin/go') if 'GOROOT' in os.environ else 'go'
    binary = output / ('argocd-ls.exe' if os.name == 'nt' else 'argocd-ls')
    command = [go, 'build', '-mod=vendor', '-buildvcs=false', '-overlay=' + str(overlay), '-o', str(binary), './cmd/argocd-ls']
    build = subprocess.run(command, cwd=root, capture_output=True, timeout=180)
    (output / 'build.stdout').write_bytes(build.stdout)
    (output / 'build.stderr').write_bytes(build.stderr)
    report = {'command': command, 'buildExit': build.returncode,
              'originalSHA256': hashlib.sha256(original.encode()).hexdigest(),
              'mutantSHA256': hashlib.sha256(mutant.read_bytes()).hexdigest(), 'passed': False}
    if build.returncode == 0:
        test = subprocess.run([sys.executable, str(root / 'tests/shared_schemaquery_e2e.py'), str(binary), str(output / 'lsp')], capture_output=True, timeout=180)
        (output / 'test.stdout').write_bytes(test.stdout)
        (output / 'test.stderr').write_bytes(test.stderr)
        report['testExit'] = test.returncode
        observed = json.loads((output / 'lsp/report.json').read_text())
        rows = observed['rows']
        report['expectedFailures'] = [row['id'] for row in rows if row.get('checks', {}).get('fourEnumActions') is False]
        report['passed'] = (test.returncode == 1 and len(rows) == 2 and
                            set(report['expectedFailures']) == {'auto-enum-repair', '3.5.6-enum-repair'} and
                            all(row.get('checks', {}).get('constraintObserved') is True and
                                row.get('checks', {}).get('sharedCompletionCandidates') is False for row in rows))
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))
    return 0 if report['passed'] else 1

if __name__ == '__main__':
    sys.exit(main())
