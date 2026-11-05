#!/usr/bin/env python3
"""Real separate-native packaging consumers; GUI acceptance remains manual."""
import argparse,hashlib,json,pathlib,shutil,subprocess,sys,zipfile
ROOT=pathlib.Path(__file__).resolve().parents[1]
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 p=argparse.ArgumentParser();p.add_argument('--manifest',type=pathlib.Path,required=True);p.add_argument('--output',type=pathlib.Path,required=True);a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
 platform=json.loads(a.manifest.read_text())['platform']
 report={'command':sys.argv,'testSHA256':sha(pathlib.Path(__file__)),'inputSHA256':sha(a.manifest),'python':sys.version,'steps':[],'assertions':[],'passed':False}
 def run(args,code=0):
  r=subprocess.run(args,cwd=ROOT,capture_output=True,text=True,timeout=240);report['steps'].append({'command':args,'exit':r.returncode,'stdout':r.stdout,'stderr':r.stderr});assert r.returncode==code,(args,r.stderr);return r
 def check(name,value):report['assertions'].append({'name':name,'passed':bool(value)});assert value,name
 def package(manifest,destination,code=0):return run([sys.executable,'tools/dev_bundle.py','--manifest',str(manifest.resolve()),'--platform',platform,'--output',str(destination)],code)
 try:
  for n in ['one','two']:package(a.manifest,out/n)
  first=json.loads((out/'one/package.json').read_text());second=json.loads((out/'two/package.json').read_text());check('repeatable archive',first['archiveSHA256']==second['archiveSHA256'])
  folder=out/'one'/first['root'];m=json.loads((folder/'server/manifest.json').read_text());binary=folder/'server'/m['binary'];check('exact native bytes',sha(binary)==m['binarySha256'])
  extension=folder/first['extension']
  check('explicit Helm task included',(extension/'examples/zed-tasks.json').is_file())
  check('extension root license',(extension/'LICENSE').read_bytes()==(ROOT/'LICENSE').read_bytes())
  check('unchanged Cargo manifest',(extension/'Cargo.toml').read_bytes()==(ROOT/'Cargo.toml').read_bytes())
  check('no embedded native feature','bundled-server' not in (extension/'Cargo.toml').read_text())
  check('no native payload in extension',not (extension/'native').exists() and all(hashlib.sha256(p.read_bytes()).hexdigest()!=m['binarySha256'] for p in extension.rglob('*') if p.is_file()))
  if not platform.startswith('windows/'):
   check('only separate server is executable',all(n=='server/'+m['binary'] or not (folder/n).stat().st_mode&0o111 for n in first['files']))
  else:report['skips']=[{'name':'POSIX extracted-file executable modes','reason':'Windows uses file extensions; ZIP executable metadata is checked separately'}]
  check('developer override points at separate binary',json.loads((folder/'settings.example.json').read_text())['lsp']['argocd-ls']['settings']['serverPath'].endswith('/server/'+m['binary']))
  binary.chmod(0o755);id=json.loads(run([str(binary),'version','--json']).stdout);check('ready identity',id==m['identity'] and id['product']=='argo-cd' and id['analysisReady'])
  native=json.loads(a.manifest.read_text())
  for name,digest in native['legalInputs'].items():check('legal '+name,sha(folder/'server'/name)==digest)
  with zipfile.ZipFile(out/'one'/first['archive']) as z:
   check('one archive root',all(n.startswith(first['root']+'/') for n in z.namelist()))
   check('exact archived file inventory',set(z.namelist())=={first['root']+'/'+n for n in first['files']})
   check('archive retains binary executable bit',(z.getinfo(first['root']+'/server/'+m['binary']).external_attr>>16)&0o111==0o111)
   for name,expected in first['files'].items():check('archive hash '+name,hashlib.sha256(z.read(first['root']+'/'+name)).hexdigest()==expected)
  for name,field,value in [('wrong-sha','binarySHA256','0'*64),('wrong-product','product','argo-workflows'),('wrong-platform','platform','linux/amd64' if platform!='linux/amd64' else 'windows/amd64')]:
   candidate=out/name;shutil.copytree(a.manifest.parent,candidate);m=json.loads((candidate/'manifest.json').read_text());m[field]=value;(candidate/'manifest.json').write_text(json.dumps(m));package(candidate/'manifest.json',out/(name+'-result'),2)
  package(out/'missing.json',out/'missing-result',2)
  report['archiveSHA256']=first['archiveSHA256'];report['passed']=True
 except Exception as e:report['error']=str(e)
 finally:(out/'result.json').write_text(json.dumps(report,indent=2)+'\n')
 print(json.dumps({'passed':report['passed'],'report':str(out/'result.json')}));return 0 if report['passed'] else 1
if __name__=='__main__':sys.exit(main())
