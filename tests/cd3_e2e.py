#!/usr/bin/env python3
"""Real CLI schema behavior oracle, written before analysis implementation."""
import hashlib,json,pathlib,subprocess,sys,tempfile
root=pathlib.Path(__file__).resolve().parents[1];binary=pathlib.Path(sys.argv[1]).resolve();target=sys.argv[2] if len(sys.argv)>2 else '3.0.0';out=root/('artifacts/cd-3' if len(sys.argv)<3 else 'artifacts/cd-3-targets/'+target);out.mkdir(parents=True,exist_ok=True)
fixtures=[('project-type','apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata:\n  name: example\nspec:\n  project: 42\n  source:\n    repoURL: https://example.com/repo\n  destination:\n    server: https://kubernetes.default.svc\n    namespace: default\n','SCHEMA_TYPE',5),('foreign-kind','apiVersion: argoproj.io/v1alpha1\nkind: Workflow\nspec:\n  project: 42\n',None,None),('duplicate','apiVersion: argoproj.io/v1alpha1\nkind: Application\nspec:\n  project: default\n  project: duplicate\n','YAML_PROJECTION_INCOMPLETE',None)]
fixtures.append(('supported-alias','apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata:\n  name: example\nspec:\n  project: &p default\n  source:\n    repoURL: *p\n','SCHEMA_REQUIRED',None))
fixtures.append(('nonstring-key','apiVersion: argoproj.io/v1alpha1\nkind: Application\nspec:\n  42: value\n','YAML_PROJECTION_INCOMPLETE',None))
fixtures.append(('missing-value','apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata:\n  name: sample\nspec:\n','SCHEMA_TYPE',4))
fixtures.append(('custom-tag','apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata:\n  name: sample\nspec: !custom {project: 3, source: {repoURL: x}, destination: {server: x}}\n','YAML_PROJECTION_INCOMPLETE',None))
fixtures.append(('oversized-scalar','apiVersion: argoproj.io/v1alpha1\nkind: Application\nspec: {project: "'+'x'*1048577+'"}\n','INPUT_BUDGET_EXCEEDED',None))
rows=[]
for name,content,code,line in fixtures:
 folder=out/name;folder.mkdir(exist_ok=True);path=folder/'input.yaml';path.write_text(content)
 cmd=[str(binary),'check','--root',str(folder),'--file',str(path),'--target-version',target,'--format','json'];p=subprocess.run(cmd,capture_output=True,timeout=15);(folder/'stdout').write_bytes(p.stdout);(folder/'stderr').write_bytes(p.stderr);errors=[]
 try:
  report=json.loads(p.stdout)
  if p.returncode!=2 or report['analysisComplete']:errors.append('partial stage must remain incomplete with exit 2')
  matches=[d for d in report['diagnostics'] if d['code']==code]
  if code and not matches:errors.append('missing independent expected diagnostic '+code)
  if line is not None and not any(d['range']['start']['line']==line for d in matches):errors.append('wrong diagnostic line')
  if code is None and report['diagnostics']:errors.append('foreign GVK must not receive diagnostics')
 except Exception as e:errors.append(str(e))
 rows.append({'id':name,'command':cmd,'expectedCode':code,'expectedLine':line,'exit':p.returncode,'errors':errors,'passed':not errors,'inputSHA256':hashlib.sha256(content.encode()).hexdigest()})
(out/'results.json').write_text(json.dumps(rows,indent=2)+'\n');(out/'manifest.json').write_text(json.dumps({'command':sys.argv,'binarySHA256':hashlib.sha256(binary.read_bytes()).hexdigest(),'testSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()},indent=2)+'\n');print(json.dumps(rows));sys.exit(0 if all(r['passed'] for r in rows) else 1)
