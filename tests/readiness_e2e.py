#!/usr/bin/env python3
"""Public local-readiness, default selection and completed-report behavior."""
import argparse,hashlib,json,pathlib,subprocess,sys
import audit_repair_e2e as wire
parser=argparse.ArgumentParser();parser.add_argument('binary',type=pathlib.Path);parser.add_argument('output',type=pathlib.Path);parser.add_argument('--expected-source-commit',required=True,help='Exact independently observed Git HEAD or none');parser.add_argument('--expected-dirty',required=True,choices=['true','false']);parser.add_argument('--expected-build-mode',required=True,choices=['development','release']);args=parser.parse_args();expected={'sourceCommit':None if args.expected_source_commit=='none' else args.expected_source_commit,'dirty':args.expected_dirty=='true','buildMode':args.expected_build_mode}
binary=args.binary.resolve();out=args.output.resolve();out.mkdir(parents=True,exist_ok=False);rows=[]
APP=wire.APP;PROJECT=wire.PROJECT
catalog=json.loads((pathlib.Path(__file__).resolve().parents[1]/'compat/catalog.lock.json').read_text());expected_window={'min':catalog['versions'][0]['version'],'max':catalog['latestVersion'],'majors':catalog['supportedMajors'],'releaseCount':len(catalog['versions'])}
CONFIG='schemaVersion: 1\ncontrolPlaneNamespace: argocd\nreferencePolicy: closed\n'
def row(name,checks,observed):
 rows.append({'id':name,'checks':checks,'observed':observed,'passed':all(checks.values())})
def run(name,args,files=None):
 folder=out/name;root=folder/'workspace';root.mkdir(parents=True)
 for path,text in (files or {}).items():wire.write(root,path,text)
 command=[str(binary)]+[str(root) if x=='ROOT' else x for x in args]
 try:
  p=subprocess.run(command,capture_output=True,timeout=12);stdout,stderr=p.stdout,p.stderr;status=p.returncode
 except subprocess.TimeoutExpired as e:stdout=e.stdout or b'';stderr=e.stderr or b'';status='timeout'
 except OSError as e:stdout=b'';stderr=str(e).encode();status='launch-failed'
 (folder/'stdout.raw').write_bytes(stdout);(folder/'stderr.raw').write_bytes(stderr);(folder/'command.json').write_text(json.dumps(command))
 try:report=json.loads(stdout)
 except Exception:report={}
 return status,report,stdout.decode(errors='replace'),root
