#!/usr/bin/env python3
"""Bind whole-auto branches and current modeled rule profiles to exact releases.

This is supplemental explanation metadata: exact schema bytes, catalog readiness
and the ordinary validator's rules are not modified. A profile means identical
inputs to our modeled local rules, not proven equivalence of controller versions.
"""
import argparse
import hashlib
import json
from pathlib import Path

from normalize_schemas import canonical, compatibility

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'internal/assets/compatibility.json'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit_sources(catalog, evidence, historical, historical_path):
    inventory_sha = hashlib.sha256(canonical([[row['version'], row['upstreamCommit']] for row in catalog['versions']])).hexdigest()
    assert evidence['coverage']['modeledInventorySHA256'] == inventory_sha, 'semantic model inventory pins changed; review rule evolution metadata before generating candidate sets'
    targets = {row['version']: row for row in catalog['versions']}
    historical_sha = digest(historical_path)
    for version, row in historical['versions'].items():
        assert targets[version]['upstreamCommit'] == row['upstreamCommit'], 'historical rule source commit differs'
        assert targets[version]['localRules']['provenanceSHA256'] == historical_sha, 'historical source evidence identity differs'
    assert evidence['coverage']['historicalSemanticManifestSHA256'] == historical_sha
    sources = {row['id']: row for row in evidence['sources']}
    raw = {}
    for identity, source in sources.items():
        assert targets[source['version']]['upstreamCommit'] == source['commit'], 'boundary source is not catalog-pinned'
        assert source['url'] == f"https://github.com/argoproj/argo-cd/blob/{source['commit']}/{source['upstreamPath']}"
        path = ROOT / source['localPath']
        assert path.resolve().is_relative_to(ROOT) and path.is_file()
        data = path.read_bytes()
        assert len(data) == source['bytes'] and hashlib.sha256(data).hexdigest() == source['sha256'], 'semantic source bytes changed: ' + identity
        assert hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() == source['gitBlobSHA1'], 'semantic Git blob differs'
        raw[identity] = data
    changes = []
    for rule in evidence['rules']:
        samples = {}
        for entry in rule.get('evidence', []):
            source = sources[entry['sourceID']]
            for function in entry['functions'] + entry['callers']:
                data = raw[function.get('sourceID', entry['sourceID'])]
                fragment = b''.join(data.splitlines(keepends=True)[function['startLine']-1:function['endLine']]).rstrip(b'\r\n')
                assert fragment.startswith(b'func ') and hashlib.sha256(fragment).hexdigest() == function['sha256'], 'semantic function/caller changed: ' + function['function']
            samples[source['version']] = {'version': source['version'], 'functionSHA256': [row['sha256'] for row in entry['functions']], 'callerSHA256': [row['sha256'] for row in entry['callers']], 'schemaSHA256': {row['kind']: row['sha256'] for row in targets[source['version']]['schemas']}}
        ordered = sorted(samples.values(), key=lambda row: tuple(map(int, row['version'].split('.'))))
        for before, after in zip(ordered, ordered[1:]):
            # Function and caller checks execute regardless of CRD equality.
            changes.append({'ruleId': rule['id'], 'before': before, 'after': after, 'sameSchemas': before['schemaSHA256'] == after['schemaSHA256'], 'functionChanged': before['functionSHA256'] != after['functionSHA256'], 'callerChanged': (before['callerSHA256'] != after['callerSHA256']) if before['callerSHA256'] and after['callerSHA256'] else None})
    return {'modeledInventorySHA256': inventory_sha, 'sourceFilesVerified': len(sources), 'historicalManifestVersionsBound': len(historical['versions']), 'scope': 'declared boundary samples and historical manifest identity; not an exhaustive controller audit', 'independentSourceAndSchemaChanges': changes}


