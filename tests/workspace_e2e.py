#!/usr/bin/env python3
"""Real CLI workspace/scopes scenarios, independent expected targets and diagnostics."""
import pathlib,json,subprocess,sys,hashlib,re
root=pathlib.Path(__file__).resolve().parents[1];binary=pathlib.Path(sys.argv[1]).resolve();out=root/'artifacts/workspace';out.mkdir(parents=True,exist_ok=True)
app='apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata:\n  name: sample\nspec:\n  project: 42\n'
config='schemaVersion: 1\ntargetVersion: "3.0.0"\nscopes:\n  - name: dev\n    include: ["dev/**/*.yaml"]\n    targetVersion: "3.5.6"\n  - name: prod\n    include: ["prod/**/*.yaml"]\n'
cases=[('scope',config,['--scope','dev'],['3.5.6'],['dev/app.yaml'],None),('override',config,['--scope','dev','--target-version','3.0.0'],['3.0.0'],['dev/app.yaml'],None),('file-scope-mismatch',config,['--scope','prod','--file','dev/app.yaml'],[],[],'FILE_SCOPE_MISMATCH'),('unknown-field',config+'mystery: true\n',[],[],[],'CONFIG_INVALID'),('overlap',config.replace('prod/**/*.yaml','dev/**/*.yaml'),[],[],[],'SCOPE_OVERLAP'),('unknown-scope',config,['--scope','missing'],[],[],'SCOPE_UNKNOWN'),('excluded',config+'exclude: ["dev/**"]\n',['--scope','dev'],['3.5.6'],[],None),('invalid-glob',config.replace('dev/**/*.yaml','dev/['),[],[],[],'CONFIG_INVALID')]
rows=[]
for name,cfg,args,versions,paths,error in cases:
 folder=out/name;folder.mkdir(exist_ok=True);(folder/'.argocd-ls.yaml').write_text(cfg)
 for f in ['dev/app.yaml','prod/app.yaml']:
  p=folder/f;p.parent.mkdir(exist_ok=True);p.write_text(app)
 cmd=[str(binary),'check','--root',str(folder),'--format','json',*args];p=subprocess.run(cmd,capture_output=True,timeout=15);(folder/'stdout').write_bytes(p.stdout);(folder/'stderr').write_bytes(p.stderr);errors=[]
 try:
  r=json.loads(p.stdout)
  if p.returncode!=2 or r['analysisComplete']:errors.append('domain stage remains incomplete')
  if error:
   if not any(x['code']==error for x in r['configurationErrors']):errors.append('missing '+error)
  else:
   assert all(re.fullmatch('[0-9a-f]{64}',x['schemaHash']) and x['rulesetId'] and x['complete']==False for x in r['targets'])
   assert all(x['targetId'] in {t['id'] for t in r['targets']} for x in r['diagnostics'])
   actual=sorted(set(x['version'] for x in r['targets']))
   if actual!=versions:errors.append('target mismatch '+str(actual))
   actual=sorted(set(pathlib.Path(x['uri'].removeprefix('file://')).relative_to(folder).as_posix() for x in r['diagnostics'] if x['code']=='SCHEMA_TYPE'))
   if actual!=paths:errors.append('diagnostic file mismatch '+str(actual))
 except Exception as e:errors.append(str(e))
 rows.append({'id':name,'command':cmd,'expected':{'versions':versions,'paths':paths,'error':error},'exit':p.returncode,'errors':errors,'passed':not errors})
cmd=[str(binary),'explain-config','--root',str(out/'scope')];p=subprocess.run(cmd,capture_output=True,timeout=15);(out/'explain.stdout').write_bytes(p.stdout);(out/'explain.stderr').write_bytes(p.stderr)
try:
 explained=json.loads(p.stdout);passed=p.returncode==0 and explained['config']['referencePolicy']=='open' and {x['scope']:x['version'] for x in explained['targets']}=={'dev':'3.5.6','prod':'3.0.0','unassigned':'3.0.0'} and explained['analysisReady']==False
except Exception:passed=False
rows.append({'id':'explain-effective-config','command':cmd,'exit':p.returncode,'passed':passed})
(out/'results.json').write_text(json.dumps(rows,indent=2)+'\n');(out/'manifest.json').write_text(json.dumps({'command':sys.argv,'binarySHA256':hashlib.sha256(binary.read_bytes()).hexdigest(),'testSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'inputSHA256':{str(p.relative_to(out)):hashlib.sha256(p.read_bytes()).hexdigest() for p in out.rglob('*') if p.is_file() and p.suffix=='.yaml'}},indent=2)+'\n');print(json.dumps(rows));sys.exit(0 if all(r['passed'] for r in rows) else 1)
