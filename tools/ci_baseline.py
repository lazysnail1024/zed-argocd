#!/usr/bin/env python3
"""Export the immutable CI contracts at a full Git commit; never claim a run passed."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    commit = subprocess.check_output(['git', 'rev-parse', args.commit + '^{commit}'], cwd=ROOT, text=True).strip()
    if commit != args.commit:
        raise ValueError('Supply an immutable full commit')
    files = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', commit], cwd=ROOT, text=True).splitlines()
    inputs = {}
    contracts = {}
    for name in files:
        if name.startswith(('tests/', 'tools/', '.github/workflows/', 'contracts/')) or name in ('go.mod', 'go.sum', 'VERSION'):
            content = subprocess.check_output(['git', 'show', commit + ':' + name], cwd=ROOT)
            inputs[name] = hashlib.sha256(content).hexdigest()
            if name in ('tests/native_ci.py', 'tests/orthogonal_ci.py', 'tests/resource_ci.py'):
                constants = {}
                for node in ast.parse(content).body:
                    if isinstance(node, ast.Assign):
                        try:
                            value = ast.literal_eval(node.value)
                        except (ValueError, TypeError):
                            continue
                        for target in node.targets:
                            if isinstance(target, ast.Name) and target.id.isupper():
                                constants[target.id] = value
                if name=='tests/native_ci.py':
                    namespace={'__name__':'baseline_native','__file__':str(ROOT/name)}
                    exec(compile(content,name,'exec'),namespace)
                    constants['requiredSteps']={platform:{suite:namespace['required_steps'](platform,suite) for suite in namespace['SUITES']} for platform in namespace['PLATFORM_COMPILERS']}
                if name=='tests/resource_ci.py':
                    constants['resourceValidationSource']={node.name:ast.get_source_segment(content.decode(),node) for node in ast.parse(content).body if isinstance(node,ast.FunctionDef) and node.name in ('validate_report','validate_limits')}
                contracts[name] = constants
            if name == 'tests/inventory.json':
                contracts[name] = json.loads(content)
    report = {'schemaVersion': 1, 'repository': 'lazysnail1024/zed-argocd', 'sourceCommit': commit,
              'sourceTree': subprocess.check_output(['git', 'rev-parse', commit + '^{tree}'], cwd=ROOT, text=True).strip(),
              'contracts': contracts, 'inputSHA256': inputs,
              'coldBuildObligation': 'Every native lane starts with empty GOCACHE and GOMODCACHE; ordinary producers retain this obligation.',
              'matrixExecution': 'unverified: export is an inventory, not execution evidence',
              'hostedTimingSamples': [], 'branchRules': 'collected separately using the GitHub API',
              'replay': ['python', 'tools/ci_baseline.py', '--commit', commit, '--output', str(args.output)]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')

if __name__ == '__main__':
    main()
