#!/usr/bin/env python3
"""Extract exact CRDs and build a compact whole-release compatibility schema.

Auto mode is an anyOf of real release schemas, never a field-wise schema union.
Object closure prevents an old permissive CRD from accepting malformed new fields.
Repeated schema nodes share local $defs; there are no runtime fetches.
"""
import argparse
import hashlib
import json
import pathlib
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / 'internal/assets/schemas'


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


def write_schema(value, api, kind, form):
    data = canonical(value)
    digest = hashlib.sha256(data).hexdigest()
    (OUT / (digest + '.json')).write_bytes(data)
    return {'apiVersion': api, 'kind': kind, 'sha256': digest,
            'path': 'internal/assets/schemas/' + digest + '.json', 'format': form, 'validated': False}


def compatibility(variants):
    definitions = {}

    def intern(node):
        if not isinstance(node, dict):
            return node
        result = dict(node)
        if '$ref' in result:
            raise ValueError('Upstream references require an explicit base-preserving conversion')
        for keyword in ('properties',):
            if keyword in result:
                result[keyword] = {name: intern(child) for name, child in result[keyword].items()}
        for keyword in ('items', 'additionalProperties'):
            if isinstance(result.get(keyword), dict):
                result[keyword] = intern(result[keyword])
        for keyword in ('anyOf', 'allOf', 'oneOf'):
            if keyword in result:
                result[keyword] = [intern(child) for child in result[keyword]]
        if result.get('x-kubernetes-preserve-unknown-fields'):
            result.setdefault('additionalProperties', True)
        elif result.get('properties'):
            result.setdefault('additionalProperties', False)
        digest = hashlib.sha256(canonical(result)).hexdigest()
        definitions[digest] = result
        return {'$ref': '#/$defs/' + digest}

    branches = []
    for source in variants:
        branch = intern(source)
        if branch not in branches:
            branches.append(branch)
    return {'anyOf': branches, '$defs': definitions}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--accept-report', type=pathlib.Path,
                        help='Mark a matching complete bootstrap acceptance report ready')
    args = parser.parse_args()
    path = ROOT / 'compat/catalog.lock.json'
    catalog = json.loads(path.read_text())
    accepted = False
    if args.accept_report:
        report = json.loads(args.accept_report.read_text())
        if (not report.get('passed') or not report.get('bootstrap')
                or report.get('catalogSHA256') != hashlib.sha256(path.read_bytes()).hexdigest()
                or set(report.get('testedVersions', [])) != {target['version'] for target in catalog['versions']}):
            parser.error('acceptance requires a passing, complete bootstrap report for this exact catalog')
        accepted = True
    OUT.mkdir(parents=True, exist_ok=True)
    canonical_sources = {}
    for target in catalog['versions']:
        previous = {row['sha256']: row.get('validated', False) for row in target.get('schemas', [])}
        target['schemas'] = []
        for source in target['sources']:
            if not source['path'].endswith('.yaml'):
                continue
            if source['sha256'] not in canonical_sources:
                raw = (ROOT / source['localPath']).read_bytes()
                assert hashlib.sha256(raw).hexdigest() == source['sha256']
                crds = [doc for doc in yaml.safe_load_all(raw)
                        if isinstance(doc, dict) and doc.get('kind') == 'CustomResourceDefinition']
                assert crds and all(crd['spec']['group'] == 'argoproj.io' for crd in crds)
                canonical_sources[source['sha256']] = [
                    (crd['spec']['group'] + '/' + version['name'], crd['spec']['names']['kind'], version['schema']['openAPIV3Schema'])
                    for crd in crds for version in crd['spec']['versions'] if version['served']]
            for api, kind, schema in canonical_sources[source['sha256']]:
                entry = write_schema(schema, api, kind, 'canonical-openapi-v3-source')
                if source.get('origin'):
                    entry['sourceURL'] = source['url']
                entry['validated'] = previous.get(entry['sha256'], False)
                target['schemas'].append(entry)
    groups = {}
    for target in reversed(catalog['versions']):
        for entry in target['schemas']:
            group = groups.setdefault((entry['apiVersion'], entry['kind']), {})
            if entry['sha256'] not in group:
                group[entry['sha256']] = json.loads((ROOT / entry['path']).read_text())
    previous_auto = {row['sha256']: row.get('validated', False) for row in catalog.get('autoSchemas', [])}
    catalog['autoSchemas'] = [write_schema(compatibility(group.values()), api, kind, 'whole-release-anyof-shared-definitions')
                              for (api, kind), group in sorted(groups.items())]
    for entry in catalog['autoSchemas']:
        entry['validated'] = previous_auto.get(entry['sha256'], False)
    if accepted:
        for target in catalog['versions']:
            target['semanticsVerified'] = True
            for entry in target['schemas']:
                entry['validated'] = True
        for entry in catalog['autoSchemas']:
            entry['validated'] = True
    catalog['ready'] = bool(catalog['versions']) and all(
        target.get('semanticsVerified') and all(entry['validated'] for entry in target['schemas'])
        for target in catalog['versions']) and all(entry['validated'] for entry in catalog['autoSchemas'])
    catalog['defaultVersion'] = 'auto'
    path.write_bytes(json.dumps(catalog, indent=2).encode() + b'\n')
    # The executable needs identity and schema references, not collection history.
    runtime = {key: catalog[key] for key in ('product', 'defaultVersion', 'latestVersion', 'supportedMajors', 'autoSchemas', 'ready')}
    runtime['versions'] = [{key: value for key, value in target.items() if key in ('version', 'upstreamCommit', 'schemas', 'semanticsVerified', 'localRules')}
                           for target in catalog['versions']]
    (ROOT / 'internal/assets/catalog.json').write_bytes(canonical(runtime))
    active = {entry['path'] for target in catalog['versions'] for entry in target['schemas']} | {entry['path'] for entry in catalog['autoSchemas']}
    for old in OUT.glob('*.json'):
        if old.relative_to(ROOT).as_posix() not in active:
            old.unlink()
    print(json.dumps({'targets': len(catalog['versions']), 'uniqueCanonicalSchemas': len(active) - len(groups),
                      'autoSchemas': {kind: len(group) for (api, kind), group in groups.items()},
                      'autoBytes': sum((ROOT / entry['path']).stat().st_size for entry in catalog['autoSchemas'])}))


if __name__ == '__main__':
    main()
