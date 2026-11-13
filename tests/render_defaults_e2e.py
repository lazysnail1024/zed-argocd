#!/usr/bin/env python3
"""Real Helm CLI and explicit LSP command acceptance, including disk authority."""
import json,os,pathlib,subprocess,sys,shutil
import audit_repair_e2e as e
import argparse, shutil
parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--helm',type=pathlib.Path);args=parser.parse_args()
if args.helm:os.environ['PATH']=str(args.helm.resolve().parent)+os.pathsep+os.environ.get('PATH','')
e.OUT=pathlib.Path(os.environ.get('CD_AUDIT_OUTPUT',str(e.HERE.parent/'artifacts/render-defaults')));e.OUT.mkdir(parents=True,exist_ok=True)
name='defaults-and-explicit-action';out,root=e.setup(name,config='schemaVersion: 1\ntargetVersion: "3.0.0"\n');(root/'.argocd-ls.yaml').unlink();c=None
try:
 chart=root/'chart';e.write(root,'chart/Chart.yaml','apiVersion: v2\nname: render-defaults\nversion: 0.1.0\n');e.write(root,'chart/values.yaml','project: team\n');text=e.APP.replace('project: team','project: {{ .Values.project }}');f=e.write(root,'chart/templates/app.yaml',text)
 rows=[]
 for label,args,cwd in [('file',['--root',str(root),'--file',str(f)],root),('cwd',[],chart),('nested-cwd',[],chart/'templates'),('root',['--root',str(chart)],root)]:
  cmd=[str(e.BINARY),'render-check',*args,'--format','json'];p=subprocess.run(cmd,cwd=cwd,capture_output=True,timeout=45);(out/(label+'.stdout')).write_bytes(p.stdout);(out/(label+'.stderr')).write_bytes(p.stderr);r=json.loads(p.stdout)
  assert p.returncode in [0,1] and r['analysisComplete'] and r['resourcesAnalyzed']==1,(p.returncode,r)
  generated=pathlib.Path(r['renderer']['output']);assert generated.is_file() and 'project: team' in generated.read_text();assert r['renderer']['chart']==str(chart) and r['renderer']['values']==''
  rows.append({'command':cmd,'cwd':str(cwd),'exit':p.returncode,'output':str(generated),'outputSHA256':e.hash_file(generated),'renderer':r['renderer']})
 c=e.LSP(out,[root]);c.open(f,text);c.diag(f,1);params={'textDocument':{'uri':f.as_uri()},'range':{'start':{'line':0,'character':0},'end':{'line':0,'character':0}},'context':{'diagnostics':[]}};a=c.request('textDocument/codeAction',params);action=next(x for x in a['result'] if x.get('command',{}).get('command')=='argocd.renderCheck');assert 'edit' not in action and 'data' not in action
 resolved=c.request('codeAction/resolve',action);assert resolved.get('result')==action,resolved
 command=action['command'];response=c.request('workspace/executeCommand',{'command':command['command'],'arguments':command['arguments']});assert 'error' not in response,response
 generated=pathlib.Path(response['result']['output']);assert generated.is_file();assert pathlib.Path(response['result']['report']).is_file();show=c.wait(lambda m:m.get('method')=='window/showDocument');assert show['params']['uri']==generated.as_uri();c.send('',None,show['id'])
 c.change(f,text.replace('{{ .Values.project }}','unsaved-team'),2);c.diag(f,2);a=c.request('textDocument/codeAction',params);action=next(x for x in a['result'] if x.get('command',{}).get('command')=='argocd.renderCheck');response=c.request('workspace/executeCommand',{'command':action['command']['command'],'arguments':action['command']['arguments']});assert 'error' in response and 'save' in response['error']['message'].lower(),response
 e.record(name,{'automaticChartHelmOutput':True,'explicitAction':True,'unsavedRefused':True},{'cli':rows,'action':action,'unsaved':response},True,root,c)
except Exception as ex:
 for client in e.CLIENTS:
  if not client.closed:client.close()
 e.record(name,{'automaticChartHelmOutput':True,'explicitAction':True,'unsavedRefused':True},{'exception':repr(ex)},False,root)
(e.OUT/'report.json').write_text(json.dumps({'command':sys.argv,'python':sys.version,'binarySHA256':e.hash_file(e.BINARY),'helmSHA256':e.hash_file(pathlib.Path(shutil.which('helm')).resolve()),'testSHA256':e.hash_file(pathlib.Path(__file__)),'clientSHA256':e.hash_file(pathlib.Path(e.__file__)),'cases':e.ROWS},indent=2)+'\n');sys.exit(0 if e.ROWS and all(r['passed'] for r in e.ROWS) else 1)
