"""Trusted case and assertion inventories for Linux semantic consumers.

These expectations describe the checked-in scenarios, never an uploaded report.
Mutation payloads have separate failure semantics and are not recursively treated
as ordinary all-true assertions.
"""
import json
import copy
from pathlib import Path
from child_evidence import validate, require

ROOT = Path(__file__).resolve().parents[1]


def row(ident, checks=None, key='id', assertion='checks', **required):
    value = {'identity': {key: ident}, 'passKey': 'passed'}
    if checks is not None:
        value[assertion] = {'keys': sorted(checks.split())}
    if required:
        value['required'] = required
    return value


def spec(rows, rows_key='rows', passed=True):
    value = {'rowsKey': rows_key, 'rootRow': rows_key is None, 'rows': rows}
    if passed:
        value['passKey'] = 'passed'
    return value


def query_contract(filename):
    cases = json.loads((ROOT / 'tests' / filename).read_text())
    rows = []
    for target in ('3.0.0', '3.5.6'):
        for snippets in (False, True):
            for case in cases:
                checks = ['terminalResponse']
                if 'expectedDiagnostics' in case:
                    checks += ['exactDiagnostics', 'analysisComplete']
                if case.get('method') == 'hover':
                    checks += ['hover', 'noInventedType']
                else:
                    checks += ['required', 'forbidden', 'plainText']
                    for field, check in [('exactLabels','exactLabels'), ('candidateLabels','branchCandidates'), ('diagnosticCodes','validationKept')]:
                        if field in case:
                            checks.append(check)
                    if 'apply' in case:
                        checks += ['explicitEdit','exactBytes','noDuplicateProjection']
                        if 'rangeText' in case:
                            checks.append('exactRange')
                if case.get('empty'):
                    checks.append('empty')
                entry = row(case['id'], ' '.join(checks))
                entry['identity'].update(target=target, snippets=snippets)
                rows.append(entry)
            entry = row('shutdown', exitCode=0)
            entry['identity'].update(target=target, snippets=snippets)
            rows.append(entry)
    return spec(rows)


