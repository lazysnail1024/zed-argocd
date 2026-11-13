#!/usr/bin/env python3
"""Independent reference candidates: visible identities, exact edits, dynamic and damage boundaries."""
import pathlib,json,sys,os
import audit_repair_e2e as e
e.OUT=pathlib.Path(os.environ.get('CD_AUDIT_OUTPUT',str(pathlib.Path(__file__).resolve().parents[1]/'artifacts/candidates')));e.OUT.mkdir(parents=True,exist_ok=True)
def at(text,needle,inside=None):
 offset=text.index(needle)+(len(needle) if inside is None else inside);before=text[:offset];return {'line':before.count('\n'),'character':len(before.rsplit('\n',1)[-1].encode('utf-16-le'))//2}
def query(c,file,text,needle):return c.request('textDocument/completion',{'textDocument':{'uri':file.as_uri()},'position':at(text,needle)}).get('result',[])
def project(name,namespace):return e.PROJECT.replace('name: team','name: '+name).replace('namespace: argocd','namespace: '+namespace)
def run(name,fn):
 out,root=e.setup(name);c=None
 try:
  c,observed=fn(out,root);e.record(name,{'contract':name},observed,True,root,c)
 except Exception as ex:
  for client in e.CLIENTS:
   if not client.closed:client.close()
  e.record(name,{'contract':name},{'exception':repr(ex),'exceptionType':type(ex).__name__},False,root)
def projects(out,root):
 e.write(root,'a.yaml',project('team','argocd'));e.write(root,'b.yaml',project('team-blue','argocd'));e.write(root,'other.yaml',project('team-out','other'));text=e.APP.replace('project: team','project: te');file=e.write(root,'app.yaml',text);c=e.LSP(out,[root]);c.open(file,text);c.diag(file,1);items=query(c,file,text,'project: te');assert [x['label'] for x in items]==['team','team-blue'];assert all('argocd' in x['detail'] and '.yaml' in x['detail'] for x in items)
 c.change(file,text.replace('project: te','project: default'),2);c.diag(file,2);assert query(c,file,text.replace('project: te','project: default'),'project: default')==[]
 return c,{'candidates':items}
run('project-visible-prefix-no-default',projects)
def namespaces(out,root):
 e.write(root,'.argocd-ls.yaml','schemaVersion: 1\ntargetVersion: "3.0.0"\n');e.write(root,'a.yaml',project('team','one'));e.write(root,'b.yaml',project('team','two'));file=e.write(root,'app.yaml',e.APP);c=e.LSP(out,[root]);c.open(file,e.APP);c.diag(file,1);items=query(c,file,e.APP,'project: team');assert len(items)==2 and all(x['label']=='team' for x in items);assert 'one' in items[0]['detail'] and 'two' in items[1]['detail'];defs=c.request('textDocument/definition',{'textDocument':{'uri':file.as_uri()},'position':at(e.APP,'project: team')});assert len(defs.get('result',[]))==2
 return c,{'candidates':items,'definition':defs}
run('namespace-unknown-keeps-origin-without-guess',namespaces)
SOURCES='apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata: {name: app}\nspec:\n  project: team\n  sources:\n    - {repoURL: x, ref: values}\n    - repoURL: chart\n      helm:\n        valueFiles: ["$va/values.yaml"] # keep\n  destination: {server: x, namespace: default}\n'
def refs(out,root):
 e.write(root,'project.yaml',e.PROJECT);text=SOURCES+'---\n'+SOURCES.replace('ref: values','ref: vault');file=e.write(root,'app.yaml',text);c=e.LSP(out,[root]);c.open(file,text);c.diag(file,1);items=query(c,file,text,'$va');assert [x['label'] for x in items]==['values'];edit=items[0]['textEdit'];assert edit['newText']=='values';assert edit['range']=={'start':at(text,'$va',1),'end':at(text,'$va')}
 c.send('textDocument/didChange',{'textDocument':{'uri':file.as_uri(),'version':2},'contentChanges':[{'range':edit['range'],'text':edit['newText']}]});ds=c.diag(file,2);assert not any(d['code']=='YAML_PROJECTION_INCOMPLETE' for d in ds)
 return c,{'candidates':items,'afterDiagnostics':ds}
run('source-ref-document-local-exact-prefix',refs)
def dynamic(out,root):
 e.write(root,'project.yaml',e.PROJECT);text='apiVersion: argoproj.io/v1alpha1\nkind: ApplicationSet\nmetadata: {name: set}\nspec:\n  goTemplate: true\n  generators: []\n  template:\n    metadata: {name: "{{.name}}"}\n    spec:\n      project: te\n      sources:\n        - {repoURL: x, ref: values}\n        - {repoURL: y, ref: "{{.ref}}"}\n        - repoURL: chart\n          helm:\n            valueFiles: ["$va/values.yaml"]\n      destination: {server: x, namespace: default}\n';file=e.write(root,'app.yaml',text);c=e.LSP(out,[root]);c.open(file,text);c.diag(file,1);items=query(c,file,text,'project: te');assert [x['label'] for x in items]==['team'];refs=query(c,file,text,'$va');assert [x['label'] for x in refs]==['values'];new=text.replace('project: te','project: "{{.project}}"');c.change(file,new,2);c.diag(file,2);assert query(c,file,new,'{{.project')==[]
 return c,{'projectCandidates':items,'refCandidates':refs}
run('appset-static-and-dynamic-leaves',dynamic)
def damage(out,root):
 e.write(root,'project.yaml',e.PROJECT);text=e.APP.replace('project: team','project: te')+'  broken: [\n';file=e.write(root,'app.yaml',text);c=e.LSP(out,[root]);c.open(file,text);ds=c.diag(file,1);items=query(c,file,text,'project: te');assert [x['label'] for x in items]==['team'];assert not any(d['code']=='CD_PROJECT_UNRESOLVED' for d in ds)
 foreign=text.replace('kind: Application','kind: Workflow');c.change(file,foreign,2);c.diag(file,2);assert query(c,file,foreign,'project: te')==[]
 c.change(file,e.APP,3);c.diag(file,3);defs=c.request('textDocument/definition',{'textDocument':{'uri':file.as_uri()},'position':at(e.APP,'project: team')});assert len(defs.get('result',[]))==1
 return c,{'reliableCandidates':items,'damagedDiagnostics':ds,'recoveredDefinition':defs}
run('damaged-reliable-project-and-foreign-recovery',damage)
def scope_boundary(out,root):
 e.write(root,'.argocd-ls.yaml','schemaVersion: 1\ntargetVersion: "3.0.0"\ncontrolPlaneNamespace: argocd\nscopes:\n  - name: apps\n    include: ["apps/**"]\n  - name: other\n    include: ["other/**"]\n');e.write(root,'other/project.yaml',e.PROJECT);text=e.APP.replace('project: team','project: te');file=e.write(root,'apps/app.yaml',text);c=e.LSP(out,[root]);c.open(file,text);c.diag(file,1);assert query(c,file,text,'project: te')==[]
 e.write(root,'apps/broken.yaml',e.PROJECT+'spec: [broken\n');c.send('workspace/didChangeWatchedFiles',{'changes':[]});c.change(file,text,2);c.diag(file,2);assert query(c,file,text,'project: te')==[]
 return c,{'noCrossScopeOrDamagedDeclaration':True}
run('scope-and-damaged-declaration-isolation',scope_boundary)
def duplicates(out,root):
 text=SOURCES.replace('    - repoURL: chart','    - {repoURL: y, ref: values}\n    - repoURL: chart');file=e.write(root,'app.yaml',text);c=e.LSP(out,[root]);c.open(file,text);c.diag(file,1);items=query(c,file,text,'$va');assert [x['label'] for x in items]==['values','values'];assert items[0]['detail']!=items[1]['detail'];assert file.as_uri() in items[0]['detail'] and file.as_uri() in items[1]['detail']
 return c,{'ambiguousOrigins':items}
run('duplicate-source-ref-origins',duplicates)
def scalar_safety(out,root):
 e.write(root,'project.yaml',e.PROJECT.replace('name: team','name: "true"'));text=e.APP.replace('project: team','project: tr');file=e.write(root,'app.yaml',text);c=e.LSP(out,[root]);c.open(file,text);c.diag(file,1);items=query(c,file,text,'project: tr');item=next(x for x in items if x['label']=='true');edit=item['textEdit'];assert edit['newText']=='"true"'
 c.send('textDocument/didChange',{'textDocument':{'uri':file.as_uri(),'version':2},'contentChanges':[{'range':edit['range'],'text':edit['newText']}]});ds=c.diag(file,2);assert not any(d['severity']==1 for d in ds)
 return c,{'completion':item,'after':ds}
run('yaml-literal-project-name-remains-string',scalar_safety)
def unsafe_projection(out,root):
 e.write(root,'project.yaml',e.PROJECT);base=e.APP.replace('project: team','project: te');file=e.write(root,'app.yaml',base);c=e.LSP(out,[root]);observed=[]
 for version,text in enumerate([base+'  project: other\n',base.replace('project: te','project: !custom te'),base.replace('project: te','project: "{{.project}}"')],1):
  if version==1:c.open(file,text)
  else:c.change(file,text,version)
  c.diag(file,version);needle='project: te' if version==1 else ('!custom te' if version==2 else '{{.project');items=query(c,file,text,needle);assert items==[];observed.append(items)
 return c,{'unsafeCandidates':observed}
run('no-recovery-for-duplicate-tag-or-dynamic',unsafe_projection)
def empty_project(out,root):
 e.write(root,'project.yaml',e.PROJECT);text=e.APP.replace('project: team','project: ');file=e.write(root,'app.yaml',text);c=e.LSP(out,[root]);c.open(file,text);c.diag(file,1);items=query(c,file,text,'project: ');assert [x['label'] for x in items]==['team'];edit=items[0]['textEdit'];assert edit['range']['start']==edit['range']['end']==at(text,'project: ')
 c.send('textDocument/didChange',{'textDocument':{'uri':file.as_uri(),'version':2},'contentChanges':[{'range':edit['range'],'text':edit['newText']}]});ds=c.diag(file,2);assert not any(x['severity']==1 for x in ds)
 return c,{'insertion':edit,'after':ds}
run('empty-project-scalar-insertion',empty_project)
(e.OUT/'report.json').write_text(json.dumps({'command':sys.argv,'python':sys.version,'binarySHA256':e.hash_file(e.BINARY),'testSHA256':e.hash_file(pathlib.Path(__file__)),'clientSHA256':e.hash_file(pathlib.Path(e.__file__)),'expectedSHA256':e.hash_file(pathlib.Path(__file__)),'cases':e.ROWS},indent=2)+'\n');sys.exit(0 if len(e.ROWS)==10 and all(r['passed'] for r in e.ROWS) else 1)
