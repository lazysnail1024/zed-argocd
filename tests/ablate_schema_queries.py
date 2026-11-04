#!/usr/bin/env python3
"""Compile query variants and observe real LSP behavior, with retained failures."""
import argparse,pathlib,subprocess,shutil,json,hashlib,sys
r=pathlib.Path(__file__).resolve().parents[1];ap=argparse.ArgumentParser();ap.add_argument('output',type=pathlib.Path);a=ap.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=True);rows=[]
files=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=r).decode().split('\0')
for name in ['without-recovery','with-obsolete-traversal','without-leaf-validation']:
 d=out/name;tree=d/'source';tree.mkdir(parents=True);row={'id':name,'passed':False};rows.append(row)
 try:
  for f in files:
   if f and (r/f).is_file():p=tree/f;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(r/f,p)
  if name=='without-recovery':
   p=tree/'internal/analysis/schema_queries.go';s=p.read_text();needle='offset, err := s.Offset(source.Position{Line: p.Line, UTF16Column: p.Character})';assert needle in s;s=s.replace(needle,'if true { return nil }\n\t'+needle,1);p.write_text(s)
  elif name=='without-leaf-validation':
   p=tree/'internal/analysis/schema_queries.go';s=p.read_text();needle='return s.Validate(candidate) == nil';assert needle in s;p.write_text(s.replace(needle,'return true',1))
   catalog=json.loads((tree/'compat/catalog.lock.json').read_text())
   for v in catalog['versions']:
    for asset in v['schemas']:
     if asset['kind']=='Application':shutil.copyfile(r/'tests/schema-query-repair-fixture.json',tree/asset['path'])
  else:
   p=tree/'internal/analysis/queries.go';p.write_text(p.read_text()+'''\nfunc schemaAt(schema map[string]any, parts []string) map[string]any {
 for _,part:=range parts { if schema["type"]=="array" {schema=object(schema["items"])} else {schema=object(object(schema["properties"])[part])} };return schema
}\n''')
  cmd=['go','build','-mod=vendor','-buildvcs=false','-trimpath','-o',str(d/'argocd-ls'),'./cmd/argocd-ls'];p=subprocess.run(cmd,cwd=tree,capture_output=True,timeout=120);(d/'build.stdout').write_bytes(p.stdout);(d/'build.stderr').write_bytes(p.stderr);row['buildExit']=p.returncode
  if p.returncode:continue
  row['binarySHA256']=hashlib.sha256((d/'argocd-ls').read_bytes()).hexdigest();cmd=[sys.executable,str(r/'tests/schema_queries_e2e.py'),str(d/'argocd-ls'),str(d/'lsp')]
  if name=='without-leaf-validation':cmd+=['--cases',str(r/'tests/schema-query-repair-fixture-cases.json')]
  p=subprocess.run(cmd,capture_output=True,timeout=240);(d/'test.stdout').write_bytes(p.stdout);(d/'test.stderr').write_bytes(p.stderr);row['testExit']=p.returncode
  report=json.load(open(d/'lsp/report.json'));failed={x['id'] for x in report['rows'] if not x['passed']};row['failedCases']=sorted(failed)
  row['passed']=p.returncode==0 if name=='with-obsolete-traversal' else p.returncode==1 and {'blank-mapping','empty-mapping','incomplete-key','array-existing'}<=failed
  if name=='without-leaf-validation':row['passed']=p.returncode==1 and {'conjunction-inside-alternative','conjunction-with-disjunction-membership'}<=failed
 except Exception as e:row['error']=repr(e)
 finally:(d/'result.json').write_text(json.dumps(row,indent=2)+'\n')
(out/'report.json').write_text(json.dumps({'rows':rows,'passed':all(x['passed'] for x in rows)},indent=2)+'\n');print(json.dumps(rows));sys.exit(0 if all(x['passed'] for x in rows) else 1)
