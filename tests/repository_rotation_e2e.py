#!/usr/bin/env python3
"""Real LSP retention observation while revisiting distinct configured scopes."""
import hashlib,json,os,pathlib,sys
import audit_repair_e2e as wire
wire.APP=wire.APP.replace('repoURL: https://example.org/repo','repoURL: https://example.org/repo, path: item-0000')
wire.BINARY=pathlib.Path(sys.argv[1]).resolve();out=pathlib.Path(sys.argv[2]).resolve();out.mkdir(parents=True,exist_ok=False);root=out/'workspace';root.mkdir();rows=[];client=None
cfg='schemaVersion: 1\ntargetVersion: "3.5.6"\ncontrolPlaneNamespace: argocd\nscopes:\n'
for n in range(100):
 cfg+=f'  - name: scope-{n:03}\n    include: ["scope-{n:03}/**"]\n    targetVersion: "'+('3.5.0' if n%2 else '3.5.6')+'"\n'
 wire.write(root,f'scope-{n:03}/app.yaml',wire.APP);wire.write(root,f'scope-{n:03}/project.yaml',wire.PROJECT)
repo=root/'repository';repo.mkdir()
for number in range(5000):(repo/f'item-{number:04}').write_text('')
cfg+='repositories:\n  - repoURL: https://example.org/repo\n    path: repository\n'
wire.write(root,'.argocd-ls.yaml',cfg)
membership=pathlib.Path('/proc/self/cgroup').read_text();cg=pathlib.Path('/sys/fs/cgroup')/membership.strip().split('::',1)[1].lstrip('/')
assert int((cg/'memory.max').read_text())==512*1024*1024
metadata={'cgroup':membership,'memory.max':(cg/'memory.max').read_text().strip(),'repositoryEntries':5000,'sameScheduleAsOriginal':True}
(out/'fixture-metadata.json').write_text(json.dumps(metadata,indent=2)+'\n')
def rss():return int(pathlib.Path(f'/proc/{client.proc.pid}/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')
try:
 client=wire.LSP(out,[root])
 for i in range(120):
  n=i%100;p=root/f'scope-{n:03}/app.yaml';version=2 if i>=100 else 1
  if i>=100:client.change(p,wire.APP,version)
  else:client.open(p,wire.APP,version)
  ds=client.diag(p,version);rows.append({'index':i,'scope':n,'rss':rss(),'passed':ds==[],'diagnostics':ds})
except Exception as e:rows.append({'passed':False,'error':repr(e)})
finally:
 if client:client.close()
 values=[r['rss'] for r in rows if 'rss' in r];report={'scope':'distinct-scope retention diagnostic, not the full 10k-cycle acceptance workload','binarySHA256':hashlib.sha256(wire.BINARY.read_bytes()).hexdigest(),'runnerSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'rows':rows,'behaviorPassed':len(values)==120 and all(r['passed'] for r in rows),'warmRSS':values[19] if len(values)>19 else None,'finalRSS':values[-1] if values else None,'exitCode':client.proc.returncode if client else None};report['growthFraction']=report['finalRSS']/report['warmRSS']-1 if report['warmRSS'] else None;report['passed']=report['behaviorPassed'] and report['growthFraction']<=.1;(out/'report.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({k:v for k,v in report.items() if k!='rows'}));sys.exit(0 if report['passed'] else 1)