def generate():
    catalog_path = ROOT / 'internal/assets/catalog.json'
    catalog = json.loads(catalog_path.read_text())
    evidence_path = ROOT / 'compat/rule-evolution.json'
    evidence = json.loads(evidence_path.read_text())
    historical_path = ROOT / 'compat/local-rules-sources.json'
    historical = json.loads(historical_path.read_text())
    source_review = audit_sources(catalog, evidence, historical, historical_path)
    versions = [row['version'] for row in catalog['versions']]
    kinds = {}
    for auto in catalog['autoSchemas']:
        kind = auto['kind']
        data = json.loads((ROOT / auto['path']).read_text())
        assert digest(ROOT / auto['path']) == auto['sha256']
        refs = [entry['$ref'] for entry in data['anyOf']]
        branch_versions = {ref: [] for ref in refs}
        normalized = {}
        profiles = {}
        for index, target in enumerate(catalog['versions']):
            exact = next((entry for entry in target['schemas'] if entry['kind'] == kind and entry['apiVersion'] == auto['apiVersion']), None)
            if exact is None:
                continue
            if exact['sha256'] not in normalized:
                path = ROOT / exact['path']
                assert digest(path) == exact['sha256']
                source = json.loads(path.read_text())
                single = compatibility([source])
                ref = single['anyOf'][0]['$ref']
                assert ref in branch_versions and all(data['$defs'][key] == value for key, value in single['$defs'].items()), 'auto branch is not the normalized exact source'
                required = []
                if kind == 'ApplicationSet':
                    node = source
                    for name in ['spec', 'template', 'spec']:
                        node = node['properties'][name]
                    required = sorted(node.get('required', []))
                normalized[exact['sha256']] = (ref, required)
            ref, required = normalized[exact['sha256']]
            branch_versions[ref].append(index)
            # Only current Domain inputs form the profile. Schema differences
            # remain independently validated above; equal schema hashes never
            # imply equal rule flags or audited upstream semantics.
            flag = target['localRules']['singleSourceRefs'] if kind != 'AppProject' else False
            key = (flag, tuple(required))
            profile = profiles.setdefault(key, {'representative': target['version'], 'singleSourceRefs': flag, 'templateRequired': required, 'versions': []})
            profile['versions'].append(index)
        assert all(branch_versions.values()), 'unmapped auto schema branch'
        kinds[kind] = {'apiVersion': auto['apiVersion'], 'schemaSHA256': auto['sha256'], 'branches': [{'ref': ref, 'versions': branch_versions[ref]} for ref in refs], 'profiles': list(profiles.values())}
    rule = next(row for row in evidence['rules'] if row['id'] == 'single-source-refs')
    for target in catalog['versions']:
        # This checks the explicit catalog model against reviewed intervals;
        # it does not expand the upstream-source evidence to unaudited releases.
        version = tuple(map(int, target['version'].split('.')))
        modeled = any(tuple(map(int, interval['from'].split('.'))) <= version <= tuple(map(int, interval['through'].split('.'))) for interval in rule['modeledActiveIntervals'])
        assert target['localRules']['singleSourceRefs'] == modeled, 'rule interval/catalog flag differs: ' + target['version']
    return {
        'schemaVersion': 1,
        'catalogSHA256': digest(catalog_path),
        'ruleEvidenceSHA256': digest(evidence_path),
        'historicalEvidenceSHA256': digest(historical_path),
        'evidenceCoverage': {
            'historicalReleaseCount': len(historical['versions']),
            'historicalVersions': list(historical['versions']),
            'boundarySampleVersions': rule['boundarySampleVersions'],
            'allCatalogSemanticsAudited': False,
            'modeledInventorySHA256': evidence['coverage']['modeledInventorySHA256'],
            'statement': 'Profiles describe currently modeled local rule inputs. Historical source records and boundary samples do not prove controller semantics for every catalog release.',
        },
        'versions': versions,
        'kinds': kinds,
        'sourceReview': source_review,
    }


def write_or_check(check=False):
    metadata = generate()
    result = canonical(metadata)
    if check:
        if not OUTPUT.is_file() or OUTPUT.read_bytes() != result:
            raise SystemExit('compatibility metadata is stale; run python3 tools/compatibility_metadata.py')
    else:
        OUTPUT.write_bytes(result)
    return metadata


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    metadata = write_or_check(parser.parse_args().check)
    print(json.dumps({'passed': True, 'catalogSHA256': metadata['catalogSHA256'], 'ruleEvidenceSHA256': metadata['ruleEvidenceSHA256'], 'sourceReview': metadata['sourceReview'], 'schemaBranches': {kind: len(value['branches']) for kind, value in metadata['kinds'].items()}}))
