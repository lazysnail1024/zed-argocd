#!/usr/bin/env python3
"""One real LSP process switches pinned semantic boundaries without stale results."""
import pathlib,json,sys,os,hashlib
import audit_repair_e2e as e
ROOT=pathlib.Path(__file__).resolve().parents[1];OUT=pathlib.Path(os.environ.get('CD_MATRIX_LSP_OUT',str(ROOT/'artifacts/domain-matrix/lsp')));OUT.mkdir(parents=True,exist_ok=True);root=OUT/'workspace';root.mkdir(exist_ok=True)
cases={c['id']:c for c in json.loads((ROOT/'tests/domain-matrix-cases.json').read_text())['cases']};sources=json.loads((ROOT/'compat/local-rules-sources.json').read_text());versions=['3.0.0','3.0.23','3.1.0','3.1.16','3.2.0','3.2.12','3.3.0','3.3.18','3.4.1','3.4.12','3.5.0','3.5.6','3.4.12'];selected=['sole-source-ref-version','invalid-ref-dot','file-parameter-ref-missing','appset-dynamic-project-static-conflict','appset-dynamic-ref-static-conflict','appset-static-patch-malformed','single-source-ignored-ref-grammar','sole-sources-unused-ref-before-boundary','multi-sources-invalid-ref-control','fast-mode-static-malformed-patch','fast-mode-dynamic-malformed-patch','fast-mode-static-valid-patch-control','fast-patch-unclosed-literal-invalid-yaml','resolved'];rows=[]
config=lambda v:'schemaVersion: 1\ntargetVersion: "'+v+'"\ncontrolPlaneNamespace: argocd\n'
e.write(root,'.argocd-ls.yaml',config(versions[0]));e.write(root,'project.yaml',json.dumps(cases['resolved']['project'],indent=2)+'\n');file=e.write(root,'input.yaml',json.dumps(cases['resolved']['application'],indent=2)+'\n');c=e.LSP(OUT,[root]);revision=1;exception=None
try:
 c.open(file,file.read_text(),revision);c.diag(file,revision)
 for target in versions:
  e.write(root,'.argocd-ls.yaml',config(target));c.send('workspace/didChangeWatchedFiles',{'changes':[{'uri':(root/'.argocd-ls.yaml').as_uri(),'type':2}]})
  for name in selected:
   fixture=cases[name];revision+=1;text=json.dumps(fixture['application'],indent=2)+'\n';c.change(file,text,revision);ds=c.diag(file,revision);want=fixture['expected35'] if target in ['3.5.0','3.5.6'] and fixture['expected35'] is not None else fixture['expected'];actual=sorted([[d['code'],d['severity']] for d in ds]);passed=actual==sorted(want)
   if name=='sole-source-ref-version':
    offset=text.index('$values')+len('$values');before=text[:offset];position={'line':before.count('\n'),'character':len(before.rsplit('\n',1)[-1])};params={'textDocument':{'uri':file.as_uri()},'position':position};definitions=c.request('textDocument/definition',params);completions=c.request('textDocument/completion',params);available=target in ['3.5.0','3.5.6'];passed=passed and len(definitions.get('result',[]))==int(available) and [x['label'] for x in completions.get('result',[])]==(['values'] if available else [])
   if name=='single-source-ignored-ref-grammar' or (name=='sole-sources-unused-ref-before-boundary' and target not in ['3.5.0','3.5.6']):
    offset=text.index('bad.ref')+len('bad.ref');before=text[:offset];params={'textDocument':{'uri':file.as_uri()},'position':{'line':before.count('\n'),'character':len(before.rsplit('\n',1)[-1])}};definitions=c.request('textDocument/definition',params);completions=c.request('textDocument/completion',params);passed=passed and definitions.get('result')==[] and completions.get('result')==[]
   rows.append({'version':target,'id':name,'revision':revision,'expected':want,'actual':actual,'passed':passed,'sourceCommit':sources['versions'][target]['upstreamCommit']})
except Exception as ex:exception=repr(ex)
finally:c.close()
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest();report={'command':sys.argv,'python':sys.version,'binarySHA256':sha(e.BINARY),'testSHA256':sha(pathlib.Path(__file__)),'expectationsSHA256':sha(ROOT/'tests/domain-matrix-cases.json'),'sourcesSHA256':sha(ROOT/'compat/local-rules-sources.json'),'expectedRows':len(versions)*len(selected),'cases':rows,'exception':exception,'exit':c.proc.returncode,'passed':len(rows)==len(versions)*len(selected) and all(r['passed'] for r in rows) and exception is None and c.proc.returncode==0};(OUT/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'rows':len(rows),'exception':exception,'failed':[r for r in rows if not r['passed']]},indent=2));sys.exit(0 if report['passed'] else 1)
