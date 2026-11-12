#!/usr/bin/env python3
"""Remove actual schema validation in a temporary build; CLI oracle must fail."""
import hashlib,json,pathlib,shutil,subprocess,tempfile,os,sys
root=pathlib.Path(__file__).resolve().parents[1];out=root/'artifacts/lsp-ablation';out.mkdir(parents=True,exist_ok=True);d=pathlib.Path(tempfile.mkdtemp(prefix='argocd-schema-ablation-'))
for p in ['go.mod','go.sum','cmd','internal']:
 source=root/p
 if source.is_dir():shutil.copytree(source,d/p)
 elif source.exists():shutil.copy2(source,d/p)
p=d/'internal/lsp/server.go';s=p.read_text();needle='!ok || p.TextDocument.Version <= old.version';assert needle in s;s=s.replace(needle,'!ok',1);p.write_text(s)
env=os.environ.copy();env.update(GOWORK='off',GOTOOLCHAIN='local',GOFLAGS='');go=env.get('GO') or (str(pathlib.Path(env['GOROOT'])/'bin/go') if env.get('GOROOT') else 'go');cmd=[go,'build','-buildvcs=false','-trimpath','-o',str(d/'argocd-ls'),'./cmd/argocd-ls'];p=subprocess.run(cmd,cwd=d,env=env,capture_output=True);(out/'build.stdout').write_bytes(p.stdout);(out/'build.stderr').write_bytes(p.stderr)
run=None
if p.returncode==0:
 run=subprocess.run([sys.executable,str(root/'tests/lsp_e2e.py'),str(d/'argocd-ls')],capture_output=True);(out/'oracle.stdout').write_bytes(run.stdout);(out/'oracle.stderr').write_bytes(run.stderr);shutil.copytree(root/'artifacts/lsp',out/'oracle',dirs_exist_ok=True)
report={'buildCommand':cmd,'buildExit':p.returncode,'oracleExit':None if run is None else run.returncode,'detected':p.returncode==0 and run.returncode==1,'variant':'accept stale buffer version','decision':'retain monotonic LSP version guard','binarySHA256':hashlib.sha256((d/'argocd-ls').read_bytes()).hexdigest() if (d/'argocd-ls').exists() else None};(out/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report));sys.exit(0 if report['detected'] else 1)
