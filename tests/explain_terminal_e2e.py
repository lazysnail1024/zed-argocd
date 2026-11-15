#!/usr/bin/env python3
"""Configuration acquisition shares CLI interrupt/deadline exit semantics."""
import ctypes,hashlib,json,os,pathlib,select,signal,subprocess,sys,time
binary=pathlib.Path(sys.argv[1]).resolve();out=pathlib.Path(sys.argv[2]).resolve();out.mkdir(parents=True,exist_ok=False);root=out/'workspace';root.mkdir();(root/'.argocd-ls.yaml').write_text('schemaVersion: 1\ntargetVersion: "3.5.6"\n')
for i in range(10000):(root/f'{i:05}.yaml').write_text('apiVersion: v1\nkind: ConfigMap\n')
rows=[]
for mode in ['interrupt','deadline']:
 libc=ctypes.CDLL(None,use_errno=True)
 libc.inotify_init1.argtypes=[ctypes.c_int];libc.inotify_init1.restype=ctypes.c_int
 libc.inotify_add_watch.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_uint32];libc.inotify_add_watch.restype=ctypes.c_int
 descriptor=libc.inotify_init1(os.O_NONBLOCK|os.O_CLOEXEC)
 if descriptor<0 or libc.inotify_add_watch(descriptor,os.fsencode(root/'00000.yaml'),0x20)<0:raise OSError(ctypes.get_errno(),'inotify setup')
 command=[str(binary),'explain-config','--root',str(root)];p=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 observed=bool(select.select([descriptor],[],[],5)[0])
 if observed:os.read(descriptor,4096)
 os.close(descriptor)
 if not observed or p.poll() is not None:
  if p.poll() is None:p.kill()
  stdout,stderr=p.communicate();rows.append({'id':mode,'checks':{'acquisitionObserved':False},'passed':False});continue
 if mode=='interrupt':p.send_signal(signal.SIGINT)
 else:p.send_signal(signal.SIGSTOP);time.sleep(5.1);p.send_signal(signal.SIGCONT)
 stdout=stderr=b''
 try:
  stdout,stderr=p.communicate(timeout=8);checks={'acquisitionObserved':observed,'exit3':p.returncode==3,'noSuccess':stdout==b'','reason':('context canceled' if mode=='interrupt' else 'context deadline exceeded') in stderr.decode()};rows.append({'id':mode,'command':command,'exit':p.returncode,'checks':checks,'passed':all(checks.values())})
 except Exception as e:
  p.kill();stdout,stderr=p.communicate();rows.append({'id':mode,'command':command,'error':repr(e),'passed':False})
 (out/(mode+'.stdout')).write_bytes(stdout);(out/(mode+'.stderr')).write_bytes(stderr)
report={'binarySHA256':hashlib.sha256(binary.read_bytes()).hexdigest(),'runnerSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'rows':rows,'passed':len(rows)==2 and all(x['passed'] for x in rows)};(out/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report));sys.exit(0 if report['passed'] else 1)
