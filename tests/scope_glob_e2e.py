#!/usr/bin/env python3
"""Real CLI scope glob membership, including literal-prefix near misses."""
import hashlib,json,pathlib,subprocess,sys
binary=pathlib.Path(sys.argv[1]).resolve();out=pathlib.Path(sys.argv[2]).resolve();out.mkdir(parents=True,exist_ok=False)
files=['dev/app.yaml','dev/nested/app.yaml','developer/app.yaml','prod/app.yaml','top.yaml']
cases=[('literal','dev/**',['dev/app.yaml','dev/nested/app.yaml']),('near-miss','de/**',[]),('star','dev*/*',['dev/app.yaml','developer/app.yaml']),('question','de?/**',['dev/app.yaml','dev/nested/app.yaml']),('class','[dp]*/app.yaml',['dev/app.yaml','developer/app.yaml','prod/app.yaml']),('recursive','**/app.yaml',files[:-1]),('single','top.yaml',['top.yaml'])]
rows=[]
for name,pattern,expected in cases:
 folder=out/name;folder.mkdir();(folder/'.argocd-ls.yaml').write_text('schemaVersion: 1\ntargetVersion: "3.5.6"\nscopes:\n  - name: selected\n    include: ['+json.dumps(pattern)+']\n')
 for f in files:
  p=folder/f;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata: {name: item}\nspec: {project: 42}\n')
 cmd=[str(binary),'check','--root',str(folder),'--scope','selected','--format','json'];row={'id':name,'command':cmd,'expected':expected}
 try:
  p=subprocess.run(cmd,capture_output=True,timeout=12);(folder/'stdout').write_bytes(p.stdout);(folder/'stderr').write_bytes(p.stderr);d=json.loads(p.stdout);actual=sorted({pathlib.Path(x['uri'].removeprefix('file://')).relative_to(folder).as_posix() for x in d['diagnostics'] if x['code']=='SCHEMA_TYPE'});row.update(exit=p.returncode,actual=actual,passed=p.returncode==(1 if expected else 0) and d['analysisComplete'] and actual==sorted(expected) and d['configurationErrors']==[])
 except Exception as e:row.update(passed=False,error=repr(e))
 rows.append(row)
report={'binarySHA256':hashlib.sha256(binary.read_bytes()).hexdigest(),'runnerSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'rows':rows,'passed':all(r['passed'] for r in rows)};(out/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'passed':report['passed'],'rows':len(rows)}));sys.exit(0 if report['passed'] else 1)
