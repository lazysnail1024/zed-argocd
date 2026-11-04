#!/usr/bin/env python3
"""Native CI entry: retain each real-process outcome, including failures."""
import argparse,hashlib,json,os,pathlib,subprocess,sys
ROOT=pathlib.Path(__file__).resolve().parents[1];ap=argparse.ArgumentParser();ap.add_argument('--platform',required=True);ap.add_argument('--source-commit');ap.add_argument('--compiler-version');ap.add_argument('--output',type=pathlib.Path,default=ROOT/'artifacts/native-ci');a=ap.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);rows=[];expected=None;cache=out/'go-cache';modules=out/'module-cache';cache.mkdir();modules.mkdir();env=dict(os.environ,GOCACHE=str(cache),GOMODCACHE=str(modules),GOWORK='off',GOTOOLCHAIN='local',CGO_ENABLED='1',GOFLAGS='');go=env.get('GO') or (str(pathlib.Path(env['GOROOT'])/'bin/go') if env.get('GOROOT') else 'go')
def run(name,command,extra=None,timeout=180):
 try:p=subprocess.run(command,cwd=ROOT,env=dict(env,**(extra or {})),capture_output=True,timeout=timeout);stdout,stderr,status=p.stdout,p.stderr,p.returncode
 except subprocess.TimeoutExpired as e:stdout,stderr,status=e.stdout or b'',e.stderr or b'','timeout'
 except OSError as e:stdout,stderr,status=b'',str(e).encode(),'launch-failed'
 (out/(name+'.stdout')).write_bytes(stdout);(out/(name+'.stderr')).write_bytes(stderr);rows.append({'id':name,'command':command,'exit':status,'passed':status==0});save();return status==0

def save():
 report={'expectedNativePlatform':a.platform,'host':sys.platform,'python':sys.version,'initialBuildCachesEmpty':True,'cachePaths':{'GOCACHE':str(cache),'GOMODCACHE':str(modules)},'runnerImage':{k:os.environ.get(k) for k in ['ImageOS','ImageVersion','RUNNER_OS','RUNNER_ARCH']},'runnerSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'expectedProvenance':expected,'rows':rows,'passed':len(rows)==13 and all(r['passed'] for r in rows)};(out/'report.json').write_text(json.dumps(report,indent=2)+'\n')
try:
 head=subprocess.run(['git','rev-parse','--verify','HEAD'],cwd=ROOT,capture_output=True);commit=head.stdout.decode().strip() if head.returncode==0 else None
 status=subprocess.run(['git','status','--porcelain','--untracked-files=all'],cwd=ROOT,capture_output=True)
 if status.returncode!=0:raise RuntimeError('Cannot independently observe Git status')
 (out/'git-head.stdout').write_bytes(head.stdout);(out/'git-head.stderr').write_bytes(head.stderr);(out/'git-status.stdout').write_bytes(status.stdout);(out/'git-status.stderr').write_bytes(status.stderr)
 expected={'sourceCommit':commit,'dirty':commit is None or bool(status.stdout),'buildMode':'development'}
 args=[sys.executable,str(ROOT/'tools/native.py'),'build','--output',str(out/'build'),'--platform',a.platform]
 if a.source_commit:args+=['--source-commit',a.source_commit]
 if not run('build',args):raise RuntimeError('native build failed')
 manifest=json.loads((out/'build/manifest.json').read_text());binary=out/'build'/manifest['binary']
 if a.compiler_version and a.compiler_version not in manifest['compiler']:raise RuntimeError('Actual compiler differs from pinned CI version')
 run('package',[sys.executable,str(ROOT/'tools/native.py'),'package','--manifest',str(out/'build/manifest.json'),'--output',str(out/'package')])
 run('provenance-and-archive',[sys.executable,str(ROOT/'tests/native_package_e2e.py'),str(out/'packaging'),'--platform',a.platform],timeout=420)
 run('compiler-environment',[sys.executable,str(ROOT/'tests/native_environment_e2e.py'),str(out/'environment'),'--platform',a.platform],timeout=900)
 run('cli',[sys.executable,str(ROOT/'tests/cli_e2e.py'),str(binary),str(out/'cli')])
 run('targets',[sys.executable,str(ROOT/'tests/target_e2e.py'),str(binary),str(out/'targets')])
 run('readiness',[sys.executable,str(ROOT/'tests/readiness_e2e.py'),str(binary),str(out/'readiness'),'--expected-source-commit',commit or 'none','--expected-dirty',str(expected['dirty']).lower(),'--expected-build-mode','development'])
 run('save-actions',[sys.executable,str(ROOT/'tests/save_action_e2e.py'),str(binary),str(out/'save-actions'),'--skip-periodic'])
 run('schema-queries',[sys.executable,str(ROOT/'tests/schema_queries_e2e.py'),str(binary),str(out/'queries')])
 run('projection',[sys.executable,str(ROOT/'tests/alias_projection_cli_e2e.py'),str(binary),str(out/'projection')])
 run('publication',[sys.executable,str(ROOT/'tests/publication_e2e.py'),str(binary),str(out/'publication')])
 helper=out/('native-helper.exe' if a.platform.startswith('windows/') else 'native-helper')
 if run('helper-build',[go,'build','-mod=vendor','-buildvcs=false','-trimpath','-o',str(helper),'./tests/native_helper']):run('native-processes',[sys.executable,str(ROOT/'tests/native_process_e2e.py'),str(binary),str(helper),str(out/'processes')],timeout=90)
except Exception as e:rows.append({'id':'runner','passed':False,'error':repr(e)})
finally:save()
sys.exit(0 if len(rows)==13 and all(r['passed'] for r in rows) else 1)
