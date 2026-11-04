#!/usr/bin/env python3
"""Native dynamic registration, unknown Helm state, and configuration refresh."""
import argparse,json,pathlib,sys,time
import audit_repair_e2e as w
p=argparse.ArgumentParser();p.add_argument('binary',type=pathlib.Path);p.add_argument('output',type=pathlib.Path);a=p.parse_args();w.BINARY=a.binary.resolve();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);root=out/'workspace';root.mkdir();w.write(root,'.argocd-ls.yaml',w.CONFIG);w.write(root,'project.yaml',w.PROJECT);f=w.write(root,'app.yaml',w.APP);rows=[]
# Enable registration through the actual initialize wire, without a server shim.
class Client(w.LSP):
 def send(self,method,params=None,ident=None):
  if method=='initialize':params['capabilities']['workspace']['didChangeWatchedFiles']={'dynamicRegistration':True}
  return super().send(method,params,ident)
def record(name,passed,observed):rows.append(dict(id=name,passed=passed,observed=observed))
c=Client(out,[root])
try:
 registration=c.wait(lambda m:m.get('method')=='client/registerCapability',timeout=2);record('watch-registration',registration['params']['registrations'][0]['method']=='workspace/didChangeWatchedFiles',registration)
 body=json.dumps({'jsonrpc':'2.0','id':registration['id'],'result':None}).encode();frame=f'Content-Length: {len(body)}\r\n\r\n'.encode()+body;c.sent.append(frame);c.proc.stdin.write(frame);c.proc.stdin.flush()
 c.open(f,w.APP);c.diag(f,1)
 w.write(root,'project.yaml',w.PROJECT.replace('name: team','name: renamed'));start=time.monotonic();c.send('workspace/didChangeWatchedFiles',{'changes':[{'uri':(root/'project.yaml').as_uri(),'type':2}]});ds=c.diag(f,1);record('watch-change-refresh',any(d['code']=='CD_PROJECT_UNRESOLVED' for d in ds) and time.monotonic()-start<5,ds)
 w.write(root,'chart/Chart.yaml','apiVersion: v2\nname: chart\nversion: 1.0.0\n');template=w.APP.replace('project: team','project: {{ .Values.project }}');t=w.write(root,'chart/templates/app.yaml',template);c.open(t,template);ds=c.diag(t,1);record('unrendered-state',any(d['code']=='HELM_UNRENDERED' and d['severity']==3 for d in ds) and not any(d['code']=='SCHEMA_TYPE' for d in ds),ds)
except Exception as e:record('runner',False,repr(e))
finally:c.close();(out/'report.json').write_text(json.dumps({'command':sys.argv,'binarySHA256':w.hash_file(w.BINARY),'runnerSHA256':w.hash_file(pathlib.Path(__file__)),'rows':rows},indent=2)+'\n')
sys.exit(len(rows)!=3 or not all(x['passed'] for x in rows))
