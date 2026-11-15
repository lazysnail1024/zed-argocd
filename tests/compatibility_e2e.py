#!/usr/bin/env python3
"""Whole-resource candidate contracts chosen from independent schema/source boundaries.

The normal-check oracle was frozen on 6a2b5c8 before this interface existed.
Neither that oracle nor the expected release boundaries is learned from reports.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

import audit_repair_e2e as wire
from analysis_status_e2e import Client, request

ROOT = Path(__file__).resolve().parents[1]
METHOD = 'argocd/compatibility'
COMMAND = 'argocd.explainCompatibility'
KIND = 'source.argocd.explainCompatibility'
LABEL = 'candidates under modeled static constraints'
CONFIG = 'schemaVersion: 1\nreferencePolicy: open\n'
APP = '''apiVersion: argoproj.io/v1alpha1
kind: Application
metadata: {name: app}
spec:
  project: default
  source: {repoURL: https://example.org/repo}
  destination: {server: https://kubernetes.default.svc, namespace: default}
'''
CONFLICT = APP.replace('  source: {repoURL: https://example.org/repo}', '''  sources:
  - {repoURL: https://example.org/repo, ref: bad.ref, tagPrefix: component/}''')
SELF_REF = APP.replace('  source: {repoURL: https://example.org/repo}', '''  sources:
  - repoURL: https://example.org/repo
    ref: values
    helm: {valueFiles: ['$values/values.yaml']}''')
SCHEMA_CONFLICT = APP.replace('  source: {repoURL: https://example.org/repo}', '''  source: {repoURL: https://example.org/repo, ksonnet: {environment: dev}}
  syncPolicy: {automated: {enabled: true}}''')
APPSET = '''apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata: {name: set}
spec:
  generators: [{list: {elements: [{name: one}]}}]
  template:
    metadata: {name: app}
    spec:
      project: default
      source: {repoURL: https://example.org/repo}
      destination: {server: https://kubernetes.default.svc, namespace: default}
'''
PATCH = APPSET + '  templatePatch: |\n    spec:\n      source: null\n'
DYNAMIC = APPSET.replace('source: {repoURL: https://example.org/repo}', "sources: [{repoURL: https://example.org/repo, ref: values, helm: {valueFiles: ['{{ .valueFile }}']}}]")
BASELINES = {'valid': APP, 'intersection': CONFLICT, 'self-ref': SELF_REF, 'schema-conflict': SCHEMA_CONFLICT}
BASELINE_TARGETS = ('auto', '3.4.13', '3.5.0', '3.6.1')


def normalize_check(report, root):
    # Paths, elapsed time and build marketing version are not behavior. Every
    # diagnostic, range, severity, target/config/catalog identity stays checked.
    copy = dict(report)
    copy.pop('elapsedMilliseconds', None)
    copy.pop('toolVersion', None)
    return json.loads(json.dumps(copy).replace(root.as_uri(), 'file://$ROOT').replace(str(root).replace('\\', '\\\\'), '$ROOT'))


def run(binary, output):
    wire.BINARY = binary
    rows = []

    def record(name, checks, observed):
        rows.append({'id': name, 'assertions': checks, 'observed': observed, 'passed': bool(checks) and all(checks.values())})
        (output / 'report.json').write_text(json.dumps({'rows': rows, 'passed': all(r['passed'] for r in rows), 'command': sys.argv, 'environment': {'python': sys.version, 'platform': platform.platform()}, 'binarySHA256': wire.hash_file(binary), 'runnerSHA256': wire.hash_file(Path(__file__))}, indent=2) + '\n')
        print(name, 'PASS' if rows[-1]['passed'] else 'FAIL', flush=True)

    def cli(name, text, target='auto', command='explain-compatibility'):
        folder = output / name
        root = folder / 'workspace'
        root.mkdir(parents=True, exist_ok=True)
        wire.write(root, '.argocd-ls.yaml', CONFIG)
        file = wire.write(root, 'input.yaml', text)
        args = [str(binary), command, '--root', str(root), '--file', str(file), '--target-version', target]
        if command == 'check': args += ['--format', 'json']
        process = subprocess.run(args, capture_output=True, timeout=20, env=dict(os.environ, ARGOCD_LS_TRACE='1'))
        (folder / 'stdout').write_bytes(process.stdout)
        (folder / 'stderr').write_bytes(process.stderr)
        (folder / 'command.json').write_text(json.dumps(args))
        try: data = json.loads(process.stdout)
        except (ValueError, UnicodeError): data = {}
        return process.returncode, data, process.stderr.decode(errors='replace'), file, root

    def resource(data, index=0):
        resources = data.get('resources', [])
        return resources[index] if len(resources) > index else {}

    exit_code, data, trace, file, root = cli('valid', APP)
    item = resource(data)
    record('whole-resource-valid', {'success': exit_code == 0 and data.get('complete') is True, 'honestLabel': data.get('label') == LABEL and 'controller-admission-and-runtime' in data.get('notEvaluated', []), 'schemaAndJoint': all(v in item.get('schemaCandidates', []) and v in item.get('candidates', []) for v in ['2.0.0', '3.4.13', '3.5.0', '3.6.1']), 'diskIdentity': data.get('inputIdentity', {}).get('source') == 'disk' and data.get('inputIdentity', {}).get('sha256') == hashlib.sha256(APP.encode()).hexdigest(), 'boundedBranchWork': 'schemaBranches=25 semanticProfiles=2' in trace, 'readOnly': file.read_text() == APP}, data)

    exit_code, data, _, _, _ = cli('schema-conflict', SCHEMA_CONFLICT)
    item = resource(data)
    record('whole-schema-not-field-union', {'explanationSucceeds': exit_code == 0 and data.get('complete') is True, 'noWholeSchema': item.get('schemaCandidates') == [], 'noJointCandidates': item.get('candidates') == [], 'actionableSchemaReasons': bool(item.get('schemaExclusions')) and all(x.get('versions') and x.get('diagnostics') for x in item.get('schemaExclusions', []))}, data)

    _, legacy, _, _, _ = cli('removed-field', APP.replace('source: {repoURL: https://example.org/repo}', 'source: {repoURL: https://example.org/repo, ksonnet: {environment: dev}}'))
    _, current, _, _, _ = cli('introduced-field', APP + '  syncPolicy: {automated: {enabled: true}}\n')
    record('schema-field-evolution', {'removedAtCollectedBoundary': '2.3.0' in resource(legacy).get('schemaCandidates', []) and '2.4.0' not in resource(legacy).get('schemaCandidates', []), 'introducedAtCollectedBoundary': '3.0.0' not in resource(current).get('schemaCandidates', []) and '3.1.0' in resource(current).get('schemaCandidates', [])}, {'removed': legacy, 'introduced': current})

    exit_code, data, _, _, _ = cli('intersection', CONFLICT)
    item = resource(data)
    record('schema-semantic-empty-intersection', {'schemaNonempty': all(v in item.get('schemaCandidates', []) for v in ['3.5.0', '3.6.1']) and '3.4.13' not in item.get('schemaCandidates', []), 'semanticsOpposeSchema': '3.4.13' in item.get('semanticCandidates', []) and all(v not in item.get('semanticCandidates', []) for v in ['3.5.0', '3.6.1']), 'intersectionEmpty': exit_code == 0 and item.get('candidates') == [], 'ruleReason': any(any(d.get('code') == 'CD_VALUES_REF_INVALID' for d in x.get('diagnostics', [])) and '3.5.0' in x.get('versions', []) for x in item.get('semanticExclusions', []))}, data)

    _, data, _, _, _ = cli('self-ref', SELF_REF)
    item = resource(data)
    record('single-source-ref-boundary', {'schemaBeforeAndAfter': all(v in item.get('schemaCandidates', []) for v in ['3.4.13', '3.5.0', '3.6.1']), 'beforeExcluded': '3.4.13' not in item.get('candidates', []) and any(any(d.get('code') == 'CD_VERSION_FEATURE' for d in x.get('diagnostics', [])) for x in item.get('semanticExclusions', [])), 'afterIncluded': all(v in item.get('candidates', []) for v in ['3.5.0', '3.6.1'])}, data)

    _, data, trace, _, _ = cli('static-patch', PATCH)
    item = resource(data)
    record('appset-schema-dependent-rule-profile', {'threeProfiles': 'schemaBranches=22 semanticProfiles=3' in trace, 'oldRequiredSourceExcluded': '2.5.22' not in item.get('semanticCandidates', []) and any(any('removes required Application spec field source' in d.get('message', '') for d in x.get('diagnostics', [])) for x in item.get('semanticExclusions', [])), 'newOptionalSourceIncluded': all(v in item.get('semanticCandidates', []) for v in ['2.6.0', '3.4.13', '3.5.0'])}, data)

    _, data, _, _, _ = cli('dynamic', DYNAMIC)
    item = resource(data)
    record('dynamic-coverage-remains-unknown', {'retainsStaticCandidates': bool(item.get('candidates')), 'unknownExplicit': any(x.get('code') == 'CD_APPSET_DYNAMIC_FIELD' for x in item.get('unknowns', [])), 'noDeploymentProof': data.get('label') == LABEL and 'generator-expansion' in data.get('notEvaluated', []) and 'cross-resource-project-policy' in data.get('notEvaluated', [])}, data)

    _, data, _, _, _ = cli('stream', APP + '---\n' + CONFLICT)
    record('yaml-stream-independent-resources', {'twoResourceResults': len(data.get('resources', [])) == 2, 'independentCandidates': bool(resource(data).get('candidates')) and resource(data, 1).get('candidates') == [], 'noInventedCommonTarget': data.get('scopeSemantics') == 'independent-resources' and 'candidates' not in data}, data)

    exit_code, data, _, _, _ = cli('damaged', APP + '---\napiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata: [\n')
    record('damaged-input-not-confident', {'incomplete': exit_code == 2 and data.get('complete') is False, 'reasonRetained': any(x.get('code') == 'INPUT_INCOMPLETE' for x in data.get('unknowns', [])), 'partialResourcesLabelled': data.get('label') == LABEL}, data)

    exit_code, data, _, _, _ = cli('unowned', 'apiVersion: v1\nkind: ConfigMap\nmetadata: {name: ordinary}\n')
    record('unowned-input-explicit', {'noCandidateClaim': exit_code == 0 and data.get('resources') == [], 'scopeExplicit': any(x.get('code') == 'NO_ARGOCD_RESOURCE' for x in data.get('unknowns', []))}, data)

    _, data, trace, _, _ = cli('project-coverage', wire.PROJECT)
    item = resource(data)
    record('project-schema-only-coverage', {'wholeProjectSchema': bool(item.get('schemaCandidates')) and 'schemaBranches=12 semanticProfiles=1' in trace, 'noInventedLocalPolicyProof': item.get('modeledRuleCoverage') == 'schema-only' and any(x.get('code') == 'NO_DOCUMENT_LOCAL_RULES' for x in item.get('unknowns', [])), 'crossResourcePolicyOmitted': 'cross-resource-project-policy' in data.get('notEvaluated', [])}, data)

    exit_code, data, _, _, _ = cli('resource-limit', '---\n'.join([APP] * 33))
    record('bounded-resource-explanation', {'boundedAndIncomplete': exit_code == 2 and data.get('complete') is False and len(data.get('resources', [])) == 32, 'omittedWorkExplicit': any(x.get('code') == 'RESOURCE_LIMIT' for x in data.get('unknowns', [])), 'completedResourcesRetained': all(x.get('candidates') for x in data.get('resources', [])) and bool(data.get('resources'))}, data)

    _, data, _, _, _ = cli('provenance', CONFLICT, '3.4.13')
    item = resource(data)
    evidence = data.get('catalog', {}).get('evidenceCoverage', {})
    record('catalog-and-source-evidence-scope', {'inventoryIdentity': data.get('catalog', {}).get('releaseCount') == 370 and data.get('catalog', {}).get('sha256') == wire.hash_file(ROOT / 'internal/assets/catalog.json'), 'coverageNotInventory': evidence.get('historicalReleaseCount') == 88 and evidence.get('allCatalogSemanticsAudited') is False and evidence.get('boundarySampleVersions') == ['3.4.11', '3.4.13', '3.5.0', '3.6.1'], 'ruleEvidenceIdentity': data.get('catalog', {}).get('ruleEvidenceSHA256') == wire.hash_file(ROOT / 'compat/rule-evolution.json') and data.get('catalog', {}).get('compatibilitySHA256') == wire.hash_file(ROOT / 'internal/assets/compatibility.json'), 'exactSelectionExplained': data.get('inputIdentity', {}).get('targetVersion') == '3.4.13' and item.get('selectedTarget', {}).get('schemaCandidate') is False and item.get('selectedTarget', {}).get('modeledCandidate') is False}, data)

    maintenance = subprocess.run([sys.executable, str(ROOT / 'tools/compatibility_metadata.py'), '--check'], capture_output=True, timeout=30)
    (output / 'maintenance.stdout').write_bytes(maintenance.stdout)
    (output / 'maintenance.stderr').write_bytes(maintenance.stderr)
    try: maintenance_data = json.loads(maintenance.stdout)
    except ValueError: maintenance_data = {}
    changes = maintenance_data.get('sourceReview', {}).get('independentSourceAndSchemaChanges', [])
    record('source-maintenance-independent-of-schemas', {'reproducibleMetadata': maintenance.returncode == 0 and maintenance_data.get('passed') is True, 'exactSourcesChecked': maintenance_data.get('sourceReview', {}).get('sourceFilesVerified') == 7 and maintenance_data.get('sourceReview', {}).get('historicalManifestVersionsBound') == 88, 'functionAndCallerDiffs': any(x.get('before', {}).get('version') == '3.4.13' and x.get('after', {}).get('version') == '3.5.0' and x.get('functionChanged') is True and x.get('callerChanged') is True for x in changes), 'equalSchemaSourcesStillChecked': any(x.get('sameSchemas') is True and x.get('before', {}).get('functionSHA256') and x.get('after', {}).get('functionSHA256') for x in changes)}, maintenance_data)

    # A deliberately changed formerly unaudited pin keeps every schema/flag
    # identical. Regeneration must still require an explicit semantic review.
    mutant = output / 'maintenance-changed-pin'
    for name in ['tools/compatibility_metadata.py', 'tools/normalize_schemas.py', 'internal/assets/catalog.json', 'compat/rule-evolution.json', 'compat/local-rules-sources.json']:
        destination = mutant / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)
    mutated_catalog = json.loads((mutant / 'internal/assets/catalog.json').read_text())
    mutated_catalog['versions'][0]['upstreamCommit'] = '0' * 40
    (mutant / 'internal/assets/catalog.json').write_text(json.dumps(mutated_catalog))
    control = subprocess.run([sys.executable, str(mutant / 'tools/compatibility_metadata.py')], capture_output=True, timeout=30)
    (mutant / 'stdout').write_bytes(control.stdout)
    (mutant / 'stderr').write_bytes(control.stderr)
    record('unreviewed-source-pin-blocks-regeneration', {'sameSchemaDoesNotBypassReview': control.returncode != 0 and b'semantic model inventory pins changed' in control.stderr, 'noCandidateMetadataProduced': not (mutant / 'internal/assets/compatibility.json').exists()}, {'exit': control.returncode, 'stderr': control.stderr.decode(errors='replace')})

    frozen = json.loads((ROOT / 'tests/fixtures/compatibility-check-before.json').read_text())
    observed = {}
    for name, text in BASELINES.items():
        for version in BASELINE_TARGETS:
            exit_code, data, _, _, root = cli('check-' + name + '-' + version, text, version, 'check')
            observed[name + '/' + version] = {'exit': exit_code, 'report': normalize_check(data, root)}
    record('ordinary-auto-and-exact-unchanged', {'exactFrozenOracle': observed == frozen['cases'], 'autoSuccessControl': observed['valid/auto']['exit'] == 0 and frozen['cases']['valid/auto']['exit'] == 0, 'boundaryBehaviorPreserved': observed['self-ref/3.4.13']['exit'] == 1 and observed['self-ref/3.5.0']['exit'] == 0}, observed)

    folder = output / 'lsp-buffer'
    root = folder / 'workspace'
    root.mkdir(parents=True)
    wire.write(root, '.argocd-ls.yaml', CONFIG)
    file = wire.write(root, 'app.yaml', APP)
    client = Client(folder, [root])
    try:
        client.open(file, CONFLICT, 7)
        client.diag(file, 7)
        params = {'textDocument': {'uri': file.as_uri()}, 'position': {'line': 0, 'character': 0}}
        data = request(client, METHOD, params).get('result') or {}
        actions = request(client, 'textDocument/codeAction', {'textDocument': params['textDocument'], 'range': {'start': params['position'], 'end': params['position']}, 'context': {'diagnostics': [], 'only': [KIND]}}).get('result', [])
        actions = [a for a in actions if a.get('command', {}).get('command') == COMMAND]
        explained = request(client, 'workspace/executeCommand', actions[0]['command']) if actions else {}
        if actions: client.wait(lambda row: row.get('method') == 'window/showMessage')
        messages = [x['message']['params']['message'] for x in client.messages if x['message'].get('method') == 'window/showMessage']
        record('lsp-unsaved-buffer-and-explicit-action', {'bufferIdentity': data.get('inputIdentity', {}).get('source') == 'buffer' and data.get('inputIdentity', {}).get('version') == 7 and data.get('inputIdentity', {}).get('sha256') == hashlib.sha256(CONFLICT.encode()).hexdigest(), 'bufferCandidates': resource(data).get('candidates') == [] and bool(resource(data).get('schemaCandidates')), 'discoverableReadOnlyAction': len(actions) == 1 and actions[0].get('kind') == KIND and 'edit' not in actions[0], 'visibleEpistemicLabel': len(messages) == 1 and LABEL in messages[0] and explained.get('result', {}).get('label') == LABEL, 'diskUnchanged': file.read_text() == APP}, {'report': data, 'actions': actions, 'messages': messages})
        client.change(file, APP, 8)
        client.diag(file, 8)
        stale = client.request('workspace/executeCommand', actions[0]['command']) if actions else {}
        fresh = request(client, METHOD, params).get('result') or {}
        record('lsp-stale-action-and-recovery', {'oldActionRefused': stale.get('error', {}).get('data', {}).get('reasonCode') == 'STALE_INPUT', 'currentCandidatesRecover': fresh.get('inputIdentity', {}).get('version') == 8 and bool(resource(fresh).get('candidates')), 'noNotificationFlood': sum(x['message'].get('method') == 'window/showMessage' for x in client.messages) == 1}, {'stale': stale, 'fresh': fresh})
    finally:
        client.close()

    folder = output / 'nested-current-config'
    parent = folder / 'workspace'
    child = parent / 'child'
    child.mkdir(parents=True)
    wire.write(parent, '.argocd-ls.yaml', CONFIG)
    wire.write(parent, 'broken.yaml', 'apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata: [\n')
    config_text = CONFIG + 'targetVersion: 3.4.13\n'
    current_config = CONFIG + 'targetVersion: 3.5.0\n'
    config_file = wire.write(child, '.argocd-ls.yaml', config_text)
    file = wire.write(child, 'app.yaml', SELF_REF)
    client = Client(folder, [parent, child])
    try:
        client.open(file, SELF_REF, 4)
        client.diag(file, 4)
        client.open(config_file, current_config, 2)
        data = request(client, METHOD, {'textDocument': {'uri': file.as_uri()}}).get('result') or {}
        record('lsp-owning-root-and-current-config', {'deepestRoot': data.get('root') == str(child) and data.get('scope') == 'root', 'acceptedUnsavedConfig': data.get('inputIdentity', {}).get('targetVersion') == '3.5.0' and data.get('inputIdentity', {}).get('configSource') == 'buffer' and data.get('inputIdentity', {}).get('configHash') == hashlib.sha256(current_config.encode()).hexdigest(), 'localResultIndependent': data.get('complete') is True and resource(data).get('selectedTarget', {}).get('modeledCandidate') is True, 'noConfigMutation': config_file.read_text() == config_text}, data)
    finally:
        client.close()

    return all(r['passed'] for r in rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('binary', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.resolve().mkdir(parents=True, exist_ok=False)
    sys.exit(0 if run(args.binary.resolve(), args.output.resolve()) else 1)