def contracts():
    result = {}
    result['catalog-integrity'] = spec([dict(row('official-inventory', ready=True),empty=['errors'])], None)
    result['alias-provenance'] = spec([row(version+'-'+name, checks) for version in ('3.0.0','3.5.6') for name,checks in [
        ('crlf-sibling','available version exactBytes clean'), ('derived-declaration','resolvedValue noInferredDeclaration noInferredSymbol'), ('shutdown','exit')]])
    result['config-input'] = spec([row('cli-'+name, 'terminal status counts complete', key='name', assertion='assertions') for name in ('fifo','directory','oversized','normal')] + [row('lsp-'+name,key='name') for name in ('fifo','oversized')])
    result['candidate-values'] = spec([row(name) for name in '''project-quoted-crlf-prefix-apply yaml-boolean-name-apply empty-project-no-separator empty-project-comment empty-project-string-and-whitespace-controls source-ref-crlf-document-local-apply duplicate-source-ref-same-line-origins unknown-namespace-candidate-definitions scope-open-exclusion-damaged-declaration recovery-query-only-and-repair appset-static-leaf-apply-with-dynamic-siblings unsafe-query-positions-refused source-ref-declaration-string-type complete-neighbor-of-damaged-document'''.split()], 'cases', False)
    result['parser-depth'] = spec([row('deep-budget-and-recovery')], 'cases', False)
    result['scope-globs'] = spec([row(name) for name in 'literal near-miss star question class recursive single'.split()])
    result['scope-refresh'] = spec([row(name,checks) for name, checks in [
        ('initial-first','noErrors'),('initial-second','noErrors'),('dependent-and-unrelated','missingProject removedDefinition otherDefinition otherTarget'),('close-restores-disk','definition'),('config-target-refresh','currentTarget'),('watched-delete','missing'),('watched-create','restored'),('deepest-root-added','target'),('root-removed-parent-restored','target'),('local-path-missing','warning'),('local-path-watch-refresh','clear'),('consistency-rescan','warning')]]+[row('shutdown',exitCode=0)])
    for name, fixture in [('schema-repair-queries','schema-query-repair-cases.json'),('schema-boundary-queries','schema-query-boundary-cases.json')]:
        result[name] = query_contract(fixture)
    result['schema-comment-replay'] = spec([dict(row(name,'exit resources files complete configuration diagnostics'),identity={'id':name,'target':target}) for target in ('3.0.0','3.5.6') for name in ('before','applied','expected')])
    for name in ('schema-fixture','schema-repair-fixture','schema-boundary-fixture'):
        names=['build','synthetic-lsp'] + ([] if name=='schema-fixture' else ['synthetic-value-truth-table'])
        result[name] = spec([row(name,exitCode=0) for name in names])
    domain = json.loads((ROOT/'tests/domain-matrix-cases.json').read_text())['cases']
    result['domain-boundaries'] = spec([dict(row(c['id']),identity={'id':c['id'],'version':version},empty=['errors']) for version in ('3.0.0','3.4.12','3.5.6') for c in sorted(domain,key=lambda c:c['id'])], 'results')
    versions='3.0.0 3.0.23 3.1.0 3.1.16 3.2.0 3.2.12 3.3.0 3.3.18 3.4.1 3.4.12 3.5.0 3.5.6 3.4.12'.split()
    names='sole-source-ref-version invalid-ref-dot file-parameter-ref-missing appset-dynamic-project-static-conflict appset-dynamic-ref-static-conflict appset-static-patch-malformed single-source-ignored-ref-grammar sole-sources-unused-ref-before-boundary multi-sources-invalid-ref-control fast-mode-static-malformed-patch fast-mode-dynamic-malformed-patch fast-mode-static-valid-patch-control fast-patch-unclosed-literal-invalid-yaml resolved'.split()
    result['domain-transitions'] = spec([dict(row(name),identity={'id':name,'version':version,'revision':2+i*len(names)+j}) for i,version in enumerate(versions) for j,name in enumerate(names)],'cases')
    result['scope-isolation'] = spec([row(name) for name in ('scope-isolation','scope-incomplete')],'cases')
    result['snippets'] = spec([{'identity':{},'passKey':'passed','required':{'exit':0}}],None)
    for name in ('render-defaults-helm3','render-defaults-helm4'):
        result[name] = spec([row('defaults-and-explicit-action')],'cases',False)
    result['render-selected-scope'] = spec([row('selected-file-determines-render-target')],'cases',False)
    result['completed-work'] = spec([row(name,'exit files resources codes complete',key='name',assertion='assertions') for name in ('errors-count','projection-skipped','mixed-completion')]+[row('deadline','exit3 deadlineReason noReadinessSuccess notAllCounted incomplete',key='name',assertion='assertions')])
    result['renderer-input'] = spec([row(name,'exit2 incomplete explicitReason toolExit noGenerated '+('rawStdout rawStderr exactHash' if name.endswith('failure') else 'noStdout noStderr'),key='name',assertion='assertions') for name in ('fifo','directory','missing','regular-failure','symlink-failure')])
    result['renderer-process-tree'] = spec([row(name,'terminal treeReclaimed incomplete noGenerated rawStdout rawStderr explicitError promptBound outputBound nonzeroStatus',key='name',assertion='assertions') for name in ('hanging-tree','parent-exit','version-tree','output-tree','nonzero','signal-tree','mixed-output-tree')])
    result['renderer-acquisition'] = spec([row(name,'exit rendererNotStarted noGenerated'+(' incomplete reason rendererAbsent zeroCounts rawReason' if name!='invalid-config' else ''),key='name',assertion='assertions') for name in ('interrupt','deadline','invalid-config')])
    result['configuration-terminal'] = spec([row(name,'exit3 noSuccess reason') for name in ('interrupt','deadline')])
    result['real-helm'] = spec([{'identity':{},'passKey':'passed','empty':['errors'],'required':{'exit':1}}],None)
    result['render-parity'] = spec([row(name,'exit complete codes input renderer target counts '+('reason' if name=='renderer-cycle' else 'sameAnalysisReport renderHumanExit renderHumanDiagnostics renderHumanConfiguration renderHumanDelivery checkHumanExit checkHumanDiagnostics checkHumanConfiguration checkHumanDelivery')) for name in ('clean','schema-error','warning','warning-strict','post-render-scalar-budget','renderer-cycle')])
    names=['help','help-short','version-human','version-json','explain']+[name+'-'+fmt for fmt in ('json','human') for name in ('check-clean','check-error','check-warning','check-warning-strict','render')]+['render-failure-json']
    rows=[]
    for name in names:
        for suffix in ('file','full'):
            checks='exit stderr stdout'
            if name.startswith('check-') and name.endswith('-json') and suffix=='file':
                checks+=' complete resources configuration'
                if name.startswith('check-warning'): checks+=' warningSeverity'
            rows.append(row(name+'-'+suffix,checks))
    result['output-failures']=spec(rows+[row(name,'exit3 writeReason') for name in ('deadline-json','deadline-human')])
    result.update(mutation_contracts())
    return result


