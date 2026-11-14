#!/usr/bin/env python3
"""Native CI entry: retain each real-process outcome, including failures."""
import argparse,hashlib,json,os,pathlib,re,subprocess,sys,signal,time,shutil
ROOT=pathlib.Path(__file__).resolve().parents[1]
BUILD_ROLES={'consumers':'ordinary','provenance':'provenance','compiler-environment':'compiler-isolation','all':'ordinary'}
SUITES=('consumers','provenance','compiler-environment')
PLATFORM_COMPILERS={'linux/amd64':'13.3.0','linux/arm64':'13.3.0','darwin/amd64':'Apple clang version 17.0.0','darwin/arm64':'Apple clang version 17.0.0','windows/amd64':'15.2.0'}
SECURITY_STEPS={'provenance':'provenance-and-archive','compiler-environment':'compiler-environment'}
COMPILER_CASES=('normal','flags','search','link','persisted-go')
BASE_STEPS=('build','package','ci-process-boundary','ci-evidence','ci-shard-gate','provenance-and-archive','compiler-environment','cli','targets','readiness','config-uris','save-actions','schema-queries','projection','publication','helper-build','native-processes')
SOURCE_ABLATIONS=(('navigation-editing-ablation','navigation_editing_ablation.py',600),('project-rename-ablation','ablate_project_rename.py',240),('shared-schemaquery-ablation','ablate_shared_schemaquery.py',240))

def audit_consumers(platform):
 # Every entry is a required real-process gate, not a documentation-only command.
 # The fixed Linux amd64 runner also retains the large workload samples and
 # deliberately broken variants. Other platforms still exercise native behavior.
 full_catalog=platform=='linux/amd64'
 cases=[
  ('multi-version-compatibility','multi_version_e2e.py',[] if full_catalog else ['--representatives'],600),
  ('lsp-lifecycle','lsp_e2e.py',[],90),
  ('navigation-defaults','navigation_defaults_e2e.py',[],90),
  ('incremental-equivalence','incremental_equivalence_e2e.py',[],180),
  ('helm-source','helm_source_e2e.py',[],120),
  ('parent-workspace','parent_workspace_lsp_e2e.py',[],90),
  ('mixed-parent-index','mixed_parent_index_e2e.py',[],90),
  ('persistent-workspace','persistent_workspace_e2e.py',['--count','1000','--padding','4096'],150),
  ('alias-projection-lsp','alias_projection_lsp_e2e.py',[],150),
  ('projection-repair','projection_repair_e2e.py',[],180),
  ('projection-aggregate','projection_aggregate_e2e.py',[],180),
  ('interaction-remediation','interaction_remediation_e2e.py',[],180),
  ('host-capabilities','host_capabilities_e2e.py',[],60),
  ('navigation-editing','navigation_editing_e2e.py',[],120),
  ('navigation-editing-guards','navigation_editing_guards_e2e.py',[],180),
  ('bounded-symbol-queries','bounded_symbol_queries_e2e.py',[],180),
  ('project-rename','project_rename_e2e.py',[],180),
  ('shared-schemaquery','shared_schemaquery_e2e.py',[],120),
  ('appset-overrides','appset_overrides_e2e.py',['--all-targets'] if full_catalog else [],480 if full_catalog else 180),
  ('quickfix-scaling','quickfix_scaling_e2e.py',[],180),
  ('repository-exclusion','repository_exclusion_e2e.py',[],300),
  ('reconciliation','reconciliation_e2e.py',[],60),
  ('query-delivery','query_delivery_e2e.py',[],180),
  ('candidate-concurrency','candidate_concurrency_e2e.py',[],120),
  ('lsp-regressions','lsp_regressions_e2e.py',[],240),
  ('semantic-regressions','semantic_regressions_e2e.py',[],180),
  ('workspace-regressions','workspace_regressions_e2e.py',[],180),
 ]
 if platform=='linux/amd64':
  cases.extend([
   ('publication-delivery','publication_delivery_e2e.py',[],45),
   ('bounded-symbol-queries-cap','bounded_symbol_queries_e2e.py',['--count','1800','--files','12'],240),
   ('persistent-workspace-50000','persistent_workspace_e2e.py',['--count','50000','--padding','4096','--reference-mix','--ordinary-files','100000','--max-rss-mib','512','--ready-timeout','120','--reconcile-timeout','120'],360),
   ('mixed-parent-index-50000','mixed_parent_index_e2e.py',['--ordinary-files','50000','--padding-bytes','4096','--background-edits'],180),
   ('incremental-performance','incremental_performance_e2e.py',['--sizes','1000,3000','--repeats','30','--require-incremental'],180),
   ('repository-performance','incremental_performance_e2e.py',['--sizes','1000,3000','--repeats','30','--require-incremental','--repository'],180),
   ('audit-regression-mutations','audit_regression_mutations.py',['--worker'],480),
   ('document-budget-ablation','ablate_document_budget.py',[],240),
   ('parent-workspace-faults','parent_workspace_faults.py',['--worker'],300),
  ])
 return cases

