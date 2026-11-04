#!/usr/bin/env python3
"""Lost file event is corrected by the actual natural 30-second reconciliation."""
import argparse,json,pathlib,sys,time,os
import audit_repair_e2e as w
p=argparse.ArgumentParser();p.add_argument('binary',type=pathlib.Path);p.add_argument('output',type=pathlib.Path);a=p.parse_args();w.BINARY=a.binary.resolve();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);r=out/'workspace';r.mkdir();w.write(r,'.argocd-ls.yaml',w.CONFIG);project=w.write(r,'project.yaml',w.PROJECT);f=w.write(r,'app.yaml',w.APP);os.environ['ARGOCD_LS_TRACE']='1';c=w.LSP(out,[r]);rows=[]
try:
 c.open(f,w.APP);ds=c.diag(f,1);assert not ds;project.write_text(w.PROJECT.replace('name: team','name: renamed'));start=time.monotonic()
 response=c.wait(lambda m:m.get('method')=='textDocument/publishDiagnostics' and m['params']['uri']==f.as_uri() and any(d['code']=='CD_PROJECT_UNRESOLVED' for d in m['params']['diagnostics']),timeout=35);rows.append({'id':'lost-event-reconciled','passed':True,'seconds':time.monotonic()-start,'response':response})
except Exception as e:rows.append({'id':'runner','passed':False,'error':repr(e)})
finally:c.close();(out/'report.json').write_text(json.dumps({'binarySHA256':w.hash_file(w.BINARY),'runnerSHA256':w.hash_file(pathlib.Path(__file__)),'rows':rows},indent=2)+'\n')
sys.exit(len(rows)!=1 or not all(r['passed'] for r in rows))
