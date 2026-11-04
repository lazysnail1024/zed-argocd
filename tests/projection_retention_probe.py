#!/usr/bin/env python3
"""Bounded observation of retained expanded documents, not latency acceptance."""
import hashlib,json,os,pathlib,subprocess,sys,threading,time
binary=pathlib.Path(sys.argv[1]).resolve();out=pathlib.Path(sys.argv[2]).resolve();out.mkdir(parents=True,exist_ok=False);work=out/'workspace';work.mkdir();cases=json.loads(pathlib.Path(__file__).with_name('alias-projection-cases.json').read_text())['cases'];text=next(c['text'] for c in cases if c['id']=='expanded-boundary--1');(work/'input.yaml').write_text('---\n'.join([text]*50));cmd=[str(binary),'check','--root',str(work),'--target-version','3.5.6','--format','json'];report={'command':cmd,'binarySHA256':hashlib.sha256(binary.read_bytes()).hexdigest(),'inputSHA256':hashlib.sha256((work/'input.yaml').read_bytes()).hexdigest(),'documents':50,'logicalNodesPerDocument':99999,'watchdog':False,'peakRSS':0};membership=pathlib.Path('/proc/self/cgroup').read_text();cg=pathlib.Path('/sys/fs/cgroup')/membership.strip().split('::',1)[1].lstrip('/');report['constraints']={n:(cg/n).read_text().strip() for n in ['memory.max','cpu.max']};assert report['constraints']['memory.max']==str(512*1024*1024);p=None;done=threading.Event()
try:
 with (out/'stdout').open('wb') as stdout,(out/'stderr').open('wb') as stderr:
  p=subprocess.Popen(cmd,stdout=stdout,stderr=stderr)
  def monitor():
   while not done.wait(.005):
    try:rss=int(pathlib.Path(f'/proc/{p.pid}/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')
    except (OSError,IndexError):continue
    report['peakRSS']=max(report['peakRSS'],rss)
    if rss>384*1024*1024 and p.poll() is None:report['watchdog']=True;p.kill();return
  monitorThread=threading.Thread(target=monitor);monitorThread.start();p.wait(timeout=8)
except Exception as e:report['error']=repr(e)
finally:
 done.set()
 if p:
  if p.poll() is None:report['harnessKilled']=True;p.kill()
  p.wait();report['exitCode']=p.returncode
 if 'monitorThread' in locals():monitorThread.join(timeout=1)
 try:report['observed']=json.loads((out/'stdout').read_text())
 except Exception:pass
 report['bounded']=not report['watchdog'] and not report.get('harnessKilled') and report.get('exitCode') in [2,3] and report.get('observed',{}).get('analysisComplete') is False;report['runnerSHA256']=hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest();(out/'report.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({k:report.get(k) for k in ['bounded','peakRSS','watchdog','exitCode','error']}));sys.exit(0 if report['bounded'] else 1)
