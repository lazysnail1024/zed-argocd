#!/usr/bin/env python3
"""Independent full-diagnostic CLI matrix for every pinned patch; no production imports."""
import argparse,collections,concurrent.futures,hashlib,json,os,pathlib,subprocess,sys,shutil
ROOT=pathlib.Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser();parser.add_argument('binary');parser.add_argument('--versions',nargs='*');parser.add_argument('--output',default=str(ROOT/'artifacts/domain-matrix/cli'));parser.add_argument('--timeout-seconds',type=float,default=20);args=parser.parse_args()
BINARY=pathlib.Path(args.binary).resolve();OUT=pathlib.Path(args.output).resolve();OUT.mkdir(parents=True,exist_ok=True)
catalog=json.loads((ROOT/'compat/catalog.lock.json').read_text());sources=json.loads((ROOT/'compat/local-rules-sources.json').read_text());cases=json.loads((ROOT/'tests/domain-matrix-cases.json').read_text())['cases'];versions=[v for v in catalog['versions'] if not args.versions or v['version'] in args.versions]
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
for source in [pathlib.Path(__file__), ROOT/'tests/domain-matrix-cases.json', ROOT/'compat/local-rules-sources.json']:
 (OUT/source.name).write_bytes(source.read_bytes())
selectionError=None
if args.versions==[] or not versions or not cases:selectionError='Selection must contain at least one known version and scenario'
if args.versions and set(args.versions)-{v['version'] for v in versions}:selectionError='Selection contains unknown target versions'
if args.timeout_seconds<=0:selectionError='Process timeout must be positive'

def expected(case,version):return case['expected35'] if version in ['3.5.0','3.5.1','3.5.2','3.5.3','3.5.4','3.5.5','3.5.6'] and case['expected35'] is not None else case['expected']
def run(pair):
 version,case=pair;out=OUT/version['version']/case['id'];root=out/'workspace'
 if root.exists():shutil.rmtree(root)
 root.mkdir(parents=True,exist_ok=True);cfg='schemaVersion: 1\ntargetVersion: "'+version['version']+'"\n'
 if 'controlPlaneNamespace:' not in case['config']:cfg+='controlPlaneNamespace: argocd\n'
 (root/'.argocd-ls.yaml').write_text(cfg+case['config']);(root/'input.yaml').write_text(json.dumps(case['application'],indent=2)+'\n')
 if case['project'] is not None:(root/'project.yaml').write_text(json.dumps(case['project'],indent=2)+'\n')
 for name,value in case['files'].items():
  p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(value)
 cmd=[str(BINARY),'check','--root',str(root),'--format','json'];want=expected(case,version['version']);errors=[];observed=None;status='exited';exit_code=None;stdout=b'';stderr=b''
 try:
  proc=subprocess.run(cmd,capture_output=True,timeout=args.timeout_seconds);exit_code=proc.returncode;stdout=proc.stdout;stderr=proc.stderr
 except subprocess.TimeoutExpired as ex:
  status='timeout';stdout=ex.stdout or b'';stderr=ex.stderr or b'';errors.append('Process exceeded timeout')
 except OSError as ex:
  status='launch-failed';errors.append(repr(ex))
 (out/'stdout').write_bytes(stdout);(out/'stderr').write_bytes(stderr)
 try:
  assert status=='exited',status
  report=json.loads(stdout);observed=sorted([[x['code'],x['severity']] for x in report['diagnostics']]);assert observed==sorted(want),('diagnostics',want,observed)
  assert exit_code==(1 if any(severity==1 for _,severity in want) else 0) and report['analysisComplete'] is True,'complete local envelope'
  assert report['configurationErrors']==[] and report['product']=='argo-cd'
  assert report['resourcesAnalyzed']==1+int(case['project'] is not None),'all input resources analyzed'
  assert report['filesAnalyzed']>=1+int(case['project'] is not None),'all YAML inputs analyzed'
  assert report['elapsedMilliseconds']<5000,'no deadline or budget skipping'
  assert len(report['targets'])==1 and report['targets'][0]['version']==version['version'],'target pin'
  assert report['targets'][0]['complete'] is True,'completed target'
  for d in report['diagnostics']:
   assert d['source']=='argocd-ls' and d['uri'] in [(root/'input.yaml').as_uri(),(root/'project.yaml').as_uri()]
   assert d['range']['start']['line']<=d['range']['end']['line'],'range ordering'
  if case['id']=='invalid-ref-dot':
   text=(root/'input.yaml').read_text();line=next(i for i,s in enumerate(text.splitlines()) if '"bad.ref"' in s);column=text.splitlines()[line].index('"bad.ref"');assert report['diagnostics'][0]['range']=={'start':{'line':line,'character':column},'end':{'line':line,'character':column+9}},'exact original scalar range'
 except Exception as ex:errors.append(repr(ex))
 row={'id':case['id'],'version':version['version'],'command':cmd,'expected':want,'observed':observed,'exit':exit_code,'processStatus':status,'errors':errors,'passed':not errors,'upstreamCommit':version['upstreamCommit'],'sourceEvidence':sources['versions'][version['version']],'inputSHA256':{str(p.relative_to(root)):sha(p) for p in root.rglob('*') if p.is_file()},'stdoutSHA256':sha(out/'stdout'),'stderrSHA256':sha(out/'stderr')};(out/'result.json').write_text(json.dumps(row,indent=2)+'\n');return {k:row[k] for k in ['id','version','passed','errors']}
rows=[];runnerError=selectionError
if selectionError is None:
 try:
  with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
   jobs={pool.submit(run,(v,c)):(v,c) for v in versions for c in cases}
   for future in concurrent.futures.as_completed(jobs):
    v,c=jobs[future]
    try:rows.append(future.result())
    except Exception as ex:
     row={'id':c['id'],'version':v['version'],'passed':False,'processStatus':'runner-failed','errors':[repr(ex)]};rows.append(row)
     folder=OUT/v['version']/c['id'];folder.mkdir(parents=True,exist_ok=True);(folder/'result.json').write_text(json.dumps(row,indent=2)+'\n')
 except Exception as ex:runnerError=repr(ex)
rows.sort(key=lambda r:(tuple(map(int,r['version'].split('.'))),r['id']))
report={'command':sys.argv,'python':sys.version,'binarySHA256':sha(BINARY),'testSHA256':sha(pathlib.Path(__file__)),'expectationsSHA256':sha(ROOT/'tests/domain-matrix-cases.json'),'upstreamEvidenceSHA256':sha(ROOT/'compat/local-rules-sources.json'),'versions':[v['version'] for v in versions],'caseCount':len(cases),'results':rows,'runnerError':runnerError,'passed':runnerError is None and bool(rows) and len(rows)==len(versions)*len(cases) and all(r['passed'] for r in rows)};(OUT/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'rows':len(rows),'failures':[x for x in rows if not x['passed']]},indent=2));sys.exit(0 if report['passed'] else 1)
