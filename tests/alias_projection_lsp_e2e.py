#!/usr/bin/env python3
"""Value-path provenance, exact sibling operations and projection recovery over LSP."""
import hashlib,json,pathlib,sys
import audit_repair_e2e as wire
wire.BINARY=pathlib.Path(sys.argv[1]).resolve();out=pathlib.Path(sys.argv[2]).resolve();out.mkdir(parents=True,exist_ok=False);rows=[]
head='apiVersion: argoproj.io/v1alpha1\nkind: Application\n';tail='  source: {repoURL: https://example.org/repo}\n  destination: {server: here}\n'
def point(text,offset):return {'line':text[:offset].count('\n'),'character':len(text[:offset].rsplit('\n',1)[-1].encode('utf-16-le'))//2}
def span(text,token):a=text.index(token);return {'start':point(text,a),'end':point(text,a+len(token))}
def record(name,checks,observed=None):rows.append({'id':name,'checks':checks,'observed':observed,'passed':all(checks.values())})
for version in ['3.0.0','3.5.6']:
 folder=out/version;root=folder/'workspace';root.mkdir(parents=True);wire.write(root,'.argocd-ls.yaml',wire.CONFIG.replace('3.0.0',version));project=wire.write(root,'project.yaml',wire.PROJECT);app=root/'app.yaml';c=None;revision=0
 try:
  c=wire.LSP(folder,[root])
  def load(text):
   global revision
   revision+=1
   if revision==1:c.open(app,text,revision)
   else:c.change(app,text,revision)
   (folder/f'input-{revision}.yaml').write_text(text);return c.diag(app,revision)
  def query(method,text,token):return c.request('textDocument/'+method,{'textDocument':{'uri':app.as_uri()},'position':point(text,text.index(token)+1)})
  def actions(text,token):return c.request('textDocument/codeAction',{'textDocument':{'uri':app.as_uri()},'range':span(text,token)})
  text=head+'metadata:\n  name: app\n  annotations: {project: &p team}\nspec:\n  project: *p\n'+tail
  ds=load(text);h=query('hover',text,'*p');definition=query('definition',text,'*p');a=actions(text,'*p');completion=query('completion',text,'*p');symbols=c.request('textDocument/documentSymbol',{'textDocument':{'uri':app.as_uri()}})
  value=(h.get('result') or {}).get('contents',{}).get('value','');record(version+'-scalar-alias-value',{'noDiagnostics':ds==[],'hoverType':'Type: string' in value,'hoverTarget':f'Target: {version}' in value,'derivedLabel':'derived' in value.lower(),'useRange':(h.get('result') or {}).get('range')==span(text,'*p'),'noInferredDefinition':definition.get('result')==[],'noDerivedEdit':a.get('result')==[],'noDerivedCompletion':completion.get('result')==[],'exactSiblingSymbol':len(symbols.get('result',[]))==1},{'diagnostics':ds,'hover':h,'definition':definition,'actions':a,'completion':completion,'symbols':symbols})
  refs=c.request('textDocument/references',{'textDocument':{'uri':project.as_uri()},'position':{'line':3,'character':9},'context':{'includeDeclaration':True}});record(version+'-no-derived-reference-range',{'onlyExactDeclaration':len(refs.get('result',[]))==1 and refs['result'][0]['uri']==project.as_uri()},refs)
  text=head+'metadata:\n  name: app\n  annotations: &defaults {project: absent}\nspec:\n  <<: *defaults\n  source: {repoURL: https://example.org/repo}\n  destination: {server: here, name: conflict}\n'
  ds=load(text);derived=[d for d in ds if d['code']=='CD_PROJECT_UNRESOLVED'];direct=[d for d in ds if d['code']=='CD_DESTINATION_CONFLICT'];record(version+'-merge-derived-and-exact-errors',{'exactCodes':sorted(d['code'] for d in ds)==['CD_DESTINATION_CONFLICT','CD_PROJECT_UNRESOLVED'],'derivedRange':len(derived)==1 and derived[0]['range']==span(text,'*defaults'),'derivedLabel':len(derived)==1 and 'derived' in derived[0]['message'].lower(),'exactSibling':len(direct)==1 and direct[0]['range']==span(text,'{server: here, name: conflict}'),'noMergeEdit':actions(text,'*defaults').get('result')==[]},ds)
  text=head+'metadata: {name: app}\nspec:\n  project: &p absent\n  source: {repoURL: *p}\n  destinatoin: {server: here}\n';ds=load(text);anchor=actions(text,'absent');a=actions(text,'destinatoin');choice=next((x for x in a.get('result',[]) if x.get('title')=='Rename field to destination'),None);checks={'sharedAnchorNoFix':anchor.get('result')==[],'exactSiblingFix':choice is not None};resolved=None
  if choice:
   resolved=c.request('codeAction/resolve',choice);changes=(resolved.get('result') or {}).get('edit',{}).get('documentChanges',[]);checks['versioned']=len(changes)==1 and changes[0]['textDocument']=={'uri':app.as_uri(),'version':revision}
   if changes:
    edited=text
    for edit in reversed(changes[0]['edits']):
     lines=edited.splitlines(keepends=True);start=edit['range']['start'];end=edit['range']['end'];a=sum(map(len,lines[:start['line']]))+start['character'];b=sum(map(len,lines[:end['line']]))+end['character'];edited=edited[:a]+edit['newText']+edited[b:]
    checks['exactBytes']=edited==text.replace('destinatoin:','destination:');after=load(edited);checks['onlyUnrelatedError']=[x['code'] for x in after]==['CD_PROJECT_UNRESOLVED']
  record(version+'-shared-anchor-and-exact-sibling-fix',checks,{'diagnostics':ds,'anchorActions':anchor,'actions':a,'resolved':resolved})
  text=head+'metadata:\n  name: app\n  annotations: &defaults {project: absent}\nspec:\n  project: team\n  <<: *defaults\n'+tail;ds=load(text);d=query('definition',text,'project: team');# Query inside the value, not the key.
  d=c.request('textDocument/definition',{'textDocument':{'uri':app.as_uri()},'position':point(text,text.index('project: team')+10)});record(version+'-explicit-override-exact-definition',{'noDiagnostics':ds==[],'exactTarget':len(d.get('result',[]))==1 and d['result'][0]['uri']==project.as_uri()},d)
  text=head+'metadata: {name: app}\nspec:\n  project: &p [*p]\n'+tail;ds=load(text);record(version+'-recursive-refusal',{'explicitIncomplete':[d['code'] for d in ds]==['YAML_PROJECTION_INCOMPLETE'],'noHover':query('hover',text,'*p').get('result') is None},ds)
  text=head+'metadata: {name: app}\nspec:\n  project: team\n'+tail;ds=load(text);record(version+'-recovery',{'noDiagnostics':ds==[],'hover':bool(query('hover',text,'repoURL').get('result'))},ds)
 except Exception as e:rows.append({'id':version+'-runner','passed':False,'error':repr(e)})
 finally:
  if c:c.close();record(version+'-shutdown',{'exit':c.proc.returncode==0})
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest();report={'binarySHA256':sha(wire.BINARY),'runnerSHA256':sha(pathlib.Path(__file__)),'rows':rows,'passed':len(rows)==16 and all(r['passed'] for r in rows)};(out/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'rows':len(rows),'failed':[r['id'] for r in rows if not r['passed']]}));sys.exit(0 if report['passed'] else 1)
