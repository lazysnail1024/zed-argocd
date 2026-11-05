#!/usr/bin/env python3
"""Official-release/tag inventory and real CLI/LSP compatibility acceptance.

--bootstrap validates collected targets before they are marked ready; normal CI
requires the public completed envelope and zero-configuration auto selection.
CI: Linux uses all targets by default. Other platforms may use --representatives
to cover one exact target per distinct schema bundle plus auto CLI/LSP probes.
"""
import argparse
import concurrent.futures
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import time
import audit_repair_e2e as wire

ROOT = pathlib.Path(__file__).resolve().parents[1]


def clone(value):
    return json.loads(json.dumps(value))


def app_spec():
    return {'project': '', 'source': {'repoURL': 'https://example.com/repo'},
            'destination': {'server': 'https://kubernetes.default.svc', 'namespace': 'default'}}


def sample(kind):
    spec = app_spec()
    if kind == 'AppProject':
        spec = {'sourceRepos': ['*'], 'destinations': [{'server': '*', 'namespace': '*'}]}
    elif kind == 'ApplicationSet':
        spec = {'generators': [{'list': {'elements': []}}], 'template': {'metadata': {'name': 'child'}, 'spec': spec}}
    return {'apiVersion': 'argoproj.io/v1alpha1', 'kind': kind, 'metadata': {'name': 'sample'}, 'spec': spec}


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(catalog):
    errors = []
    observed = set()
    for source in catalog['inventorySources']:
        path = ROOT / source['file']
        if checksum(path) != source['sha256']: errors.append('inventory digest ' + str(path))
        for row in json.loads(path.read_text()):
            tag = row['tag_name']
            if re.fullmatch(r'v\d+\.\d+\.\d+', tag) and not row['draft'] and not row['prerelease'] and row['published_at'] <= catalog['cutoff'] and int(tag[1:].split('.')[0]) in catalog['supportedMajors']:
                observed.add(tag[1:])
    actual = {target['version'] for target in catalog['versions']}
    if not observed <= actual: errors.append('missing stable releases: ' + str(observed - actual))
    newest = tuple(map(int, catalog['latestVersion'].split('.')))
    if catalog['supportedMajors'] != [newest[0]-1, newest[0]]: errors.append('rolling major window')
    refs = {}
    objects = {}
    for source in catalog['tagSources']:
        path = ROOT / source['file']
        if checksum(path) != source['sha256']: errors.append('tag inventory digest ' + str(path))
        value = json.loads(path.read_text())
        if source['type'] == 'refs':
            for row in value:
                tag = row['ref'].split('/')[-1]
                if re.fullmatch(r'v\d+\.\d+\.\d+', tag) and int(tag[1:].split('.')[0]) in catalog['supportedMajors'] and tuple(map(int,tag[1:].split('.'))) <= newest:
                    refs[tag[1:]] = row['object']
        else:
            objects[value['sha']] = value['object']
    if set(refs) != actual: errors.append('missing/extra stable tags: ' + str(set(refs) ^ actual))
    for target in catalog['versions']:
        obj = refs.get(target['version'])
        seen = set()
        while obj and obj['type'] == 'tag' and obj['sha'] not in seen:
            seen.add(obj['sha'])
            obj = objects.get(obj['sha'])
        if not obj or obj['type'] != 'commit' or obj['sha'] != target['upstreamCommit']:
            errors.append('tag commit mismatch ' + target['version'])
    checked = set()
    for target in catalog['versions']:
        for source in target['sources']:
            if target['upstreamCommit'] not in source['url']: errors.append('mutable source ' + source['url'])
            if source['sha256'] not in checked and checksum(ROOT / source['localPath']) != source['sha256']: errors.append('source digest ' + source['localPath'])
            checked.add(source['sha256'])
        for entry in target['schemas']:
            if entry['sha256'] not in checked and checksum(ROOT / entry['path']) != entry['sha256']: errors.append('schema digest ' + entry['path'])
            checked.add(entry['sha256'])
    for entry in catalog['autoSchemas']:
        if checksum(ROOT / entry['path']) != entry['sha256']: errors.append('auto schema digest ' + entry['path'])
        checked.add(entry['sha256'])
    runtime = json.loads((ROOT / 'internal/assets/catalog.json').read_text())
    projection = {key: catalog[key] for key in ('product','defaultVersion','latestVersion','supportedMajors','autoSchemas','ready')}
    projection['versions'] = [{key: value for key, value in target.items() if key in ('version','upstreamCommit','schemas','semanticsVerified','localRules')} for target in catalog['versions']]
    if runtime != projection: errors.append('embedded runtime catalog differs from verified inventory')
    return {'id': 'official-inventory', 'passed': not errors, 'errors': errors, 'stableReleases': len(observed), 'stableTags': len(actual), 'tagOnly': sorted(actual - observed), 'distinctAssets': len(checked)}


