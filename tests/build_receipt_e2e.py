#!/usr/bin/env python3
"""CLI controls for build receipt identity; fixture bytes are not native proof."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    rows = []
    fixture = args.output.resolve() / 'fixture'
    fixture.mkdir()
    binary = b'fixture bytes, not an executable'
    (fixture / 'argocd-ls').write_bytes(binary)
    manifest = {'binary': 'argocd-ls', 'binarySHA256': hashlib.sha256(binary).hexdigest(), 'platform': 'linux/amd64',
                'identity': {'sourceCommit': 'a'*40, 'buildMode': 'release', 'serverVersion': '1.0.0', 'dirty': False},
                'sourceInputs': {'go.mod': 'b'*64}}
    (fixture / 'manifest.json').write_text(json.dumps(manifest))
    receipt = {'schemaVersion': 1, 'buildRole': 'ordinary', 'sourceCommit': 'a'*40,
               'platform': 'linux/amd64', 'buildMode': 'release', 'version': '1.0.0',
               'runID': '123', 'attempt': '1', 'job': 'producer', 'initialBuildCachesEmpty': True,
               'manifestSHA256': hashlib.sha256((fixture / 'manifest.json').read_bytes()).hexdigest(),
               'binarySHA256': manifest['binarySHA256']}
    cases = ['complete', 'wrong-attempt', 'wrong-role', 'wrong-mode', 'wrong-version', 'wrong-source', 'wrong-platform', 'warm-cache', 'unknown-schema', 'bad-binary', 'bad-manifest', 'forged-receipt']
    for case in cases:
        value = copy.deepcopy(receipt)
        changes = {'wrong-attempt': ('attempt','2'), 'wrong-role': ('buildRole','mutation'), 'wrong-mode': ('buildMode','development'),
                   'wrong-version': ('version','0.0.1'), 'wrong-source': ('sourceCommit','c'*40), 'wrong-platform': ('platform','linux/arm64'),
                   'warm-cache': ('initialBuildCachesEmpty',False), 'unknown-schema': ('schemaVersion',99),
                   'bad-binary': ('binarySHA256','c'*64), 'bad-manifest': ('manifestSHA256','c'*64)}
        if case in changes:
            key, change = changes[case]; value[key] = change
        if case=='forged-receipt': value['job']='forged-producer'
        (fixture / 'receipt.json').write_text(json.dumps(value))
        command = [sys.executable, str(ROOT/'tools/build_receipt.py'), 'verify', '--directory', str(fixture), '--source-commit', 'a'*40,
                   '--receipt-sha', hashlib.sha256(json.dumps(receipt if case=='forged-receipt' else value).encode()).hexdigest(), '--platform', 'linux/amd64', '--mode', 'release', '--version', '1.0.0', '--run-id', '123', '--attempt', '1']
        result = subprocess.run(command, capture_output=True, timeout=20)
        (args.output/(case+'.stdout')).write_bytes(result.stdout)
        (args.output/(case+'.stderr')).write_bytes(result.stderr)
        rows.append({'id':case, 'command':command, 'exit':result.returncode, 'passed':result.returncode == (0 if case=='complete' else 1)})
    report={'scope':'synthetic receipt validator controls only', 'rows':rows, 'passed':all(row['passed'] for row in rows),
            'runnerSHA256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'python':sys.version}
    (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))
    return 0 if report['passed'] else 1

if __name__=='__main__':
    sys.exit(main())
