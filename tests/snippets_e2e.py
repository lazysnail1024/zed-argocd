#!/usr/bin/env python3
import pathlib,json,subprocess,sys,os,re,hashlib
root=pathlib.Path(__file__).resolve().parents[1];binary=pathlib.Path(sys.argv[1]).resolve();out=pathlib.Path(os.environ.get('CD_AUDIT_OUTPUT',str(root/'artifacts/snippets')));out.mkdir(parents=True,exist_ok=True);snippets=json.loads((root/'snippets/yaml.json').read_text());inputs=[]
for title,item in snippets.items():
 assert item['prefix'].startswith('argo-cd-') and '3.0.0' in item['description'];body='\n'.join(item['body'])+'\n';body=re.sub(r'\$\{\d+:([^}]+)\}',lambda m:m[1],body).replace('$0','');inputs.append(body)
assert len(inputs)==3
(out/'input.yaml').write_text('---\n'.join(inputs));(out/'.argocd-ls.yaml').write_text('schemaVersion: 1\ntargetVersion: "3.0.0"\ncontrolPlaneNamespace: argocd\n');cmd=[str(binary),'check','--root',str(out),'--format','json'];p=subprocess.run(cmd,capture_output=True,timeout=15);(out/'stdout').write_bytes(p.stdout);(out/'stderr').write_bytes(p.stderr)
try:
 report=json.loads(p.stdout);passed=p.returncode==0 and report['resourcesAnalyzed']==3 and report['analysisComplete'] and not any(d['severity']==1 for d in report['diagnostics'])
except Exception:passed=False
(out/'results.json').write_text(json.dumps({'command':cmd,'exit':p.returncode,'expected':{'resources':3,'errorDiagnostics':0,'analysisComplete':True},'passed':passed},indent=2)+'\n');(out/'manifest.json').write_text(json.dumps({'binarySHA256':hashlib.sha256(binary.read_bytes()).hexdigest(),'testSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'snippetsSHA256':hashlib.sha256((root/'snippets/yaml.json').read_bytes()).hexdigest(),'inputSHA256':hashlib.sha256((out/'input.yaml').read_bytes()).hexdigest()},indent=2)+'\n');print(json.dumps({'passed':passed}));sys.exit(0 if passed else 1)
