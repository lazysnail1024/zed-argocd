#!/usr/bin/env python3
"""Config authority follows supported filesystem identity but replies use client URI."""
import hashlib,json,pathlib,sys
import audit_repair_e2e as wire
wire.BINARY=pathlib.Path(sys.argv[1]).resolve();out=pathlib.Path(sys.argv[2]).resolve();out.mkdir(parents=True,exist_ok=False);rows=[]
def record(name,checks,observed=None):rows.append({'id':name,'checks':checks,'observed':observed,'passed':all(checks.values())})
for form in ['canonical','localhost']:
 folder=out/form;root=folder/'workspace';root.mkdir(parents=True);disk=wire.CONFIG;config=wire.write(root,'.argocd-ls.yaml',disk);app=wire.write(root,'app.yaml',wire.APP);wire.write(root,'project.yaml',wire.PROJECT);uri=config.as_uri() if form=='canonical' else config.as_uri().replace('file://','file://localhost',1);client=None
 try:
  client=wire.LSP(folder,[root]);opened=disk.replace('3.0.0','3.5.6')+'scopes:\n  - name: dev\n    include: ["**"]\n';client.send('textDocument/didOpen',{'textDocument':{'uri':uri,'languageId':'yaml','version':1,'text':opened}});client.open(app,wire.APP,1);client.diag(app,1)
  def hover():return client.request('textDocument/hover',{'textDocument':{'uri':app.as_uri()},'position':{'line':5,'character':12}})
  def target(name,version):
   h=hover();value=(h.get('result') or {}).get('contents',{}).get('value','');checks={'target':f'Target: {version}' in value,'source':('c2b1e3017e2cec86b122172e861dcd026453ae44' if version=='3.5.6' else 'e98f483bfd5781df2592fef1aeed1148f150d9c9') in value};record(form+'-'+name,checks,h)
  def change(version,text):client.send('textDocument/didChange',{'textDocument':{'uri':uri,'version':version},'contentChanges':[{'text':text}]})
  def diagnostic(version):
   predicate=lambda m:m.get('method')=='textDocument/publishDiagnostics' and m['params'].get('uri')==uri and m['params'].get('version')==version and m['params'].get('x-argocd-analysis-scope','workspace')=='workspace'
   prior=next((x['message'] for x in reversed(client.messages) if predicate(x['message'])),None);return (prior if prior is not None else client.wait(predicate))['params']
  target('opened-authority','3.5.6')
  actions=client.request('textDocument/codeAction',{'textDocument':{'uri':uri},'range':{'start':{'line':5,'character':4},'end':{'line':5,'character':13}}});selected=next((a for a in actions.get('result',[]) if a.get('title')=='Pin dev to 3.5.6'),None);checks={'offered':selected is not None};resolved=None
  if selected:
   resolved=client.request('codeAction/resolve',selected);edits=(resolved.get('result') or {}).get('edit',{}).get('documentChanges',[]);checks['clientIdentity']=len(edits)==1 and edits[0]['textDocument']=={'uri':uri,'version':1}
   if edits:
    text=opened
    for e in reversed(edits[0]['edits']):
     lines=text.splitlines(keepends=True);a=e['range']['start'];b=e['range']['end'];start=sum(map(len,lines[:a['line']]))+a['character'];end=sum(map(len,lines[:b['line']]))+b['character'];text=text[:start]+e['newText']+text[end:]
    checks['exactEditedBytes']=text==opened.replace('  - name: dev\n','  - name: dev\n    targetVersion: "3.5.6"\n')
  record(form+'-pin-client-identity',checks,{'actions':actions,'resolved':resolved})
  for version,text,code in [(2,opened.replace('3.5.6','9.9.9'),'TARGET_UNKNOWN'),(3,opened+'unknownField: true\n','CONFIG_INVALID')]:
   change(version,text);p=diagnostic(version);h=hover();record(form+'-invalid-'+str(version),{'clientURI':p['uri']==uri,'version':p['version']==version,'exactError':[d['code'] for d in p['diagnostics']]==[code],'noStaleHover':h.get('error',{}).get('code')==-32801},{'publication':p,'hover':h})
  change(4,opened);diagnostic(4);target('valid-recovery','3.5.6')
  change(5,'#'+'x'*(5*1024*1024));p=diagnostic(5);h=hover();record(form+'-refused-config',{'budget':[d['code'] for d in p['diagnostics']]==['INPUT_BUDGET_EXCEEDED'],'clientIdentity':p['uri']==uri and p['version']==5,'noDiskFallback':h.get('error',{}).get('code')==-32801},{'publication':p,'hover':h})
  client.send('textDocument/didChange',{'textDocument':{'uri':uri,'version':6},'contentChanges':[{'range':{'start':{'line':0,'character':0},'end':{'line':0,'character':0}},'text':'#'}]});p=diagnostic(6);record(form+'-unavailable-incremental',{'budget':[d['code'] for d in p['diagnostics']]==['INPUT_BUDGET_EXCEEDED'],'noDiskFallback':hover().get('error',{}).get('code')==-32801},p)
  change(7,opened);diagnostic(7);target('full-replacement-recovery','3.5.6')
  client.send('textDocument/didClose',{'textDocument':{'uri':uri}});client.change(app,wire.APP,2);client.diag(app,2);target('close-restores-disk','3.0.0')
 except Exception as e:rows.append({'id':form+'-runner','passed':False,'error':repr(e)})
 finally:
  if client:client.close();record(form+'-shutdown',{'exit':client.proc.returncode==0})
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest();report={'binarySHA256':sha(wire.BINARY),'runnerSHA256':sha(pathlib.Path(__file__)),'wireSHA256':sha(pathlib.Path(wire.__file__)),'rows':rows,'passed':len(rows)==20 and all(r['passed'] for r in rows)};(out/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'rows':len(rows),'failed':[r['id'] for r in rows if not r['passed']],'passed':report['passed']}));sys.exit(0 if report['passed'] else 1)
