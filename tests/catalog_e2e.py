#!/usr/bin/env python3
import hashlib,json,pathlib,sys
root=pathlib.Path(__file__).resolve().parents[1];out=root/'artifacts/cd-2/audit';out.mkdir(parents=True,exist_ok=True);lock=json.loads((root/'compat/catalog.lock.json').read_text());rows=[]
for v in lock['versions']:
 if v['assetStatus']!='raw-collected':continue
 for source in v['sources']:
  b=(root/source['localPath']).read_bytes();valid=hashlib.sha256(b).hexdigest()==source['sha256']
  if source['path']!='LICENSE':
   kind={'application-crd.yaml':'Application','appproject-crd.yaml':'AppProject','applicationset-crd.yaml':'ApplicationSet'}[source['path'].split('/')[-1]]
   valid=valid and ('kind: '+kind+'\n').encode() in b and b'group: argoproj.io' in b
  rows.append({'version':v['version'],'source':source['path'],'hashAndKindValid':valid})
passed=bool(rows) and all(x['hashAndKindValid'] for x in rows) and lock['defaultVersion'] is None and not lock['ready']
(out/'results.json').write_text(json.dumps({'passed':passed,'checks':rows,'notEvaluated':['schema normalization','schema behavior','per-patch domain semantics','default selection']},indent=2)+'\n')
(out/'manifest.json').write_text(json.dumps({'command':sys.argv,'inputSHA256':hashlib.sha256((root/'compat/catalog.lock.json').read_bytes()).hexdigest(),'testSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()},indent=2)+'\n');print(json.dumps({'passed':passed,'checks':len(rows)}));sys.exit(0 if passed else 1)
