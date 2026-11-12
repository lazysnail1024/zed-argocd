#!/usr/bin/env python3
import os
import pathlib,json,subprocess,sys,hashlib
root=pathlib.Path(__file__).resolve().parents[1];binary=pathlib.Path(sys.argv[1]).resolve();out=pathlib.Path(os.environ.get('CD_AUDIT_OUTPUT',str(root/'artifacts/domain')));out.mkdir(parents=True,exist_ok=True)
base={'apiVersion':'argoproj.io/v1alpha1','kind':'Application','metadata':{'name':'app','namespace':'tenant'},'spec':{'project':'team','source':{'repoURL':'https://example.org/repo'},'destination':{'server':'https://kubernetes.default.svc','namespace':'workload'}}}
project={'apiVersion':'argoproj.io/v1alpha1','kind':'AppProject','metadata':{'name':'team','namespace':'argocd'},'spec':{'sourceRepos':['*'],'destinations':[{'server':'*','namespace':'*'}]}}
cases=[]
def case(name,change,expected,absent=[],proj=project,extra=''):
 doc=json.loads(json.dumps(base));change(doc);cases.append((name,doc,proj,extra,expected,absent))
case('resolved',lambda d:None,[],['CD_PROJECT_UNRESOLVED'])
case('wrong-control-plane',lambda d:None,[('CD_PROJECT_UNRESOLVED',3)],proj={**project,'metadata':{'name':'team','namespace':'workload'}})
case('closed-missing',lambda d:None,[('CD_PROJECT_UNRESOLVED',1)],proj=None,extra='referencePolicy: closed\n')
case('default-not-synthesized',lambda d:d['spec'].update(project='default'),[('CD_PROJECT_UNRESOLVED',3)],proj=None)
case('destination-conflict',lambda d:d['spec']['destination'].update(name='cluster'),[('CD_DESTINATION_CONFLICT',1)])
case('source-ignored',lambda d:d['spec'].update(sources=[{'repoURL':'https://example.org/repo'}]),[('CD_SOURCE_IGNORED',2)])
case('duplicate-ref',lambda d:d['spec'].update(sources=[{'repoURL':'a','ref':'values'},{'repoURL':'b','ref':'values'}]),[('CD_SOURCE_REF_DUPLICATE',1)])
case('missing-values-ref',lambda d:d['spec'].update(sources=[{'repoURL':'a','helm':{'valueFiles':['$missing/values.yaml']}}]),[('CD_VALUES_REF_MISSING',1)])
case('ref-chart',lambda d:d['spec'].update(sources=[{'repoURL':'a','chart':'app','ref':'values'},{'repoURL':'b','helm':{'valueFiles':['$values/values.yaml']}}]),[('CD_VALUES_REF_INVALID',1)])
case('source-options',lambda d:d['spec']['source'].update(helm={},kustomize={}),[('CD_SOURCE_OPTIONS_CONFLICT',1)])
case('policy-repo',lambda d:None,[('CD_PROJECT_POLICY_CONTRADICTION',2)],proj={**project,'spec':{'sourceRepos':['https://elsewhere/repo'],'destinations':[{'server':'*','namespace':'*'}]}})
case('closed-incomplete',lambda d:None,[],['CD_PROJECT_UNRESOLVED'],proj=None,extra='referencePolicy: closed\n')
case('policy-destination',lambda d:None,[('CD_PROJECT_POLICY_CONTRADICTION',2)],proj={**project,'spec':{'sourceRepos':['*'],'destinations':[{'server':'https://elsewhere','namespace':'*'}]}})
case('mapped-path-missing',lambda d:d['spec']['source'].update(targetRevision='HEAD',path='charts/missing'),[('CD_LOCAL_PATH_MISSING',2)],extra='repositories: [{repoURL: https://example.org/repo, targetRevision: HEAD, path: repo}]\n')
case('unmapped-path-not-asserted',lambda d:d['spec']['source'].update(path='missing'),[],['CD_LOCAL_PATH_MISSING'])
case('closed-parser-budget',lambda d:None,[],['CD_PROJECT_UNRESOLVED'],proj=None,extra='referencePolicy: closed\n')
rows=[]
for name,doc,proj,extra,expected,absent in cases:
 folder=out/name;folder.mkdir(exist_ok=True);(folder/'.argocd-ls.yaml').write_text('schemaVersion: 1\ntargetVersion: "3.0.0"\ncontrolPlaneNamespace: argocd\n'+extra);(folder/'app.yaml').write_text(json.dumps(doc));pfile=folder/'project.yaml'
 if proj is not None:pfile.write_text(json.dumps(proj))
 elif pfile.exists():pfile.unlink()
 if name=='mapped-path-missing':(folder/'repo').mkdir(exist_ok=True)
 if name=='closed-parser-budget':(folder/'deep.yaml').write_text('apiVersion: argoproj.io/v1alpha1\nkind: AppProject\n'+''.join('  '*i+'x:\n' for i in range(300)))
 if name=='closed-incomplete':(folder/'broken.yaml').write_text('apiVersion: argoproj.io/v1alpha1\nkind: AppProject\nmetadata: [broken\n')
 cmd=[str(binary),'check','--root',str(folder),'--format','json'];p=subprocess.run(cmd,capture_output=True,timeout=15);(folder/'stdout').write_bytes(p.stdout);(folder/'stderr').write_bytes(p.stderr);errors=[]
 try:
  report=json.loads(p.stdout);d=report['diagnostics'];incomplete=name in ('closed-incomplete','closed-parser-budget')
  wanted_exit=2 if incomplete else (1 if any(x['severity']==1 for x in d) else 0)
  if p.returncode!=wanted_exit or report['analysisComplete'] is not (not incomplete):errors.append('completion/exit contract mismatch')
  allowed_errors={code for code,severity in expected if severity==1}
  if {x['code'] for x in d if x['severity']==1}!=allowed_errors:errors.append('unexpected Error diagnostic')
  if not incomplete and report['configurationErrors']:errors.append('unexpected configuration error')
  for code,severity in expected:
   if not any(x['code']==code and x['severity']==severity for x in d):errors.append('missing '+code)
  for code in absent:
   if any(x['code']==code for x in d):errors.append('unexpected '+code)
 except Exception as e:errors.append(str(e))
 rows.append({'id':name,'command':cmd,'expected':expected,'absent':absent,'exit':p.returncode,'errors':errors,'passed':not errors})
(out/'results.json').write_text(json.dumps(rows,indent=2)+'\n');(out/'manifest.json').write_text(json.dumps({'command':sys.argv,'binarySHA256':hashlib.sha256(binary.read_bytes()).hexdigest(),'testSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'inputSHA256':{str(p.relative_to(out)):hashlib.sha256(p.read_bytes()).hexdigest() for p in out.rglob('*.yaml')}},indent=2)+'\n');print(json.dumps(rows));sys.exit(0 if all(r['passed'] for r in rows) else 1)
