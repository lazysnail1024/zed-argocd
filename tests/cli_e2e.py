#!/usr/bin/env python3
"""Independent real-process CD-1 assertions; output is retained even on failure."""
import hashlib,json,pathlib,subprocess,sys,platform,re
root=pathlib.Path(__file__).resolve().parents[1]
binary=pathlib.Path(sys.argv[1]).resolve(); out=pathlib.Path(sys.argv[2]).resolve(); out.mkdir(parents=True,exist_ok=False)
hashfile=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
workspace=out/'workspace';workspace.mkdir()
cases=json.loads((root/'tests/cli-cases.json').read_text()); results=[]
for c in cases:
 try:
  p=subprocess.run([str(binary),*c['args']],cwd=workspace,capture_output=True,timeout=10);stdout,stderr,status=p.stdout,p.stderr,p.returncode
 except subprocess.TimeoutExpired as e:stdout,stderr,status=e.stdout or b'',e.stderr or b'','timeout'
 except OSError as e:stdout,stderr,status=b'',str(e).encode(),'launch-failed'
 (out/(c['id']+'.stdout')).write_bytes(stdout);(out/(c['id']+'.stderr')).write_bytes(stderr)
 errors=[]
 if status!=c['exit']:errors.append(f'exit expected {c["exit"]}, got {status}')
 if 'stdout' in c and stdout.decode()!=c['stdout']:errors.append('stdout mismatch')
 if 'contains' in c and c['contains'] not in stdout.decode():errors.append('missing stdout text')
 if 'stderrContains' in c and c['stderrContains'] not in stderr.decode():errors.append('missing stderr text')
 if 'nonEmptyFields' in c:
  try:
   for key in c['nonEmptyFields']:
    if not json.loads(stdout).get(key):errors.append('missing identity: '+key)
  except Exception as e:errors.append(str(e))
 if 'sha256Fields' in c:
  try:
   identity=json.loads(stdout)
   for key in c['sha256Fields']:
    if not re.fullmatch('[0-9a-f]{64}',identity.get(key,'')):errors.append('missing SHA256 identity: '+key)
  except Exception as e:errors.append(str(e))
 if 'json' in c:
  try:
   value=json.loads(stdout)
   for k,v in c['json'].items():
    if k not in value or value[k]!=v:errors.append('JSON mismatch: '+k)
  except Exception as e:errors.append(str(e))
 results.append({'id':c['id'],'command':[str(binary),*c['args']],'exit':status,'assertions':c,'errors':errors,'passed':not errors})
(out/'results.json').write_text(json.dumps(results,indent=2)+'\n')
try:binaryHash=hashfile(binary);identityError=None
except OSError as e:binaryHash=None;identityError=str(e)
passed=bool(cases) and len(results)==len(cases) and identityError is None and all(r['passed'] for r in results)
(out/'manifest.json').write_text(json.dumps({'command':sys.argv,'python':platform.python_version(),'binary':str(binary),'binarySHA256':binaryHash,'binaryIdentityError':identityError,'passed':passed,'sources':{str(p.relative_to(root)):hashfile(p) for p in [pathlib.Path(__file__),root/'tests/cli-cases.json']},'cases':len(cases)},indent=2)+'\n')
print(json.dumps({'cases':len(cases),'passed':sum(r['passed'] for r in results),'artifact':str(out)}))
sys.exit(0 if passed else 1)
