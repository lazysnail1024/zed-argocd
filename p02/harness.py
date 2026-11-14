#!/usr/bin/env python3
"""Synthetic fixtures and real gate CLIs only. Zero product executions."""
import argparse,hashlib,json,os,pathlib,subprocess,sys,time,tarfile,shutil
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
import ci_shards_e2e as fixture

def save(path,value):
 path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value,indent=2)+'\n')
def run(out,label,cmd):
 out.mkdir(parents=True,exist_ok=True);r=subprocess.run(list(map(str,cmd)),cwd=ROOT,capture_output=True);(out/(label+'.stdout')).write_bytes(r.stdout);(out/(label+'.stderr')).write_bytes(r.stderr);save(out/(label+'.json'),{'command':list(map(str,cmd)),'exit':r.returncode,'scope':'Synthetic verifier fixture; no product execution'});return r.returncode

def main():
 p=argparse.ArgumentParser();p.add_argument('command',choices=['prepare','fixtures','finish','wait-cancel','native','zed']);p.add_argument('--output',type=pathlib.Path,required=True);p.add_argument('--mode',default='scenario');a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=True)
 for name,digest in json.loads((ROOT/'p02/frozen-inputs.json').read_text()).items():
  assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest, name
 if a.command=='prepare':
  scenario=json.loads((ROOT/'p02/scenario.json').read_text())['scenario']
  if a.mode!='scenario':
   assert os.environ['GITHUB_EVENT_NAME']=='workflow_dispatch';scenario='success'
  save(out/'scenario.json',{'scenario':scenario,'scope':'Synthetic fixtures and orchestration only; no product acceptance'})
  if os.environ.get('GITHUB_OUTPUT'):
   with open(os.environ['GITHUB_OUTPUT'],'a') as f:f.write('scenario='+scenario+'\n')
  return run(out,'plan-create',[sys.executable,ROOT/'tools/ci_plan.py','create','--output',out/'plan.json'])
 scenario=json.loads((out/'scenario.json').read_text())['scenario']
 if a.command=='fixtures':
  src,head,tree,digest=fixture.fixture_source(out);reports,assets=fixture.fixtures(head,tree,digest)
  # Existing synthetic factory writes complete child reports and invokes the genuine gate.
  control=fixture.invoke(src,'complete',reports,assets,head,True);assert control['passed'],control
  folder=src/'artifacts/complete';report=next((folder/'reports').glob('*/report.json'))
  if scenario=='missing-report':report.unlink()
  if scenario=='wrong-sha':
   plan=json.loads((out/'plan.json').read_text());plan['testedSHA']='0'*40;save(out/'plan.json',plan)
   value=json.loads(report.read_text());value['expectedProvenance']['sourceCommit']='0'*40;save(report,value)
  save(out/'fixture.json',{'head':head,'version':(ROOT/'VERSION').read_text().strip(),'scope':'Synthetic five-platform reports; no platform builds/tests','positiveControl':control})
  with tarfile.open(out/'fixture.tar.gz','w:gz') as archive:archive.add(src,arcname='synthetic-source')
  shutil.rmtree(src);return 0
 if a.command=='finish':return 11 if scenario in ['upstream-failure','manual-green'] else 0
 if a.command=='wait-cancel':
  print('Ready for real upstream cancellation',flush=True);time.sleep(600);raise RuntimeError('No actual cancellation observed')
 needs=json.loads(os.environ['P02_NEEDS_JSON']);save(out/(a.command+'-raw-needs.json'),needs)
 if a.command=='zed':
  # Exact production adapter acceptance expression; actual upstream result on both edges.
  result=needs['upstream']['result'];env=dict(os.environ,RUST_RESULT=result,PACKAGE_RESULT=result)
  r=subprocess.run(['bash','-c','test "$RUST_RESULT" = success && test "$PACKAGE_RESULT" = success'],env=env,capture_output=True)
  save(out/'zed-gate.json',{'exit':r.returncode,'actualUpstream':result,'scope':'Production shell predicate on one synthetic upstream; not adapter execution'});return r.returncode
 mapped={'prepare':{'result':needs['prepare']['result']},**{k:{'result':needs['upstream']['result']} for k in ['native','orthogonal','resources','ordinary']}}
 code=run(out,'plan-verify',[sys.executable,ROOT/'tools/ci_plan.py','verify','--plan',out/'plan.json','--needs',json.dumps(mapped)])
 if code:return code
 with tarfile.open(out/'fixture.tar.gz') as archive:archive.extractall(out,filter='data')
 info=json.loads((out/'fixture.json').read_text());src=out/'synthetic-source';folder=src/'artifacts/complete'
 return run(out,'native-gate',[sys.executable,src/'tools/ci_gate.py','--inputs',folder/'reports','--assets',folder/'assets','--source-commit',info['head'],'--version',info['version'],'--run-id','123','--run-attempt','1','--jobs-result',needs['upstream']['result'],'--output',folder/'p02-accepted.json'])
if __name__=='__main__':sys.exit(main())
