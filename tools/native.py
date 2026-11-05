#!/usr/bin/env python3
"""Build a native development/release candidate or package an existing build."""
import argparse,gzip,hashlib,io,json,os,pathlib,re,shutil,stat,subprocess,sys,tarfile,tempfile,zipfile
ROOT=pathlib.Path(__file__).resolve().parents[1]
PLATFORMS={'linux/amd64','linux/arm64','darwin/amd64','darwin/arm64','windows/amd64'}
MODULE='github.com/lazysnail1024/zed-argocd'
def digest(data):return hashlib.sha256(data).hexdigest()
def content(path,limit=128*1024*1024):
 if path.is_symlink() or not path.is_file() or path.stat().st_size>limit:raise ValueError('Expected bounded regular file: '+str(path))
 with path.open('rb') as f:
  if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):raise ValueError('Not a regular file: '+str(path))
  data=f.read(limit+1)
 if len(data)>limit:raise ValueError('Input exceeds size limit: '+str(path))
 return data
def canonical(value):return (json.dumps(value,sort_keys=True,indent=2)+'\n').encode()
def write(path,data):path.write_bytes(data)
def main():
 ap=argparse.ArgumentParser();sub=ap.add_subparsers(dest='command',required=True)
 b=sub.add_parser('build');b.add_argument('--output',type=pathlib.Path,required=True);b.add_argument('--platform',required=True);b.add_argument('--mode',choices=['development','release'],default='development');b.add_argument('--version',default=(ROOT/'VERSION').read_text().strip()+'-dev');b.add_argument('--source-commit')
 p=sub.add_parser('package');p.add_argument('--output',type=pathlib.Path,required=True);p.add_argument('--manifest',type=pathlib.Path,required=True)
 args=ap.parse_args();out=args.output.absolute();report={'passed':False,'command':sys.argv,'steps':[]};created=False
 # Trust selected tools/system SDK, not inherited compiler flags or search paths.
 env={k:v for k,v in os.environ.items() if k.upper() in {'PATH','HOME','USERPROFILE','HOMEDRIVE','HOMEPATH','TMPDIR','TMP','TEMP','SYSTEMROOT','SYSTEMDRIVE','WINDIR','COMSPEC','PATHEXT','LOCALAPPDATA','APPDATA','PROGRAMFILES','PROGRAMFILES(X86)','PROGRAMW6432','GO','GOROOT','GOCACHE','GOMODCACHE','GOTMPDIR','CC'}}
 env.update(GOENV='off',GOWORK='off',CGO_ENABLED='1',GOTOOLCHAIN='local',GOFLAGS='',GOPROXY='off',GOSUMDB='off',CGO_CFLAGS='-O2 -g',CGO_CPPFLAGS='',CGO_CXXFLAGS='-O2 -g',CGO_FFLAGS='-O2 -g',CGO_LDFLAGS='-O2 -g',MACOSX_DEPLOYMENT_TARGET='13.0',LC_ALL='C')
 go=env.get('GO') or (str(pathlib.Path(env['GOROOT'])/'bin/go') if env.get('GOROOT') else shutil.which('go'))
 def run(command,required=True,cwd=ROOT):
  r=subprocess.run(command,cwd=cwd,env=env,capture_output=True,timeout=180);step={'command':command,'cwd':str(cwd),'exit':r.returncode,'stdout':r.stdout.decode(errors='replace'),'stderr':r.stderr.decode(errors='replace')};report['steps'].append(step)
  if required and r.returncode:raise RuntimeError('Command failed: '+repr(command)+'\n'+step['stderr'])
  return r
 try:
  if out.exists() or out.is_symlink():raise ValueError('Output must not already exist')
  try:relative=out.relative_to(ROOT)
  except ValueError:relative=None
  if relative is not None and (not relative.parts or relative.parts[0] not in ['artifacts','build','dist']):raise ValueError('In-checkout outputs must be under artifacts, build or dist')
  if not go:raise ValueError('Pinned Go toolchain is required')
  if args.command=='build':
   if args.platform not in PLATFORMS:raise ValueError('Unsupported native platform')
   if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?',args.version):raise ValueError('Unsafe or invalid server version')
   if args.mode=='release' and not re.fullmatch(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-(?:alpha|beta|rc)\.(?:0|[1-9][0-9]*))?',args.version):raise ValueError('Release requires a stable or alpha/beta/rc server version')
   expectedGo='go'+next(x.split()[1] for x in (ROOT/'go.mod').read_text().splitlines() if x.startswith('go '))
   ge=json.loads(run([go,'env','-json','GOHOSTOS','GOHOSTARCH','GOOS','GOARCH','GOVERSION','GOROOT','CC']).stdout)
   if ge['GOVERSION']!=expectedGo or args.platform!=ge['GOHOSTOS']+'/'+ge['GOHOSTARCH'] or args.platform!=ge['GOOS']+'/'+ge['GOARCH']:raise ValueError('Toolchain or actual native host platform mismatch')
   cc=env.get('CC') or ge['CC'];compiler=run([cc,'--version']).stdout.decode(errors='replace')
   windows=None
   if ge['GOOS']=='windows':
    windows=json.loads(content(ROOT/'tools/windows-runtime.json'));ccPath=pathlib.Path(shutil.which(cc,path=env.get('PATH')) or cc).resolve();toolRoot=ccPath.parent.parent
    if ccPath.name.lower()!='gcc.exe':raise ValueError('Windows static runtime requires the pinned gcc.exe installation')
    for name,expected in windows['runtimeFiles'].items():
     if digest(content(toolRoot/name))!=expected:raise ValueError('Windows runtime input differs from pinned distribution: '+name)
    for name,expected in windows['legalFiles'].items():
     if digest(content(toolRoot/'licenses'/name))!=expected:raise ValueError('Windows runtime notice differs from pinned distribution: '+name)
    report['windowsCompilerPath']=str(ccPath)
   head=run(['git','rev-parse','--verify','HEAD'],False);commit=head.stdout.decode().strip() if head.returncode==0 else None
   status=run(['git','status','--porcelain','--untracked-files=all'],False);dirty=commit is None or status.returncode!=0 or bool(status.stdout)
   if args.source_commit and (not re.fullmatch('[0-9a-f]{40}',args.source_commit) or args.source_commit!=commit):raise ValueError('Requested source commit must equal exact HEAD')
   if args.mode=='release' and (not args.source_commit or not commit or dirty):raise ValueError('Release requires known exact clean committed provenance')
   paths=run(['git','ls-files','--cached','--others','--exclude-standard','-z']).stdout.decode().split('\0');sources={name:digest(content(ROOT/name)) for name in sorted(set(paths)) if name}
   if not sources:raise ValueError('Empty source inventory')
   if args.source_commit:
    entries=run(['git','ls-tree','-rz',commit]).stdout.split(b'\0');tree={}
    for entry in entries:
     if entry:
      header,path=entry.split(b'\t',1);tree[path.decode()]=header.split()[2].decode()
    for name in sources:
     data=content(ROOT/name);blob=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
     if tree.get(name)!=blob:raise ValueError('Checkout bytes differ from requested commit: '+name)
   out.mkdir(parents=True);created=True;binary=out/('argocd-ls.exe' if ge['GOOS']=='windows' else 'argocd-ls')
   flags=f'-X {MODULE}/internal/buildinfo.ServerVersion={args.version} -X {MODULE}/internal/buildinfo.Dirty={str(dirty).lower()} -X {MODULE}/internal/buildinfo.BuildMode={args.mode}'
   if commit:flags+=' -X '+MODULE+'/internal/buildinfo.SourceCommit='+commit
   if windows:flags+=' -linkmode=external -extldflags "-static -Wl,-Map,argocd-native-link.map"'
   # Compile only validated bytes, never ignored checkout inputs or adjacent repository files.
   with tempfile.TemporaryDirectory(prefix='argocd-native-source-') as temporary:
    snapshot=pathlib.Path(temporary)
    if windows:
     # GCC can expand Windows short names and use forward slashes in __FILE__.
     canonicalSnapshot=pathlib.Path(os.path.realpath(snapshot,strict=True))
     prefixes=dict.fromkeys([str(snapshot),snapshot.as_posix(),str(canonicalSnapshot),canonicalSnapshot.as_posix()])
     env['CGO_CPPFLAGS']=' '.join('"-ffile-prefix-map='+prefix+'=//_/_/argocd-native-source"' for prefix in prefixes)
    for name,expected in sources.items():
     value=content(ROOT/name)
     if digest(value)!=expected:raise ValueError('Source changed before snapshot: '+name)
     destination=snapshot/name;destination.parent.mkdir(parents=True,exist_ok=True);write(destination,value)
    run([go,'build','-mod=vendor','-buildvcs=false','-trimpath','-ldflags',flags,'-o',str(binary),'./cmd/argocd-ls'],cwd=snapshot)
    if windows:
     linkMap=content(snapshot/'argocd-native-link.map');write(out/'native-link.map',linkMap);report['linkMap']={'file':'native-link.map','sha256':digest(linkMap),'buildDirectory':str(snapshot)}
   if any(digest(content(ROOT/name))!=expected for name,expected in sources.items()):raise ValueError('Source changed during build')
   metadata=json.loads(run([go,'version','-m','-json',str(binary)]).stdout);identity=json.loads(run([str(binary),'version','--json']).stdout)
   if identity.get('sourceCommit')!=commit or identity.get('dirty')!=dirty or identity.get('buildMode')!=args.mode or identity.get('serverVersion')!=args.version:raise ValueError('Built identity does not match requested provenance')
   legal={'LICENSE':content(ROOT/'LICENSE'),'licenses/go/LICENSE':content(pathlib.Path(ge['GOROOT'])/'LICENSE'),'licenses/go/PATENTS':content(pathlib.Path(ge['GOROOT'])/'PATENTS')}
   if windows:
    for name,expected in windows['legalFiles'].items():
     value=content(toolRoot/'licenses'/name)
     if digest(value)!=expected:raise ValueError('Windows runtime notice changed during build: '+name)
     legal['licenses/windows-runtime/'+name]=value
   for dep in metadata['Deps']:
    folder=ROOT/'vendor'/dep['Path'];found=False
    for source in sorted(folder.rglob('*')):
     if source.is_file() and source.name in ['LICENSE','LICENSE.libyaml','NOTICE','PATENTS','COPYING']:
      rel=source.relative_to(ROOT).as_posix();legal['licenses/'+rel.removeprefix('vendor/')]=content(source);found=True
    if not found:raise ValueError('Missing legal inputs for linked module '+dep['Path'])
   catalog=json.loads(content(ROOT/'compat/catalog.lock.json'))
   for target in catalog['versions']:
    for source in target['sources']:
     if source['path'] in ['LICENSE','NOTICE']:
      data=content(ROOT/source['localPath'])
      if digest(data)!=source['sha256']:raise ValueError('CRD legal input hash mismatch')
      legal['licenses/argo-cd/'+source['sha256']+'/'+source['path']]=data
   legal['NOTICE.txt']=b'Original server: MIT, Copyright (c) 2026 snail (lazysnail1024).\nArgo CD embedded schemas are normalized derivatives of the pinned Apache-2.0 CRDs.\nVendored golang.org/x/text CJK character data uses equivalent Unicode escapes; a comment uses Unicode notation.\nGo standard-library/runtime, linked modules and native parser legal texts are included unchanged.\nThis native package does not contain the Rust Zed adapter.\n'
   # Keep legal bytes bound to the build; packaging compares current source legal inputs below.
   if windows:legal['NOTICE.txt']+=b'Windows runtime is statically linked from the fixed MinGW-Builds distribution; full MinGW, winpthreads and GCC runtime notices are included.\n'
   legalInputs={}
   for name,data in legal.items():
    path=out/'legal'/name;path.parent.mkdir(parents=True,exist_ok=True);write(path,data);legalInputs[name]=digest(data)
   if ge['GOOS']=='linux':runtime=run(['ldd',str(binary)]).stdout.decode(errors='replace')
   elif ge['GOOS']=='darwin':runtime=run(['otool','-L',str(binary)]).stdout.decode(errors='replace')
   else:
    runtime=run(['objdump','-p',str(binary)]).stdout.decode(errors='replace')
    libraries=re.findall(r'DLL Name:\s*(\S+)',runtime)
    allowed={'kernel32.dll','msvcrt.dll','ntdll.dll','ws2_32.dll','advapi32.dll','shell32.dll','user32.dll','iphlpapi.dll','bcrypt.dll','secur32.dll','crypt32.dll','ole32.dll','oleaut32.dll','combase.dll','ucrtbase.dll'}
    if not libraries or any(x.lower() not in allowed and not x.lower().startswith('api-ms-win-') for x in libraries):raise ValueError('Non-system Windows runtime dependency requires explicit reviewed redistribution')
   m={'formatVersion':1,'product':'argo-cd','platform':args.platform,'binary':binary.name,'binarySHA256':digest(content(binary)),'identity':identity,'goBuild':metadata,'goEnvironment':ge,'buildEnvironment':{k:env[k] for k in ['GOENV','GOWORK','GOTOOLCHAIN','GOFLAGS','CGO_ENABLED','CGO_CFLAGS','CGO_CPPFLAGS','CGO_CXXFLAGS','CGO_FFLAGS','CGO_LDFLAGS','MACOSX_DEPLOYMENT_TARGET']},'compiler':compiler,'toolSHA256':{'go':digest(content(pathlib.Path(shutil.which(go) or go).resolve())),'cc':digest(content(pathlib.Path(shutil.which(cc) or cc).resolve()))},'runtimeLibraries':re.sub(r'0x[0-9a-fA-F]+','ADDRESS',runtime),'sourceInputs':sources,'legalInputs':legalInputs,'catalogSHA256':digest(content(ROOT/'internal/assets/catalog.json'))}
   if windows:m['windowsRuntime']=windows
   write(out/'manifest.json',canonical(m));report.update(manifest='manifest.json',binarySHA256=m['binarySHA256'],platform=args.platform)
  else:
   manifest=args.manifest.absolute();m=json.loads(content(manifest,4*1024*1024));folder=manifest.parent
   if m.get('formatVersion')!=1 or m.get('product')!='argo-cd' or m.get('platform') not in PLATFORMS:raise ValueError('Invalid build manifest identity/platform')
   platform=m['platform'];identity=m['identity'];version=identity['serverVersion']
   if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?',version):raise ValueError('Unsafe server version')
   name='argocd-ls.exe' if platform.startswith('windows/') else 'argocd-ls'
   if m['binary']!=name:raise ValueError('Unexpected binary path')
   binary=folder/name;data=content(binary)
   if digest(data)!=m['binarySHA256']:raise ValueError('Binary hash differs from build manifest')
   metadata=json.loads(run([go,'version','-m','-json',str(binary)]).stdout);settings={x['Key']:x['Value'] for x in metadata['Settings']}
   if metadata!=m['goBuild'] or metadata['Main']['Path']!=MODULE or settings['GOOS']+'/'+settings['GOARCH']!=platform:raise ValueError('Embedded Go platform/module/build metadata mismatch')
   if json.loads(run([str(binary),'version','--json']).stdout)!=identity or identity['product']!='argo-cd':raise ValueError('Native identity mismatch')
   if identity['buildMode'] not in ['development','release']:raise ValueError('Unknown build mode')
   if identity['buildMode']=='release' and (identity['dirty'] or not re.fullmatch('[0-9a-f]{40}',identity.get('sourceCommit') or '') or not re.fullmatch(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-(?:alpha|beta|rc)\.(?:0|[1-9][0-9]*))?',version)):raise ValueError('Invalid release provenance')
   actualGo=json.loads(run([go,'env','-json','GOROOT','GOVERSION']).stdout)
   expectedGo='go'+next(x.split()[1] for x in (ROOT/'go.mod').read_text().splitlines() if x.startswith('go '))
   if actualGo['GOVERSION']!=expectedGo or metadata['GoVersion']!=expectedGo:raise ValueError('Legal source toolchain version differs from pinned build')
   crdLegal={}
   expectedLegal={'LICENSE','licenses/go/LICENSE','licenses/go/PATENTS','NOTICE.txt'}
   windows=json.loads(content(ROOT/'tools/windows-runtime.json')) if platform.startswith('windows/') else None
   if windows:
    if m.get('windowsRuntime')!=windows:raise ValueError('Windows runtime provenance differs from fixed source')
    expectedLegal.update('licenses/windows-runtime/'+name for name in windows['legalFiles'])
   for dep in metadata['Deps']:
    for source in sorted((ROOT/'vendor'/dep['Path']).rglob('*')):
     if source.is_file() and source.name in ['LICENSE','LICENSE.libyaml','NOTICE','PATENTS','COPYING']:expectedLegal.add('licenses/'+source.relative_to(ROOT/'vendor').as_posix())
   catalog=json.loads(content(ROOT/'compat/catalog.lock.json'))
   for target in catalog['versions']:
    for source in target['sources']:
     if source['path'] in ['LICENSE','NOTICE']:
      value=content(ROOT/source['localPath'])
      if digest(value)!=source['sha256']:raise ValueError('CRD legal source changed')
      legalName='licenses/argo-cd/'+source['sha256']+'/'+source['path'];expectedLegal.add(legalName);crdLegal[legalName]=source['sha256']
   if set(m['legalInputs'])!=expectedLegal:raise ValueError('Legal inventory differs from actual included dependencies/assets')
   files={name:data}
   for path,expected in m['legalInputs'].items():
    pure=pathlib.PurePosixPath(path)
    if pure.is_absolute() or '..' in pure.parts or '\\' in path:raise ValueError('Unsafe legal archive path')
    value=content(folder/'legal'/path,4*1024*1024)
    if digest(value)!=expected:raise ValueError('Build legal input hash mismatch')
    if path.startswith('licenses/go/') and content(pathlib.Path(actualGo['GOROOT'])/pure.name)!=value:raise ValueError('Go legal text differs from pinned toolchain source')
    if path.startswith('licenses/windows-runtime/') and digest(value)!=windows['legalFiles'][path.removeprefix('licenses/windows-runtime/')]:raise ValueError('Windows runtime notice differs from fixed source')
    if path in crdLegal and digest(value)!=crdLegal[path]:raise ValueError('CRD legal text differs from pinned source')
    if path=='LICENSE' and content(ROOT/'LICENSE')!=value:raise ValueError('Current project license differs from build')
    if path.startswith('licenses/') and not path.startswith(('licenses/go/','licenses/argo-cd/','licenses/windows-runtime/')) and content(ROOT/'vendor'/path.removeprefix('licenses/'))!=value:raise ValueError('Current dependency license differs from build')
    files[path]=value
   if 'LICENSE' not in files or 'licenses/go/LICENSE' not in files or 'NOTICE.txt' not in files or not any(p.startswith('licenses/argo-cd/') for p in files):raise ValueError('Missing required legal inventory')
   files['provenance/build.json']=canonical(m)
   root='argocd-ls-'+version+'-'+platform.replace('/','-');archiveName=root+('.zip' if platform.startswith('windows/') else '.tar.gz')
   out.mkdir(parents=True);created=True;archive=out/archiveName
   if archive.suffix=='.zip':
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_STORED) as z:
     for path,value in sorted(files.items()):
      info=zipfile.ZipInfo(root+'/'+path,(1980,1,1,0,0,0));info.create_system=3;info.external_attr=(0o100755 if path==name else 0o100644)<<16;info.compress_type=zipfile.ZIP_STORED;z.writestr(info,value)
   else:
    with archive.open('wb') as raw,gzip.GzipFile(fileobj=raw,mode='wb',filename='',mtime=0,compresslevel=0) as gz,tarfile.open(fileobj=gz,mode='w',format=tarfile.USTAR_FORMAT) as tar:
     for path,value in sorted(files.items()):
      info=tarfile.TarInfo(root+'/'+path);info.size=len(value);info.mode=0o755 if path==name else 0o644;info.mtime=0;tar.addfile(info,io.BytesIO(value))
   package={'platform':platform,'root':root,'archive':archiveName,'archiveSHA256':digest(content(archive)),'binarySHA256':m['binarySHA256'],'identity':identity,'buildManifestSHA256':digest(content(manifest)),'files':{p:digest(v) for p,v in sorted(files.items())}};write(out/'package.json',canonical(package));report.update(package='package.json',archiveSHA256=package['archiveSHA256'])
  report['passed']=True
 except Exception as e:report['error']=str(e)
 finally:
  if created:write(out/'result.json',canonical(report))
  print(json.dumps(report))
  if not report['passed']:print(report.get('error','operation failed'),file=sys.stderr)
 return 0 if report['passed'] else 2
if __name__=='__main__':sys.exit(main())
