#!/usr/bin/env python3
"""Repeatable hot comment-edit timings and opt-in acquisition/analysis traces."""
import argparse,json,pathlib,platform,statistics,sys,time,os,math
import audit_repair_e2e as w
p=argparse.ArgumentParser();p.add_argument('binary',type=pathlib.Path);p.add_argument('output',type=pathlib.Path);p.add_argument('--repeats',type=int,default=30);p.add_argument('--sizes',default='1000,3000');p.add_argument('--require-incremental',action='store_true');p.add_argument('--repository',action='store_true');a=p.parse_args();w.BINARY=a.binary.resolve();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);os.environ['ARGOCD_LS_TRACE']='1';rows=[]
try:
 for n in map(int,a.sizes.split(',')):
  d=out/str(n);r=d/'workspace';r.mkdir(parents=True);w.write(r,'.argocd-ls.yaml',w.CONFIG+('repositories: [{repoURL: https://example.org/repo, path: repository}]\n' if a.repository else ''));w.write(r,'project.yaml',w.PROJECT)
  if a.repository:(r/'repository'/'existing').mkdir(parents=True)
  for i in range(n):w.write(r,str(i)+'.yaml',w.APP.replace('name: app','name: app'+str(i)).replace('repoURL: https://example.org/repo','repoURL: https://example.org/repo, path: existing') if a.repository else w.APP.replace('name: app','name: app'+str(i)))
  f=r/'0.yaml';text=f.read_text();c=w.LSP(d,[r]);c.open(f,text);c.diag(f,1);samples=[];rss=[]
  for i in range(a.repeats):
   start=time.monotonic();c.change(f,text+'# sample '+str(i)+'\n',i+2);ds=c.diag(f,i+2);samples.append((time.monotonic()-start)*1000);status=pathlib.Path('/proc')/str(c.proc.pid)/'status';rss.append(next((line.split(':',1)[1].strip() for line in status.read_text().splitlines() if line.startswith('VmRSS:')),None) if status.exists() else None);assert not any(x['severity']==1 for x in ds)
  c.close();traces=[json.loads(line.split(' ',1)[1]) for line in (d/'stderr').read_text().splitlines() if line.startswith('argocd-analysis ')];hot=[x for x in traces if not x['fullScan']]
  if a.require_incremental:assert len(hot)==a.repeats and all(x['diskReads']==0 and x['parsedFiles']==1 and x['domainDocuments']==2 and x.get('repositoryDecodes',0)<=1 and x.get('acquisitionFallbacks')==0 for x in hot),hot
  rows.append({'size':n,'rssSamples':rss,'traces':traces,'samplesMs':samples,'p50':statistics.median(samples),'p95':sorted(samples)[math.ceil(.95*len(samples))-1]});print(json.dumps(rows[-1]),flush=True)
finally:
 for c in w.CLIENTS:c.close()
 (out/'report.json').write_text(json.dumps({'command':sys.argv,'binarySHA256':w.hash_file(w.BINARY),'runnerSHA256':w.hash_file(pathlib.Path(__file__)),'python':sys.version,'platform':platform.platform(),'rows':rows},indent=2)+'\n')
