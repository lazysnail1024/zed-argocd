#!/usr/bin/env python3
"""Portable renderer lifetime and report delivery through the real render CLI.

The explicit native helper is a controlled renderer, not a Helm implementation.
"""
import ctypes,hashlib,json,os,pathlib,subprocess,sys,time,threading
if os.name == "posix":
 import resource
binary=pathlib.Path(sys.argv[1]).resolve();helper=pathlib.Path(sys.argv[2]).resolve();out=pathlib.Path(sys.argv[3]).resolve();out.mkdir(parents=True,exist_ok=False);rows=[]
def alive(pid):
 if os.name=='nt':
  kernel=ctypes.WinDLL('kernel32',use_last_error=True);kernel.OpenProcess.restype=ctypes.c_void_p;handle=kernel.OpenProcess(0x1000,False,pid)
  if not handle:return False
  try:
   code=ctypes.c_ulong();kernel.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)];return bool(kernel.GetExitCodeProcess(handle,ctypes.byref(code))) and code.value==259
  finally:kernel.CloseHandle.argtypes=[ctypes.c_void_p];kernel.CloseHandle(handle)
 p=subprocess.run(['ps','-p',str(pid),'-o','stat='],capture_output=True);return p.returncode==0 and bool(p.stdout.strip()) and not p.stdout.strip().startswith(b'Z')
def observed_run(cmd, env, pids, marker, phase):
 # Observation never decides acceptance. kill(pid, 0) treats zombies as present,
 # so disappearance is a conservative upper bound, not an exact death time.
 stop = threading.Event()
 events, errors = [], []
 started = time.monotonic()
 phase.update(observationStartUnixNs=time.time_ns(), observerSupported=os.name == 'posix',
              observation='PID disappearance includes zombie reaping; poll interval 25 ms')
 cpu_before = None
 if os.name == 'posix':
  cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)
 def observe():
  owned = None
  disappeared = set()
  while not stop.is_set():
   try:
    if owned is None and pids.exists():
     data = json.loads(pids.read_text())
     if len(data) != 2 or any(type(pid) is not int or pid <= 0 for pid in data):
      raise ValueError('invalid task-owned helper PID evidence')
     owned = data
     events.append({'event': 'pids-observed', 'seconds': time.monotonic() - started, 'pids': owned})
    for pid in owned or []:
     if pid in disappeared:
      continue
     try:
      os.kill(pid, 0)
     except ProcessLookupError:
      disappeared.add(pid)
      events.append({'event': 'pid-disappeared', 'seconds': time.monotonic() - started, 'pid': pid})
   except Exception as error:
    message = repr(error)
    if message not in errors:
     errors.append(message)
   stop.wait(.025)
 observer = threading.Thread(target=observe, daemon=True) if os.name == 'posix' else None
 try:
  if observer is not None:
   observer.start()
  run_started = time.monotonic()
  phase['subprocessStartSeconds'] = run_started - started
  phase['wallStartUnixNs'] = time.time_ns()
  try:
   completed = subprocess.run(cmd, env=env, capture_output=True, timeout=36)
  finally:
   run_elapsed = time.monotonic() - run_started
   phase['wallEndUnixNs'] = time.time_ns()
   phase['subprocessSeconds'] = run_elapsed
  return completed, run_elapsed
 finally:
  if cpu_before is not None:
   cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
   phase['childCPUScope'] = 'RUSAGE_CHILDREN aggregate for waited children; not per-descendant attribution'
   phase['childCPUSeconds'] = {'user': cpu_after.ru_utime - cpu_before.ru_utime,
                               'system': cpu_after.ru_stime - cpu_before.ru_stime}
  stop.set()
  if observer is not None and observer.ident is not None:
   observer.join(timeout=.1)
   phase['observerJoined'] = not observer.is_alive()
  phase['events'], phase['observerErrors'] = list(events), list(errors)
  for name, path in [('helperPids', pids), ('heartbeat', marker)]:
   try:
    phase[name] = {'bytesHex': path.read_bytes().hex(), 'mtimeNs': path.stat().st_mtime_ns}
   except FileNotFoundError:
    phase[name] = None
   except OSError as error:
    phase[name] = {'error': repr(error)}
  phase['observationOverheadSeconds'] = time.monotonic() - started - phase.get('subprocessSeconds', 0)

