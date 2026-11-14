#!/usr/bin/env python3
"""Summarize raw GitHub jobs without conflating CI time with product latency."""
import argparse,datetime,hashlib,json,statistics
from pathlib import Path

def stamp(value):return datetime.datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--jobs',type=Path,nargs='+',required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();runs=[]
 for source in a.jobs:
  raw=json.loads(source.read_text());jobs=raw['jobs'];rows=[]
  if not jobs or raw.get('total_count',len(jobs))!=len(jobs):raise ValueError('Missing jobs or incomplete pagination: '+str(source))
  for job in jobs:
   if not job.get('started_at') or not job.get('completed_at') or not job.get('conclusion'):raise ValueError('Incomplete job timing: '+str(job.get('id')))
   rows.append({'id':job['id'],'name':job['name'],'runID':job.get('run_id'),'attempt':job.get('run_attempt'),'conclusion':job['conclusion'],'seconds':stamp(job['completed_at'])-stamp(job['started_at']),
                'steps':[{'name':step['name'],'conclusion':step.get('conclusion'),'seconds':stamp(step['completed_at'])-stamp(step['started_at'])} for step in job.get('steps',[]) if step.get('started_at') and step.get('completed_at')]})
  runs.append({'input':str(source),'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'jobs':rows,'jobMinutes':sum(row['seconds'] for row in rows)/60,
               'queueTime':'unavailable without workflow trigger/queue metadata; not inferred from step durations',
               'executionSpanSeconds':max((stamp(j['completed_at']) for j in jobs if j.get('completed_at')),default=0)-min((stamp(j['started_at']) for j in jobs if j.get('started_at')),default=0),
               'failedJobs':[row['name'] for row in rows if row['conclusion'] not in ('success','skipped')]})
 report={'schemaVersion':1,'runs':runs,'sampleCount':len(runs),'medianJobMinutes':statistics.median(r['jobMinutes'] for r in runs),
         'comparison':'unverified: callers must pair identical source-selection/platform/toolchain/cache groups; no warm/cold or p95 claim from unpaired samples',
         'requiredComparisonSamples':{'warmPairs':5,'coldPairs':3,'naturalPRRuns':20,'observationLimitDays':7},
         'coldBuildPolicy':'Both baseline and candidate native producers retain empty caches; warm compiler comparison is not applicable.',
         'productThresholds':'Unchanged; product latency and RSS remain in original resource reports, not these CI durations.'}
 a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(report,indent=2)+'\n')
if __name__=='__main__':main()