def mutation_contracts():
    result={}
    result['config-helm-guards']=spec([dict(row(name,key='variant'),passKey='detected',required={'control.exit':0,'build.exit':0,'run.exit':1}) for name in ('unbound-definition-candidates','dynamic-key-proof','saved-buffer-authority')],'cases',False)
    rows=[row('classifier-'+name,expectedDetection=(name=='named-assertion')) for name in ('named-assertion','unrelated-failure','timeout','missing-positive-control','missing-evidence','boolean-exit','all-passed')]
    for script in ('appset_e2e.py','domain_e2e.py'):
        for name in ('baseline','wrong-exit','unexpected-error','drop-diagnostics','false-incomplete'):
            rows.append(row(script+'-'+name,'exit allCasesExecuted structuredAssertions outcome noHarnessCrash'+(' specificFailure' if name!='baseline' else '')))
    result['consumer-oracles']=spec(rows)
    result['named-guards']=spec([row(name) for name in 'schema-actual-document domain-control-plane-namespace appset-dynamic-reference-unknown workspace-explicit-target-precedence lsp-stale-notification fix-new-error-rejection candidates-damaged-prefix-recovery candidates-literal-encoding candidates-source-prefix-span pin-client-version pin-stale-revision pin-review-post-simulation-completeness pin-review-root-identity'.split()])
    result['schema-constraints']=spec([row(name,'compiled exactTestRan normalCompletion namedConstraint noPanic') for name in ('baseline','type','required','enum')])
    for name,ids in [('query-guards',('without-recovery','without-leaf-validation')),('query-boundaries',('comment-separator','exclusive-cardinality'))]:
        result[name]=spec([row(ident,controlPassed=True,buildExit=0,testExit=1) for ident in ids])
    result['lifecycle-facts']=spec([dict(row(name),passKey='caughtExpectedRegression',required={'run.exit':1}) for name in ('missing-watch-observation','empty-facts-recommit')])
    result['workspace-guards']=spec([dict(row(name,key='variant'),passKey='detected',required={'build.exit':0,'oracle.exit':1}) for name in ('cancel','root-owner','buffer-first','excluded-isolation','array-fields','fix-bytes')],'cases')
    return result


