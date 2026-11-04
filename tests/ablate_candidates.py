#!/usr/bin/env python3
"""Isolated, compilable removals of independently specified behavior guards."""
import pathlib,subprocess,os,sys,hashlib,json,shutil,tempfile
root=pathlib.Path(__file__).resolve().parents[1];out=root/'artifacts/candidates-ablation';out.mkdir(parents=True,exist_ok=True)
env=os.environ.copy();env.update(GOROOT='/tmp/zed-argo-go-mod/golang.org/toolchain@v0.0.1-go1.27.3.linux-amd64',GOMODCACHE='/tmp/zed-argo-go-mod',GOCACHE='/tmp/zed-argo-go-build',GOWORK='off',GOPROXY='off')
variants=[('namespace-filter','internal/analysis/candidates.go','(options.ControlPlaneNamespace != "" && ns != options.ControlPlaneNamespace)','false','candidates'),('syntax-only-recovery','internal/analysis/candidates.go','if !damaged {','if !damaged && false {','candidates'),('literal-encoding','internal/analysis/candidates.go','if yaml.UnmarshalStrict([]byte(name), &value) != nil || value != name {','if (yaml.UnmarshalStrict([]byte(name), &value) != nil || value != name) && false {','candidates'),('source-prefix-span','internal/analysis/candidates.go','Start: start + 1, End: start + slash','Start: start, End: start + slash','candidates')]

rows=[]
for name,file,old,new,mode in variants:
 folder=out/name;folder.mkdir(exist_ok=True);copy=pathlib.Path(tempfile.mkdtemp(prefix='cd-repair-'))
 for part in ['go.mod','go.sum','cmd','internal','vendor']:
  p=root/part
  if p.is_dir():shutil.copytree(p,copy/part)
  else:shutil.copy2(p,copy/part)
 p=copy/file;s=p.read_text();assert old in s,(name,old);p.write_text(s.replace(old,new,1));(folder/'mutated-source.txt').write_text(p.read_text())
 binary=folder/'argocd-ls';cmd=[env['GOROOT']+'/bin/go','build','-mod=vendor','-buildvcs=false','-trimpath','-o',str(binary),'./cmd/argocd-ls'];build=subprocess.run(cmd,cwd=copy,env=env,capture_output=True);(folder/'build.stdout').write_bytes(build.stdout);(folder/'build.stderr').write_bytes(build.stderr)
 row={'variant':name,'mutation':{'file':file,'from':old,'to':new},'buildCommand':cmd,'buildExit':build.returncode,'detected':False}
 if build.returncode==0:
  e=env.copy();e.update(CD_AUDIT_BINARY=str(binary),CD_AUDIT_OUTPUT=str(folder/'oracle'));command=[sys.executable,str(root/'tests/candidates_e2e.py')];run=subprocess.run(command,env=e,capture_output=True,timeout=120);(folder/'oracle.stdout').write_bytes(run.stdout);(folder/'oracle.stderr').write_bytes(run.stderr);shutil.copytree(root/'artifacts/candidates',folder/'oracle',dirs_exist_ok=True);row.update(command=command,oracleExit=run.returncode,detected=run.returncode==1,binarySHA256=hashlib.sha256(binary.read_bytes()).hexdigest())
 row['mutatedSourceSHA256']=hashlib.sha256(p.read_bytes()).hexdigest();rows.append(row);print(name,row['detected'],flush=True);shutil.rmtree(copy)
(out/'report.json').write_text(json.dumps({'python':sys.version,'environment':{k:env[k] for k in ['GOROOT','GOMODCACHE','GOCACHE','GOWORK','GOPROXY']},'testSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'oracleSHA256':hashlib.sha256((root/'tests/candidates_e2e.py').read_bytes()).hexdigest(),'cases':rows},indent=2)+'\n');sys.exit(0 if rows and all(r['detected'] for r in rows) else 1)
