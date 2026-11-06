#!/usr/bin/env python3
"""Observe read-query cancellation independently, then stale epoch delivery."""
import hashlib,importlib.util,json,os,pathlib,shutil,sys,time
HERE=pathlib.Path(__file__).resolve().parent
BINARY=pathlib.Path(sys.argv[1] if len(sys.argv)>1 else os.environ.get('CD_AUDIT_BINARY',str(HERE.parent/'build/argocd-ls'))).resolve()
OUT=pathlib.Path(sys.argv[2] if len(sys.argv)>2 else os.environ.get('CD_AUDIT_OUTPUT',str(HERE.parent/'artifacts/candidate-concurrency'))).resolve();OUT.mkdir(parents=True,exist_ok=len(sys.argv)<=2)
os.environ['CD_AUDIT_BINARY']=str(BINARY)
spec=importlib.util.spec_from_file_location('client',HERE/'audit_repair_e2e.py');e=importlib.util.module_from_spec(spec);spec.loader.exec_module(e)
root=OUT/'workspace'
if root.exists():shutil.rmtree(root)
root.mkdir()
e.write(root,'.argocd-ls.yaml',e.CONFIG);e.write(root,'project.yaml',e.PROJECT)
# Many short comments keep scalar/document budgets valid while making recovery
# reparse nontrivial. The damaged suffix never becomes a declaration.
padding=('# '+('x'*62)+'\n')*2048
text=padding+e.APP.replace('project: team','project: te')+'  broken: [\n'
file=e.write(root,'app.yaml',text);line=text[:text.index('project: te')].count('\n')
p={'line':line,'character':13};params={'textDocument':{'uri':file.as_uri()},'position':p}
c=e.LSP(OUT,[root]);rows=[];observed={};error=None
def await_diag(version):
    predicate=lambda m:m.get('method')=='textDocument/publishDiagnostics' and m.get('params',{}).get('uri')==file.as_uri() and m['params'].get('version')==version and m['params'].get('x-argocd-analysis-scope','workspace')=='workspace'
    for item in c.messages:
        if predicate(item['message']):return item['message']
    return c.wait(predicate)
try:
    c.open(file,text,1);await_diag(1)
    start=time.monotonic();baseline=c.request('textDocument/completion',params);observed['uncancelledSeconds']=time.monotonic()-start;observed['uncancelled']=baseline
    assert [x['label'] for x in baseline.get('result',[])]==['team'],'positive query control must work'
    for n in range(3):
        ident=500+n;start=time.monotonic();c.send('textDocument/completion',params,ident);c.send('$/cancelRequest',{'id':ident})
        response=c.wait(lambda m:m.get('id')==ident,timeout=.2);elapsed=time.monotonic()-start
        rows.append({'id':f'cancel-{n}','passed':response.get('error',{}).get('code')==-32800 and elapsed<=.2,'response':response,'seconds':elapsed})
    c.send('textDocument/completion',params,600)
    c.send('textDocument/didChange',{'textDocument':{'uri':file.as_uri(),'version':2},'contentChanges':[{'range':{'start':{'line':line,'character':11},'end':{'line':line,'character':13}},'text':'zz'}]})
    response=c.wait(lambda m:m.get('id')==600,timeout=10)
    rows.append({'id':'change-invalidates-pending-query','passed':response.get('error',{}).get('code')==-32801,'response':response})
    await_diag(2);current=c.request('textDocument/completion',params);observed['currentQuery']=current
    rows.append({'id':'current-epoch-query','passed':current.get('result')==[] and 'error' not in current,'response':current})
    c.send('textDocument/didChange',{'textDocument':{'uri':file.as_uri(),'version':3},'contentChanges':[{'range':{'start':{'line':line,'character':11},'end':{'line':line,'character':13}},'text':'te'}]})
    await_diag(3);again=c.request('textDocument/completion',params);observed['recoveredQuery']=again
    rows.append({'id':'normal-query-after-interleaving','passed':[x['label'] for x in again.get('result',[])]==['team'],'response':again})
except Exception as ex:error=repr(ex)
finally:c.close()
counts={str(i):sum(x['message'].get('id')==i for x in c.messages) for i in [500,501,502,600]}
rows.append({'id':'single-terminal-response','passed':all(v==1 for v in counts.values()),'counts':counts})
report={'command':sys.argv,'binarySHA256':hashlib.sha256(BINARY.read_bytes()).hexdigest(),'testSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'python':sys.version,'expected':'positive candidate; cancellation -32800 <=200ms before shutdown; stale epoch -32801; current queries work; one terminal reply per ID','observed':observed,'cases':rows,'exception':error,'exit':c.proc.returncode,'inputSHA256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()},'passed':error is None and len(rows)==7 and all(r['passed'] for r in rows) and c.proc.returncode==0}
(OUT/'report.json').write_text(json.dumps(report,indent=2)+'\n');(OUT/'concurrency_e2e.py.txt').write_bytes(pathlib.Path(__file__).read_bytes());print(json.dumps(report,indent=2));sys.exit(0 if report['passed'] else 1)