def validate_consumer(name, value, folder=None):
    definitions=contracts()
    require(name in definitions, 'Unknown orthogonal consumer')
    require(isinstance(value,dict), 'Orthogonal report must be an object')
    for key in ('error','runnerError','exception'):
        require(not value.get(key), 'Orthogonal report contains a harness error')
    definition=definitions[name]
    validate(value,definition,verify_identity=False)
    rows=[value] if definition['rootRow'] else value[definition['rowsKey']]
    for item in rows:
        require(not any(item.get(key) for key in ('error','runnerError','exception')), 'Orthogonal case contains a harness error')
    validate_observations(name, rows)
    if name=='named-guards':
        from legacy_mutations import VARIANTS, detected
        for item,variant in zip(rows,VARIANTS):
            control=value['controls'][variant[5]]
            require(type(control['exit']) is int and control['exit']==0 and all(r['passed'] is True for r in control['cases']), 'Missing positive mutation control')
            require(detected(variant[5],control,item['oracle'],variant[6]), 'Missing named mutation failure')
    if name in ('query-guards','query-boundaries'):
        wanted={'without-recovery':{'blank-mapping','empty-mapping','incomplete-key','array-existing'},'without-leaf-validation':{'conjunction-inside-alternative','conjunction-with-disjunction-membership'},'comment-separator':{'empty-before-comment'},'exclusive-cardinality':{'exclusiveChoice','duplicateExclusive','referencedExclusive'}}
        for item in rows:
            failed=item.get('failedCases')
            require(isinstance(failed,list) and all(type(x) is str for x in failed) and len(set(failed))==len(failed), 'Invalid named query failures')
            require(set(failed)==wanted[item['id']] if name=='query-boundaries' else wanted[item['id']]<=set(failed), 'Missing expected query mutation failures')
    if name=='config-helm-guards':
        wanted={'unbound-definition-candidates':'unbound-project-all-local-candidates-no-authority','dynamic-key-proof':'dynamic-key-refuses-projection','saved-buffer-authority':'defaults-and-explicit-action'}
        for item in rows:
            require(wanted[item['variant']] in item.get('failedCases',[]), 'Missing expected config/Helm failure')
    if name=='workspace-guards':
        validate_workspace_mutations(value)
        wanted={'cancel':'code-action-cancel-budget-60','root-owner':'nested-roots-parent-first','buffer-first':'buffer-overrides-budgeted-disk','excluded-isolation':'opened-excluded-project','array-fields':'sequence-existing-key-completion','fix-bytes':'versioned-fix-and-stale-rejection'}
        for item in rows:
            actual=item.get('oracle',{}).get('report',{}).get('cases',[])
            failed=[case['id'] for case in actual if case.get('passed') is False]
            require(failed==[wanted[item['variant']]], 'Missing named workspace failure')
    if name=='lifecycle-facts':
        validate_lifecycle_mutations(value, folder)
    if folder is not None and name in ('schema-fixture','schema-repair-fixture','schema-boundary-fixture'):
        fixture={'schema-fixture':'schema-query-fixture-cases.json','schema-repair-fixture':'schema-query-repair-fixture-cases.json','schema-boundary-fixture':'schema-query-boundary-fixture-cases.json'}[name]
        from resource_ci import load_json
        child=load_json((folder/'lsp/report.json').read_text())
        validate(child,query_contract(fixture),verify_identity=False)
        if name!='schema-fixture':
            allowed={'nestedConjunction':{'b','d'},'baseAndAlternatives':{'b'}} if name=='schema-repair-fixture' else {'exclusiveChoice':{'a','c'},'inclusiveChoice':{'a','b','c'},'duplicateExclusive':set(),'referencedExclusive':{'a','c'},'duplicateInclusive':{'a','b'}}
            definition=spec([row(target+'-'+field+'-'+value,'exit readiness resource schemaDecision configuration',expectedSchemaAccepted=value in accepted) for target in ('3.0.0','3.5.6') for field,accepted in allowed.items() for value in ('a','b','c','d')])
            definition['passKey']='validatorPassed'
            validate(load_json((folder/'values/report.json').read_text()),definition,verify_identity=False)
    if folder is not None and name in ('query-guards','query-boundaries'):
        from resource_ci import load_json
        for item in rows:
            fixture={'without-recovery':'fixtures/contracts/schema-query/cases.json','without-leaf-validation':'schema-query-repair-fixture-cases.json','comment-separator':'schema-query-boundary-cases.json','exclusive-cardinality':'schema-query-boundary-fixture-cases.json'}[item['id']]
            definition=query_contract(fixture)
            baseline=load_json((folder/item['id']/'control-lsp/report.json').read_text())
            validate(baseline,definition,verify_identity=False)
            mutant=load_json((folder/item['id']/'lsp/report.json').read_text())
            require(mutant.get('passed') is False, 'Mutant must fail its consumer')
            actual=mutant.get('rows')
            require(isinstance(actual,list) and len(actual)==len(definition['rows']), 'Mutant case inventory differs')
            for observed,expected in zip(actual,definition['rows']):
                require(type(observed.get('passed')) is bool, 'Mutant pass value is not boolean')
                if 'checks' not in expected:
                    require(observed['passed'] is True, 'Mutant shutdown failed')
                adjusted=copy.deepcopy(expected)
                adjusted.pop('passKey')
                checks=observed.get('checks')
                if 'checks' in adjusted:
                    if isinstance(checks,dict) and checks.get('explicitEdit') is False:
                        adjusted['checks']['keys']=[key for key in adjusted['checks']['keys'] if key not in ('exactBytes','exactRange','noDuplicateProjection')]
                    require(isinstance(checks,dict) and sorted(checks)==adjusted['checks']['keys'] and all(type(v) is bool for v in checks.values()), 'Mutant assertion inventory differs')
                    require(checks.get('terminalResponse') is True and observed.get('passed') is all(checks.values()), 'Mutant did not fail a behavior assertion')
                    adjusted['checks']={'literal':checks}
                validate(observed,{'rootRow':True,'rowsKey':None,'rows':[adjusted]},verify_identity=False)
            require(sorted({r['id'] for r in actual if r.get('passed') is False})==item['failedCases'], 'Named failures disagree with raw mutant report')
    return True


