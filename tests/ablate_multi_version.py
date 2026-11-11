#!/usr/bin/env python3
"""Ablate compatibility requirements in isolated native builds, retaining evidence."""
import argparse
import hashlib
import json
import os
import pathlib
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

import multi_version_e2e as acceptance

ROOT = pathlib.Path(__file__).resolve().parents[1]


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def inline(node, definitions):
    if isinstance(node, dict):
        if '$ref' in node:
            return inline(definitions[node['$ref'].split('/')[-1]], definitions)
        return {key: inline(value, definitions) for key, value in node.items() if key != '$defs'}
    if isinstance(node, list):
        return [inline(value, definitions) for value in node]
    return node


def open_objects(node):
    if isinstance(node, dict):
        return {key: open_objects(value) for key, value in node.items()
                if not (key == 'additionalProperties' and value is False)}
    if isinstance(node, list):
        return [open_objects(value) for value in node]
    return node


def timing(binary, folder):
    work = folder / 'timing-workspace'
    work.mkdir(parents=True, exist_ok=True)
    for kind in ('Application', 'AppProject', 'ApplicationSet'):
        (work / (kind + '.yaml')).write_text(json.dumps(acceptance.sample(kind)))
    samples = []
    for _ in range(5):
        begin = time.monotonic()
        try:
            result = subprocess.run([str(binary), 'check', '--root', str(work), '--format', 'json'], capture_output=True, timeout=30)
        except subprocess.TimeoutExpired as error:
            (folder / 'timing-timeout.stdout').write_bytes(error.stdout or b'')
            (folder / 'timing-timeout.stderr').write_bytes(error.stderr or b'')
            return {'completed': False, 'timeoutSeconds': 30, 'elapsedMilliseconds': round((time.monotonic() - begin) * 1000, 2), 'coldCLIThreeKindsMilliseconds': samples}
        samples.append(round((time.monotonic() - begin) * 1000, 2))
        if result.returncode:
            raise RuntimeError('timing fixture failed: ' + result.stdout.decode()[:1000])
    return {'completed': True, 'coldCLIThreeKindsMilliseconds': samples, 'medianMilliseconds': statistics.median(samples)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=pathlib.Path)
    parser.add_argument('output', type=pathlib.Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    original = args.binary.resolve()
    catalog = json.loads((ROOT / 'internal/assets/catalog.json').read_text())
    rows = []
    for variant in ('baseline', 'without-object-closure', 'newest-schema-only', 'without-shared-definitions', 'without-query-version-filter', 'without-historical-appset-addon'):
        folder = output / variant
        folder.mkdir(parents=True, exist_ok=True)
        row = {'id': variant}
        rows.append(row)
        try:
            with tempfile.TemporaryDirectory(prefix='argocd-ablation-') as scratch:
                tree = pathlib.Path(scratch)
                binary = original
                changes = {}
                if variant != 'baseline':
                    # Copy only native inputs. The checked-out repository is never
                    # edited, and no editor or release module participates.
                    for directory in ('cmd', 'internal', 'vendor'):
                        shutil.copytree(ROOT / directory, tree / directory)
                    for filename in ('go.mod', 'go.sum'):
                        shutil.copyfile(ROOT / filename, tree / filename)
                    if variant == 'without-query-version-filter':
                        source = tree / 'internal/analysis/schema_queries.go'
                        text = source.read_text()
                        # Keep shared shape selection and validation intact, but
                        # prevent the plugin from narrowing queries to the
                        # compatible release branches it selected.
                        selection = '\tselected.AnyOf = possible\n'
                        if text.count(selection) != 1:
                            raise RuntimeError('query selection mutation no longer matches')
                        source.write_text(text.replace(selection, '\tselected.AnyOf = doc.CompiledSchema.AnyOf\n'))
                    current = json.loads(json.dumps(catalog))
                    for entry in current['autoSchemas']:
                        if variant == 'without-query-version-filter':
                            continue
                        if variant == 'without-historical-appset-addon' and entry['kind'] != 'ApplicationSet':
                            continue
                        data = json.loads((ROOT / entry['path']).read_text())
                        before = len(canonical(data))
                        if variant == 'without-object-closure':
                            data = open_objects(data)
                        elif variant == 'newest-schema-only':
                            data['anyOf'] = data['anyOf'][:1]
                        elif variant == 'without-historical-appset-addon':
                            before_branches = len(data['anyOf'])
                            data['anyOf'] = [branch for branch in data['anyOf']
                                             if 'ksonnet' not in inline(branch, data['$defs'])['properties']['spec']['properties']['template']['properties']['spec']['properties']['source']['properties']]
                            if len(data['anyOf']) != before_branches - 1:
                                raise RuntimeError('historical ApplicationSet schema removal no longer matches exactly one variant')
                        else:
                            data = inline(data, data['$defs'])
                        raw = canonical(data)
                        digest = hashlib.sha256(raw).hexdigest()
                        entry['sha256'] = digest
                        entry['path'] = f'internal/assets/schemas/{digest}.json'
                        (tree / entry['path']).write_bytes(raw)
                        changes[entry['kind']] = {'beforeBytes': before, 'afterBytes': len(raw)}
                    (tree / 'internal/assets/catalog.json').write_bytes(canonical(current))
                    # Remove the unused baseline auto assets from the mutant,
                    # keeping every exact-release schema present in production.
                    exact = {entry['path'] for target in current['versions'] for entry in target['schemas']}
                    auto = {entry['path'] for entry in current['autoSchemas']}
                    for path in (tree / 'internal/assets/schemas').glob('*.json'):
                        if path.relative_to(tree).as_posix() not in exact | auto:
                            path.unlink()
                    binary = folder / ('argocd-ls.exe' if os.name == 'nt' else 'argocd-ls')
                    build = subprocess.run(['go', 'build', '-mod=vendor', '-buildvcs=false', '-trimpath', '-o', str(binary), './cmd/argocd-ls'], cwd=tree, capture_output=True, timeout=180)
                    (folder / 'build.stderr').write_bytes(build.stderr)
                    if build.returncode:
                        raise RuntimeError('mutant build failed')
                begin = time.monotonic()
                try:
                    result = acceptance.cross_version(binary, folder, False)
                except subprocess.TimeoutExpired as error:
                    if variant != 'without-shared-definitions':
                        raise
                    # This counterfactual specifically removes the optimization
                    # needed to meet fixed consumer deadlines. A timeout in the
                    # complete compatibility workload is already that evidence;
                    # do not raise its budget or claim unobserved functional results.
                    (folder / 'compatibility-timeout.stdout').write_bytes(error.stdout or b'')
                    (folder / 'compatibility-timeout.stderr').write_bytes(error.stderr or b'')
                    result = {'id': 'auto-old-new-combination', 'completed': False, 'passed': None,
                              'command': error.cmd, 'timeoutSeconds': error.timeout,
                              'elapsedMilliseconds': round((time.monotonic() - begin) * 1000, 2)}
                (folder / 'probe.json').write_text(json.dumps(result, indent=2) + '\n')
                row.update({'acceptancePassed': result['passed'], 'probe': result, 'schemaChanges': changes,
                            'binarySHA256': acceptance.checksum(binary), 'binaryBytes': binary.stat().st_size})
                if variant in ('baseline', 'without-query-version-filter', 'without-historical-appset-addon'):
                    row['queryProbe'] = acceptance.lsp_auto(binary, folder)
                if variant == 'without-shared-definitions' and result.get('completed') is False:
                    row['timing'] = {'completed': False, 'probe': 'compatibility-cases',
                                     'timeoutSeconds': result['timeoutSeconds'], 'elapsedMilliseconds': result['elapsedMilliseconds']}
                    row['passed'] = True
                elif variant in ('baseline', 'without-shared-definitions'):
                    row['timing'] = timing(binary, folder)
                    row['passed'] = result['passed'] and (row['timing']['completed'] or variant == 'without-shared-definitions')
                    if variant == 'baseline':
                        row['passed'] = row['passed'] and row['queryProbe']['passed']
                elif variant == 'without-query-version-filter':
                    # Validation still rejects impossible combinations, but the
                    # editor must not suggest introducing those combinations.
                    failed = {probe['id'] for probe in row['queryProbe']['rows'] if not probe['passed']}
                    row['passed'] = result['passed'] and {'legacy-selects-whole-version', 'current-selects-whole-version'} <= failed
                elif variant == 'without-historical-appset-addon':
                    failed = {probe['id'] for probe in row['queryProbe']['rows'] if not probe['passed']}
                    row['passed'] = (not result['passed'] and all(error.startswith('appset-') for error in result['errors'])
                                     and {'appset-legacy-field', 'appset-legacy-hover'} <= failed)
                else:
                    # Both removals must be caught by the independent old/new
                    # fixtures, rather than being accepted as a simplification.
                    row['passed'] = not result['passed']
        except Exception as error:
            row.update({'passed': False, 'error': repr(error)})
        (folder / 'result.json').write_text(json.dumps(row, indent=2) + '\n')
    report = {'passed': all(row['passed'] for row in rows), 'rows': rows,
              'scope': 'Local native CLI counterfactuals; timing is one Linux host, not a portable benchmark'}
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'variants': [{key: row.get(key) for key in ('id', 'passed', 'acceptancePassed', 'timing', 'error')} for row in rows]}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