cases=[
 ('clean',{'app.yaml':APP,'project.yaml':PROJECT},0,True,3,2,[],{}),
 ('schema-error',{'app.yaml':APP.replace('project: team','project: 42')},1,True,2,1,['SCHEMA_TYPE'],{}),
 ('warning',{'app.yaml':APP.replace('  project: team','  mystery: true\n  project: team'),'project.yaml':PROJECT},0,True,3,2,['SCHEMA_UNKNOWN_FIELD'],{}),
 ('warning-strict',{'app.yaml':APP.replace('  project: team','  mystery: true\n  project: team'),'project.yaml':PROJECT},1,True,3,2,['SCHEMA_UNKNOWN_FIELD'],{'flags':['--warnings-as-errors']}),
 ('domain-error',{'app.yaml':APP},1,True,2,1,['CD_PROJECT_UNRESOLVED'],{}),
 ('projection',{'app.yaml':APP.replace('project: team','project: &x [*x]')},2,False,1,0,['YAML_PROJECTION_INCOMPLETE'],{}),
 ('unknown-malformed',{'app.yaml':'metadata: [\n'},2,False,1,0,[],{}),
 ('foreign',{'foreign.yaml':'apiVersion: v1\nkind: ConfigMap\nmetadata: {name: x}\n'},0,True,2,0,[],{}),
 ('zero',{},0,True,0,0,[],{'noConfig':True}),
 ('mixed',{'mixed.yaml':APP+'---\napiVersion: v1\nkind: ConfigMap\nmetadata: {name: x}\n','project.yaml':PROJECT},0,True,3,2,[],{}),
 ('file-dependencies',{'app.yaml':APP,'project.yaml':PROJECT},0,True,3,2,[],{'flags':['--file','app.yaml']}),
 ('file-output-filter',{'app.yaml':APP,'project.yaml':PROJECT.replace('spec:\n','spec:\n  mystery: true\n')},0,True,3,2,[],{'flags':['--file','app.yaml']}),
 ('scope-partial',{'good/app.yaml':APP,'good/project.yaml':PROJECT,'bad/app.yaml':APP.replace('project: team','project: &x [*x]')},2,False,3,2,['YAML_PROJECTION_INCOMPLETE'],{'config':CONFIG+'scopes:\n- name: good\n  include: ["good/**"]\n- name: bad\n  include: ["bad/**"]\n','targetComplete':{'good':True,'bad':False,'unassigned':True}}),
]
try:
 status,identity,_,_=run('identity',['version','--json']);row('identity',{'exit0':status==0,'ready':identity.get('analysisReady') is True,'config':[1]==identity.get('configSchemaVersions'),'report':[1]==identity.get('reportSchemaVersions'),'sourceHonest':all(identity.get(k)==v for k,v in expected.items()),'product':identity.get('product')=='argo-cd','autoIdentity':identity.get('defaultTargetVersion')=='auto','supportWindow':identity.get('supportedVersions')==expected_window},identity)
 for name,files,exitcode,complete,count,resources,codes,options in cases:
  files=dict(files)
  if not options.get('noConfig'):files['.argocd-ls.yaml']=options.get('config',CONFIG)
  status,r,_,_=run(name,['check','--root','ROOT','--format','json']+options.get('flags',[]),files)
  expected_targets=options.get('targetComplete',{'root':complete})
  checks={'exit':status==exitcode,'complete':r.get('analysisComplete')==complete,'files':r.get('filesAnalyzed')==count,'resources':r.get('resourcesAnalyzed')==resources,'codes':sorted(d['code'] for d in r.get('diagnostics',[]))==sorted(codes),'noBootstrapError':all(e['code']!='TARGET_NOT_VERIFIED' for e in r.get('configurationErrors',[])),'targets':{t['scope']:t['complete'] for t in r.get('targets',[])}==expected_targets,'default':all(t['version']=='auto' and t['source']=='default' for t in r.get('targets',[]))}
  row(name,checks,{'exit':status,'report':r})
 for name,files,args,code in [('invalid-config',{'.argocd-ls.yaml':'schemaVersion: 9\n'},[],'CONFIG_INVALID'),('unknown-target',{},['--target-version','9.9.9'],'TARGET_UNKNOWN'),('missing-file',{},['--file','absent.yaml'],'INPUT_INCOMPLETE')]:
  status,r,_,_=run(name,['check','--root','ROOT','--format','json']+args,files);row(name,{'exit2':status==2,'incomplete':r.get('analysisComplete') is False,'reason':[e['code'] for e in r.get('configurationErrors',[])]==[code],'noWork':r.get('resourcesAnalyzed')==0 and r.get('filesAnalyzed')==0},r)
 status,r,_,_=run('explain-default',['explain-config','--root','ROOT']);row('explain-default',{'exit0':status==0,'ready':r.get('analysisReady') is True,'default':len(r.get('targets',[]))==1 and r['targets'][0]['version']=='auto' and r['targets'][0]['source']=='default'},r)
 status,_,text,_=run('human-clean',['check','--root','ROOT'],{'app.yaml':APP});row('human-clean',{'exit0':status==0,'completeSummary':'analysis complete' in text and 'analysis incomplete' not in text},text)
 folder=out/'lsp-default';root=folder/'workspace';root.mkdir(parents=True);app=wire.write(root,'app.yaml',APP);wire.BINARY=binary;client=None
 try:
  client=wire.LSP(folder,[root]);client.open(app,APP,1);diagnostics=client.diag(app,1);hover=client.request('textDocument/hover',{'textDocument':{'uri':app.as_uri()},'position':{'line':5,'character':4}});initialize=next(x['message'] for x in client.messages if x['message'].get('id')==1);caps=initialize['result']['capabilities'];row('lsp-default',{'informationOnly':[d['severity'] for d in diagnostics]==[3],'target':'auto' in json.dumps(hover),'capabilities':all(k in caps and caps[k] is not False for k in ['hoverProvider','definitionProvider','referencesProvider','documentSymbolProvider','completionProvider','codeActionProvider']),'privateRenamePrepare':caps.get('renameProvider',{}).get('prepareProvider')},{'diagnostics':diagnostics,'hover':hover,'initialize':initialize})
 finally:
  if client:client.close();row('lsp-exit',{'exit0':client.proc.returncode==0},client.proc.returncode)
except Exception as e:row('runner',{'completed':False},repr(e))
finally:
 report={'expectedProvenance':expected,'binarySHA256':wire.hash_file(binary),'runnerSHA256':wire.hash_file(pathlib.Path(__file__)),'wireSHA256':wire.hash_file(pathlib.Path(wire.__file__)),'rows':rows,'passed':len(rows)==21 and all(x['passed'] for x in rows)};(out/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'rows':len(rows),'failed':[x['id'] for x in rows if not x['passed']]}))
sys.exit(0 if report['passed'] else 1)