def object_keys(value, keys, label):
    require(isinstance(value, dict) and set(value) == set(keys.split()), label + ' observation inventory differs')


def clean_exit(value, expected=0):
    require(type(value) is int and value == expected, 'Consumer did not exit normally')


def validate_observations(name, rows):
    if name=='parser-depth':
        item=rows[0];clean_exit(item.get('exit'));observed=item.get('observed')
        object_keys(observed,'deepDiagnostics legalDiagnostics legalSymbols recovered','Parser depth')
        require(any(d.get('code') in ('INPUT_INCOMPLETE','ANALYSIS_INCOMPLETE','INPUT_BUDGET_EXCEEDED') for d in observed['deepDiagnostics']), 'Missing deep-input refusal')
        require(not any(d.get('code') in ('INPUT_INCOMPLETE','ANALYSIS_INCOMPLETE') for d in observed['legalDiagnostics']), 'Legal depth did not recover')
        require(any(s.get('name')=='legal' for s in observed['legalSymbols'].get('result',[])) and any(s.get('name')=='app' for s in observed['recovered'].get('result',[])), 'Missing parser recovery symbols')
    elif name=='scope-globs':
        expected=[['dev/app.yaml','dev/nested/app.yaml'],[],['dev/app.yaml','developer/app.yaml'],['dev/app.yaml','dev/nested/app.yaml'],['dev/app.yaml','developer/app.yaml','prod/app.yaml'],['dev/app.yaml','dev/nested/app.yaml','developer/app.yaml','prod/app.yaml'],['top.yaml']]
        for item,wanted in zip(rows,expected):
            require(item.get('actual')==wanted and item.get('expected')==wanted, 'Wrong observed scope membership')
            clean_exit(item.get('exit'),1 if wanted else 0)
    elif name=='domain-transitions':
        fixtures={c['id']:c for c in json.loads((ROOT/'tests/domain-matrix-cases.json').read_text())['cases']}
        for item in rows:
            fixture=fixtures[item['id']]
            wanted=fixture['expected35'] if item['version'] in ('3.5.0','3.5.6') and fixture['expected35'] is not None else fixture['expected']
            require(item.get('actual')==sorted(wanted) and item.get('expected')==wanted, 'Domain transition observations differ from fixture')
    elif name=='candidate-values':
        validate_candidate_observations(rows)


