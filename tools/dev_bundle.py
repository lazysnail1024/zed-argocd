#!/usr/bin/env python3
"""Package a verified native build with Zed development-extension sources."""
import argparse,json,pathlib,subprocess,sys,tarfile,tempfile,zipfile
from native import ROOT,PLATFORMS,content,digest,canonical

def main():
 p=argparse.ArgumentParser();p.add_argument('--manifest',type=pathlib.Path,required=True);p.add_argument('--platform',choices=sorted(PLATFORMS),required=True);p.add_argument('--output',type=pathlib.Path,required=True);a=p.parse_args();out=a.output.absolute();report={'command':sys.argv,'passed':False,'steps':[]};created=False
 try:
  if out.exists() or out.is_symlink():raise ValueError('Output must not already exist')
  manifest=a.manifest.resolve();m=json.loads(content(manifest,4*1024*1024))
  if m.get('platform')!=a.platform or m.get('product')!='argo-cd':raise ValueError('Native product or requested platform mismatch')
  if not m.get('identity',{}).get('analysisReady'):raise ValueError('Native analysis is not ready')
  with tempfile.TemporaryDirectory(prefix='argocd-dev-bundle-') as temporary:
   verified=pathlib.Path(temporary)/'verified'
   command=[sys.executable,str(ROOT/'tools/native.py'),'package','--manifest',str(manifest),'--output',str(verified)]
   r=subprocess.run(command,capture_output=True,text=True);report['steps'].append({'command':command,'exit':r.returncode,'stdout':r.stdout,'stderr':r.stderr})
   if r.returncode:raise ValueError('Native package verification failed: '+r.stderr)
   package=json.loads(content(verified/'package.json'));archive=verified/package['archive'];legal={}
   if archive.suffix=='.zip':
    with zipfile.ZipFile(archive) as z:legal={n[len(package['root'])+1:]:z.read(n) for n in z.namelist()}
   else:
    with tarfile.open(archive) as t:legal={n.name[len(package['root'])+1:]:t.extractfile(n).read() for n in t.getmembers() if n.isfile()}
   files={name:content(ROOT/name) for name in ['Cargo.toml','Cargo.lock','rust-toolchain.toml','extension.toml','LICENSE','README.md','DEVELOPMENT.md']}
   for directory in ['src','snippets']:
    for path in sorted((ROOT/directory).rglob('*')):
     if path.is_file():files[path.relative_to(ROOT).as_posix()]=content(path)
   cargo=files['Cargo.toml'].decode()
   if 'default = []' not in cargo:raise ValueError('Source Cargo default features must be empty')
   files['Cargo.toml']=cargo.replace('default = []','default = ["bundled-server"]',1).encode()
   data=content(manifest.parent/m['binary']);files['native/server.bin']=data
   if digest(data)!=m['binarySHA256']:raise ValueError('Native changed during packaging')
   bundled={'schemaVersion':1,'product':'argo-cd','platform':a.platform,'binarySha256':digest(data),'binarySize':len(data),'identity':m['identity'],'buildManifestSha256':digest(content(manifest))}
   files['native/manifest.json']=canonical(bundled)
   for name,value in legal.items():
    if name!=m['binary']:files['native/legal/'+name]=value
   root='zed-argocd-dev-'+a.platform.replace('/','-');out.mkdir(parents=True);created=True;folder=out/root;folder.mkdir();archive=out/(root+'.zip')
   with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_STORED) as z:
    for name,value in sorted(files.items()):
     path=folder/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(value);mode=0o755 if name=='native/server.bin' else 0o644;path.chmod(mode)
     info=zipfile.ZipInfo(root+'/'+name,(1980,1,1,0,0,0));info.create_system=3;info.external_attr=(0o100000|mode)<<16;info.compress_type=zipfile.ZIP_STORED;z.writestr(info,value)
   result={'root':root,'archive':archive.name,'archiveSHA256':digest(content(archive)),'native':bundled,'files':{n:digest(v) for n,v in sorted(files.items())}}
   (out/'package.json').write_bytes(canonical(result));report.update(passed=True,archiveSHA256=result['archiveSHA256'])
 except Exception as e:report['error']=str(e)
 finally:
  if created:(out/'result.json').write_bytes(canonical(report))
  print(json.dumps(report))
 return 0 if report['passed'] else 2
if __name__=='__main__':sys.exit(main())
