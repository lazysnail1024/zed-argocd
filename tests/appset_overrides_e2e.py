#!/usr/bin/env python3
"""Static overrides across distinct schema/rule combinations, using real CLI processes."""
import argparse,json,pathlib,subprocess,sys
import audit_repair_e2e as w
p=argparse.ArgumentParser();p.add_argument('binary',type=pathlib.Path);p.add_argument('output',type=pathlib.Path);p.add_argument('--all-targets',action='store_true',help='Cover every eligible target through its distinct ApplicationSet schema and local-rule group');a=p.parse_args();binary=a.binary.resolve();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);catalog=json.loads((pathlib.Path(__file__).resolve().parents[1]/'compat/catalog.lock.json').read_text());versions=[];groups={}
for target in catalog['versions']:
 entry=next((s for s in target['schemas'] if s['kind']=='ApplicationSet'),None)
 if entry is None:continue
 data=json.loads((pathlib.Path(__file__).resolve().parents[1]/entry['path']).read_text())
 # This oracle exercises templatePatch and goTemplate, which were introduced
 # after ApplicationSet's initial releases. Missing features are covered by the
 # exact-release schema matrix, not treated as implemented controller behavior.
 if {'templatePatch','goTemplate'} <= set(data['properties']['spec']['properties']):
  versions.append(target['version'])
  # The complete exact-target matrix independently checks every tag. These deep
  # fixtures need one execution per identical schema and local-rule contract.
  key=(entry['sha256'],json.dumps(target['localRules'],sort_keys=True))
  group=groups.setdefault(key,{'representative':target['version'],'schemaSHA256':entry['sha256'],'localRules':target['localRules'],'versions':[]})
  group['versions'].append(target['version'])
eligible_versions=list(versions)
if a.all_targets:
 versions=[group['representative'] for group in groups.values()]
 covered=[version for group in groups.values() for version in group['versions']]
 assert len(covered)==len(set(covered)) and set(covered)==set(eligible_versions)
 assert len(versions)==len(groups) and set(versions)<=set(eligible_versions)
else:versions=[v for v in ['3.0.0',catalog['latestVersion']] if v in versions]
versions.append('auto');rows=[]
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
(out/'report.json').write_text(json.dumps({'binarySHA256':w.hash_file(binary),'runnerSHA256':w.hash_file(pathlib.Path(__file__)),'selection':{'mode':'schema-and-local-rules' if a.all_targets else 'smoke','eligibleVersions':eligible_versions,'testedVersions':versions,'groups':list(groups.values()) if a.all_targets else []},'rows':rows,'inputAndArtifactSHA256':{str(f.relative_to(out)):w.hash_file(f) for f in out.rglob('*') if f.is_file()}},indent=2));print(json.dumps({'count':len(rows),'failed':[x for x in rows if not x['passed']]}));sys.exit(not rows or not all(x['passed'] for x in rows))
