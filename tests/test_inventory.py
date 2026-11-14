#!/usr/bin/env python3
"""Reject unclassified tests, dead CI references and an incomplete required gate.

This is a structural audit of test wiring. Actual subprocess receipts remain
mandatory in the native, orthogonal, resource and adapter workflows.
"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def inventory():
    python = set(ROOT.glob('tests/**/*.py')) | set(ROOT.glob('editors/zed/tests/**/*.py'))
    go = set(ROOT.glob('internal/**/*_test.go'))
    rust = {path for path in (ROOT / 'editors/zed').rglob('*.rs')
            if 'target' not in path.parts and '#[test]' in path.read_text(encoding='utf-8')}
    return {path.relative_to(ROOT).as_posix() for path in python | go | rust}


def verify(plan):
    claimed = set()
    for boundary, groups in plan['execution'].items():
        for via, names in groups.items():
            if not names:
                raise ValueError('Empty execution group: ' + via)
            for name in names:
                if name in claimed:
                    raise ValueError('Test classified twice: ' + name)
                claimed.add(name)
                path = ROOT / name
                if not path.is_file():
                    raise ValueError('Missing classified test: ' + name)
                if boundary == 'go':
                    if './' + path.parent.relative_to(ROOT).as_posix() not in (ROOT / via).read_text():
                        raise ValueError('Go test package is absent from its workflow: ' + name)
                elif boundary == 'aliases':
                    family=path.stem.removeprefix('ablate_').replace('_','-')
                    if 'from legacy_mutations import main' not in path.read_text() or repr(family) not in (ROOT / via).read_text():
                        raise ValueError('Alias does not delegate to a live mutation family: ' + name)
                elif boundary == 'rust':
                    if 'cargo test --locked' not in (ROOT / via).read_text():
                        raise ValueError('Rust tests are not executed by their workflow')
                elif boundary == 'manual-observation':
                    if not via.strip():
                        raise ValueError('Manual observations need an explicit coverage boundary')
                else:
                    caller = (ROOT / via).read_text()
                    if path.name not in caller and path.stem not in caller:
                        raise ValueError('Test has no reference in its declared caller: ' + name)
    actual = inventory()
    if actual != claimed:
        raise ValueError('Unclassified=' + repr(sorted(actual - claimed)) + '; obsolete=' + repr(sorted(claimed - actual)))
    workflow = (ROOT / '.github/workflows/native.yml').read_text()
    gate = workflow.split('  candidate:\n', 1)[1]
    for job in ('native', 'orthogonal', 'resources'):
        if '\n    - ' + job + '\n' not in gate or ('needs.' + job + '.result' not in gate and 'toJSON(needs)' not in gate):
            raise ValueError('Candidate does not require the whole ' + job + ' boundary')
    return len(actual)


def main():
    try:
        plan = json.loads((ROOT / 'tests/inventory.json').read_text())
        count = verify(plan)
        # A missing classification is a failure even if every currently selected
        # test passes. Check this policy without creating a fake repository file.
        negative = json.loads(json.dumps(plan))
        next(iter(next(iter(negative['execution'].values())).values())).pop()
        try:
            verify(negative)
        except ValueError:
            pass
        else:
            raise ValueError('Inventory control accepted an unclassified test')
        print(json.dumps({'passed': True, 'classifiedFiles': count, 'missingClassificationRejected': True}))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'passed': False, 'error': str(error)}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