def validate_candidate_observations(rows):
    # Ordered response kinds and labels come from the independent candidate
    # scenarios, not from the report's own expected field.
    responses=[['team','team-blue'],['on'],['team'],['team'],[['team'],['true'],['true']],['values'],['values','values'],['team','team'],['team-local'],['team'],[['values'],['team'],[]],[[]]*8,[[],[],['true']],None]
    applied={0,1,2,3,4,5,7,9,10}
    methods={4:['completion']*3,5:['completion','definition'],7:['completion','definition'],9:['completion','definition','documentSymbol','definition'],10:['completion']*3,11:['completion']*8,12:['completion']*3,13:['definition','documentSymbol']}
    for index,item in enumerate(rows):
        observed=item.get('observed');keys='beforeDiagnostics responses'
        if index in applied: keys+=' actualApplied expectedApplied afterDiagnostics'
        if index==9: keys+=' fixes'
        if index==11: keys+=' forms'
        if index==12: keys+=' typedRefs'
        object_keys(observed,keys,'Candidate')
        exits=item.get('exits');require(isinstance(exits,list) and len(exits)==(3 if index==4 else 1), 'Missing candidate process exits')
        for code in exits:clean_exit(code)
        actual=observed['responses'];wanted_methods=methods.get(index,['completion'])
        require(isinstance(actual,list) and [r.get('method') for r in actual]==wanted_methods, 'Candidate response inventory differs')
        for response in actual:
            require(isinstance(response.get('response'),dict) and 'result' in response['response'] and 'error' not in response['response'], 'Candidate request did not succeed')
        wanted=responses[index]
        if wanted is not None:
            wanted_sets=wanted if index in (4,10,11,12) else [wanted]
            for response,labels in zip([r for r in actual if r['method']=='completion'],wanted_sets):
                require([i.get('label') for i in response['response']['result']]==labels, 'Candidate labels differ from scenario')
        if index in applied:
            require(type(observed['actualApplied']) is str and observed['actualApplied']==observed['expectedApplied'] and observed['actualApplied'], 'Candidate edit bytes differ')
        if index==11:
            require([r.get('id') for r in observed['forms']]==['foreign','current-line-damage','complete-duplicate','tag','escaped','dynamic','source-suffix','multiline'] and all(r.get('items')==[] for r in observed['forms']), 'Unsafe candidate positions were admitted')
        if index==12:
            require(observed['typedRefs']==[{'declaration':'true','expected':[],'actual':[]},{'declaration':'42','expected':[],'actual':[]},{'declaration':'"true"','expected':['true'],'actual':['true']}], 'Typed reference observations differ')
        if index==13:
            require(len(actual[0]['response']['result'])==1 and [s.get('name') for s in actual[1]['response']['result']]==['app'], 'Damaged neighbor changed reliable navigation')


def validate_lifecycle_mutations(value, folder):
    clean_exit(value.get('control',{}).get('exit'))
    require(folder is not None, 'Lifecycle mutation raw evidence is required')
    control=(folder/'control/stdout').read_text()+(folder/'control/stderr').read_text()
    require(control.split()[:2]==['ok','github.com/lazysnail1024/zed-argocd/internal/lsp'] and '[no tests to run]' not in control, 'Missing lifecycle positive test output')
    expected=[('TestWatchCreationBetweenEnumerationAndSweep','completed discovery lost the new event or kept the deleted file'),('TestIncrementalFactsOwnershipAndReuse','unchanged file did not reuse fresh facts')]
    command=value['control'].get('command',[])
    require('-count=1' in command and '-run' in command and command[-1]=='^('+'|'.join(item[0] for item in expected)+')$', 'Lifecycle positive command did not select both tests')
    for item,(test,failure) in zip(value['rows'],expected):
        clean_exit(item.get('run',{}).get('exit'),1)
        command=item['run'].get('command',[])
        require('-count=1' in command and '-run' in command and command[-1]=='^('+test+')$', 'Lifecycle command did not select its designated test')
        raw=(folder/item['id']/'stdout').read_text()+(folder/item['id']/'stderr').read_text()
        require('--- FAIL: '+test+' (' in raw and failure in raw and '[build failed]' not in raw and 'panic:' not in raw and 'fatal error:' not in raw, 'Missing designated lifecycle assertion failure')


