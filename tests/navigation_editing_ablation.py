#!/usr/bin/env python3
"""Compile deletion controls, then fail an independently named stdio assertion."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('output', type=Path)
parser.add_argument('--variants', help='Comma-separated deletion controls; default runs all controls')
args = parser.parse_args()
output = args.output.resolve()
output.mkdir(parents=True, exist_ok=True)
env = os.environ | {'GOWORK': 'off', 'GOTOOLCHAIN': 'local', 'GOPROXY': 'off', 'CGO_ENABLED': '1'}
go = str(Path(env['GOROOT']) / 'bin/go') if env.get('GOROOT') else shutil.which('go')
if not go:
    raise RuntimeError('go is required on PATH or GOROOT')
variants = [
    ('all-source-uses', 'internal/analysis/rename.go',
     'for _, use := range helmValuePaths(object(src["helm"])) {',
     'for _, use := range helmValuePaths(object(src["helm"])) {\n\t\t\tif len(use.path) > 0 && use.path[0] == "fileParameters" { continue }',
     'rename', 'all-source-uses-renamed', 'allFourOccurrences'),
    ('unique-source-binding', 'internal/analysis/rename.go',
     'if definitions != 1 {', 'if false && definitions != 1 {',
     'rename', 'duplicate-source-definition-refused', 'noAmbiguousEdit'),
    ('reader-upper-bound', 'internal/lsp/server.go',
     'readSlots := make(chan struct{}, 4)', 'readSlots := make(chan struct{}, 128)',
     'capacity', 'bounded-reader-admission', 'finiteCapacityRejectsBurst'),
    ('reservation-release', 'internal/lsp/server.go',
     'defer func() { <-slots }()', 'defer func() { _ = slots }()',
     'capacity', 'cancelled-worker-reservations-released', 'capacityRecovered'),
    ('scope-generation', 'internal/lsp/incremental_query.go',
     'if proof.allScope {', 'if false && proof.allScope {',
     'proof', 'TestProjectRenameProofRejectsNewMembersAndRootRecreation', 'new member commit must not revive an older scope generation'),
    ('root-fence', 'internal/lsp/incremental.go',
     'func (s *workspaceIndex) invalidateRoot(root string) {', 'func (s *workspaceIndex) invalidateRoot(root string) {\n\treturn',
     'proof', 'TestProjectRenameProofRejectsNewMembersAndRootRecreation', 'same-path root recreation must revoke proof at notification acceptance'),
    ('symbol-owner-lock', 'internal/lsp/symbols.go',
     'func (s *workspaceIndex) workspaceSymbols(ctx context.Context, query string) ([]workspaceSymbol, *symbolReadProof, bool, error) {',
     'func (s *workspaceIndex) workspaceSymbols(ctx context.Context, query string) ([]workspaceSymbol, *symbolReadProof, bool, error) {\n\ts.mu.RLock()\n\tdefer s.mu.RUnlock()',
     'proof', 'TestWorkspaceSymbolCheckpointsReleaseOwner', 'symbol traversal held the workspace owner lock'),
]
if args.variants:
    selected = set(args.variants.split(','))
    unknown = selected - {variant[0] for variant in variants}
    if unknown:
        parser.error('unknown deletion controls: ' + ', '.join(sorted(unknown)))
    variants = [variant for variant in variants if variant[0] in selected]
reports = []
for name, filename, before, after, suite, expected_case, expected_assertion in variants:
    dest = output / name
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cd-navigation-ablation-') as directory:
        copy = Path(directory)
        for folder in ['cmd', 'compat', 'internal', 'vendor']:
            shutil.copytree(ROOT / folder, copy / folder)
        for file in ['go.mod', 'go.sum']:
            shutil.copy2(ROOT / file, copy / file)
        source = copy / filename
        original = source.read_text()
        if original.count(before) != 1:
            raise AssertionError((name, 'mutation must match once', original.count(before)))
        source.write_text(original.replace(before, after))
        shutil.copy2(source, dest / 'mutation.go.txt')
        binary = dest / ('server.exe' if os.name == 'nt' else 'server')
        build_command = [go, 'build', '-mod=vendor', '-buildvcs=false', '-trimpath', '-o', binary, './cmd/argocd-ls']
        build = subprocess.run([str(a) for a in build_command], cwd=copy, env=env, capture_output=True, timeout=240)
        (dest / 'build.stdout').write_bytes(build.stdout)
        (dest / 'build.stderr').write_bytes(build.stderr)
        row = {'variant': name, 'buildExitCode': build.returncode, 'expectedCase': expected_case, 'expectedAssertion': expected_assertion, 'detected': False}
        if build.returncode != 0:
            reports.append(row)
            (output / 'report.json').write_text(json.dumps(reports, indent=2) + '\n')
            raise AssertionError('compile failure is not ablation evidence')
        if suite == 'proof':
            command = [go, 'test', '-mod=vendor', '-count=1', '-run', '^' + expected_case + '$', './internal/lsp']
        else:
            command = [sys.executable, ROOT / 'tests/navigation_editing_guards_e2e.py', binary, dest / 'run', '--cases', suite]
        tested = subprocess.run([str(a) for a in command], cwd=copy if suite == 'proof' else ROOT, env=env, capture_output=True, timeout=120)
        (dest / 'test.stdout').write_bytes(tested.stdout)
        (dest / 'test.stderr').write_bytes(tested.stderr)
        if suite == 'proof':
            observed = tested.stdout.decode(errors='replace')
            detected = tested.returncode == 1 and '--- FAIL: ' + expected_case in observed and expected_assertion in observed
            failed = [expected_case] if detected else []
        else:
            results = json.loads((dest / 'run/results.json').read_text())
            failed = [result['id'] for result in results['cases'] if not result['passed']]
            matched = next((result for result in results['cases'] if result['id'] == expected_case), {})
            detected = tested.returncode == 1 and 'runner-error' not in failed and matched.get('assertions', {}).get(expected_assertion) is False
        row.update(testExitCode=tested.returncode, binarySHA256=hashlib.sha256(binary.read_bytes()).hexdigest(), command=[str(a) for a in command], failedCases=failed, detected=detected)
        reports.append(row)
        (output / 'report.json').write_text(json.dumps(reports, indent=2) + '\n')
        print(name, 'DETECTED' if row['detected'] else 'NOT DETECTED', flush=True)
sys.exit(0 if len(reports) == len(variants) and all(row['detected'] for row in reports) else 1)
