#!/usr/bin/env python3
"""Real native build/package controls for excluded compiler environment inputs."""
import argparse,hashlib,json,os,pathlib,shutil,subprocess,sys
ROOT=pathlib.Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('output',type=pathlib.Path);p.add_argument('--platform',required=True);a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);rows=[];commands=[]
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
def run(name,cmd,env,cwd=ROOT):
 folder=out/'commands'/name;folder.mkdir(parents=True)
 try:r=subprocess.run(cmd,cwd=cwd,env=env,capture_output=True,timeout=180);stdout,stderr,status=r.stdout,r.stderr,r.returncode
 except subprocess.TimeoutExpired as e:stdout,stderr,status=e.stdout or b'',e.stderr or b'','timeout'
 except OSError as e:stdout,stderr,status=b'',str(e).encode(),'launch-failed'
 (folder/'stdout').write_bytes(stdout);(folder/'stderr').write_bytes(stderr);commands.append({'id':name,'command':cmd,'cwd':str(cwd),'exit':status});return status,stdout
try:
 fixture=out/'synthetic-source';fixture.mkdir()
 for name in subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT).decode().split('\0'):
  if name:q=fixture/name;q.parent.mkdir(parents=True,exist_ok=True);q.write_bytes((ROOT/name).read_bytes())
 env=dict(os.environ,GIT_AUTHOR_NAME='Synthetic fixture',GIT_AUTHOR_EMAIL='fixture@example.invalid',GIT_COMMITTER_NAME='Synthetic fixture',GIT_COMMITTER_EMAIL='fixture@example.invalid',GIT_AUTHOR_DATE='2000-01-01T00:00:00Z',GIT_COMMITTER_DATE='2000-01-01T00:00:00Z')
 for key in ['CGO_CFLAGS','CGO_CPPFLAGS','CGO_CXXFLAGS','CGO_FFLAGS','CGO_LDFLAGS','CPATH','C_INCLUDE_PATH','CPLUS_INCLUDE_PATH','OBJC_INCLUDE_PATH','LIBRARY_PATH','COMPILER_PATH','GCC_EXEC_PREFIX','GOENV','GOFLAGS']:env.pop(key,None)
 for i,cmd in enumerate([['git','init'],['git','-c','core.autocrlf=false','add','.'],['git','-c','commit.gpgsign=false','commit','-m','Synthetic environment provenance only']]):assert run('git-'+str(i),cmd,env,fixture)[0]==0
 commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=fixture).decode().strip()
 poison=out/'undeclared-inputs';poison.mkdir();header=poison/'marker.h';header.write_text('#ifndef __ASSEMBLER__\nextern char *getenv(const char *);\nextern int open(const char *, int, ...);\nextern long write(int, const void *, unsigned long);\nextern int close(int);\n__attribute__((constructor)) static void nd_environment_marker(void) {\n const char *p=getenv("CD_ENV_MARKER");\n if (p) { int fd=open(p, 1|64|512, 0600); if (fd>=0) { const char value[]="undeclared environment header executed\\n"; (void)write(fd,value,sizeof(value)-1); (void)close(fd); } }\n}\n#endif\n')
 search=poison/'search';search.mkdir();(search/'stddef.h').write_text('#error UNDECLARED_SEARCH_HEADER\n')
 go=env.get('GO') or (str(pathlib.Path(env['GOROOT'])/'bin/go') if env.get('GOROOT') else 'go');persisted=poison/'go-env';assert run('write-fixture-goenv',[go,'env','-w','CGO_CFLAGS=-include '+str(header)],dict(env,GOENV=str(persisted)))[0]==0
 cases=[('normal',{}),('flags',{'CGO_CFLAGS':'-include '+str(header)}),('search',{'CPATH':str(search),'C_INCLUDE_PATH':str(search),'CPLUS_INCLUDE_PATH':str(search),'OBJC_INCLUDE_PATH':str(search)}),('link',{'CGO_LDFLAGS':'-Wl,--native-delivery-untrusted-input','LIBRARY_PATH':str(poison)}),('persisted-go',{'GOENV':str(persisted)})]
 expected=None
 for name,extra in cases:
  cache=out/(name+'-cache');cache.mkdir();marker=out/(name+'-marker');current=dict(env,**extra,GOCACHE=str(cache),CD_ENV_MARKER=str(marker));build=out/(name+'-build');cmd=[sys.executable,str(fixture/'tools/native.py'),'build','--platform',a.platform,'--mode','release','--version','0.1.0','--source-commit',commit,'--output',str(build)];status,stdout=run(name+'-build',cmd,current,fixture)
  checks={'buildExit0':status==0};observed={'environmentInputs':extra,'markerBeforeNative':marker.exists()}
  if status==0:
   manifest=json.loads((build/'manifest.json').read_text());binary=build/manifest['binary'];nativeStatus,nativeOut=run(name+'-native',[str(binary),'version','--json'],current,fixture)
   if expected is None:expected=manifest
   checks.update(nativeExit0=nativeStatus==0,identity=json.loads(nativeOut)==expected['identity'],binary=manifest['binarySHA256']==expected['binarySHA256'],sourceInputs=manifest['sourceInputs']==expected['sourceInputs'],noMarker=not marker.exists())
   package=out/(name+'-package');packageStatus,_=run(name+'-package',[sys.executable,str(fixture/'tools/native.py'),'package','--manifest',str(build/'manifest.json'),'--output',str(package)],current,fixture);checks['packageExit0']=packageStatus==0
   if packageStatus==0:checks['packagedBinary']=json.loads((package/'package.json').read_text())['binarySHA256']==expected['binarySHA256']
   observed['nativeSHA256']=manifest['binarySHA256'];observed['markerAfterNative']=marker.exists()
  rows.append({'id':name,'checks':checks,'observed':observed,'passed':all(checks.values())})
except Exception as e:rows.append({'id':'runner','passed':False,'error':repr(e)})
finally:
 report={'scope':'synthetic-source-history/test-only, trusted compiler/system SDK; not a sandbox','platform':a.platform,'runnerSHA256':sha(pathlib.Path(__file__)),'expectedCases':5,'rows':rows,'commands':commands,'passed':len(rows)==5 and all(r['passed'] for r in rows)};(out/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'passed':report['passed'],'rows':len(rows)}))
sys.exit(0 if report['passed'] else 1)
