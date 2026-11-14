#!/usr/bin/env python3
"""Exercise conservative selection through the real planner CLI."""
import argparse,json,subprocess,sys,os,shutil,copy
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
 p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False);rows=[]
 cases=[('docs',[['M','README.md']],False),('mixed',[['M','README.md'],['M','cmd/main.go']],True),('snippet',[['M','editors/zed/snippets/yaml.json']],True),('header',[['M','vendor/x.h']],True),('unknown',[['M','new-file']],True),('rename',[['R100','README.md','other.md']],True),('delete',[['D','README.md']],True),('empty',[],True),('failed',None,True)]
 for name,changes,full in cases:
  source=a.output/(name+'.json');source.write_text(json.dumps(changes));command=[sys.executable,str(ROOT/'tools/ci_plan.py'),'classify','--changes',str(source)];r=subprocess.run(command,capture_output=True,timeout=10);(a.output/(name+'.stdout')).write_bytes(r.stdout);(a.output/(name+'.stderr')).write_bytes(r.stderr)
  observed=json.loads(r.stdout) if r.returncode==0 else {};rows.append({'id':name,'command':command,'exit':r.returncode,'passed':r.returncode==0 and observed.get('full') is full})
 fixture=a.output.resolve()/'repository';(fixture/'tools').mkdir(parents=True);shutil.copyfile(ROOT/'tools/ci_plan.py',fixture/'tools/ci_plan.py');(fixture/'README.md').write_text('before\n')
 env=dict(os.environ,GIT_AUTHOR_NAME='Plan controls',GIT_AUTHOR_EMAIL='fixture@example.invalid',GIT_COMMITTER_NAME='Plan controls',GIT_COMMITTER_EMAIL='fixture@example.invalid')
 for command in [['git','init'],['git','add','.'],['git','-c','commit.gpgsign=false','commit','-m','baseline']]:subprocess.run(command,cwd=fixture,env=env,check=True,capture_output=True)
 base=subprocess.check_output(['git','rev-parse','HEAD'],cwd=fixture,text=True).strip();(fixture/'README.md').write_text('after\n')
 for command in [['git','add','.'],['git','-c','commit.gpgsign=false','commit','-m','prose']]:subprocess.run(command,cwd=fixture,env=env,check=True,capture_output=True)
 head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=fixture,text=True).strip();event=a.output.resolve()/'event.json';event.write_text(json.dumps({'pull_request':{'base':{'sha':base},'head':{'sha':head}}}))
 env.update(GITHUB_EVENT_PATH=str(event),GITHUB_EVENT_NAME='pull_request',GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='1');env.pop('GITHUB_OUTPUT',None)
 plan_path=a.output.resolve()/'plan.json';planner=str(fixture/'tools/ci_plan.py')
 subprocess.run([sys.executable,planner,'create','--output',str(plan_path)],env=env,check=True,capture_output=True)
 original=json.loads(plan_path.read_text());lanes=['native','orthogonal','resources','ordinary']
 for name in ['docs-skipped','excluded-ran','missing-lane','duplicate-plan-lane','unknown-plan','wrong-tested-sha','wrong-attempt','changed-digest','false-reason']:
  plan=copy.deepcopy(original);needs={lane:{'result':'skipped'} for lane in lanes};needs['prepare']={'result':'success'}
  if name=='excluded-ran':needs['native']['result']='success'
  if name=='missing-lane':needs.pop('resources')
  if name=='duplicate-plan-lane':plan['selectedLanes']=['native','native']
  if name=='unknown-plan':plan['planVersion']=99
  if name=='wrong-tested-sha':plan['testedSHA']='0'*40
  if name=='wrong-attempt':plan['attempt']='2'
  if name=='changed-digest':plan['changedPathsDigest']='0'*64
  if name=='false-reason':plan['excludedLanes']['native']='self-approved skip'
  plan_path.write_text(json.dumps(plan));command=[sys.executable,planner,'verify','--plan',str(plan_path),'--needs',json.dumps(needs)];result=subprocess.run(command,env=env,capture_output=True,timeout=15)
  (a.output/(name+'.stdout')).write_bytes(result.stdout);(a.output/(name+'.stderr')).write_bytes(result.stderr)
  rows.append({'id':name,'command':command,'exit':result.returncode,'passed':result.returncode==(0 if name=='docs-skipped' else 1)})
 env['GITHUB_EVENT_NAME']='push';subprocess.run([sys.executable,planner,'create','--output',str(plan_path)],env=env,check=True,capture_output=True)
 for status in ['success','failure','cancelled','skipped','timed_out','unknown']:
  needs={lane:{'result':'success'} for lane in lanes};needs['prepare']={'result':'success'};needs['native']['result']=status
  command=[sys.executable,planner,'verify','--plan',str(plan_path),'--needs',json.dumps(needs)];result=subprocess.run(command,env=env,capture_output=True,timeout=15)
  (a.output/('selected-'+status+'.stdout')).write_bytes(result.stdout);(a.output/('selected-'+status+'.stderr')).write_bytes(result.stderr)
  rows.append({'id':'selected-'+status,'command':command,'exit':result.returncode,'passed':result.returncode==(0 if status=='success' else 1)})
 report={'rows':rows,'passed':all(r['passed'] for r in rows)};(a.output/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report));return 0 if report['passed'] else 1
if __name__=='__main__':sys.exit(main())
