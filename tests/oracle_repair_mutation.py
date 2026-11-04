#!/usr/bin/env python3
"""Mutate real CLI report envelopes to audit the existing AppSet oracle."""
import hashlib,json,pathlib,shutil,subprocess,sys
root=pathlib.Path(__file__).resolve().parents[1]
binary=root/'build/argocd-ls';out=root/'artifacts/oracle-repair-mutants';out.mkdir(exist_ok=True);(root/'artifacts').mkdir(exist_ok=True)
mutations={
 'baseline':'',
 'false-complete':"report['analysisComplete']=True;report['configurationErrors']=[];report['notEvaluated']=[];exit_code=0",
 'unexpected-error':"report['diagnostics'].append({'uri':report['diagnostics'][0]['uri'] if report['diagnostics'] else '', 'range':{'start':{'line':0,'character':0},'end':{'line':0,'character':1}},'code':'SCHEMA_TYPE','source':'argocd-ls','severity':1,'message':'injected false-positive error'})",
 'drop-diagnostics':"report['diagnostics']=[]",
}
rows=[]
for name,mutation in mutations.items():
 folder=out/name;folder.mkdir(exist_ok=True);wrapper=folder/'report_wrapper.py'
 wrapper.write_text('#!/usr/bin/env python3\nimport json,subprocess,sys\np=subprocess.run('+repr([str(binary)])+'+sys.argv[1:],capture_output=True)\nexit_code=p.returncode\nreport=json.loads(p.stdout)\n'+mutation+'\nsys.stdout.write(json.dumps(report)+"\\n")\nsys.stderr.buffer.write(p.stderr)\nsys.exit(exit_code)\n');wrapper.chmod(0o755)
 cmd=[sys.executable,str(root/'tests/appset_e2e.py'),str(wrapper)]
 p=subprocess.run(cmd,cwd=root,capture_output=True,timeout=40);(folder/'oracle.stdout').write_bytes(p.stdout);(folder/'oracle.stderr').write_bytes(p.stderr)
 shutil.copytree(root/'artifacts/appset',folder/'evidence',dirs_exist_ok=True)
 results=json.loads((folder/'evidence/results.json').read_text())
 row={'variant':name,'command':cmd,'oracleExit':p.returncode,'expectedOracleExit':0 if name=='baseline' else 1,'passedCases':sum(r['passed'] for r in results),'caseCount':len(results),'wrapperSHA256':hashlib.sha256(wrapper.read_bytes()).hexdigest()}
 rows.append(row);print(json.dumps(row),flush=True)
report={'binarySHA256':hashlib.sha256(binary.read_bytes()).hexdigest(),'oracleSHA256':hashlib.sha256((root/'tests/appset_e2e.py').read_bytes()).hexdigest(),'runnerSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'python':sys.version,'rows':rows}
(out/'report.json').write_text(json.dumps(report,indent=2)+'\n')

sys.exit(0 if rows and all(r['oracleExit']==r['expectedOracleExit'] for r in rows) else 1)