def run_cli(binary, out, target, kinds, bootstrap):
    version = target or 'default'
    folder = out / version
    work = folder / 'workspace'
    work.mkdir(parents=True, exist_ok=True)
    expectations = {}
    for kind in kinds:
        value = sample(kind)
        cases = [('valid', value, [])]
        invalid = clone(value)
        if kind == 'Application': invalid['spec']['project'] = 42
        elif kind == 'AppProject': invalid['spec']['sourceRepos'] = 42
        else: invalid['spec']['template']['spec']['project'] = 42
        cases.append(('type', invalid, ['SCHEMA_TYPE']))
        invalid = clone(value)
        if kind == 'Application': invalid['spec'].pop('destination')
        elif kind == 'AppProject': invalid['spec']['roles'] = [{}]
        else: invalid['spec'].pop('template')
        cases.append(('required', invalid, ['SCHEMA_REQUIRED']))
        if kind == 'Application':
            invalid = clone(value); invalid['spec']['destination']['name'] = 'cluster'
            cases.append(('conflict', invalid, ['CD_DESTINATION_CONFLICT']))
        for name, value, expected in cases:
            path = work / (kind + '-' + name + '.yaml')
            path.write_text(json.dumps(value, indent=2) + '\n')
            expectations[path.as_uri()] = expected
    command = [str(binary), 'check', '--root', str(work), '--format', 'json']
    if target: command += ['--target-version', target]
    begin = time.monotonic()
    process = subprocess.run(command, capture_output=True, timeout=20)
    (folder / 'stdout.json').write_bytes(process.stdout)
    (folder / 'stderr').write_bytes(process.stderr)
    errors = []
    try:
        report = json.loads(process.stdout)
        for uri, expected in expectations.items():
            actual = sorted(set(d['code'] for d in report['diagnostics'] if d['uri'] == uri and d['severity'] == 1))
            if actual != expected: errors.append(uri.rsplit('/',1)[-1] + ': ' + str(actual) + ' expected ' + str(expected))
        if report['resourcesAnalyzed'] != len(expectations): errors.append('resources dropped')
        if len(report['targets']) != 1 or report['targets'][0]['version'] != (target or 'auto'): errors.append('wrong resolved target')
        if bootstrap:
            if any(e['code'] != 'TARGET_NOT_VERIFIED' for e in report['configurationErrors']): errors.append('unexpected bootstrap error')
        elif not report['analysisComplete'] or report['configurationErrors'] or process.returncode != 1:
            errors.append('incomplete public report')
    except Exception as error: errors.append(repr(error))
    return {'id': version, 'passed': not errors, 'errors': errors, 'command': command, 'exit': process.returncode,
            'resources': len(expectations), 'seconds': round(time.monotonic()-begin,4)}


def cross_version(binary, out, bootstrap):
    folder = out / 'auto-fields';work=folder/'workspace';work.mkdir(parents=True, exist_ok=True)
    cases = [
        ('legacy-valid', {'source': {'repoURL': 'x', 'ksonnet': {'environment': 'dev'}}}, []),
        ('legacy-type', {'source': {'repoURL': 'x', 'ksonnet': {'environment': 42}}}, ['SCHEMA_TYPE']),
        ('current-valid', {'syncPolicy': {'automated': {'enabled': True}}}, []),
        ('current-type', {'syncPolicy': {'automated': {'enabled': 'yes'}}}, ['SCHEMA_TYPE']),
        ('impossible-combination', {'source': {'repoURL': 'x', 'ksonnet': {'environment': 'dev'}}, 'syncPolicy': {'automated': {'enabled': True}}}, ['SCHEMA_VERSION_COMBINATION']),
        ('unknown-warning', {'mystery': True}, ['SCHEMA_UNKNOWN_FIELD']),
    ]
    expected = {}
    for name, fields, codes in cases:
        value=sample('Application');value['spec'].update(fields);path=work/(name+'.yaml');path.write_text(json.dumps(value));expected[path.as_uri()]=codes
    command=[str(binary),'check','--root',str(work),'--format','json']
    if bootstrap: command += ['--target-version','auto']
    process=subprocess.run(command,capture_output=True,timeout=20);(folder/'stdout.json').write_bytes(process.stdout);(folder/'stderr').write_bytes(process.stderr)
    report=json.loads(process.stdout);errors=[]
    for uri,codes in expected.items():
        actual=sorted(d['code'] for d in report['diagnostics'] if d['uri']==uri)
        if actual!=codes:errors.append(uri.rsplit('/',1)[-1]+': '+str(actual)+' expected '+str(codes))
    if not bootstrap and (not report['analysisComplete'] or report['targets'][0]['source']!='default'):errors.append('auto requires configuration')
    return {'id':'auto-old-new-combination','passed':not errors,'errors':errors,'command':command,'exit':process.returncode}