for mode in ['parent-exit','output','nonzero','deadline']:
 folder=out/mode;root=folder/'workspace';chart=root/'chart';chart.mkdir(parents=True);(chart/'Chart.yaml').write_text('apiVersion: v2\nname: native-helper\nversion: 0.1.0\n');marker=folder/'child-heartbeat';pids=folder/'pids.json';env=dict(os.environ,CD_HELPER_MODE=mode,CD_HELPER_CHILD=str(marker),CD_HELPER_PIDS=str(pids),CD_HELPER_STOP=str(folder/'stop'));cmd=[str(binary),'render-check','--root',str(root),'--chart',str(chart),'--helm-binary',str(helper),'--output',str(folder/'generated'),'--format','json'];stdout=stderr=b'';status=None;phase={};start=time.monotonic()
 try:
  if mode in ['parent-exit','output']:
   p,elapsed=observed_run(cmd,env,pids,marker,phase)
  else:
   p=subprocess.run(cmd,env=env,capture_output=True,timeout=36);elapsed=time.monotonic()-start
  stdout,stderr,status=p.stdout,p.stderr,p.returncode;r=json.loads(stdout);before=marker.read_bytes() if marker.exists() else None;time.sleep(.25);after=marker.read_bytes() if marker.exists() else None;tool=r.get('renderer') or {};checks={'exit':status==(3 if mode=='deadline' else 2),'incomplete':r['analysisComplete'] is False,'reason':bool(r['configurationErrors']),'rawOutput':'renderer-started' in tool.get('stdout','') and 'renderer-stderr' in tool.get('stderr',''),'childStopped':before==after,'boundedOutput':len(tool.get('stdout','').encode())+len(tool.get('stderr','').encode())<=10*1024*1024,'nonzero':mode!='nonzero' or tool.get('exit')==7,'prompt':elapsed<5 if mode in ['parent-exit','output'] else elapsed<35}
  if mode!='nonzero':checks['childSpawned']=pids.exists() and len(json.loads(pids.read_text()))==2;checks['processesReclaimed']=pids.exists() and not any(alive(pid) for pid in json.loads(pids.read_text()))
  rows.append({'id':mode,'command':cmd,'exit':status,'elapsed':elapsed,'checks':checks,'passed':all(checks.values())})
 except Exception as e:
  if isinstance(e,subprocess.TimeoutExpired):stdout,stderr=e.stdout or b'',e.stderr or b''
  rows.append({'id':mode,'command':cmd,'exit':status,'error':repr(e),'passed':False})
 if phase:rows[-1]['phases']=phase
 (folder/'stop').write_text('stop task-owned helpers');time.sleep(.1)
 (folder/'stdout').write_bytes(stdout);(folder/'stderr').write_bytes(stderr)
