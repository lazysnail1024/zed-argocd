#!/usr/bin/env python3
"""Exact supported and unsupported CLI targets on an isolated empty workspace."""
import hashlib,json,pathlib,platform,subprocess,sys
root=pathlib.Path(__file__).resolve().parents[1];binary=pathlib.Path(sys.argv[1]).resolve();out=pathlib.Path(sys.argv[2]).resolve();out.mkdir(parents=True,exist_ok=False);rows=[]
workspace=out/'workspace';workspace.mkdir();cases=json.loads((root/'tests/target-cases.json').read_text())
for c in cases:
 command=[str(binary),*c['args']]
 try:
  p=subprocess.run(command,cwd=workspace,capture_output=True,timeout=10);stdout,stderr,status=p.stdout,p.stderr,p.returncode
 except subprocess.TimeoutExpired as e:stdout,stderr,status=e.stdout or b'',e.stderr or b'','timeout'
 except OSError as e:stdout,stderr,status=b'',str(e).encode(),'launch-failed'
 (out/(c['id']+'.stdout')).write_bytes(stdout);(out/(c['id']+'.stderr')).write_bytes(stderr)
 try:
  r=json.loads(stdout);checks={'exit':status==c['exit'],'complete':r['analysisComplete']==c['complete'],'configuration':[e['code'] for e in r['configurationErrors']]==c['configurationCodes'],'zeroWork':r['filesAnalyzed']==0 and r['resourcesAnalyzed']==0,'diagnostics':r['diagnostics']==[]}
  if 'targets' in c:checks['targets']=r['targets']==c['targets']
  if 'target' in c:checks['target']=len(r['targets'])==1 and all(r['targets'][0].get(k)==v for k,v in c['target'].items())
 except Exception as e:checks={'report':False};r={'error':repr(e)}
 rows.append({'case':c,'command':command,'exit':status,'observed':r,'checks':checks,'passed':all(checks.values())})
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
try:binaryHash=sha(binary);identityError=None
except OSError as e:binaryHash=None;identityError=str(e)
report={'command':sys.argv,'python':platform.python_version(),'binarySHA256':binaryHash,'binaryIdentityError':identityError,'testSHA256':sha(pathlib.Path(__file__)),'expectedSHA256':sha(root/'tests/target-cases.json'),'rows':rows,'passed':identityError is None and bool(cases) and len(rows)==len(cases) and all(r['passed'] for r in rows)}
(out/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report));sys.exit(0 if report['passed'] else 1)
