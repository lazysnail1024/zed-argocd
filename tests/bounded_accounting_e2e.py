#!/usr/bin/env python3
"""Exact completed-work accounting and actual deadline precedence."""
import hashlib,json,pathlib,signal,subprocess,sys,time
binary=pathlib.Path(sys.argv[1]).resolve();out=pathlib.Path(sys.argv[2]).resolve();out.mkdir(parents=True,exist_ok=True);rows=[]
base='apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata: {name: app}\nspec: {project: absent, source: {repoURL: https://example.org/repo}, destination: {server: here}}\n'
for name,text,files,resources,codes in [('errors-count',base.replace('project: absent','project: 42'),1,1,['SCHEMA_TYPE']),('projection-skipped',base.replace('project: absent','project: &p [*p]'),0,0,['YAML_PROJECTION_INCOMPLETE']),('mixed-completion',base+'---\n'+base.replace('project: absent','project: &p [*p]'),0,1,['YAML_PROJECTION_INCOMPLETE'])]:
 folder=out/name;folder.mkdir(exist_ok=True);(folder/'input.yaml').write_text(text);cmd=[str(binary),'check','--root',str(folder),'--target-version','3.5.6','--format','json'];p=subprocess.run(cmd,capture_output=True,timeout=12);(folder/'stdout.raw').write_bytes(p.stdout);(folder/'stderr.raw').write_bytes(p.stderr)
 try:
  r=json.loads(p.stdout);assertions={'exit':p.returncode==(1 if name=='errors-count' else 2),'files':r['filesAnalyzed']==files,'resources':r['resourcesAnalyzed']==resources,'codes':sorted(d['code'] for d in r['diagnostics'])==sorted(codes),'complete':r['analysisComplete']==(name=='errors-count')};rows.append({'name':name,'command':cmd,'assertions':assertions,'passed':all(assertions.values())})
 except Exception as e:rows.append({'name':name,'error':repr(e),'passed':False})
# Stop the actual CLI while its real five-second deadline advances. No private clock or limits override.
folder=out/'deadline';folder.mkdir(exist_ok=True)
for i in range(1000):(folder/f'{i}.yaml').write_text(base)
cmd=[str(binary),'check','--root',str(folder),'--target-version','3.5.6','--format','json'];p=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE);time.sleep(.025);p.send_signal(signal.SIGSTOP);time.sleep(5.1);p.send_signal(signal.SIGCONT)
try:
 stdout,stderr=p.communicate(timeout=5);(folder/'stdout.raw').write_bytes(stdout);(folder/'stderr.raw').write_bytes(stderr);r=json.loads(stdout);assertions={'exit3':p.returncode==3,'deadlineReason':'ANALYSIS_DEADLINE_EXCEEDED' in [e['code'] for e in r['configurationErrors']],'noReadinessSuccess':'TARGET_NOT_VERIFIED' not in [e['code'] for e in r['configurationErrors']],'notAllCounted':r['resourcesAnalyzed']<1000,'incomplete':not r['analysisComplete']};rows.append({'name':'deadline','command':cmd,'assertions':assertions,'passed':all(assertions.values())})
except Exception as e:p.kill();p.communicate();rows.append({'name':'deadline','error':repr(e),'passed':False})
report={'binarySHA256':hashlib.sha256(binary.read_bytes()).hexdigest(),'runnerSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'rows':rows,'passed':len(rows)==4 and all(r['passed'] for r in rows)};(out/'report.json').write_text(json.dumps(report,indent=2));print(json.dumps(report));sys.exit(0 if report['passed'] else 1)
