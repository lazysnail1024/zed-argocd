#!/usr/bin/env python3
"""Real server survives a budget-exceeding parse then analyzes legal deep and ordinary text."""
import pathlib,json,sys
import audit_repair_e2e as e
e.OUT=pathlib.Path(__file__).resolve().parents[1]/'artifacts/dependency-depth';e.OUT.mkdir(parents=True,exist_ok=True)
name='deep-budget-and-recovery';out,root=e.setup(name);file=e.write(root,'app.yaml',e.APP)
c=e.LSP(out,[root])
try:
 c.open(file,e.APP);c.diag(file,1)
 deep='apiVersion: argoproj.io/v1alpha1\nkind: AppProject\n'+''.join('  '*i+'x:\n' for i in range(300))
 e.write(root,'deep-input.txt',deep);c.change(file,deep,2)
 c.wait(lambda m:m.get('method')=='textDocument/publishDiagnostics' and m.get('params',{}).get('version')==2)
 legal='apiVersion: argoproj.io/v1alpha1\nkind: AppProject\nmetadata: {name: legal}\nspec:\n'+''.join('  '*(i+1)+'x:\n' for i in range(16))
 e.write(root,'legal-input.txt',legal);c.change(file,legal,3);ds=c.diag(file,3)
 symbols=c.request('textDocument/documentSymbol',{'textDocument':{'uri':file.as_uri()}})
 c.change(file,e.APP,4);c.diag(file,4);recovered=c.request('textDocument/documentSymbol',{'textDocument':{'uri':file.as_uri()}})
 passed=any(x.get('name')=='legal' for x in symbols.get('result',[])) and any(x.get('name')=='app' for x in recovered.get('result',[])) and not any(x['code'] in ['INPUT_INCOMPLETE','ANALYSIS_INCOMPLETE'] for x in ds)
 e.record(name,{'deepBudget':'process survives; current input not treated complete','legalDepth16':'symbol legal','recovered':'symbol app in same process'},{'legalDiagnostics':ds,'legalSymbols':symbols,'recovered':recovered},passed,root,c)
except Exception as exc:
 c.close();e.record(name,{'serverSurvivesAndRecovers':True},{'exception':repr(exc)},False,root)
(out/'report.json').write_text(json.dumps({'command':sys.argv,'python':sys.version,'testSHA256':e.hash_file(pathlib.Path(__file__)),'clientSHA256':e.hash_file(pathlib.Path(e.__file__)),'binarySHA256':e.hash_file(e.BINARY),'cases':e.ROWS},indent=2)+'\n');sys.exit(0 if e.ROWS and all(x['passed'] for x in e.ROWS) else 1)
