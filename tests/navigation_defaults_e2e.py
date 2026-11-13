#!/usr/bin/env python3
"""Protocol oracles for candidate-only projects and physical generator templates."""
import json, os, pathlib, sys
import audit_repair_e2e as e
e.OUT=pathlib.Path(os.environ.get('CD_AUDIT_OUTPUT',str(e.HERE.parent/'artifacts/navigation-defaults')));e.OUT.mkdir(parents=True,exist_ok=True)
if len(sys.argv)==3:
 e.BINARY=pathlib.Path(sys.argv[1]).resolve();e.OUT=pathlib.Path(sys.argv[2]).resolve();e.OUT.mkdir(parents=True,exist_ok=True)

def pos(text, needle):
 offset=text.index(needle)+len(needle)-1;prefix=text[:offset]
 return {'line':prefix.count('\n'),'character':len(prefix.rsplit('\n',1)[-1].encode('utf-16-le'))//2}
def request(c,method,file,text,needle,**extra):
 r=c.request('textDocument/'+method,dict(textDocument={'uri':file.as_uri()},position=pos(text,needle),**extra));assert 'error' not in r,r;return r['result']
def run(name,fn):
 out,root=e.setup(name,config='schemaVersion: 1\ntargetVersion: "3.0.0"\n');c=None
 try:
  c,observed=fn(out,root);e.record(name,{'contract':name},observed,True,root,c)
 except Exception as ex:
  for client in e.CLIENTS:
   if not client.closed:client.close()
  e.record(name,{'contract':name},{'exception':repr(ex)},False,root)
def defaults(out,root):
 (root/'.argocd-ls.yaml').unlink();a=e.write(root,'one.yaml',e.PROJECT);b=e.write(root,'two.yaml',e.PROJECT.replace('namespace: argocd','namespace: other'));f=e.write(root,'app.yaml',e.APP);c=e.LSP(out,[root]);c.open(f,e.APP);ds=c.diag(f,1)
 defs=request(c,'definition',f,e.APP,'project: team');assert {d['uri'] for d in defs}=={a.as_uri(),b.as_uri()},defs
 assert not any(d['severity']<=2 and d['code']=='CD_PROJECT_AMBIGUOUS' for d in ds),ds
 for include in [False,True]:assert request(c,'references',f,e.APP,'project: team',context={'includeDeclaration':include})==[]
 return c,{'definitions':defs,'diagnostics':ds}
run('unbound-project-all-local-candidates-no-authority',defaults)
BASE='''apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata: {name: apps}
spec:
  generators:
    - list:
        elements: [{name: demo}]
        template:
          metadata: {name: generated}
          spec:
            project: team
            sources:
              - {repoURL: https://example.org/config, ref: values}
              - repoURL: https://example.org/chart
                helm:
                  valueFiles: ["$values/team.yaml"]
            destination: {server: https://kubernetes.default.svc, namespace: default}
  template:
    metadata: {name: base}
    spec:
      project: base
      source: {repoURL: https://example.org/base}
      destination: {server: https://kubernetes.default.svc, namespace: default}
'''
def generator(out,root):
 p=e.write(root,'project.yaml',e.PROJECT);f=e.write(root,'set.yaml',BASE);c=e.LSP(out,[root]);c.open(f,BASE);c.diag(f,1)
 project=request(c,'definition',f,BASE,'project: team');assert len(project)==1 and project[0]['uri']==p.as_uri(),project
 refs=request(c,'definition',f,BASE,'$values/team.yaml');assert len(refs)==1 and refs[0]['uri']==f.as_uri() and refs[0]['range']['start']==pos(BASE,'ref: v'),refs
 return c,{'project':project,'values':refs}
run('generator-list-physical-project-and-values-definitions',generator)
def pinned(out,root):
 e.write(root,'.argocd-ls.yaml',e.CONFIG);a=e.write(root,'one.yaml',e.PROJECT);e.write(root,'two.yaml',e.PROJECT.replace('namespace: argocd','namespace: other'));f=e.write(root,'app.yaml',e.APP);c=e.LSP(out,[root]);c.open(f,e.APP);c.diag(f,1);defs=request(c,'definition',f,e.APP,'project: team');assert len(defs)==1 and defs[0]['uri']==a.as_uri();return c,{'definitions':defs}
run('explicit-namespace-still-filters',pinned)
(e.OUT/'report.json').write_text(json.dumps({'command':sys.argv,'python':sys.version,'binarySHA256':e.hash_file(e.BINARY),'testSHA256':e.hash_file(pathlib.Path(__file__)),'clientSHA256':e.hash_file(pathlib.Path(e.__file__)),'cases':e.ROWS},indent=2)+'\n');sys.exit(0 if len(e.ROWS)==3 and all(r['passed'] for r in e.ROWS) else 1)
