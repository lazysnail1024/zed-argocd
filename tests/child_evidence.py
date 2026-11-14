"""Validate original consumer reports against checked-in case/assertion contracts.

Contracts are acceptance inputs reviewed with the consumer, not learned from an
uploaded report. Raw reports are copied unchanged for the independent gate.
"""
import hashlib
import json
from pathlib import Path
import shutil
import copy

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / 'tests/child_contracts.json'


def require(value, message):
    if not value:
        raise ValueError(message)


def load(path):
    require(path.is_file() and not path.is_symlink(), 'Missing regular child report: '+str(path))
    require(path.stat().st_size <= 64*1024*1024, 'Child report exceeds 64 MiB: '+str(path))
    return json.loads(path.read_text(encoding='utf-8'))


def contracts(platform):
    catalog = load(CONTRACT)
    require(catalog.get('schemaVersion') == 1, 'Unknown child contract schema')
    result = catalog['consumers'].copy()
    for name, variants in catalog.get('platforms', {}).items():
        if platform in variants:
            result[name] = variants[platform]
    return result


def equal(actual, expected):
    return json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)


def validate_row(row, spec):
    require(isinstance(row, dict), 'Child case is not an object')
    for key, expected in spec['identity'].items():
        require(key in row and equal(row[key], expected), 'Wrong child case identity: '+key)
    if 'passKey' in spec:
        require(row.get(spec['passKey']) is True, 'Child case failed')
    for key in spec.get('empty', []):
        require(key in row and row[key] in (None, []), 'Child case contains an error')
    for key in ('checks', 'assertions'):
        if key not in spec:
            require(key not in row, 'Unexpected assertion set: '+key)
            continue
        actual, expected = row.get(key), spec[key]
        if 'keys' in expected:
            require(isinstance(actual, dict) and sorted(actual) == expected['keys'], 'Missing or unexpected child assertions')
            require(all(value is True for value in actual.values()), 'Child assertion is false or not boolean')
        elif 'rows' in expected:
            require(isinstance(actual, list) and len(actual) == len(expected['rows']) and actual, 'Missing child assertion rows')
            for child, definition in zip(actual, expected['rows']):
                validate_row(child, definition)
        else:
            require(equal(actual, expected['literal']), 'Child assertion oracle differs')
    for path, expected in spec.get('required', {}).items():
        actual=row
        for key in path.split('.'):
            require(isinstance(actual,dict) and key in actual, 'Missing child observation: '+path)
            actual=actual[key]
        require(equal(actual,expected), 'Wrong child observation: '+path)


def validate(raw, spec, binary_sha=None, verify_identity=True):
    if spec.get('applicability'):
        spec=copy.deepcopy(spec)
        applicability=raw.get('applicability',{})
        require(set(applicability)=={'symlinks','fifo','socket'}, 'Missing evidence transport applicability')
        for key,value in applicability.items():
            require(type(value)==bool or (isinstance(value,str) and value.startswith('unavailable on this host: ')), 'Unknown transport applicability')
        if applicability['symlinks'] is not True:
            spec['rows']=[r for r in spec['rows'] if r['identity'].get('id') not in ('linked-root','wrong-link')]
        keys=next(r for r in spec['rows'] if r['identity'].get('id')=='retention-and-replay')['checks']['keys']
        for capability,key in [('symlinks','linkMetadataRetained'),('fifo','fifoMetadataOnly'),('socket','socketMetadataOnly')]:
            if applicability[capability] is not True:keys.remove(key)
    if spec['rootRow']:
        rows=[raw]
    elif spec['rowsKey'] is None:
        rows=raw
    else:
        require(isinstance(raw,dict), 'Expected child report object')
        rows=raw.get(spec['rowsKey'])
    require(isinstance(rows,list) and rows and len(rows)==len(spec['rows']), 'Missing, empty, or duplicate child cases')
    for row, expected in zip(rows,spec['rows']):
        validate_row(row,expected)
    if isinstance(raw,dict):
        if 'passKey' in spec:
            require(raw.get(spec['passKey']) is True, 'Child report failed')
        if verify_identity and 'runnerKey' in spec:
            require(raw.get(spec['runnerKey'])==hashlib.sha256((ROOT/spec['runner']).read_bytes()).hexdigest(), 'Child runner differs from checked-out source')
        if binary_sha is not None and 'binaryKey' in spec:
            require(raw.get(spec['binaryKey'])==binary_sha, 'Child used a different native binary')
    if spec.get('performance'):
        for row in rows:
            require(len(row.get('samplesMs',[]))==30 and all(type(x) in (int,float) and x>=0 for x in row['samplesMs']), 'Missing performance samples')
            hot=[x for x in row.get('traces',[]) if x.get('fullScan') is False]
            require(len(hot)==30 and all(x.get('diskReads')==0 and x.get('parsedFiles')==1 and x.get('domainDocuments')==2 and x.get('repositoryDecodes',0)<=1 and x.get('acquisitionFallbacks')==0 for x in hot), 'Incremental work contract failed')


