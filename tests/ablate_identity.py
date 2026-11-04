#!/usr/bin/env python3
"""Two isolated, compiling guard deletions; the pre-existing process oracle must reject them."""
import hashlib,json,pathlib,subprocess,tempfile,shutil,sys
root=pathlib.Path(__file__).resolve().parents[1]; out=root/'artifacts/cd-1/ablation';out.mkdir(parents=True,exist_ok=True)
rows=[]
for name,line in [('product','    if id.product != "argo-cd" { return Err("Expected argo-cd server product".into()); }'),('readiness','    if !id.ready { return Err("Argo CD server analysis is not ready".into()); }')]:
 d=pathlib.Path(tempfile.mkdtemp(prefix='argocd-ablation-'))
 for path in ['Cargo.toml','Cargo.lock','src','examples']:
  src=root/path
  if src.is_dir():shutil.copytree(src,d/path)
  else:shutil.copy2(src,d/path)
 p=d/'src/identity.rs';text=p.read_text();assert line in text;p.write_text(text.replace(line,''))
 cmd=['cargo','+stable','build','--offline','--locked','--manifest-path',str(d/'Cargo.toml'),'--example','identity_probe']
 b=subprocess.run(cmd,capture_output=True);(out/(name+'.build.stdout')).write_bytes(b.stdout);(out/(name+'.build.stderr')).write_bytes(b.stderr)
 binary=d/'target/debug/examples/identity_probe';run=None
 if b.returncode==0:
  run=subprocess.run([sys.executable,str(root/'tests/adapter_e2e.py'),str(binary),str(out/name)],capture_output=True)
  (out/(name+'.stdout')).write_bytes(run.stdout);(out/(name+'.stderr')).write_bytes(run.stderr)
 rows.append({'variant':name,'directory':str(d),'buildCommand':cmd,'buildExit':b.returncode,'oracleExit':None if run is None else run.returncode,'detected':b.returncode==0 and run.returncode==1,'binarySHA256':hashlib.sha256(binary.read_bytes()).hexdigest() if binary.exists() else None,'decision':'retain guard: needed to reject incompatible server'})
(out/'report.json').write_text(json.dumps(rows,indent=2)+'\n');print(json.dumps(rows));sys.exit(0 if all(x['detected'] for x in rows) else 1)
