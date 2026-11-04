#!/usr/bin/env python3
"""Excluded subtrees, referenced repository paths, and live path changes."""
import argparse,json,pathlib,subprocess,sys
import audit_repair_e2e as w
p=argparse.ArgumentParser();p.add_argument('binary',type=pathlib.Path);p.add_argument('output',type=pathlib.Path);a=p.parse_args();w.BINARY=a.binary.resolve();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);r=out/'workspace';r.mkdir();cfg=w.CONFIG+'exclude: ["generated/**"]\nrepositories: [{repoURL: https://example.org/repo, path: .}]\n';w.write(r,'.argocd-ls.yaml',cfg);w.write(r,'project.yaml',w.PROJECT);generated=r/'generated';generated.mkdir();(generated/'existing').mkdir();(generated/'other').mkdir()
for i in range(50001):(generated/str(i)).touch()
text=w.APP.replace('repoURL: https://example.org/repo','repoURL: https://example.org/repo, path: generated/existing');f=w.write(r,'app.yaml',text);rows=[];c=None
try:
 cmd=[str(w.BINARY),'check','--root',str(r),'--format','json'];q=subprocess.run(cmd,capture_output=True);(out/'cli.stdout').write_bytes(q.stdout);(out/'cli.stderr').write_bytes(q.stderr);report=json.loads(q.stdout);assert q.returncode==0 and report['analysisComplete'] and report['resourcesAnalyzed']==2;rows.append({'id':'excluded-reference-exists','passed':True,'command':cmd,'exit':q.returncode})
 c=w.LSP(out,[r]);c.open(f,text);c.diag(f,1)
 for version,path,missing in [(2,'generated/absent',True),(3,'generated/other',False),(4,'generated/existing',False)]:
  value=text.replace('generated/existing',path);c.change(f,value,version);ds=c.diag(f,version);assert any(d['code']=='CD_LOCAL_PATH_MISSING' for d in ds)==missing,ds;rows.append({'id':path,'passed':True,'diagnostics':ds})
 c.close();w.write(r,'.argocd-ls.yaml',cfg.replace('exclude: ["generated/**"]\n',''));q=subprocess.run(cmd,capture_output=True);(out/'budget.stdout').write_bytes(q.stdout);(out/'budget.stderr').write_bytes(q.stderr);report=json.loads(q.stdout);assert q.returncode==2 and not report['analysisComplete'] and report['configurationErrors'];rows.append({'id':'unexcluded-budget-refusal','passed':True})
except Exception as e:rows.append({'id':'runner','passed':False,'error':repr(e)})
finally:
 if c:c.close()
 (out/'report.json').write_text(json.dumps({'binarySHA256':w.hash_file(w.BINARY),'runnerSHA256':w.hash_file(pathlib.Path(__file__)),'rows':rows},indent=2)+'\n')
print(json.dumps(rows));sys.exit(len(rows)!=5 or not all(x['passed'] for x in rows))
