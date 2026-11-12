#!/usr/bin/env python3
"""Build a clearly synthetic embedded-schema consumer; never mutate real assets."""
import argparse, hashlib, json, os, pathlib, shutil, subprocess, sys

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 ap=argparse.ArgumentParser();ap.add_argument('output',type=pathlib.Path);ap.add_argument('--fixture',default='schema-query-fixture.json');ap.add_argument('--cases',default='schema-query-fixture-cases.json');args=ap.parse_args();root=pathlib.Path(__file__).resolve().parents[1];out=args.output.resolve();out.mkdir(parents=True,exist_ok=True);tree=out/'source';tree.mkdir();rows=[]
 try:
  files=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=root).decode().split('\0')
  for name in files:
   if name and (root/name).is_file():p=tree/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(root/name,p)
  catalog=json.loads((tree/'compat/catalog.lock.json').read_text());replaced=set();fixture=root/'tests'/args.fixture
  for v in catalog['versions']:
   for s in v['schemas']:
    if s['kind']=='Application':shutil.copyfile(fixture,tree/s['path']);replaced.add(s['path'])
  binary=out/'argocd-ls';cmd=['go','build','-mod=readonly','-buildvcs=false','-trimpath','-o',str(binary),'./cmd/argocd-ls'];r=subprocess.run(cmd,cwd=tree,capture_output=True,timeout=120);(out/'build.stdout').write_bytes(r.stdout);(out/'build.stderr').write_bytes(r.stderr);rows.append({'id':'build','command':cmd,'exitCode':r.returncode,'passed':r.returncode==0})
  if r.returncode==0:
   cmd=[sys.executable,str(root/'tests/schema_queries_e2e.py'),str(binary),str(out/'lsp'),'--cases',str(root/'tests'/args.cases)];r=subprocess.run(cmd,capture_output=True,timeout=240);(out/'lsp.stdout').write_bytes(r.stdout);(out/'lsp.stderr').write_bytes(r.stderr);rows.append({'id':'synthetic-lsp','command':cmd,'exitCode':r.returncode,'passed':r.returncode==0,'binarySHA256':sha(binary)})
 except Exception as e:rows.append({'id':'runner','passed':False,'error':repr(e)})
 finally:
  report={'scope':'synthetic consumer fixture; NOT upstream semantic acceptance','fixtureSHA256':sha(root/'tests'/args.fixture),'sourceFiles':{str(p.relative_to(tree)):sha(p) for p in tree.rglob('*') if p.is_file()},'rows':rows,'passed':bool(rows) and all(x['passed'] for x in rows)};(out/'report.json').write_text(json.dumps(report,indent=2)+'\n')
 print(json.dumps(rows));return 0 if report['passed'] else 1
if __name__=='__main__':sys.exit(main())
