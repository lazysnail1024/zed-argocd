#!/usr/bin/env python3
"""Conservative scheduling. Unknown or unavailable diffs always select full."""
import argparse,hashlib,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
LANES=['native','orthogonal','resources','ordinary']
DOCUMENTS={'README.md','DEVELOPMENT.md'}
def classify(changes):
    docs=isinstance(changes,list) and bool(changes) and all(isinstance(row,list) and len(row)==2 and row[0]=='M' and row[1] in DOCUMENTS for row in changes)
    return {'full':not docs,'reason':'registered prose-only modifications' if docs else 'full: unknown, unavailable, empty or behavior-affecting diff'}
def git(*args):return subprocess.check_output(['git',*args],cwd=ROOT,text=True).strip()
def plan():
    tested=git('rev-parse','HEAD');event=json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text());changes=None;base=head=None
    if os.environ.get('GITHUB_EVENT_NAME')=='pull_request':
        base=event['pull_request']['base']['sha'];head=event['pull_request']['head']['sha']
        try:
            merge=git('merge-base',base,head)
            raw=subprocess.check_output(['git','diff','--name-status','-z',merge,head],cwd=ROOT)
            # Rename/copy/delete statuses are full by policy, without discarding either path.
            parts=raw.decode().split('\0');changes=[];index=0
            while index<len(parts)-1:
                status=parts[index];width=3 if status.startswith(('R','C')) else 2
                changes.append(parts[index:index+width]);index+=width
            if index!=len(parts)-1:changes=None
        except (OSError,ValueError,subprocess.SubprocessError):changes=None
    selection=classify(changes)
    return {'planVersion':1,'baseSHA':base,'headSHA':head,'testedSHA':tested,
            'changedPathsDigest':hashlib.sha256(json.dumps(changes,separators=(',',':')).encode()).hexdigest(),
            'selectedLanes':LANES if selection['full'] else [],
            'excludedLanes':{} if selection['full'] else {lane:selection['reason'] for lane in LANES},
            'selectorSHA256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'runID':os.environ.get('GITHUB_RUN_ID'),'attempt':os.environ.get('GITHUB_RUN_ATTEMPT'),**selection}
def main():
 p=argparse.ArgumentParser();p.add_argument('command',choices=['classify','create','verify']);p.add_argument('--changes',type=Path);p.add_argument('--output',type=Path);p.add_argument('--plan',type=Path);p.add_argument('--needs');a=p.parse_args()
 try:
  if a.command=='classify':result=classify(json.loads(a.changes.read_text()))
  elif a.command=='create':
   result=plan();a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(result,indent=2)+'\n')
   if os.environ.get('GITHUB_OUTPUT'):
    with open(os.environ['GITHUB_OUTPUT'],'a') as out:out.write('full='+str(result['full']).lower()+'\n')
  else:
   result=json.loads(a.plan.read_text());expected=plan()
   if result!=expected:raise ValueError('Plan schema/source/diff/run/attempt differs from trusted recomputation')
   needs=json.loads(a.needs)
   if set(needs)!=set(LANES)|{'prepare'}:raise ValueError('Unexpected or missing blocking lane')
   if needs['prepare']['result']!='success':raise ValueError('Plan preparation failed')
   for lane in LANES:
    wanted='success' if lane in result['selectedLanes'] else 'skipped'
    if needs[lane]['result']!=wanted:raise ValueError(lane+' must be '+wanted+', observed '+needs[lane]['result'])
   result={**result,'passed':True}
  print(json.dumps(result));return 0
 except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as error:
  print(json.dumps({'passed':False,'error':str(error)}),file=sys.stderr);return 1
if __name__=='__main__':sys.exit(main())