def collect(directory, step, platform):
    spec=contracts(platform).get(step)
    if spec is None:
        require(step in ('build','package','helper-build'), 'Unregistered child report contract: '+step)
        return None
    source=directory/spec['file']
    raw=load(source)
    validate(raw,spec)
    destination=directory/'child-evidence'/(step+'.json')
    destination.parent.mkdir(exist_ok=True)
    companions={relative:load(source.parent/relative) for relative in spec.get('companions',[])}
    validate_extra(raw,spec,companions)
    shutil.copyfile(source,destination)
    receipt={'path':'child-evidence/'+step+'.json','sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),'companions':{}}
    for index,relative in enumerate(spec.get('companions',[])):
        child=destination.parent/(step+'-child-'+str(index)+'.json')
        shutil.copyfile(source.parent/relative,child)
        receipt['companions'][relative]={'path':'child-evidence/'+child.name,'sha256':hashlib.sha256(child.read_bytes()).hexdigest()}
    return receipt


def verify(directory, report, steps, platform, binary_sha):
    definitions=contracts(platform)
    required=[step for step in steps if step not in ('build','package','helper-build')]
    require(all(step in definitions for step in required), 'A required consumer has no child report contract')
    evidence=report.get('childEvidence')
    require(isinstance(evidence,dict) and set(evidence)==set(required), 'Missing or unexpected original child evidence')
    for step in required:
        entry=evidence[step]
        expected_path='child-evidence/'+step+'.json'
        require(isinstance(entry,dict) and entry.get('path')==expected_path, 'Wrong child evidence path')
        path=directory/expected_path
        require(not path.parent.is_symlink(), 'Child evidence directory cannot be a symlink')
        raw=load(path)
        require(hashlib.sha256(path.read_bytes()).hexdigest()==entry.get('sha256'), 'Original child report digest differs')
        spec=definitions[step]
        validate(raw,spec,binary_sha)
        items=entry.get('companions',{})
        require(set(items)==set(spec.get('companions',[])), 'Missing original nested child reports')
        companions={}
        for index,relative in enumerate(spec.get('companions',[])):
            child=directory/'child-evidence'/(step+'-child-'+str(index)+'.json')
            require(items[relative].get('path')=='child-evidence/'+child.name, 'Wrong nested evidence path')
            companions[relative]=load(child)
            require(hashlib.sha256(child.read_bytes()).hexdigest()==items[relative].get('sha256'), 'Changed nested child report')
        validate_extra(raw,spec,companions)


def validate_extra(raw, spec, companions):
    kind=spec.get('kind')
    if kind=='audit-mutations':
        from audit_regression_mutations import MUTATIONS, behavior_failure
        require(raw.get('completed') is True, 'Mutation consumer did not complete')
        for row, definition in zip(raw['rows'], MUTATIONS):
            require(row.get('build',{}).get('cleanupError') is None and row.get('oracle',{}).get('cleanupError') is None, 'Mutation execution leaked a child')
            require(behavior_failure(row.get('oracle',{}).get('report',{}),definition), 'Missing named mutation failure evidence')
    elif kind=='parent-faults':
        from parent_workspace_faults import detected_loss
        require(raw.get('completed') is True and raw.get('control',{}).get('exit')==0, 'Missing original positive parent control')
        validate(raw['control'].get('report'),contracts('linux/amd64')['parent-workspace'],verify_identity=False)
        positive,negative=raw['rows']
        require(positive.get('oracle',{}).get('exit')==0 and positive['oracle'].get('report',{}).get('passed') is True and positive['oracle']['report'].get('timeoutBuildRequired') is True, 'Missing deadline recovery control')
        deadline=copy.deepcopy(contracts('linux/amd64')['parent-workspace'])
        deadline['rows']=deadline['rows'][:1]
        deadline['rows'][0]['identity']={'id':'six-repositories-deadline'}
        for check in deadline['rows'][0]['checks']['rows']:
            if check['identity']['id']=='cross-repository-reference-analysis-recovers-without-restart':
                check['identity']['id']='permanent-deadline-still-allows-valid-local-buffer'
        validate(positive['oracle']['report'],deadline,verify_identity=False)
        require(detected_loss(negative.get('oracle',{})), 'Missing named publication recovery failure')
    elif kind=='project-rename-ablation':
        rows=companions['lsp/report.json'].get('rows',[])
        require(len(rows)==1 and rows[0].get('id')=='duplicate', 'Missing duplicate binding mutant case')
        checks=rows[0].get('checks',{})
        changes=(rows[0].get('renamed',{}).get('result') or {}).get('documentChanges',[])
        require(checks.get('refusedWithoutEdits') is False and checks.get('cleanExit') is True and len(changes)==4, 'Missing ambiguous binding mutation evidence')
    elif kind=='shared-schemaquery-ablation':
        rows=companions['lsp/report.json'].get('rows',[])
        require([r.get('id') for r in rows]==['auto-enum-repair','3.5.6-enum-repair'], 'Missing shared query mutant cases')
        require(all(r.get('checks',{}).get('fourEnumActions') is False and r['checks'].get('sharedCompletionCandidates') is False and r['checks'].get('constraintObserved') is True for r in rows), 'Missing shared query mutation assertions')
    elif kind=='document-budget-ablation':
        validate(companions['baseline/report.json'],contracts('linux/amd64')['projection-aggregate'],raw.get('baselineBinarySHA256'))
        mutant=companions['mutant/report.json'];rows=mutant.get('rows',[])
        expected=[r['identity']['id'] for r in contracts('linux/amd64')['projection-aggregate']['rows']]
        require([r.get('id') for r in rows]==expected, 'Missing document budget mutant cases')
        lifecycle=next(r for r in rows if r['id']=='False-publication-lifecycle')
        require(lifecycle.get('checks',{}).get('noFalseBudgetInAnyPhase') is False and lifecycle['checks'].get('exit') is True, 'Missing document budget failed assertion')
        require(any(e.get('message',{}).get('params',{}).get('x-argocd-analysis-scope')=='document' for e in lifecycle.get('observed',{}).get('unexpectedBudget',[])), 'Missing original false document budget publication')
