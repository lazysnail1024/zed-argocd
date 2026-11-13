#!/usr/bin/env python3
"""Independent source-position and refusal oracles through the actual LSP."""
import json, os, pathlib, sys
import audit_repair_e2e as e
e.OUT=pathlib.Path(os.environ.get('CD_AUDIT_OUTPUT',str(e.HERE.parent/'artifacts/helm-source')));e.OUT.mkdir(parents=True,exist_ok=True)
if len(sys.argv)==3:
 e.BINARY=pathlib.Path(sys.argv[1]).resolve();e.OUT=pathlib.Path(sys.argv[2]).resolve();e.OUT.mkdir(parents=True,exist_ok=True)

def pos(text,needle):
 offset=text.index(needle)+len(needle)-1;prefix=text[:offset];return {'line':prefix.count('\n'),'character':len(prefix.rsplit('\n',1)[-1].encode('utf-16-le'))//2}
def query(c,method,file,text,needle):
 r=c.request('textDocument/'+method,{'textDocument':{'uri':file.as_uri()},'position':pos(text,needle)});assert 'error' not in r,r;return r['result']
def run(name,text,expect,foreign=False):
 out,root=e.setup(name,config='schemaVersion: 1\ntargetVersion: "3.0.0"\n');c=None
 try:
  p=e.write(root,'project.yaml',e.PROJECT);e.write(root,'chart/Chart.yaml','apiVersion: v2\nname: chart\nversion: 0.1.0\n');f=e.write(root,'chart/templates/app.yaml',text);c=e.LSP(out,[root]);c.open(f,text);ds=c.diag(f,1)
  defs=query(c,'definition',f,text,'project: team');assert (len(defs)==1 and defs[0]['uri']==p.as_uri()) if expect else defs==[],defs
  if expect:
   refs=query(c,'definition',f,text,'$values/a.yaml');assert len(refs)==1 and refs[0]['range']['start']==pos(text,'ref: v'),refs
   assert any(d['code']=='HELM_UNRENDERED' for d in ds)==('{{' in text),ds
  if foreign:assert not any(d['code']=='HELM_UNRENDERED' for d in ds),ds
  actions=c.request('textDocument/codeAction',{'textDocument':{'uri':f.as_uri()},'range':{'start':pos(text,'project: team'),'end':pos(text,'project: team')},'context':{'diagnostics':[]}});assert 'error' not in actions,actions
  assert all('edit' not in a and 'data' not in a for a in actions.get('result',[])),actions
  if name in ['structural-if-refuses-navigation','unknown-include-refuses-navigation']:assert any(a.get('command',{}).get('command')=='argocd.renderCheck' for a in actions['result']),actions
  if foreign:assert actions.get('result')==[],actions
  e.record(name,{'definitionsAvailable':expect,'foreignSilent':foreign,'noSourceEdits':True},{'definitions':defs,'diagnostics':ds,'actions':actions},True,root,c)
 except Exception as ex:
  for client in e.CLIENTS:
   if not client.closed:client.close()
  e.record(name,{'definitionsAvailable':expect},{'exception':repr(ex)},False,root)
APP='''apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: literal
  annotations: {description: "prefix {{ .Values.note }} suffix"}
spec:
  project: team
  sources:
    - {repoURL: https://example.org/config, ref: values}
    - repoURL: https://example.org/chart
      helm:
        valueFiles: ["$values/a.yaml"]
  destination: {server: https://kubernetes.default.svc, namespace: default}
'''
run('static-helm-fields-exact-source',APP,True)
run('escaped-appset-tags-preserve-static-neighbors',APP.replace('{{ .Values.note }}','{{ `{{.note}}` }}'),True)
run('structural-if-refuses-navigation','{{ if .Values.enabled }}\n'+APP+'{{ end }}\n',False)
run('unknown-include-refuses-navigation',APP.replace('{{ .Values.note }}','{{ include "fragment" . }}'),False)
run('dynamic-project-refuses-binding',APP.replace('project: team','project: team{{ .Values.suffix }}'),False)
run('foreign-workflow-silent',APP.replace('kind: Application','kind: Workflow'),False,True)
run('plain-yaml-in-chart-is-normal',APP.replace('{{ .Values.note }}','plain'),True)
run('dynamic-key-refuses-projection',APP.replace('  project: team','  {{ .Values.key }}: other\n  project: team'),False)
run('unicode-action-before-static-ref-crlf',APP.replace('ref: values','note: "{{ `}}\U0001f300` }}", ref: values').replace('\n','\r\n'),True)
(e.OUT/'report.json').write_text(json.dumps({'command':sys.argv,'python':sys.version,'binarySHA256':e.hash_file(e.BINARY),'testSHA256':e.hash_file(pathlib.Path(__file__)),'clientSHA256':e.hash_file(pathlib.Path(e.__file__)),'cases':e.ROWS},indent=2)+'\n');sys.exit(0 if len(e.ROWS)==9 and all(r['passed'] for r in e.ROWS) else 1)
