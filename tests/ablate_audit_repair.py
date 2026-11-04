#!/usr/bin/env python3
"""Isolated, compilable removals of independently specified behavior guards."""
import pathlib,subprocess,os,sys,hashlib,json,shutil,tempfile
root=pathlib.Path(__file__).resolve().parents[1];out=root/'artifacts/audit-repair-ablation';out.mkdir(parents=True,exist_ok=True)
env=os.environ.copy();env.update(GOROOT='/tmp/zed-argo-go-mod/golang.org/toolchain@v0.0.1-go1.27.3.linux-amd64',GOMODCACHE='/tmp/zed-argo-go-mod',GOCACHE='/tmp/zed-argo-go-build',GOWORK='off',GOPROXY='off')
variants=[('cancel','internal/lsp/server.go','if request, ok := requests[key]; ok {','if request, ok := requests[key]; ok && false {','budget'),('root-owner','internal/workspace/workspace.go','len(r) > len(owner)','owner == ""','lsp'),('buffer-first','internal/workspace/workspace.go','snap, opened := buffers[resolved]','snap, opened := buffers[resolved]; opened = false','lsp'),('excluded-isolation','internal/workspace/workspace.go','if excluded && opened {\n\t\t\towner = "isolated:" + resolved','if excluded && opened && false {\n\t\t\towner = "isolated:" + resolved','lsp'),('array-fields','internal/analysis/queries.go','case []any:\n\t\t\ti, err := strconv.Atoi(p)','case []any:\n            if len(v) >= 0 { return nil }; i, err := strconv.Atoi(p)','lsp')]
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
  e=env.copy();e.update(CD_AUDIT_BINARY=str(binary),CD_AUDIT_OUTPUT=str(folder/'oracle'));command=[sys.executable,str(root/'tests/audit_repair_e2e.py'),mode];run=subprocess.run(command,env=e,capture_output=True,timeout=120);(folder/'oracle.stdout').write_bytes(run.stdout);(folder/'oracle.stderr').write_bytes(run.stderr);row.update(command=command,oracleExit=run.returncode,detected=run.returncode==1,binarySHA256=hashlib.sha256(binary.read_bytes()).hexdigest())
 row['mutatedSourceSHA256']=hashlib.sha256(p.read_bytes()).hexdigest();rows.append(row);print(name,row['detected'],flush=True);shutil.rmtree(copy)
(out/'report.json').write_text(json.dumps({'python':sys.version,'environment':{k:env[k] for k in ['GOROOT','GOMODCACHE','GOCACHE','GOWORK','GOPROXY']},'testSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'oracleSHA256':hashlib.sha256((root/'tests/audit_repair_e2e.py').read_bytes()).hexdigest(),'cases':rows},indent=2)+'\n');sys.exit(0 if rows and all(r['detected'] for r in rows) else 1)