def lsp_auto(binary,out):
    folder=out/'auto-lsp';work=folder/'workspace';work.mkdir(parents=True,exist_ok=True)
    fixtures=folder/'fixtures';fixtures.mkdir(exist_ok=True)
    wire.BINARY=binary
    client=wire.LSP(folder,[work]);path=work/'app.yaml';rows=[]
    head='apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata: {name: demo}\nspec:\n'
    required='  project: ""\n  destination: {}\n'
    legacy='  source: {repoURL: x, ksonnet: {environment: dev}}\n'
    current='  syncPolicy: {automated: {enabled: true}}\n'
    automated='  syncPolicy:\n    automated:\n      |\n'
    source='  source:\n    repoURL: x\n    |\n'
    cases=[
        ('legacy-field',head+required+'  source:\n    repoURL: x\n    kson|\n',['ksonnet'],[]),
        ('current-field',head+required+'  source: {repoURL: x}\n  syncPolicy:\n    automated:\n      ena|\n',['enabled'],[]),
        ('legacy-selects-whole-version',head+required+legacy+automated,['prune'],['enabled']),
        ('current-selects-whole-version',head+required+current+source,['helm'],['ksonnet']),
        # Ignoring an already present active field prevents an incompatible pair
        # from forcing the all-branches repair fallback during a key edit.
        ('active-legacy-field-excluded',head+required+current+'  source:\n    repoURL: x\n    ksonnet|: {environment: dev}\n',[],['ksonnet']),
        ('active-current-field-excluded',head+required+legacy+'  syncPolicy:\n    automated:\n      enabled|: true\n',[],['enabled']),
        ('missing-required-still-completes',head+legacy+automated,['prune'],['enabled']),
        ('ordinary-type-errors-still-complete',head+'  project: 42\n  destination: {}\n'+legacy.replace('environment: dev','environment: 42')+automated,['prune'],['enabled']),
        ('current-type-error-still-selects',head+required+current.replace('enabled: true','enabled: "yes"')+source,['helm'],['ksonnet']),
        ('array-selects-whole-version',head+required+'  source: {repoURL: x}\n'+automated+'status:\n  history:\n    - source: {repoURL: x, ksonnet: {environment: dev}}\n',['prune'],['enabled']),
        # No complete version matches an unknown name or conflicting siblings.
        # Keep a repair path instead of suppressing all ordinary suggestions.
        ('unknown-sibling-repair',head+required+'  unknown: true\n'+current+source,['helm'],[]),
        ('conflicting-siblings-repair',head+required+current+'  source:\n    repoURL: x\n    ksonnet: {environment: dev}\n    hel|\n',['helm'],[]),
    ]
    try:
        for revision,(name,text,labels,forbidden) in enumerate(cases,1):
            point=text.index('|');plain=text.replace('|','');before=plain[:point];position={'line':before.count('\n'),'character':len(before.rsplit('\n',1)[-1])}
            (fixtures/(name+'.yaml')).write_text(text)
            if revision==1:client.open(path,plain,revision)
            else:client.change(path,plain,revision)
            client.diag(path,revision)
            result=client.request('textDocument/completion',{'textDocument':{'uri':path.as_uri()},'position':position})
            observed=[item['label'] for item in result.get('result') or []]
            rows.append({'id':name,'passed':not result.get('error') and set(labels)<=set(observed) and not set(forbidden)&set(observed),'expected':labels,'forbidden':forbidden,'observed':observed,'error':result.get('error')})
    finally:client.close()
    return {'id':'auto-lsp-completion','passed':all(row['passed'] for row in rows) and client.proc.returncode==0,'rows':rows}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('binary',type=pathlib.Path);parser.add_argument('output',type=pathlib.Path);parser.add_argument('--bootstrap',action='store_true');parser.add_argument('--representatives',action='store_true',help='One representative per distinct bundle; use all exact tags by default');args=parser.parse_args()
    binary=args.binary.resolve();out=args.output.resolve();out.mkdir(parents=True,exist_ok=True);catalog=json.loads((ROOT/'compat/catalog.lock.json').read_text());rows=[inventory(catalog)]
    selected=[];seen=set()
    for target in catalog['versions']:
        bundle=tuple(entry['sha256'] for entry in target['schemas'])
        if args.representatives and bundle in seen:continue
        selected.append(target);seen.add(bundle)
    def run(target):
        try:return run_cli(binary,out,target['version'],[entry['kind'] for entry in target['schemas']],args.bootstrap)
        except Exception as error:return {'id':target['version'],'passed':False,'errors':[repr(error)]}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool: rows.extend(pool.map(run,selected))
    rows.append(run_cli(binary,out,'auto' if args.bootstrap else None,['Application','AppProject','ApplicationSet'],args.bootstrap))
    rows.append(cross_version(binary,out,args.bootstrap))
    if not args.bootstrap:rows.append(lsp_auto(binary,out))
    report={'binarySHA256':checksum(binary),'runnerSHA256':checksum(pathlib.Path(__file__)),'catalogSHA256':checksum(ROOT/'compat/catalog.lock.json'),'bootstrap':args.bootstrap,'testedVersions':[target['version'] for target in selected],'rows':rows,'passed':bool(rows) and all(row['passed'] for row in rows),'scope':'Collected CRD validation, core local static checks and editor queries; no controller execution claim'}
    (out/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'rows':len(rows),'passed':report['passed'],'failed':[row for row in rows if not row['passed']]}))
    return 0 if report['passed'] else 1


if __name__=='__main__':sys.exit(main())