# Successful rendering must preserve the same public analysis as checking the
# generated file directly, including severity exits and readable diagnostics.
for name, mode, flags, expected_exit, diagnostics in [
 ('render-warning', 'application', [], 0, [('SCHEMA_UNKNOWN_FIELD', 2), ('CD_PROJECT_NAMESPACE_UNBOUND', 3)]),
 ('render-warning-strict', 'application', ['--warnings-as-errors'], 1, [('SCHEMA_UNKNOWN_FIELD', 2), ('CD_PROJECT_NAMESPACE_UNBOUND', 3)]),
 ('render-schema-error', 'application-error', [], 1, [('SCHEMA_TYPE', 1)]),
]:
 folder = out / name
 root = folder / 'workspace'
 chart = root / 'chart'
 chart.mkdir(parents=True)
 (root / '.argocd-ls.yaml').write_text('schemaVersion: 1\ntargetVersion: "3.0.0"\n')
 (chart / 'Chart.yaml').write_text('apiVersion: v2\nname: native-helper\nversion: 0.1.0\n')
 generated = folder / 'generated'
 generated_file = generated / 'generated.yaml'
 marker, pids = folder / 'child-heartbeat', folder / 'pids.json'
 env = dict(os.environ, CD_HELPER_MODE=mode, CD_HELPER_CHILD=str(marker),
            CD_HELPER_PIDS=str(pids), CD_HELPER_STOP=str(folder / 'stop'))
 expected_yaml = ('apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata:\n'
                  '  name: native-report\nspec:\n'
                  + ('  project: 42\n' if mode == 'application-error' else '  mystery: true\n  project: team\n') +
                  '  source: {repoURL: https://example.org/repo}\n'
                  '  destination: {server: https://kubernetes.default.svc, namespace: default}\n')
 render_command = [str(binary), 'render-check', '--root', str(root), '--chart', str(chart),
                   '--helm-binary', str(helper), '--output', str(generated), '--format', 'json', *flags]
 check_command = [str(binary), 'check', '--root', str(generated), '--file', str(generated_file),
                  '--target-version', '3.0.0', '--format', 'json', *flags]
 commands = []
 try:
  rendered = subprocess.run(render_command, env=env, capture_output=True, timeout=15)
  (folder / 'render.json.stdout').write_bytes(rendered.stdout)
  (folder / 'render.json.stderr').write_bytes(rendered.stderr)
  commands.append({'command': render_command, 'exit': rendered.returncode})
  r = json.loads(rendered.stdout)
  checked = subprocess.run(check_command, env=env, capture_output=True, timeout=15)
  (folder / 'check.json.stdout').write_bytes(checked.stdout)
  (folder / 'check.json.stderr').write_bytes(checked.stderr)
  commands.append({'command': check_command, 'exit': checked.returncode})
  direct = json.loads(checked.stdout)
  ignored = {'renderer', 'inputSource', 'elapsedMilliseconds'}
  render_facts = {key: value for key, value in r.items() if key not in ignored}
  check_facts = {key: value for key, value in direct.items() if key not in ignored}
  tool = r.get('renderer') or {}
  checks = {
   'exit': rendered.returncode == checked.returncode == expected_exit,
   'sameAnalysisReport': json.dumps(render_facts, sort_keys=True) == json.dumps(check_facts, sort_keys=True),
   'complete': r['analysisComplete'] is True,
   'counts': type(r['filesAnalyzed']) is int and r['filesAnalyzed'] == 1
             and type(r['resourcesAnalyzed']) is int and r['resourcesAnalyzed'] == 1,
   'configurationErrors': r['configurationErrors'] == [],
   'diagnosticSeverity': sorted((d['code'], d['severity']) for d in r['diagnostics']) == sorted(diagnostics)
                          and all(type(d['severity']) is int for d in r['diagnostics']),
   'diagnosticURI': all(d['uri'] == generated_file.as_uri() for d in r['diagnostics']),
   'target': len(r['targets']) == 1 and r['targets'][0]['version'] == '3.0.0'
             and r['targets'][0]['complete'] is True,
   'inputSource': r['inputSource'] == 'helm-generated' and direct['inputSource'] == 'filesystem',
   'renderer': tool.get('exit') == 0 and tool.get('version') == 'v3.19.0-test'
               and tool.get('binary') == str(helper)
               and tool.get('binarySHA256') == hashlib.sha256(helper.read_bytes()).hexdigest(),
   'renderedBytes': tool.get('stdout') == expected_yaml and tool.get('stderr') == ''
                    and generated_file.read_bytes() == expected_yaml.encode(),
   'noChild': not pids.exists() and not marker.exists(),
   'jsonStderrEmpty': rendered.stderr == checked.stderr == b'',
  }
  for label, command in [('render', render_command), ('check', check_command)]:
   human_command = list(command)
   human_command[human_command.index('--format') + 1] = 'human'
   human = subprocess.run(human_command, env=env, capture_output=True, timeout=15)
   (folder / (label + '.human.stdout')).write_bytes(human.stdout)
   (folder / (label + '.human.stderr')).write_bytes(human.stderr)
   commands.append({'command': human_command, 'exit': human.returncode})
   text = human.stdout.decode('utf-8')
   checks[label + 'HumanExit'] = human.returncode == expected_exit
   checks[label + 'HumanDiagnostics'] = all(text.count(d['code']) == 1 and d['message'] in text
                                            and d['uri'] in text for d in r['diagnostics'])
   checks[label + 'HumanComplete'] = ('analysisComplete=true' in text if label == 'render'
                                      else 'Files analyzed: 1; resources analyzed: 1; analysis complete.' in text)
   checks[label + 'HumanDelivery'] = bool(text) and human.stderr == b''
  rows.append({'id': name, 'rendererFixture': 'controlled native helper; not real Helm',
               'commands': commands, 'checks': checks, 'passed': all(checks.values())})
 except Exception as error:
  rows.append({'id': name, 'commands': commands, 'error': repr(error), 'passed': False})
report={'binarySHA256':hashlib.sha256(binary.read_bytes()).hexdigest(),'helperSHA256':hashlib.sha256(helper.read_bytes()).hexdigest(),'runnerSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'rows':rows,'passed':len(rows)==7 and all(r['passed'] for r in rows)};(out/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'passed':report['passed'],'rows':len(rows)}));sys.exit(0 if report['passed'] else 1)
