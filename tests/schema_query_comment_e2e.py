#!/usr/bin/env python3
"""Check actual completion edits through the real CLI, preserving resource truth."""
import argparse,hashlib,json,pathlib,subprocess,sys
ap=argparse.ArgumentParser();ap.add_argument('binary',type=pathlib.Path);ap.add_argument('lsp_report',type=pathlib.Path);ap.add_argument('output',type=pathlib.Path);a=ap.parse_args();binary=a.binary.resolve();out=a.output.resolve();out.mkdir(parents=True,exist_ok=True);rows=[]
case=next(c for c in json.loads(pathlib.Path(__file__).with_name('schema-query-boundary-cases.json').read_text()) if c['id']=='empty-before-comment')
observations=json.loads(a.lsp_report.read_text())['rows']
for target in ['3.0.0','3.5.6']:
 observed=next(r for r in observations if r['id']==case['id'] and r['target']==target and r['snippets'] is False)
 for name,text in [('before',case['text'].replace('|','',1)),('applied',observed.get('applied','')),('expected',case['expected'])]:
  folder=out/(target+'-'+name);work=folder/'workspace';work.mkdir(parents=True,exist_ok=True);(work/'.argocd-ls.yaml').write_text('schemaVersion: 1\ntargetVersion: "'+target+'"\n');(work/'input.yaml').write_text(text);cmd=[str(binary),'check','--root',str(work),'--format','json'];row={'id':name,'target':target,'command':cmd,'passed':False};rows.append(row)
  try:
   p=subprocess.run(cmd,capture_output=True,timeout=10);(folder/'stdout').write_bytes(p.stdout);(folder/'stderr').write_bytes(p.stderr);d=json.loads(p.stdout);codes=sorted(x['code'] for x in d['diagnostics']);expected=['SCHEMA_REQUIRED','SCHEMA_TYPE'] if name=='before' else ['SCHEMA_REQUIRED']
   checks={'exit':p.returncode==1,'resources':d['resourcesAnalyzed']==1,'files':d['filesAnalyzed']==2,'complete':d['analysisComplete'] is True,'configuration':d['configurationErrors']==[],'diagnostics':codes==expected};row.update(exit=p.returncode,checks=checks,passed=all(checks.values()))
  except Exception as e:row['error']=repr(e)
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest();report={'binarySHA256':sha(binary),'runnerSHA256':sha(pathlib.Path(__file__)),'lspReportSHA256':sha(a.lsp_report),'rows':rows,'passed':len(rows)==6 and all(x['passed'] for x in rows)};(out/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report));sys.exit(0 if report['passed'] else 1)
