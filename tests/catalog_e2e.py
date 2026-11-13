#!/usr/bin/env python3
"""Offline inventory integrity; behavior is covered by multi_version_e2e.py."""
import json
import os
import pathlib
import sys
from multi_version_e2e import inventory

root = pathlib.Path(__file__).resolve().parents[1]
out = pathlib.Path(os.environ.get('CD_AUDIT_OUTPUT',str(root / 'artifacts/catalog')))
out.mkdir(parents=True, exist_ok=True)
lock = json.loads((root / 'compat/catalog.lock.json').read_text())
result = inventory(lock)
result['ready'] = lock['ready'] and lock['defaultVersion'] == 'auto'
result['passed'] = result['passed'] and result['ready']
(out / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result))
sys.exit(0 if result['passed'] else 1)
