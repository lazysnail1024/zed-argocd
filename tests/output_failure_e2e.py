#!/usr/bin/env python3
"""Actual stdout device failures must not masquerade as completed CLI delivery."""
import hashlib,json,pathlib,signal,subprocess,sys,time
import audit_repair_e2e as wire
binary=pathlib.Path(sys.argv[1]).resolve();helm=pathlib.Path(sys.argv[2]).resolve();out=pathlib.Path(sys.argv[3]).resolve();out.mkdir(parents=True,exist_ok=False);rows=[]
root=out/'workspace';root.mkdir();wire.write(root,'project.yaml',wire.PROJECT);wire.write(root,'.argocd-ls.yaml',wire.CONFIG);app=wire.write(root,'app.yaml',wire.APP)
chart=root/'chart';wire.write(chart,'Chart.yaml','apiVersion: v2\nname: output-proof\nversion: 0.1.0\n');wire.write(chart,'templates/app.yaml',wire.APP)
cases=[('help',['help'],0),('help-short',['-h'],0),('version-human',['version'],0),('version-json',['version','--json'],0),('explain',['explain-config','--root',str(root)],0)]
for fmt in ['json','human']:
 for kind,code in [('clean',0),('error',1),('warning',0),('warning-strict',1)]:cases.append(('check-'+kind+'-'+fmt,['check','--root',str(root),'--format',fmt]+(['--warnings-as-errors'] if kind=='warning-strict' else []),code))
 cases.append(('render-'+fmt,['render-check','--root',str(root),'--chart',str(chart),'--helm-binary',str(helm),'--output',str(out/('generated-'+fmt)),'--format',fmt],0))
cases.append(('render-failure-json',['render-check','--root',str(root),'--chart',str(chart),'--helm-binary',str(helm),'--output',str(out/'generated-failure'),'--format','json'],2))
try:
 for name,args,normalExit in cases:
  (chart/'templates/app.yaml').write_text(wire.APP.replace('project: team','project: &x [*x]') if name=='render-failure-json' else wire.APP)
  app.write_text(wire.APP.replace('project: team','project: 42') if name.startswith('check-error') else wire.APP.replace('  project: team','  mystery: true\n  project: team') if name.startswith('check-warning') else wire.APP)
  for full in [False,True]:
   ident=name+('-full' if full else '-file');folder=out/ident;folder.mkdir();cmd=[str(binary)]+args;stdout=folder/'stdout.raw';stdout.write_bytes(b'');stderr=b'';status=None
   try:
    with (open('/dev/full','wb') if full else stdout.open('wb')) as sink:p=subprocess.run(cmd,stdout=sink,stderr=subprocess.PIPE,timeout=40)
    stderr,status=p.stderr,p.returncode;checks={'exit':status==(2 if full else normalExit),'stderr':('no space left on device' in stderr.decode().lower()) if full else stderr==b'','stdout':stdout.stat().st_size==0 if full else stdout.stat().st_size>0}
    if not full and name.startswith('check-') and name.endswith('json'):
     r=json.loads(stdout.read_text());checks.update(complete=r['analysisComplete'],resources=r['resourcesAnalyzed']==2,configuration=r['configurationErrors']==[])
     if 'warning' in name:checks['warningSeverity']=[d['severity'] for d in r['diagnostics']]==[2]
    rows.append({'id':ident,'command':cmd,'stdoutDestination':'/dev/full' if full else str(stdout),'exit':status,'checks':checks,'passed':all(checks.values())})
   except Exception as e:
    if isinstance(e,subprocess.TimeoutExpired):stderr=e.stderr or b''
    rows.append({'id':ident,'command':cmd,'exit':status,'error':repr(e),'passed':False})
   (folder/'stderr.raw').write_bytes(stderr)
 # The real check deadline still takes precedence when its report cannot be written.
 deadline=out/'deadline';deadline.mkdir()
 for i in range(1000):(deadline/f'{i:04}.yaml').write_text(wire.APP)
 for fmt in ['json','human']:
  name='deadline-'+fmt;cmd=[str(binary),'check','--root',str(deadline),'--format',fmt];stderr=b''
  with open('/dev/full','wb') as sink:
   p=subprocess.Popen(cmd,stdout=sink,stderr=subprocess.PIPE);time.sleep(.012);p.send_signal(signal.SIGSTOP);time.sleep(5.1);p.send_signal(signal.SIGCONT)
   try:
    _,stderr=p.communicate(timeout=8);checks={'exit3':p.returncode==3,'writeReason':'no space left on device' in stderr.decode().lower()};rows.append({'id':name,'command':cmd,'exit':p.returncode,'checks':checks,'passed':all(checks.values())})
   except Exception as e:p.kill();_,stderr=p.communicate();rows.append({'id':name,'error':repr(e),'passed':False})
  (out/(name+'.stdout.raw')).write_bytes(b'');(out/(name+'.stderr.raw')).write_bytes(stderr)
except Exception as e:rows.append({'id':'runner','error':repr(e),'passed':False})
finally:
 sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest();report={'binarySHA256':sha(binary),'helmSHA256':sha(helm),'runnerSHA256':sha(pathlib.Path(__file__)),'wireSHA256':sha(pathlib.Path(wire.__file__)),'rows':rows,'passed':len(rows)==34 and all(r['passed'] for r in rows)};(out/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'rows':len(rows),'failed':[r['id'] for r in rows if not r['passed']]}))
sys.exit(0 if report['passed'] else 1)
