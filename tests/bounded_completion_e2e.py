#!/usr/bin/env python3
"""Completion latency with independently known Project candidates."""
import hashlib,importlib.util,json,os,pathlib,sys,time
binary=pathlib.Path(sys.argv[1]).resolve();out=pathlib.Path(sys.argv[2]).resolve();out.mkdir(parents=True,exist_ok=True);os.environ['CD_AUDIT_BINARY']=str(binary);spec=importlib.util.spec_from_file_location('client',pathlib.Path(__file__).with_name('audit_repair_e2e.py'));e=importlib.util.module_from_spec(spec);spec.loader.exec_module(e);c=None;rows=[]
cgroup=pathlib.Path('/proc/self/cgroup').read_text();base=pathlib.Path('/sys/fs/cgroup')/cgroup.strip().split('::',1)[1].lstrip('/');limits={'cgroup':cgroup,'affinity':sorted(os.sched_getaffinity(0))}
for name in ['memory.max','cpu.max']:limits[name]=(base/name).read_text().strip()
try:
 roots=[]
 for version in ['3.0.0','3.5.6']:
  root=out/version;root.mkdir(exist_ok=True);roots.append(root);e.write(root,'.argocd-ls.yaml',e.CONFIG.replace('3.0.0',version));e.write(root,'project.yaml',e.PROJECT)
  for i in range(98):e.write(root,f'app-{i}.yaml',e.APP.replace('name: app','name: app-'+str(i)))
 text=e.APP.replace('project: team','project: te')+'#'+'x'*199000+'\n';file=e.write(roots[0],'active.yaml',text);e.write(roots[1],'active.yaml',e.APP);c=e.LSP(out,roots);c.open(file,text,1);c.diag(file,1)
 for sample in range(5):
  elapsed=[];valid=True
  for i in range(200):
   start=time.monotonic();r=c.request('textDocument/completion',{'textDocument':{'uri':file.as_uri()},'position':{'line':5,'character':13}});elapsed.append(time.monotonic()-start);valid=valid and [x['label'] for x in r.get('result',[])]==['team'] and 'error' not in r
  rows.append({'name':f'sample-{sample}','seconds':elapsed,'p95Seconds':sorted(elapsed)[189],'expectedLabels':['team'],'passed':valid and sorted(elapsed)[189]<=.1})
except Exception as exc:rows.append({'name':'runner','error':repr(exc),'passed':False})
finally:
 if c:c.close()
 report={'binarySHA256':hashlib.sha256(binary.read_bytes()).hexdigest(),'runnerSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'effectiveConstraints':limits,'rows':rows,'passed':len(rows)==5 and all(x['passed'] for x in rows)};(out/'report.json').write_text(json.dumps(report,indent=2));print(json.dumps({'passed':report['passed'],'p95':[x.get('p95Seconds') for x in rows]}));sys.exit(0 if report['passed'] else 1)
