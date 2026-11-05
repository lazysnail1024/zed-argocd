#!/usr/bin/env python3
"""Native dynamic registration, unknown Helm state, and configuration refresh."""
import argparse,json,pathlib,sys,time
import audit_repair_e2e as w
p=argparse.ArgumentParser();p.add_argument('binary',type=pathlib.Path);p.add_argument('output',type=pathlib.Path);a=p.parse_args();w.BINARY=a.binary.resolve();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);root=out/'workspace';root.mkdir();w.write(root,'.argocd-ls.yaml',w.CONFIG);w.write(root,'project.yaml',w.PROJECT);f=w.write(root,'app.yaml',w.APP);rows=[]
# Enable registration through the actual initialize wire, without a server shim.
class Client(w.LSP):
 def __init__(self,out,roots,edit=True,watch=True):
  self.edit,self.watch=edit,watch
  super().__init__(out,roots)
 def send(self,method,params=None,ident=None):
  if method=='initialize':
   params['capabilities']['workspace']['didChangeWatchedFiles']={'dynamicRegistration':self.watch}
   params['capabilities']['workspace']['workspaceEdit']={'documentChanges':self.edit}
  return super().send(method,params,ident)
def record(name,passed,observed):rows.append(dict(id=name,passed=passed,observed=observed))
def initialize_contract(client,label):
 result=next(m['message'] for m in client.messages if m['message'].get('id')==1)['result']
 expected={
  'textDocumentSync':{'openClose':True,'change':2,'save':True},
  'completionProvider':{},'hoverProvider':True,'definitionProvider':True,
  'referencesProvider':True,'documentSymbolProvider':True,
  'workspace':{'workspaceFolders':{'supported':True,'changeNotifications':True}},
 }
 if client.edit:expected['codeActionProvider']={'resolveProvider':True}
 info=result.get('serverInfo',{})
 record('initialize-'+label,result.get('capabilities')==expected and info.get('name')=='argocd-ls' and bool(info.get('version')),result)
c=Client(out,[root])
try:
 initialize_contract(c,'edit-watch')
 registration=c.wait(lambda m:m.get('method')=='client/registerCapability',timeout=2);record('watch-registration',registration['params']['registrations'][0]['method']=='workspace/didChangeWatchedFiles',registration)
 body=json.dumps({'jsonrpc':'2.0','id':registration['id'],'result':None}).encode();frame=f'Content-Length: {len(body)}\r\n\r\n'.encode()+body;c.sent.append(frame);c.proc.stdin.write(frame);c.proc.stdin.flush()
 c.open(f,w.APP);c.diag(f,1)
 w.write(root,'project.yaml',w.PROJECT.replace('name: team','name: renamed'));start=time.monotonic();c.send('workspace/didChangeWatchedFiles',{'changes':[{'uri':(root/'project.yaml').as_uri(),'type':2}]});ds=c.diag(f,1);record('watch-change-refresh',any(d['code']=='CD_PROJECT_UNRESOLVED' for d in ds) and time.monotonic()-start<5,ds)
 w.write(root,'chart/Chart.yaml','apiVersion: v2\nname: chart\nversion: 1.0.0\n');template=w.APP.replace('project: team','project: {{ .Values.project }}');t=w.write(root,'chart/templates/app.yaml',template);c.open(t,template);ds=c.diag(t,1);record('unrendered-state',any(d['code']=='HELM_UNRENDERED' and d['severity']==3 for d in ds) and not any(d['code']=='SCHEMA_TYPE' for d in ds),ds)
except Exception as e:record('runner',False,repr(e))
finally:c.close()
# The same protocol must remain useful when a host lacks either optional feature.
for edit,watch in [(False,False),(False,True),(True,False)]:
 label=f'edit-{edit}-watch-{watch}';folder=out/label;folder.mkdir();c=None
 try:
  case_root=folder/'workspace';case_root.mkdir();w.write(case_root,'.argocd-ls.yaml',w.CONFIG);w.write(case_root,'project.yaml',w.PROJECT);case_file=w.write(case_root,'app.yaml',w.APP)
  c=Client(folder,[case_root],edit=edit,watch=watch);initialize_contract(c,label)
  text=w.APP.replace('project: team','proj: team');c.open(case_file,text);c.diag(case_file,1)
  reply=c.request('textDocument/codeAction',{'textDocument':{'uri':case_file.as_uri()},'range':{'start':{'line':5,'character':2},'end':{'line':5,'character':6}},'context':{'diagnostics':[]}})
  actions=reply.get('result');registered=any(m['message'].get('method')=='client/registerCapability' for m in c.messages)
  action_ok=isinstance(actions,list) and (any(x.get('data',{}).get('fixCode')=='FIX_FIELD_NAME' for x in actions) if edit else actions==[])
  record('negotiated-behavior-'+label,action_ok and registered==watch,{'reply':reply,'registered':registered})
 except Exception as e:record('runner-'+label,False,repr(e))
 finally:
  if c:c.close()
(out/'report.json').write_text(json.dumps({'command':sys.argv,'binarySHA256':w.hash_file(w.BINARY),'runnerSHA256':w.hash_file(pathlib.Path(__file__)),'rows':rows},indent=2)+'\n')
sys.exit(len(rows)!=10 or not all(x['passed'] for x in rows))
