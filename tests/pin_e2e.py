#!/usr/bin/env python3
"""Pin edits use only current explicit targets and versioned, opened configuration text."""
import pathlib,json,sys,os
import audit_repair_e2e as e
e.OUT=pathlib.Path(os.environ.get('CD_AUDIT_OUTPUT',str(pathlib.Path(__file__).resolve().parents[1]/'artifacts/pin')));e.OUT.mkdir(parents=True,exist_ok=True)
def actions(c,uri,line):
 return c.request('textDocument/codeAction',{'textDocument':{'uri':uri},'range':{'start':{'line':line,'character':0},'end':{'line':line+1,'character':0}},'context':{'diagnostics':[]}})
def apply(text,change):
 lines=text.splitlines(keepends=True)
 def offset(p):return len(''.join(lines[:p['line']]))+len(lines[p['line']].encode('utf-16-le')[:p['character']*2].decode('utf-16-le')) if p['line']<len(lines) else len(text)
 for edit in reversed(change['edits']):
  a,b=offset(edit['range']['start']),offset(edit['range']['end']);text=text[:a]+edit['newText']+text[b:]
 return text
name='root-pin-open-version-bytes';out,root=e.setup(name);file=e.write(root,'app.yaml',e.APP);config=root/'.argocd-ls.yaml';uri=config.as_uri();c=e.LSP(out,[root])
try:
 c.open(file,e.APP);c.diag(file,1)
 assert actions(c,uri,1).get('result')==[]
 text='# \u4e2d\u6587 😀\r\nschemaVersion: 1\r\ntargetVersion: \'3.5.6\' # keep\r\ncontrolPlaneNamespace: argocd\r\n'
 c.send('workspace/didChangeConfiguration',{'settings':{'targetVersion':'3.0.0'}});c.open(config,text,7);ds=c.diag(config,7);assert not any(d.get('severity') in [1,2] for d in ds)
 action=next(a for a in actions(c,uri,2)['result'] if a['data']['fixCode']=='FIX_PIN_VERSION');resolved=c.request('codeAction/resolve',action)['result'];change=resolved['edit']['documentChanges'][0];assert change['textDocument']=={'uri':uri,'version':7};edited=apply(text,change);assert edited==text.replace("'3.5.6'","'3.0.0'")
 c.change(config,text+'# later\r\n',8);c.diag(config,8);assert c.request('codeAction/resolve',action)['error']['code']==-32801
 c.change(config,edited,9);c.diag(config,9);assert not actions(c,uri,2).get('result');assert c.request('codeAction/resolve',action)['error']['code']==-32801
 e.record(name,{'edit':'only quoted target scalar; CRLF/comment/emoji unchanged','version':7,'stale':-32801},{'before':text,'after':edited,'resolved':resolved},True,root,c)
except Exception as ex:c.close();e.record(name,{'safePin':True},{'exception':repr(ex)},False,root)
name='scope-pin-insert-and-verified-default';out,root=e.setup(name);file=e.write(root,'apps/app.yaml',e.APP);config=root/'.argocd-ls.yaml';uri=config.as_uri();c=e.LSP(out,[root])
try:
 text='schemaVersion: 1\ntargetVersion: "3.0.0"\ncontrolPlaneNamespace: argocd\nscopes:\n  - name: dev # keep\n    include: ["apps/**"]\n'
 c.open(config,text,1);assert not any(d.get('severity') in [1,2] for d in c.diag(config,1));action=next(a for a in actions(c,uri,4)['result'] if a['data']['fixCode']=='FIX_PIN_VERSION');resolved=c.request('codeAction/resolve',action)['result'];change=resolved['edit']['documentChanges'][0];edited=apply(text,change);assert edited==text.replace('  - name: dev # keep\n','  - name: dev # keep\n    targetVersion: "3.0.0"\n');assert change['textDocument']['version']==1
 c.change(config,edited,2);c.diag(config,2);assert not actions(c,uri,4).get('result')
 defaultText='schemaVersion: 1\n';c.change(config,defaultText,3);c.diag(config,3);defaultActions=actions(c,uri,0)['result'];assert not [a for a in defaultActions if a.get('data',{}).get('fixCode')=='FIX_PIN_VERSION'],'automatic mode must not offer an unnecessary pin'
 e.record(name,{'scope':'exact insertion after name; only dev scope','verifiedDefault':'automatic mode requires no persisted pin'}, {'before':text,'after':edited,'resolved':resolved},True,root,c)
except Exception as ex:c.close();e.record(name,{'scopePin':True},{'exception':repr(ex)},False,root)
name='root-insertion-close-and-invalid-target';out,root=e.setup(name);file=e.write(root,'app.yaml',e.APP);config=root/'.argocd-ls.yaml';uri=config.as_uri();c=e.LSP(out,[root])
try:
 c.open(file,e.APP);c.diag(file,1)
 c.send('workspace/didChangeConfiguration',{'settings':{'targetVersion':'3.0.0'}})
 text='# retain\nschemaVersion: 1 # keep\ncontrolPlaneNamespace: argocd\n'
 c.open(config,text,10);c.diag(config,10)
 action=next(a for a in actions(c,uri,1)['result'] if a['data']['fixCode']=='FIX_PIN_VERSION')
 forged=json.loads(json.dumps(action));forged['data']['newText']='targetVersion: "99.0.0"\n';assert c.request('codeAction/resolve',forged)['error']['code']==-32801
 result=c.request('codeAction/resolve',action)['result'];edited=apply(text,result['edit']['documentChanges'][0]);assert edited==text.replace('schemaVersion: 1 # keep\n','schemaVersion: 1 # keep\ntargetVersion: "3.0.0"\n')
 c.send('textDocument/didClose',{'textDocument':{'uri':uri}});assert c.request('codeAction/resolve',action)['error']['code']==-32801
 c.send('workspace/didChangeConfiguration',{'settings':{'targetVersion':'99.0.0'}});c.open(config,text,11);c.diag(config,11);assert not actions(c,uri,1).get('result')
 e.record(name,{'rootInsert':'only targetVersion line','forgedAndClosed':'no edit','unknownTarget':'no candidates'}, {'before':text,'after':edited},True,root,c)
except Exception as ex:c.close();e.record(name,{'rootInsertionAndBoundaries':True},{'exception':repr(ex)},False,root)
(e.OUT/'report.json').write_text(json.dumps({'command':sys.argv,'python':sys.version,'binarySHA256':e.hash_file(e.BINARY),'testSHA256':e.hash_file(pathlib.Path(__file__)),'clientSHA256':e.hash_file(pathlib.Path(e.__file__)),'cases':e.ROWS},indent=2)+'\n');sys.exit(0 if len(e.ROWS)==3 and all(r['passed'] for r in e.ROWS) else 1)
