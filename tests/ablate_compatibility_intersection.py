#!/usr/bin/env python3
"""Remove the modeled intersection and require independent CLI/LSP failures."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    output = parser.parse_args().output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[1]
    source = root / 'internal/analysis/compatibility.go'
    original = source.read_text()
    needle = 'r.Candidates = schemaSet.intersection(semanticSet).names(metadata.Versions)'
    assert original.count(needle) == 1, 'Intersection implementation changed; review the behavioral ablation'
    mutant = output / 'compatibility.go'
    mutant.write_text(original.replace(needle, 'r.Candidates = schemaSet.names(metadata.Versions)', 1))
    overlay = output / 'overlay.json'
    overlay.write_text(json.dumps({'Replace': {str(source): str(mutant)}}))
    go = os.environ.get('GO') or (str(Path(os.environ['GOROOT']) / 'bin/go') if 'GOROOT' in os.environ else 'go')
    binary = output / ('argocd-ls.exe' if os.name == 'nt' else 'argocd-ls')
    command = [go, 'build', '-mod=vendor', '-buildvcs=false', '-overlay=' + str(overlay), '-o', str(binary), './cmd/argocd-ls']
    build = subprocess.run(command, cwd=root, capture_output=True, timeout=180, env=dict(os.environ, GOWORK='off'))
    (output / 'build.stdout').write_bytes(build.stdout)
    (output / 'build.stderr').write_bytes(build.stderr)
    report = {'command': command, 'buildExit': build.returncode, 'originalSHA256': hashlib.sha256(original.encode()).hexdigest(), 'mutantSHA256': hashlib.sha256(mutant.read_bytes()).hexdigest(), 'passed': False}
    if build.returncode == 0:
        test = subprocess.run([sys.executable, str(root / 'tests/compatibility_e2e.py'), str(binary), str(output / 'consumer')], capture_output=True, timeout=180)
        (output / 'test.stdout').write_bytes(test.stdout)
        (output / 'test.stderr').write_bytes(test.stderr)
        observed = json.loads((output / 'consumer/report.json').read_text())
        rows = {row['id']: row for row in observed['rows']}
        report['testExit'] = test.returncode
        report['expectedFailures'] = ['schema-semantic-empty-intersection', 'single-source-ref-boundary', 'yaml-stream-independent-resources', 'lsp-unsaved-buffer-and-explicit-action']
        report['failedCases'] = [row['id'] for row in observed['rows'] if not row['passed']]
        report['ordinaryCheckUnchanged'] = rows['ordinary-auto-and-exact-unchanged']['passed']
        observed_conflict = rows['schema-semantic-empty-intersection']['observed']['resources'][0]
        report['schemaStillNonempty'] = bool(observed_conflict['schemaCandidates'])
        report['falseJointCandidates'] = observed_conflict['candidates'] == observed_conflict['schemaCandidates']
        report['passed'] = (test.returncode == 1 and report['failedCases'] == report['expectedFailures'] and report['ordinaryCheckUnchanged'] and report['schemaStillNonempty'] and report['falseJointCandidates'])
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
