#!/usr/bin/env python3
"""Real Helm rendering must propagate local report completion and severity."""
import json,pathlib,subprocess,sys
import audit_repair_e2e as wire
binary=pathlib.Path(sys.argv[1]).resolve();helm=pathlib.Path(sys.argv[2]).resolve();out=pathlib.Path(sys.argv[3]).resolve();out.mkdir(parents=True,exist_ok=False);rows=[]
for name,text,code,complete,codes,flags in [('clean',wire.APP,0,True,['CD_PROJECT_NAMESPACE_UNBOUND'],[]),('schema-error',wire.APP.replace('project: team','project: 42'),1,True,['SCHEMA_TYPE'],[]),('warning',wire.APP.replace('  project: team','  mystery: true\n  project: team'),0,True,['SCHEMA_UNKNOWN_FIELD','CD_PROJECT_NAMESPACE_UNBOUND'],[]),('warning-strict',wire.APP.replace('  project: team','  mystery: true\n  project: team'),1,True,['SCHEMA_UNKNOWN_FIELD','CD_PROJECT_NAMESPACE_UNBOUND'],['--warnings-as-errors']),('post-render-scalar-budget',wire.APP.replace('  name: app','  name: app\n  annotations: {large: '+('x'*1048577)+'}'),2,False,['INPUT_BUDGET_EXCEEDED'],[]),('renderer-cycle',wire.APP.replace('project: team','project: &x [*x]'),2,False,[],[])]:
 folder=out/name;root=folder/'workspace';chart=root/'chart';(chart/'templates').mkdir(parents=True);(chart/'Chart.yaml').write_text('apiVersion: v2\nname: local-readiness\nversion: 0.1.0\n');(chart/'templates/app.yaml').write_text(text)
 command=[str(binary),'render-check','--root',str(root),'--chart',str(chart),'--helm-binary',str(helm),'--output',str(folder/'generated'),'--format','json']+flags
 stdout=stderr=b'';status=None
 try:
  p=subprocess.run(command,capture_output=True,timeout=40);stdout,stderr,status=p.stdout,p.stderr,p.returncode;r=json.loads(stdout);checks={'exit':status==code,'complete':r['analysisComplete']==complete,'codes':sorted(x['code'] for x in r['diagnostics'])==sorted(codes),'input':'helm-generated'==r['inputSource'],'renderer':r['renderer']['exit']==0 and r['renderer']['binarySHA256']==wire.hash_file(helm),'target':len(r['targets'])==1 and r['targets'][0]['version']=='auto' and r['targets'][0]['complete']==complete,'counts':r['resourcesAnalyzed']==(1 if complete else 0)}
  if name=='renderer-cycle':checks.update(renderer=r['renderer']['exit']==1 and r['renderer']['binarySHA256']==wire.hash_file(helm),target=r['targets']==[],reason=[x['code'] for x in r['configurationErrors']]==['RENDER_FAILED'])
  else:
   # Compare actual public reports; rendering must not alter analysis facts or
   # turn typed diagnostics into a different JSON shape at an internal boundary.
   direct=[str(binary),'check','--root',str(folder/'generated'),'--file',str(folder/'generated/generated.yaml'),'--target-version',r['targets'][0]['version'],'--format','json']+flags
   checked=subprocess.run(direct,capture_output=True,timeout=15);(folder/'check.stdout').write_bytes(checked.stdout);(folder/'check.stderr').write_bytes(checked.stderr)
   comparable=lambda report:{k:v for k,v in report.items() if k not in ['renderer','inputSource','elapsedMilliseconds']}
   checks['sameAnalysisReport']=checked.returncode==code and comparable(json.loads(checked.stdout))==comparable(r)
   for label,args in [('render',command),('check',direct)]:
    human=list(args);human[human.index('--format')+1]='human';delivered=subprocess.run(human,capture_output=True,timeout=40);(folder/(label+'.human.stdout')).write_bytes(delivered.stdout);(folder/(label+'.human.stderr')).write_bytes(delivered.stderr)
    output=delivered.stdout.decode();checks[label+'HumanExit']=delivered.returncode==code;checks[label+'HumanDiagnostics']=all(d['code'] in output and d['message'] in output for d in r['diagnostics']);checks[label+'HumanConfiguration']=all(e['code'] in output and e['message'] in output for e in r['configurationErrors']);checks[label+'HumanDelivery']=bool(output) and delivered.stderr==b''
  rows.append({'id':name,'command':command,'exit':status,'checks':checks,'passed':all(checks.values())})
 except Exception as e:
  if isinstance(e,subprocess.TimeoutExpired):stdout,stderr=e.stdout or b'',e.stderr or b''
  rows.append({'id':name,'command':command,'exit':status,'error':repr(e),'passed':False})
 (folder/'stdout.raw').write_bytes(stdout);(folder/'stderr.raw').write_bytes(stderr)
report={'binarySHA256':wire.hash_file(binary),'helmSHA256':wire.hash_file(helm),'runnerSHA256':wire.hash_file(pathlib.Path(__file__)),'rows':rows,'passed':len(rows)==6 and all(r['passed'] for r in rows)};(out/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report));sys.exit(0 if report['passed'] else 1)
