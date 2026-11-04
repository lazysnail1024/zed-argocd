#!/usr/bin/env python3
"""Static override controls across each pinned target, using real CLI processes."""
import argparse,json,pathlib,subprocess,sys
import audit_repair_e2e as w
p=argparse.ArgumentParser();p.add_argument('binary',type=pathlib.Path);p.add_argument('output',type=pathlib.Path);p.add_argument('--all-targets',action='store_true');a=p.parse_args();binary=a.binary.resolve();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);catalog=json.loads((pathlib.Path(__file__).resolve().parents[1]/'compat/catalog.lock.json').read_text());versions=[v['version'] for v in catalog['versions']] if a.all_targets else ['3.0.0','3.5.6'];rows=[]
base={'apiVersion':'argoproj.io/v1alpha1','kind':'ApplicationSet','metadata':{'name':'set'},'spec':{'goTemplate':True,'generators':[{'list':{'elements':[]}}],'template':{'metadata':{'name':'child'},'spec':{'project':'team','source':{'repoURL':'x'},'destination':{'server':'https://kubernetes.default.svc','namespace':'dev'}}}}}
project=w.PROJECT.replace('namespace: "*"','namespace: prod')
for version in versions:
 for name,namespace,patch,generator,expected in [('patch-removes-policy','dev','spec:\n  destination:\n    namespace: prod\n',None,False),('patch-adds-policy','prod','spec:\n  destination:\n    namespace: dev\n',None,True),('generator-removes-policy','dev',None,'prod',False),('generator-adds-policy','prod',None,'dev',True),('patch-project-protected','prod','spec:\n  project: missing\n',None,False),('dynamic-patch-unknown','dev','{{ .patch }}',None,False),('generator-empty-list-inherits','prod',None,'empty',True),('generator-empty-map-inherits','prod',None,'empty-map',True),('generator-empty-scalar-inherits','prod',None,'empty-scalar',True),('patch-empty-list-clears','prod','spec:\n  sources: []\n',None,False),('patch-null-list-deletes','prod','spec:\n  sources: null\n',None,False)]:
  d=out/version/name;r=d/'workspace';r.mkdir(parents=True);w.write(r,'.argocd-ls.yaml',w.CONFIG.replace('3.0.0',version));w.write(r,'project.yaml',project);v=json.loads(json.dumps(base));v['spec']['template']['spec']['destination']['namespace']=namespace
  if patch is not None:v['spec']['templatePatch']=patch
  if generator:v['spec']['generators'][0]['list']['template']={'metadata':{},'spec':{'project':'team','destination':{'namespace':generator}}}
  if generator=='empty':
   v['spec']['template']['spec'].pop('source');v['spec']['template']['spec']['sources']=[{'repoURL':'forbidden'}];v['spec']['generators'][0]['list']['template']['spec']={'project':'','destination':{},'sources':[]};w.write(r,'project.yaml',project.replace('sourceRepos: ["*"]','sourceRepos: ["allowed"]'))
  if generator in ['empty-map','empty-scalar']:
   v['spec']['template']['spec']['source']={'repoURL':'forbidden'};v['spec']['generators'][0]['list']['template']['spec']={'project':'','destination':{},'source':{} if generator=='empty-map' else {'repoURL':''}};w.write(r,'project.yaml',project.replace('sourceRepos: ["*"]','sourceRepos: ["allowed"]'))
  if name in ['patch-empty-list-clears','patch-null-list-deletes']:
   v['spec']['template']['spec'].pop('source');v['spec']['template']['spec']['sources']=[{'repoURL':'forbidden'}];w.write(r,'project.yaml',project.replace('sourceRepos: ["*"]','sourceRepos: ["allowed"]'))
  w.write(r,'app.yaml',json.dumps(v,indent=2));cmd=[str(binary),'check','--root',str(r),'--format','json'];q=subprocess.run(cmd,capture_output=True,timeout=15);(d/'stdout').write_bytes(q.stdout);(d/'stderr').write_bytes(q.stderr);error=None
  try:
   report=json.loads(q.stdout);ds=report['diagnostics'];conflicts=[x for x in ds if 'CD_PROJECT_POLICY_CONTRADICTION' in x['message']];assert bool(conflicts)==expected,(name,ds);assert q.returncode==(1 if name=='generator-empty-map-inherits' else 0) and report['analysisComplete'] and report['resourcesAnalyzed']==2
   if name=='generator-empty-map-inherits':assert [x['code'] for x in ds if x['severity']==1]==['SCHEMA_REQUIRED']
   else:assert not any(x['severity']==1 for x in ds)
   if name=='dynamic-patch-unknown':assert any(x['code']=='CD_APPSET_DYNAMIC_FIELD' for x in ds) and report['notEvaluated']
  except Exception as e:error=repr(e)
  rows.append(dict(version=version,id=name,command=cmd,exit=q.returncode,passed=error is None,error=error))
(out/'report.json').write_text(json.dumps({'binarySHA256':w.hash_file(binary),'runnerSHA256':w.hash_file(pathlib.Path(__file__)),'rows':rows,'inputAndArtifactSHA256':{str(f.relative_to(out)):w.hash_file(f) for f in out.rglob('*') if f.is_file()}},indent=2));print(json.dumps({'count':len(rows),'failed':[x for x in rows if not x['passed']]}));sys.exit(not rows or not all(x['passed'] for x in rows))
