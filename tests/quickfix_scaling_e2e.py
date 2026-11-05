#!/usr/bin/env python3
"""Candidate counts, selected-only validation, and stale action rejection."""
import argparse,json,pathlib,sys,time,os,statistics
import audit_repair_e2e as w
p=argparse.ArgumentParser();p.add_argument('binary',type=pathlib.Path);p.add_argument('output',type=pathlib.Path);a=p.parse_args();w.BINARY=a.binary.resolve();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);os.environ['ARGOCD_LS_TRACE']='1';rows=[]
try:
 for n in [1,20,100,300]:
  d=out/str(n);r=d/'workspace';r.mkdir(parents=True);w.write(r,'.argocd-ls.yaml',w.CONFIG);text=w.APP.replace('project: team','project: missing');f=w.write(r,'app.yaml',text)
  for i in range(n):w.write(r,str(i)+'.yaml',w.PROJECT.replace('name: team','name: team'+str(i)))
  c=w.LSP(d,[r]);c.open(f,text);c.diag(f,1);listing=[];resolving=[]
  for i in range(5):
   begin=time.monotonic();response=c.request('textDocument/codeAction',{'textDocument':{'uri':f.as_uri()},'range':{'start':{'line':5,'character':11},'end':{'line':5,'character':18}},'context':{'diagnostics':[]}});listing.append(time.monotonic()-begin);actions=response.get('result',[]);assert len(actions)==n,response
   action=actions[-1];begin=time.monotonic();resolved=c.request('codeAction/resolve',action);resolving.append(time.monotonic()-begin);change=resolved.get('result',{}).get('edit',{}).get('documentChanges',[]);assert len(change)==1 and change[0]['textDocument']=={'uri':f.as_uri(),'version':1}
  c.change(f,text+'# stale\n',2);stale=c.request('codeAction/resolve',action);assert stale.get('error',{}).get('code')==-32801;c.close();trace=[json.loads(line.split(' ',1)[1]) for line in (d/'stderr').read_text().splitlines() if line.startswith('argocd-fix ')];assert len(trace)==5 and all(t['safePlans']==1 and t['documentAnalyses']==2 and t['scopeDomainAnalyses']==2 for t in trace),trace;rows.append({'candidates':n,'listSeconds':listing,'resolveSeconds':resolving,'listP50':statistics.median(listing),'resolveP50':statistics.median(resolving),'trace':trace,'passed':True})
except Exception as e:rows.append({'passed':False,'error':repr(e)})
finally:
 for c in w.CLIENTS:c.close()
 (out/'report.json').write_text(json.dumps({'command':sys.argv,'binarySHA256':w.hash_file(w.BINARY),'runnerSHA256':w.hash_file(pathlib.Path(__file__)),'rows':rows},indent=2)+'\n')
print(json.dumps(rows));sys.exit(len(rows)!=4 or not all(x['passed'] for x in rows))
