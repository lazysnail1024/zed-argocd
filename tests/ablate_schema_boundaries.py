#!/usr/bin/env python3
"""Compile the two boundary deletions and replay their independent LSP oracles."""
import argparse,hashlib,json,pathlib,shutil,subprocess,sys
from module_source import toolkit_copy
r=pathlib.Path(__file__).resolve().parents[1];ap=argparse.ArgumentParser();ap.add_argument('output',type=pathlib.Path);out=ap.parse_args().output.resolve();out.mkdir(parents=True,exist_ok=True);rows=[]
files=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=r).decode().split('\0')
for mode in ['comment-separator','exclusive-cardinality']:
 folder=out/mode;tree=folder/'source';tree.mkdir(parents=True);row={'id':mode,'passed':False};rows.append(row);flags=[]
 try:
  for name in files:
   if name and (r/name).is_file():p=tree/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(r/name,p)
  if mode=='comment-separator':
   p=tree/'internal/analysis/queries.go';s=p.read_text();assert 'suffix = " "' in s;p.write_text(s.replace('suffix = " "','suffix = ""',1));cases=r/'tests/schema-query-boundary-cases.json';wanted={'empty-before-comment'}
  else:
   source,modfile=toolkit_copy(tree,folder,'schemaquery/query.go');s=source.read_text();assert 'i == 1 && matches != 1' in s
   source.write_text(s.replace('i == 1 && matches != 1','i == 1 && false && matches != 1',1));flags=[modfile]
   cases=r/'tests/schema-query-boundary-fixture-cases.json';wanted={'exclusiveChoice','duplicateExclusive','referencedExclusive'}
   catalog=json.loads((tree/'compat/catalog.lock.json').read_text())
   for target in catalog['versions']:
    for asset in target['schemas']:
     if asset['kind']=='Application':shutil.copyfile(r/'tests/schema-query-boundary-fixture.json',tree/asset['path'])
  cmd=['go','build','-mod=readonly','-buildvcs=false','-trimpath',*flags,'-o',str(folder/'argocd-ls'),'./cmd/argocd-ls'];p=subprocess.run(cmd,cwd=tree,capture_output=True,timeout=120);(folder/'build.stdout').write_bytes(p.stdout);(folder/'build.stderr').write_bytes(p.stderr);row.update(buildCommand=cmd,buildExit=p.returncode)
  if p.returncode:continue
  cmd=[sys.executable,str(r/'tests/schema_queries_e2e.py'),str(folder/'argocd-ls'),str(folder/'lsp'),'--cases',str(cases)];p=subprocess.run(cmd,capture_output=True,timeout=120);(folder/'test.stdout').write_bytes(p.stdout);(folder/'test.stderr').write_bytes(p.stderr);report=json.load(open(folder/'lsp/report.json'));failed={x['id'] for x in report['rows'] if not x['passed']};row.update(command=cmd,testExit=p.returncode,failedCases=sorted(failed),binarySHA256=hashlib.sha256((folder/'argocd-ls').read_bytes()).hexdigest(),passed=p.returncode==1 and failed==wanted)
 except Exception as e:row['error']=repr(e)
 finally:(folder/'report.json').write_text(json.dumps(row,indent=2)+'\n')
(out/'report.json').write_text(json.dumps({'rows':rows,'passed':len(rows)==2 and all(x['passed'] for x in rows)},indent=2)+'\n');print(json.dumps(rows));sys.exit(0 if all(x['passed'] for x in rows) else 1)
