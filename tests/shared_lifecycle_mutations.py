#!/usr/bin/env python3
"""Prove the two lifecycle consumers detect lost membership and retained facts."""
import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys

root = pathlib.Path(__file__).resolve().parents[1]
source = root / 'internal/lsp/incremental.go'
variants = (
    ('missing-watch-observation', 'TestWatchCreationBetweenEnumerationAndSweep',
     '\tif read {\n\t\tcopy.seen = root.membership.Observe()\n\t}\n', '',
     'completed discovery lost the new event or kept the deleted file'),
    ('empty-facts-recommit', 'TestIncrementalFactsOwnershipAndReuse',
     'change, err = s.facts.Reuse(ticket)', 'change, err = s.facts.Commit(ticket, file.summary.Facts)',
     'unchanged file did not reuse fresh facts'),
)
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('output', type=pathlib.Path)
parser.add_argument('--variant', choices=[item[0] for item in variants])
args = parser.parse_args()
out = args.output.resolve()
out.mkdir(parents=True, exist_ok=False)
selected = [item for item in variants if args.variant is None or item[0] == args.variant]
env = dict(os.environ, GOWORK='off', GOTOOLCHAIN='local', GOPROXY='off', CGO_ENABLED='1')
go = env.get('GO') or (str(pathlib.Path(env['GOROOT']) / 'bin/go')
                       if env.get('GOROOT') else shutil.which('go'))
report = {'command': sys.argv, 'rows': [], 'passed': False,
          'runnerSHA256': hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),
          'sourceSHA256': hashlib.sha256(source.read_bytes()).hexdigest(),
          'consumerSHA256': hashlib.sha256((root / 'internal/lsp/incremental_test.go').read_bytes()).hexdigest()}


def run(folder, test, overlay=None):
    command = [go, 'test', '-mod=readonly', '-count=1', '-timeout=30s']
    if overlay:
        command.append('-overlay=' + str(overlay))
    command += ['./internal/lsp', '-run', '^(' + test + ')$']
    result = subprocess.run(command, cwd=root, env=env, capture_output=True, timeout=180)
    (folder / 'stdout').write_bytes(result.stdout)
    (folder / 'stderr').write_bytes(result.stderr)
    return {'command': command, 'exit': result.returncode}, (result.stdout + result.stderr).decode(errors='replace')


try:
    if not go:
        raise RuntimeError('Go toolchain is required (GO, GOROOT or PATH)')
    control = out / 'control'
    control.mkdir()
    report['control'], _ = run(control, '|'.join(item[1] for item in selected))
    if report['control']['exit'] != 0:
        raise RuntimeError('Unmodified lifecycle consumers did not pass')
    for name, test, before, after, failure in selected:
        folder = out / name
        folder.mkdir()
        text = source.read_text()
        if text.count(before) != 1:
            raise RuntimeError(name + ': mutation anchor must occur exactly once')
        replacement = folder / 'incremental.go'
        replacement.write_text(text.replace(before, after, 1))
        overlay = folder / 'overlay.json'
        overlay.write_text(json.dumps({'Replace': {str(source): str(replacement)}}, indent=2) + '\n')
        result, output = run(folder, test, overlay)
        detected = (result['exit'] == 1 and '--- FAIL: ' + test in output
                    and failure in output and '[build failed]' not in output)
        report['rows'].append({'id': name, 'mutation': {'before': before, 'after': after},
                               'run': result, 'caughtExpectedRegression': detected})
        print(name, detected, flush=True)
    report['passed'] = len(report['rows']) == len(selected) and all(
        row['caughtExpectedRegression'] for row in report['rows'])
except Exception as error:
    report['error'] = repr(error)
finally:
    (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
sys.exit(0 if report['passed'] else 1)