def required_steps(platform,suite):
 if suite in SECURITY_STEPS:return [SECURITY_STEPS[suite]]
 steps=[name for name in BASE_STEPS if suite=='all' or name not in SECURITY_STEPS.values()]
 return steps+[case[0] for case in audit_consumers(platform)]+([case[0] for case in SOURCE_ABLATIONS] if platform=='linux/amd64' else [])

def inventory_digest(sources):
 return hashlib.sha256(json.dumps(sources,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def build_evidence(path):
 manifest=json.loads(path.read_text());ge=manifest['goEnvironment']
 if manifest['platform']!=a.platform or ge['GOHOSTOS']+'/'+ge['GOHOSTARCH']!=a.platform or ge['GOOS']+'/'+ge['GOARCH']!=a.platform:raise RuntimeError('Build did not execute on the requested native platform')
 if ge['GOVERSION']!='go'+next(line.split()[1] for line in (ROOT/'go.mod').read_text().splitlines() if line.startswith('go ')):raise RuntimeError('Actual Go differs from the pinned version')
 if a.compiler_version and a.compiler_version not in manifest['compiler']:raise RuntimeError('Actual compiler differs from pinned CI version')
 binary=path.parent/('argocd-ls.exe' if a.platform.startswith('windows/') else 'argocd-ls')
 if hashlib.sha256(binary.read_bytes()).hexdigest()!=manifest['binarySHA256']:raise RuntimeError('Evidence binary differs from its native build manifest')
 return {'platform':manifest['platform'],'identity':manifest['identity'],'binarySHA256':manifest['binarySHA256'],'sourceInputsSHA256':inventory_digest(manifest['sourceInputs']),'goEnvironment':{key:ge[key] for key in ('GOVERSION','GOHOSTOS','GOHOSTARCH','GOOS','GOARCH')},'compiler':manifest['compiler'],'toolSHA256':manifest['toolSHA256']}

def accepted():
 return completed and [r['id'] for r in rows]==expected_steps and all(r.get('passed') is True and type(r.get('exit')) is int and r['exit']==0 and r.get('cleanupError') is None for r in rows)

def configure(argv=None):
 global a,out,rows,expected,cache,modules,env,go,active,completed,expected_steps,builds,security_cases,child_evidence
 ap=argparse.ArgumentParser();ap.add_argument('--platform',required=True);ap.add_argument('--suite',choices=('all',)+SUITES,default='all');ap.add_argument('--build-directory',type=pathlib.Path);ap.add_argument('--producer-receipt-sha');ap.add_argument('--source-commit');ap.add_argument('--compiler-version');ap.add_argument('--release-version',default='',help='Run the same native consumers against a clean stable or prerelease build');ap.add_argument('--output',type=pathlib.Path,default=ROOT/'artifacts/native-ci');a=ap.parse_args(argv);out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);rows=[];child_evidence={};expected=None;active=None;completed=False;builds=[];security_cases=[];cache=out/'go-cache';modules=out/'module-cache';cache.mkdir();modules.mkdir();env=dict(os.environ,GOCACHE=str(cache),GOMODCACHE=str(modules),GOWORK='off',GOTOOLCHAIN='local',CGO_ENABLED='1',GOFLAGS='',PYTHONUTF8='1');go=env.get('GO') or (str(pathlib.Path(env['GOROOT'])/'bin/go') if env.get('GOROOT') else 'go')
 expected_steps=required_steps(a.platform,a.suite)
def start_command(command,child_env,stdin=None):
 # Own this CI command and its descendants before any helper code executes.
 if os.name!='nt':
  p=subprocess.Popen(command,cwd=ROOT,env=child_env,stdin=stdin,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
  def close():
   try:os.killpg(p.pid,signal.SIGKILL)
   except ProcessLookupError:pass
  return p,close
 import ctypes
 from ctypes import wintypes as w
 k=ctypes.WinDLL('kernel32',use_last_error=True);nt=ctypes.WinDLL('ntdll')
 for name,args,result in [('CreateJobObjectW',[w.LPVOID,w.LPCWSTR],w.HANDLE),('SetInformationJobObject',[w.HANDLE,ctypes.c_int,w.LPVOID,w.DWORD],w.BOOL),('AssignProcessToJobObject',[w.HANDLE,w.HANDLE],w.BOOL),('OpenProcess',[w.DWORD,w.BOOL,w.DWORD],w.HANDLE),('CloseHandle',[w.HANDLE],w.BOOL)]:
  f=getattr(k,name);f.argtypes=args;f.restype=result
 nt.NtResumeProcess.argtypes=[w.HANDLE];nt.NtResumeProcess.restype=w.LONG
 class Basic(ctypes.Structure):
  _fields_=[('processTime',ctypes.c_longlong),('jobTime',ctypes.c_longlong),('flags',w.DWORD),('min',ctypes.c_size_t),('max',ctypes.c_size_t),('active',w.DWORD),('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
 class Extended(ctypes.Structure):
  _fields_=[('basic',Basic),('io',ctypes.c_ulonglong*6),('processMemory',ctypes.c_size_t),('jobMemory',ctypes.c_size_t),('peakProcessMemory',ctypes.c_size_t),('peakJobMemory',ctypes.c_size_t)]
 job=k.CreateJobObjectW(None,None)
 if not job:raise ctypes.WinError(ctypes.get_last_error())
 def close():
  nonlocal job
  if job:
   handle=job;job=None
   if not k.CloseHandle(handle):raise ctypes.WinError(ctypes.get_last_error())
 p=None
 try:
  limits=Extended();limits.basic.flags=0x2000 # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
  if not k.SetInformationJobObject(job,9,ctypes.byref(limits),ctypes.sizeof(limits)):raise ctypes.WinError(ctypes.get_last_error())
  p=subprocess.Popen(command,cwd=ROOT,env=child_env,stdin=stdin,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=0x00000004) # CREATE_SUSPENDED
  process=k.OpenProcess(0x0100|0x0001|0x0800,False,p.pid) # SET_QUOTA, TERMINATE, SUSPEND_RESUME
  if not process:raise ctypes.WinError(ctypes.get_last_error())
  try:
   if not k.AssignProcessToJobObject(job,process):raise ctypes.WinError(ctypes.get_last_error())
   status=nt.NtResumeProcess(process)
   if status:raise OSError('NtResumeProcess failed: NTSTATUS '+hex(status & 0xffffffff))
  finally:
   if not k.CloseHandle(process):raise ctypes.WinError(ctypes.get_last_error())
  return p,close
 except BaseException:
  close()
  if p is not None:
   if p.poll() is None:p.kill()
   p.communicate(timeout=3)
  raise

def run(name,command,extra=None,timeout=180):
 global active
 active={'id':name,'command':command,'timeoutSeconds':timeout};save();started=time.monotonic();p=None;close=None;stdout=stderr=b'';status='launch-failed';cleanupError=None
 print(json.dumps({'step':name,'state':'started','command':command}),flush=True)
 try:
  p,close=start_command(command,dict(env,**(extra or {})))
  try:stdout,stderr=p.communicate(timeout=timeout);status=p.returncode
  except subprocess.TimeoutExpired as e:
   stdout,stderr,status=e.stdout or b'',e.stderr or b'','timeout'
   close();close=None
   stdout,stderr=p.communicate(timeout=3)
 except (OSError,subprocess.TimeoutExpired) as e:
  if status!='timeout':status='launch-failed'
  stderr+=('\n'+repr(e)).encode()
 finally:
  if close:
   try:close()
   except OSError as e:cleanupError=repr(e)
  if p is not None:
   try:
    if p.poll() is None:p.kill()
    p.wait(timeout=3)
   except (OSError,subprocess.TimeoutExpired) as e:cleanupError=repr(e)
   if p.stdout:p.stdout.close()
   if p.stderr:p.stderr.close()
   if os.name=='nt':p._handle.Close()
  (out/(name+'.stdout')).write_bytes(stdout);(out/(name+'.stderr')).write_bytes(stderr);rows.append({'id':name,'command':command,'exit':status,'elapsedSeconds':time.monotonic()-started,'cleanupError':cleanupError,'passed':status==0 and cleanupError is None});active=None;save()
 print(json.dumps({'step':name,'state':'completed','exit':status,'passed':rows[-1]['passed'],'elapsedSeconds':rows[-1]['elapsedSeconds']}),flush=True)
 if not rows[-1]['passed']:
  # Keep failures inspectable even if the evidence transport itself failed.
  print(json.dumps({'step':name,'failureOutput':{'stdout':stdout[-4000:].decode(errors='replace'),'stderr':stderr[-4000:].decode(errors='replace')}}),flush=True)
 return rows[-1]['passed']

def replace_report(pending, published):
 # Windows indexing or AV may briefly deny replacement even when the
 # observing handle explicitly permits delete sharing. Keep the report atomic.
 deadline=time.monotonic()+2
 retries=0
 while True:
  try:
   pending.replace(published)
   return retries
  except PermissionError:
   if os.name!='nt' or time.monotonic()>=deadline:raise
   retries+=1
   time.sleep(.05)
def save():
 report={'schemaVersion':2,'childEvidence':child_evidence,'contractDigest':hashlib.sha256((ROOT/'tests/inventory.json').read_bytes()).hexdigest(),'buildRole':BUILD_ROLES[a.suite],'suite':a.suite,'releaseVersion':a.release_version,'workflowRun':{key:os.environ.get(key) for key in ('GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT')},'expectedNativePlatform':a.platform,'host':sys.platform,'python':sys.version,'initialBuildCachesEmpty':True,'buildOrigin':'producer' if a.build_directory else 'local','producerReceipt':json.loads((a.build_directory/'receipt.json').read_text()) if a.build_directory and (a.build_directory/'receipt.json').is_file() else None,'cachePaths':{'GOCACHE':str(cache),'GOMODCACHE':str(modules)},'runnerImage':{k:os.environ.get(k) for k in ['ImageOS','ImageVersion','RUNNER_OS','RUNNER_ARCH']},'runnerSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'expectedProvenance':expected,'expectedSteps':expected_steps,'buildEvidence':builds,'securityCases':security_cases,'rows':rows,'activeCommand':active,'completed':completed,'passed':accepted()};pending=out/'report.pending.json';pending.write_text(json.dumps(report,indent=2)+'\n');replace_report(pending,out/'report.json')

def security_suite(suite):
 global security_cases
 if suite=='provenance':
  # Five intentional builds each retain their own 180-second limit; Windows
  # also needs bounded time for fixture copies, inventories and package checks.
  provenance_timeout = 1200 if a.platform == 'windows/amd64' else 420
  if run('provenance-and-archive',[sys.executable,str(ROOT/'tests/native_package_e2e.py'),str(out/'packaging'),'--platform',a.platform],timeout=provenance_timeout):builds.append(build_evidence(out/'packaging/development-build/manifest.json'))
 else:
  if run('compiler-environment',[sys.executable,str(ROOT/'tests/native_environment_e2e.py'),str(out/'environment'),'--platform',a.platform],timeout=900):
   builds.extend(build_evidence(out/'environment'/(name+'-build')/'manifest.json') for name in COMPILER_CASES)
   security_cases=json.loads((out/'environment/report.json').read_text())['rows']

def collect_children():
 from child_evidence import collect
 for step in expected_steps:
  entry=collect(out,step,a.platform)
  if entry is not None:child_evidence[step]=entry


def main():
 global expected,completed
 configure()
 try:
  head=subprocess.run(['git','rev-parse','--verify','HEAD'],cwd=ROOT,capture_output=True);commit=head.stdout.decode().strip() if head.returncode==0 else None
  status=subprocess.run(['git','status','--porcelain','--untracked-files=all'],cwd=ROOT,capture_output=True)
  if status.returncode!=0:raise RuntimeError('Cannot independently observe Git status')
  (out/'git-head.stdout').write_bytes(head.stdout);(out/'git-head.stderr').write_bytes(head.stderr);(out/'git-status.stdout').write_bytes(status.stdout);(out/'git-status.stderr').write_bytes(status.stderr)
  mode='release' if a.release_version else 'development'
  tree=subprocess.check_output(['git','rev-parse','HEAD^{tree}'],cwd=ROOT,text=True).strip() if commit else None
  expected={'sourceCommit':commit,'sourceTree':tree,'dirty':commit is None or bool(status.stdout),'buildMode':mode}
  if a.source_commit and (not re.fullmatch('[0-9a-f]{40}',a.source_commit) or a.source_commit!=commit):raise RuntimeError('Requested source commit must equal exact HEAD')
  if a.release_version and (not a.source_commit or expected['dirty']):raise RuntimeError('Native acceptance requires exact clean release source')
  if a.suite in SECURITY_STEPS:
   # Both security consumers already build real native binaries. A redundant
   # main build here would add ten cold builds to the five-platform matrix.
   security_suite(a.suite);collect_children();completed=True
   return 0 if accepted() else 1
  args=[sys.executable,str(ROOT/'tools/native.py'),'build','--output',str(out/'build'),'--platform',a.platform]
  if a.release_version:args+=['--mode','release','--version',a.release_version]
  if a.source_commit:args+=['--source-commit',a.source_commit]
  if a.build_directory:
   if a.suite!='consumers' or not a.release_version:raise RuntimeError('Only ordinary release consumers may import this build')
   args=[sys.executable,str(ROOT/'tools/build_receipt.py'),'verify','--directory',str(a.build_directory),'--platform',a.platform,'--source-commit',a.source_commit,'--mode','release','--version',a.release_version,'--receipt-sha',a.producer_receipt_sha,'--check-source']
   if not run('build',args):raise RuntimeError('producer verification failed')
   shutil.copytree(a.build_directory,out/'build')
   (out/'build'/'argocd-ls').chmod(0o755)
  elif not run('build',args):raise RuntimeError('native build failed')
  manifest=json.loads((out/'build/manifest.json').read_text());binary=out/'build'/manifest['binary']
  builds.append(build_evidence(out/'build/manifest.json'))
  run('package',[sys.executable,str(ROOT/'tools/native.py'),'package','--manifest',str(out/'build/manifest.json'),'--output',str(out/'package')])
  run('ci-process-boundary',[sys.executable,str(ROOT/'tests/native_ci_process_e2e.py'),str(out/'ci-process-boundary'),'--platform',a.platform],timeout=45)
  run('ci-evidence',[sys.executable,str(ROOT/'tests/ci_evidence_e2e.py'),'--output',str(out/'ci-evidence')],timeout=90)
  run('ci-shard-gate',[sys.executable,str(ROOT/'tests/ci_shards_e2e.py'),'--output',str(out/'ci-shard-gate')],timeout=90)
  if a.suite=='all':
   security_suite('provenance');security_suite('compiler-environment')
  run('cli',[sys.executable,str(ROOT/'tests/cli_e2e.py'),str(binary),str(out/'cli')])
  run('targets',[sys.executable,str(ROOT/'tests/target_e2e.py'),str(binary),str(out/'targets')])
  run('readiness',[sys.executable,str(ROOT/'tests/readiness_e2e.py'),str(binary),str(out/'readiness'),'--expected-source-commit',commit or 'none','--expected-dirty',str(expected['dirty']).lower(),'--expected-build-mode',mode])
  run('config-uris',[sys.executable,str(ROOT/'tests/config_uri_e2e.py'),str(binary),str(out/'config-uris')])
  run('save-actions',[sys.executable,str(ROOT/'tests/save_action_e2e.py'),str(binary),str(out/'save-actions'),'--skip-periodic'])
  run('schema-queries',[sys.executable,str(ROOT/'tests/schema_queries_e2e.py'),str(binary),str(out/'queries')])
  run('projection',[sys.executable,str(ROOT/'tests/alias_projection_cli_e2e.py'),str(binary),str(out/'projection')])
  run('publication',[sys.executable,str(ROOT/'tests/publication_e2e.py'),str(binary),str(out/'publication')])
  helper=out/('native-helper.exe' if a.platform.startswith('windows/') else 'native-helper')
  if run('helper-build',[go,'build','-mod=vendor','-buildvcs=false','-trimpath','-o',str(helper),'./tests/native_helper']):run('native-processes',[sys.executable,str(ROOT/'tests/native_process_e2e.py'),str(binary),str(helper),str(out/'processes')],timeout=90)
  for name,script,options,timeout in audit_consumers(a.platform):
   run(name,[sys.executable,str(ROOT/'tests'/script),str(binary),str(out/name),*options],timeout=timeout)
  if a.platform=='linux/amd64':
   for name,script,timeout in SOURCE_ABLATIONS:
    run(name,[sys.executable,str(ROOT/'tests'/script),str(out/name)],timeout=timeout)
  collect_children();completed=True
 except Exception as e:rows.append({'id':'runner','passed':False,'error':repr(e)})
 finally:save()
 return 0 if accepted() else 1

if __name__=='__main__':sys.exit(main())
