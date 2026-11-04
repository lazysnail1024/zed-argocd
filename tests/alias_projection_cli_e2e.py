#!/usr/bin/env python3
"""Real CLI projection values, refusal, ownership and truthful accounting."""
import hashlib,json,pathlib,subprocess,sys
import audit_repair_e2e as fixtures
binary=pathlib.Path(sys.argv[1]).resolve();out=pathlib.Path(sys.argv[2]).resolve();out.mkdir(parents=True,exist_ok=False);source=pathlib.Path(__file__).with_name('alias-projection-cases.json');cases=json.loads(source.read_text())['cases'];rows=[]
for case in cases:
 root=out/case['id'];root.mkdir();text=case['text'].replace('SCALAR_BOUNDARY_VALUE','x'*case['scalarBytes']) if 'scalarBytes' in case else case['text'];(root/'input.yaml').write_text(text);(root/'project.yaml').write_text(fixtures.PROJECT);(root/'.argocd-ls.yaml').write_text(fixtures.CONFIG.replace('3.0.0','3.5.6'));cmd=[str(binary),'check','--root',str(root),'--format','json'];row={'id':case['id'],'command':cmd,'expected':case}
 try:
  p=subprocess.run(cmd,capture_output=True,timeout=8);(root/'stdout').write_bytes(p.stdout);(root/'stderr').write_bytes(p.stderr);d=json.loads(p.stdout);checks={'exit':p.returncode==(2 if not case['projectionComplete'] else 1 if any(x!='SCHEMA_UNKNOWN_FIELD' for x in case['expectedCodes']) else 0),'complete':d['analysisComplete']==case['projectionComplete'],'configuration':d['configurationErrors']==[],'exactCodes':sorted(x['code'] for x in d['diagnostics'])==sorted(case['expectedCodes']),'resources':d['resourcesAnalyzed']==case['resources']+1,'files':d['filesAnalyzed']==(3 if case['projectionComplete'] else 2)};row.update(checks=checks,passed=all(checks.values()),observed=d)
 except Exception as e:
  if isinstance(e,subprocess.TimeoutExpired):(root/'stdout').write_bytes(e.stdout or b'');(root/'stderr').write_bytes(e.stderr or b'')
  row.update(passed=False,error=repr(e))
 rows.append(row)
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest();report={'binarySHA256':sha(binary),'runnerSHA256':sha(pathlib.Path(__file__)),'casesSHA256':sha(source),'rows':rows,'passed':bool(rows) and all(x['passed'] for x in rows)};(out/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'rows':len(rows),'failed':[x['id'] for x in rows if not x['passed']]}));sys.exit(0 if report['passed'] else 1)
