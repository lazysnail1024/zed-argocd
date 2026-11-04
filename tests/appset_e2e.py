#!/usr/bin/env python3
import pathlib,json,subprocess,sys,os,hashlib
root=pathlib.Path(__file__).resolve().parents[1];binary=pathlib.Path(sys.argv[1]).resolve();out=pathlib.Path(os.environ.get('CD_AUDIT_OUTPUT',str(root/'artifacts/appset')));out.mkdir(parents=True,exist_ok=True)
base={'apiVersion':'argoproj.io/v1alpha1','kind':'ApplicationSet','metadata':{'name':'set'},'spec':{'goTemplate':True,'generators':[{'list':{'elements':[]}}],'template':{'metadata':{'name':'{{.name}}'},'spec':{'project':'{{.project}}','source':{'repoURL':'https://example.org/repo'},'destination':{'server':'https://kubernetes.default.svc','namespace':'default'}}}}}
cases=[]
def case(name,change,required,forbidden=[]):
 value=json.loads(json.dumps(base));change(value);cases.append((name,value,required,forbidden))
case('dynamic-project',lambda v:None,['CD_APPSET_DYNAMIC_FIELD'],['CD_PROJECT_UNRESOLVED','CD_APPSET_TEMPLATE_SYNTAX'])
case('static-conflict',lambda v:v['spec']['template']['spec']['destination'].update(name='cluster'),['CD_APPSET_STATIC_FIELD'])
case('boolean-stays-typed',lambda v:v['spec']['template']['spec'].update(syncPolicy={'automated':{'prune':'{{.prune}}'}}),['SCHEMA_TYPE'])
case('broken-go-template',lambda v:v['spec']['template']['metadata'].update(name='{{ if .name }}'),['CD_APPSET_TEMPLATE_SYNTAX'])
case('parse-functions-without-execution',lambda v:v['spec']['template']['metadata'].update(name='{{ .name | normalize }}'),[],['CD_APPSET_TEMPLATE_SYNTAX'])
case('dynamic-ref-propagates-unknown',lambda v:v['spec']['template']['spec'].update(sources=[{'repoURL':'a','ref':'{{.ref}}'},{'repoURL':'b','helm':{'valueFiles':['$values/values.yaml']}}]),['CD_APPSET_DYNAMIC_FIELD'],['CD_APPSET_STATIC_FIELD'])
rows=[]
for name,value,required,forbidden in cases:
 if name=='dynamic-ref-propagates-unknown':value['spec']['template']['spec'].pop('source',None)
 folder=out/name;folder.mkdir(exist_ok=True);(folder/'.argocd-ls.yaml').write_text('schemaVersion: 1\ntargetVersion: "3.0.0"\ncontrolPlaneNamespace: argocd\n');(folder/'input.yaml').write_text(json.dumps(value));cmd=[str(binary),'check','--root',str(folder),'--format','json'];p=subprocess.run(cmd,capture_output=True,timeout=15);(folder/'stdout').write_bytes(p.stdout);(folder/'stderr').write_bytes(p.stderr);errors=[]
 try:
  report=json.loads(p.stdout);ds=report['diagnostics'];codes=[d['code'] for d in ds]
  errors=[f'missing {c}' for c in required if c not in codes]+[f'unexpected {c}' for c in forbidden if c in codes]
  if p.returncode != (1 if name in ['static-conflict','boolean-stays-typed','broken-go-template'] else 0) or report.get('analysisComplete') is not True:errors.append('expected completed severity exit')
  allowed={'static-conflict':['CD_APPSET_STATIC_FIELD'],'boolean-stays-typed':['SCHEMA_TYPE'],'broken-go-template':['CD_APPSET_TEMPLATE_SYNTAX']}.get(name,[])
  if sum(d.get('severity')==1 for d in ds) != (1 if allowed else 0):errors.append('unexpected Error count')
  errors += [f"unexpected Error {d['code']}" for d in ds if d.get('severity')==1 and d['code'] not in allowed]
 except Exception as e:errors.append(str(e))
 rows.append({'id':name,'command':cmd,'expected':required,'forbidden':forbidden,'exit':p.returncode,'errors':errors,'passed':not errors})
(out/'results.json').write_text(json.dumps(rows,indent=2)+'\n');(out/'manifest.json').write_text(json.dumps({'command':sys.argv,'binarySHA256':hashlib.sha256(binary.read_bytes()).hexdigest(),'testSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'inputs':{str(p.relative_to(out)):hashlib.sha256(p.read_bytes()).hexdigest() for p in out.rglob('*.yaml')}},indent=2)+'\n');print(json.dumps(rows));sys.exit(0 if all(r['passed'] for r in rows) else 1)
