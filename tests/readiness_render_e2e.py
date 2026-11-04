#!/usr/bin/env python3
"""Real Helm rendering must propagate local report completion and severity."""
import json,pathlib,subprocess,sys
import audit_repair_e2e as wire
binary=pathlib.Path(sys.argv[1]).resolve();helm=pathlib.Path(sys.argv[2]).resolve();out=pathlib.Path(sys.argv[3]).resolve();out.mkdir(parents=True,exist_ok=False);rows=[]
for name,text,code,complete,codes,flags in [('clean',wire.APP,0,True,['CD_PROJECT_UNRESOLVED'],[]),('schema-error',wire.APP.replace('project: team','project: 42'),1,True,['SCHEMA_TYPE'],[]),('warning',wire.APP.replace('  project: team','  mystery: true\n  project: team'),0,True,['SCHEMA_UNKNOWN_FIELD','CD_PROJECT_UNRESOLVED'],[]),('warning-strict',wire.APP.replace('  project: team','  mystery: true\n  project: team'),1,True,['SCHEMA_UNKNOWN_FIELD','CD_PROJECT_UNRESOLVED'],['--warnings-as-errors']),('post-render-scalar-budget',wire.APP.replace('  name: app','  name: app\n  annotations: {large: '+('x'*1048577)+'}'),2,False,['INPUT_BUDGET_EXCEEDED'],[]),('renderer-cycle',wire.APP.replace('project: team','project: &x [*x]'),2,False,[],[])]:
 folder=out/name;root=folder/'workspace';chart=root/'chart';(chart/'templates').mkdir(parents=True);(chart/'Chart.yaml').write_text('apiVersion: v2\nname: local-readiness\nversion: 0.1.0\n');(chart/'templates/app.yaml').write_text(text)
 command=[str(binary),'render-check','--root',str(root),'--chart',str(chart),'--helm-binary',str(helm),'--output',str(folder/'generated'),'--format','json']+flags
 stdout=stderr=b'';status=None
 try:
  p=subprocess.run(command,capture_output=True,timeout=40);stdout,stderr,status=p.stdout,p.stderr,p.returncode;r=json.loads(stdout);checks={'exit':status==code,'complete':r['analysisComplete']==complete,'codes':sorted(x['code'] for x in r['diagnostics'])==sorted(codes),'input':'helm-generated'==r['inputSource'],'renderer':r['renderer']['exit']==0 and r['renderer']['binarySHA256']==wire.hash_file(helm),'target':len(r['targets'])==1 and r['targets'][0]['version']=='3.5.6' and r['targets'][0]['complete']==complete,'counts':r['resourcesAnalyzed']==(1 if complete else 0)}
  if name=='renderer-cycle':checks.update(renderer=r['renderer']['exit']==1 and r['renderer']['binarySHA256']==wire.hash_file(helm),target=r['targets']==[],reason=[x['code'] for x in r['configurationErrors']]==['RENDER_FAILED'])
  rows.append({'id':name,'command':command,'exit':status,'checks':checks,'passed':all(checks.values())})
 except Exception as e:
  if isinstance(e,subprocess.TimeoutExpired):stdout,stderr=e.stdout or b'',e.stderr or b''
  rows.append({'id':name,'command':command,'exit':status,'error':repr(e),'passed':False})
 (folder/'stdout.raw').write_bytes(stdout);(folder/'stderr.raw').write_bytes(stderr)
report={'binarySHA256':wire.hash_file(binary),'helmSHA256':wire.hash_file(helm),'runnerSHA256':wire.hash_file(pathlib.Path(__file__)),'rows':rows,'passed':len(rows)==6 and all(r['passed'] for r in rows)};(out/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report));sys.exit(0 if report['passed'] else 1)
