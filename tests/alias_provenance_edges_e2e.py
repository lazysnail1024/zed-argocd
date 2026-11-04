#!/usr/bin/env python3
"""Derived declaration guards and exact CRLF/UTF-16 sibling edits via real LSP."""
import hashlib,json,pathlib,sys
import audit_repair_e2e as wire
wire.BINARY=pathlib.Path(sys.argv[1]).resolve();out=pathlib.Path(sys.argv[2]).resolve();out.mkdir(parents=True,exist_ok=False);rows=[]
def pos(text,off):return {'line':text[:off].count('\n'),'character':len(text[:off].rsplit('\n',1)[-1].encode('utf-16-le'))//2}
def offset(text,p):lines=text.splitlines(keepends=True);return sum(map(len,lines[:p['line']]))+len(lines[p['line']].encode('utf-16-le')[:2*p['character']].decode('utf-16-le'))
def record(name,checks,observed):rows.append({'id':name,'checks':checks,'observed':observed,'passed':all(checks.values())})
for target in ['3.0.0','3.5.6']:
 folder=out/target;root=folder/'workspace';root.mkdir(parents=True);wire.write(root,'.argocd-ls.yaml',wire.CONFIG.replace('3.0.0',target));project=wire.write(root,'project.yaml',wire.PROJECT);app=wire.write(root,'app.yaml',wire.APP);client=None
 try:
  client=wire.LSP(folder,[root]);text='apiVersion: argoproj.io/v1alpha1\r\nkind: Application\r\nmetadata: {name: app, annotations: {note: "\\U0001f600"}}\r\nspec: {project: &p team, source: {repoURL: *p, helm: {releaseName: "\U0001f600"}}, destinatoin: {server: here}} # keep\r\n'.replace('\\U0001f600','\U0001f600');client.open(app,text,1);ds=client.diag(app,1);i=text.index('destinatoin');r={'start':pos(text,i),'end':pos(text,i+11)};reply=client.request('textDocument/codeAction',{'textDocument':{'uri':app.as_uri()},'range':r});choice=next((a for a in reply.get('result',[]) if a['title']=='Rename field to destination'),None);checks={'available':choice is not None};resolved=None
  if choice:
   resolved=client.request('codeAction/resolve',choice);changes=(resolved.get('result') or {}).get('edit',{}).get('documentChanges',[]);checks['version']=len(changes)==1 and changes[0]['textDocument']=={'uri':app.as_uri(),'version':1};edited=text
   if changes:
    for edit in reversed(changes[0]['edits']):a=offset(edited,edit['range']['start']);b=offset(edited,edit['range']['end']);edited=edited[:a]+edit['newText']+edited[b:]
    checks['exactBytes']=edited==text.replace('destinatoin:','destination:');client.change(app,edited,2);checks['clean']=client.diag(app,2)==[]
  record(target+'-crlf-sibling',checks,{'diagnostics':ds,'response':resolved})
  declared=wire.PROJECT.replace('  name: team','  annotations: {selected: &name team}\n  name: *name');client.open(project,declared,1);client.diag(project,1);client.change(app,wire.APP,3);ds=client.diag(app,3);where=pos(wire.APP,wire.APP.index('project: team')+11);d=client.request('textDocument/definition',{'textDocument':{'uri':app.as_uri()},'position':where});symbols=client.request('textDocument/documentSymbol',{'textDocument':{'uri':project.as_uri()}});record(target+'-derived-declaration',{'resolvedValue':ds==[],'noInferredDeclaration':d.get('result')==[],'noInferredSymbol':symbols.get('result')==[]},{'diagnostics':ds,'definition':d,'symbols':symbols})
 except Exception as e:record(target+'-runner',{'terminal':False},repr(e))
 finally:
  if client:client.close();record(target+'-shutdown',{'exit':client.proc.returncode==0},client.proc.returncode)
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest();report={'binarySHA256':sha(wire.BINARY),'runnerSHA256':sha(pathlib.Path(__file__)),'rows':rows,'passed':len(rows)==6 and all(r['passed'] for r in rows)};(out/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'passed':report['passed'],'failed':[r['id'] for r in rows if not r['passed']]}));sys.exit(0 if report['passed'] else 1)
