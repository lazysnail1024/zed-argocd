#!/usr/bin/env python3
"""Selected source scope controls real Helm CLI and editor render commands."""
import json,os,pathlib,subprocess,sys
import audit_repair_e2e as e
import argparse, shutil
parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--helm',type=pathlib.Path);args=parser.parse_args()
if args.helm:os.environ['PATH']=str(args.helm.resolve().parent)+os.pathsep+os.environ.get('PATH','')
e.OUT=pathlib.Path(os.environ.get('CD_AUDIT_OUTPUT',str(e.HERE.parent/'artifacts/render-scope'))).resolve();e.OUT.mkdir(parents=True,exist_ok=True)
CONFIG='schemaVersion: 1\nscopes:\n  - name: chart\n    include: ["chart/**"]\n    targetVersion: "3.0.0"\n  - name: other\n    include: ["other/**"]\n    targetVersion: "2.0.0"\n'
name='selected-file-determines-render-target';out,root=e.setup(name,config=CONFIG);c=None
try:
 e.write(root,'chart/Chart.yaml','apiVersion: v2\nname: scope-check\nversion: 0.1.0\n');e.write(root,'chart/values.yaml','project: team\n');text=e.APP.replace('project: team','project: {{ .Values.project }}');f=e.write(root,'chart/templates/app.yaml',text)
 cmd=[str(e.BINARY),'render-check','--root',str(root),'--file',str(f),'--format','json'];p=subprocess.run(cmd,capture_output=True,timeout=45);(out/'cli.stdout').write_bytes(p.stdout);(out/'cli.stderr').write_bytes(p.stderr);r=json.loads(p.stdout);assert p.returncode==0 and r['analysisComplete'] and r['resourcesAnalyzed']==1,(p.returncode,r);assert {t['version'] for t in r['targets']}=={'3.0.0'},r['targets']
 c=e.LSP(out,[root]);c.open(f,text);c.diag(f,1);actions=c.request('textDocument/codeAction',{'textDocument':{'uri':f.as_uri()},'range':{'start':{'line':0,'character':0},'end':{'line':0,'character':0}},'context':{'diagnostics':[]}});action=next(a for a in actions['result'] if a.get('command',{}).get('command')=='argocd.renderCheck');response=c.request('workspace/executeCommand',{'command':action['command']['command'],'arguments':action['command']['arguments']});assert 'error' not in response,response;report=json.loads(pathlib.Path(response['result']['report']).read_text());assert report['analysisComplete'] and {t['version'] for t in report['targets']}=={'3.0.0'},report
 negatives=[]
 for label,flags,config in [('explicit-mismatch',['--scope','other'],CONFIG),('overlap',[],CONFIG.replace('other/**','chart/**'))]:
  e.write(root,'.argocd-ls.yaml',config);argv=cmd+flags+['--output',str(out/label)];failed=subprocess.run(argv,capture_output=True,timeout=45);(out/(label+'.stdout')).write_bytes(failed.stdout);(out/(label+'.stderr')).write_bytes(failed.stderr);assert failed.returncode==2 and not (out/label/'generated.yaml').exists(),(label,failed.returncode,failed.stderr);negatives.append({'name':label,'command':argv,'exit':failed.returncode})
 e.record(name,{'selectedTarget':'3.0.0','CLIAndActionSucceed':True,'scopeMismatchAndOverlapRefused':True},{'cliCommand':cmd,'cliExit':p.returncode,'action':response,'negativeCommands':negatives},True,root,c)
except Exception as ex:
 for client in e.CLIENTS:
  if not client.closed:client.close()
 e.record(name,{'selectedTarget':'3.0.0','CLIAndActionSucceed':True},{'exception':repr(ex)},False,root)
(e.OUT/'report.json').write_text(json.dumps({'command':sys.argv,'python':sys.version,'binarySHA256':e.hash_file(e.BINARY),'testSHA256':e.hash_file(pathlib.Path(__file__)),'clientSHA256':e.hash_file(pathlib.Path(e.__file__)),'helmSHA256':e.hash_file(pathlib.Path(shutil.which('helm')).resolve()),'cases':e.ROWS},indent=2)+'\n');sys.exit(0 if e.ROWS and all(r['passed'] for r in e.ROWS) else 1)
