#!/usr/bin/env python3
"""Remove Project uniqueness, compile, and require the duplicate-binding E2E to fail."""
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
    source = root / 'internal/analysis/project_rename.go'
    original = source.read_text()
    needle = '\tif declarations != 1 {'
    assert original.count(needle) == 1, 'Project uniqueness implementation changed; review ablation'
    mutant = output / 'project_rename.go'
    mutant.write_text(original.replace(needle, '\tif false && declarations != 1 {', 1))
    overlay = output / 'overlay.json'
    overlay.write_text(json.dumps({'Replace': {str(source): str(mutant)}}, indent=2) + '\n')
    go = str(pathlib.Path(os.environ['GOROOT']) / 'bin/go') if 'GOROOT' in os.environ else 'go'
    binary = output / ('argocd-ls.exe' if os.name == 'nt' else 'argocd-ls')
    command = [go, 'build', '-mod=readonly', '-buildvcs=false', '-overlay=' + str(overlay), '-o', str(binary), './cmd/argocd-ls']
    build = subprocess.run(command, cwd=root, capture_output=True, timeout=180)
    (output / 'build.stdout').write_bytes(build.stdout)
    (output / 'build.stderr').write_bytes(build.stderr)
    report = {'command': command, 'buildExit': build.returncode,
              'originalSHA256': hashlib.sha256(original.encode()).hexdigest(),
              'mutantSHA256': hashlib.sha256(mutant.read_bytes()).hexdigest(), 'passed': False}
    if build.returncode == 0:
        test = subprocess.run([sys.executable, str(root / 'tests/project_rename_e2e.py'), str(binary), str(output / 'lsp'), '--case', 'duplicate'], capture_output=True, timeout=90)
        (output / 'test.stdout').write_bytes(test.stdout)
        (output / 'test.stderr').write_bytes(test.stderr)
        report['testExit'] = test.returncode
        observed = json.loads((output / 'lsp/report.json').read_text())
        rows = observed['rows']
        report['expectedFailures'] = [row['id'] for row in rows if row.get('checks', {}).get('refusedWithoutEdits') is False]
        changes = (rows[0].get('renamed', {}).get('result') or {}).get('documentChanges', []) if rows else []
        report['ambiguousBindingEdited'] = len(changes) == 4
        report['passed'] = (test.returncode == 1 and len(rows) == 1 and
                            report['expectedFailures'] == ['duplicate'] and report['ambiguousBindingEdited'] and
                            rows[0].get('checks', {}).get('cleanExit') is True)
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