def workspace_outcome(item, designated=None):
    ident=item['id'];observed=item.get('observed')
    require(isinstance(observed,dict) and observed, 'Missing workspace observation')
    if designated=='fix-bytes' and ident=='versioned-fix-and-stale-rejection':
        object_keys(observed,'exception exceptionType','Fix mutation')
        require(observed['exceptionType']=='AssertionError' and observed['exception']=="AssertionError('returned fix does not produce the expected Application')" and item.get('exit') is None, 'Missing designated returned-edit byte failure')
        return False
    clean_exit(item.get('exit'))
    if ident=='code-action-cancel-budget-60':
        from resource_ci import number
        object_keys(observed,'shutdownLatencySeconds timedoutAt8Seconds actionResponseCount actionLatencySeconds actionError assertions diagnosticCount shutdown','Cancellation')
        number(observed['shutdownLatencySeconds'],'shutdown latency');number(observed['actionLatencySeconds'],'cancellation latency')
        require(type(observed['actionResponseCount']) is int and observed['actionResponseCount']==1 and observed['timedoutAt8Seconds'] is False, 'Cancellation did not terminate normally')
        checks={'shutdownWithin5Seconds':observed['shutdownLatencySeconds']<=5,'cancellationObservedWithin200ms':isinstance(observed['actionError'],dict) and observed['actionError'].get('code')==-32800 and observed['actionLatencySeconds']<=.2}
        require(observed['assertions']==checks and all(type(v) is bool for v in observed['assertions'].values()), 'Cancellation assertions contradict observations')
        return all(checks.values())
    if ident=='opened-excluded-project':
        object_keys(observed,'before after definition','Excluded project')
        return any(d.get('code')=='CD_PROJECT_UNRESOLVED' for d in observed['after']) and observed['definition'].get('result')==[]
    if ident=='opened-helm-source':
        object_keys(observed,'diagnostics','Helm source')
        return any(d.get('code')=='SCHEMA_TYPE' for d in observed['diagnostics'])
    if ident=='buffer-overrides-budgeted-disk':
        object_keys(observed,'firstDiagnostic symbols','Buffer authority')
        return any(s.get('name')=='app' for s in observed['symbols'].get('result',[]))
    if ident.startswith('nested-roots-'):
        object_keys(observed,'diagnostics definition','Root ownership')
        return any(d.get('code')=='CD_PROJECT_UNRESOLVED' and d.get('severity')==1 for d in observed['diagnostics']) and observed['definition'].get('result')==[]
    if ident=='sequence-existing-key-completion':
        object_keys(observed,'before completion afterApplyingHelm','Sequence completion')
        return not any(i.get('label')=='helm' for i in observed['completion'].get('result',[]))
    require(ident=='versioned-fix-and-stale-rejection','Unknown workspace observation')
    object_keys(observed,'before resolved appliedText after stale','Versioned edit')
    changes=observed['resolved'].get('result',{}).get('edit',{}).get('documentChanges',[])
    require(len(changes)==1 and type(changes[0].get('textDocument',{}).get('version')) is int and changes[0]['textDocument']['version']==1 and changes[0].get('edits'), 'Missing versioned edit')
    from audit_repair_e2e import APP
    require(observed['appliedText']==APP, 'Versioned edit did not produce the fixture Application')
    return any(d.get('code')=='SCHEMA_UNKNOWN_FIELD' for d in observed['before']) and not any(d.get('code')=='SCHEMA_UNKNOWN_FIELD' or d.get('severity')==1 for d in observed['after']) and observed['stale'].get('error',{}).get('code')==-32801


def validate_workspace_mutations(value):
    clean_exit(value.get('controlBuild',{}).get('exit'))
    controls=value.get('controls');require(isinstance(controls,dict) and set(controls)=={'budget','lsp'}, 'Missing workspace positive controls')
    ids={'budget':['code-action-cancel-budget-60'],'lsp':['opened-excluded-project','opened-helm-source','buffer-overrides-budgeted-disk','nested-roots-parent-first','nested-roots-child-first','sequence-existing-key-completion','versioned-fix-and-stale-rejection']}
    for mode,control in controls.items():
        clean_exit(control.get('exit'));report=control.get('report',{});cases=report.get('cases',[])
        require(report.get('passed') is True and [r.get('id') for r in cases]==ids[mode], 'Workspace positive inventory differs')
        for item in cases:require(item.get('passed') is True and workspace_outcome(item), 'Workspace positive observation failed')
    wanted={'cancel':'code-action-cancel-budget-60','root-owner':'nested-roots-parent-first','buffer-first':'buffer-overrides-budgeted-disk','excluded-isolation':'opened-excluded-project','array-fields':'sequence-existing-key-completion','fix-bytes':'versioned-fix-and-stale-rejection'}
    for mutation in value['cases']:
        variant=mutation['variant'];mode='budget' if variant=='cancel' else 'lsp';report=mutation.get('oracle',{}).get('report',{});cases=report.get('cases',[])
        require(report.get('passed') is False and [r.get('id') for r in cases]==ids[mode], 'Workspace mutant inventory differs')
        for item in cases:
            expected=item['id']!=wanted[variant]
            require(item.get('passed') is expected and workspace_outcome(item,variant) is expected, 'Workspace mutation lacks its designated behavioral failure')
