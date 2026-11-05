#!/usr/bin/env python3
"""Real build/package processes and disposable source-history provenance controls."""
import argparse,hashlib,json,os,pathlib,shutil,subprocess,sys,tarfile,zipfile,time
ROOT=pathlib.Path(__file__).resolve().parents[1]
ap=argparse.ArgumentParser();ap.add_argument('output',type=pathlib.Path);ap.add_argument('--platform',required=True);a=ap.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);rows=[];active=None;completed=False
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
def save():
 report={'scope':'native packaging and synthetic-source-history/test-only; not a product release','runnerSHA256':sha(pathlib.Path(__file__)),'platform':a.platform,'command':sys.argv,'python':sys.version,'activeCommand':active,'completed':completed,'rows':rows,'passed':completed and bool(rows) and all(x['passed'] for x in rows)}
 pending=out/'report.pending.json';pending.write_text(json.dumps(report,indent=2)+'\n');pending.replace(out/'report.json');return report
def record(row):
 rows.append(row);save()
def invoke(name,root,command,expected=0):
 global active
 folder=out/'commands'/name;folder.mkdir(parents=True);cmd=[sys.executable,str(root/'tools/native.py')]+command
 active={'id':name,'command':cmd,'timeoutSeconds':180};save();started=time.monotonic()
 try:p=subprocess.run(cmd,cwd=root,capture_output=True,timeout=180);stdout,stderr,status=p.stdout,p.stderr,p.returncode
 except subprocess.TimeoutExpired as e:stdout,stderr,status=e.stdout or b'',e.stderr or b'','timeout'
 except OSError as e:stdout,stderr,status=b'',str(e).encode(),'launch-failed'
 (folder/'stdout').write_bytes(stdout);(folder/'stderr').write_bytes(stderr)
 try:r=json.loads(stdout)
 except Exception:r={}
 checks={'exit':status==expected,'structured':r.get('passed')==(expected==0)}
 if expected==2:
  reason={'real-release-refused':'provenance','wrong-platform':'platform','unsafe-version':'version','commit-mismatch':'commit','dirty-tracked':'provenance','dirty-untracked':'provenance','existing-output':'already exist','tampered-hash':'hash','package-platform-mismatch':'binary path','missing-dependency-notice':'inventory','modified-license':'license'}.get(name)
  if reason:checks['failureReason']=reason.lower() in r.get('error','').lower()
 active=None
 record({'id':name,'command':cmd,'exit':status,'elapsedSeconds':time.monotonic()-started,'checks':checks,'passed':all(checks.values())});return r
save()
try:
 head=subprocess.run(['git','rev-parse','--verify','HEAD'],cwd=ROOT,capture_output=True);expectedCommit=head.stdout.decode().strip() if head.returncode==0 else None;expectedDirty=expectedCommit is None or bool(subprocess.check_output(['git','status','--porcelain','--untracked-files=all'],cwd=ROOT))
 real=invoke('development',ROOT,['build','--output',str(out/'development-build'),'--platform',a.platform]);assert real.get('passed'),'development tool unavailable'
 manifest=out/'development-build/manifest.json';m=json.loads(manifest.read_text());identity=m['identity'];record({'id':'real-development-identity','passed':identity['buildMode']=='development' and identity['dirty']==expectedDirty and identity['sourceCommit']==expectedCommit})
 invoke('real-release-refused',ROOT,['build','--output',str(out/'real-release'),'--platform',a.platform,'--mode','release','--version','0.1.0'],2)
 invoke('wrong-platform',ROOT,['build','--output',str(out/'wrong-platform'),'--platform','unsupported/amd64'],2)
 invoke('unsafe-version',ROOT,['build','--output',str(out/'unsafe-version'),'--platform',a.platform,'--version','../escape'],2)
 fixture=out/'synthetic-source-history';fixture.mkdir();names=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT).decode().split('\0')
 for name in names:
  if name:p=fixture/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,p)
 env=dict(os.environ,GIT_AUTHOR_NAME='Synthetic fixture',GIT_AUTHOR_EMAIL='fixture@example.invalid',GIT_COMMITTER_NAME='Synthetic fixture',GIT_COMMITTER_EMAIL='fixture@example.invalid',GIT_AUTHOR_DATE='2000-01-01T00:00:00Z',GIT_COMMITTER_DATE='2000-01-01T00:00:00Z')
 for command in [['git','init'],['git','-c','core.autocrlf=false','add','.'],['git','-c','commit.gpgsign=false','commit','-m','Synthetic E2E source history only']]:
  p=subprocess.run(command,cwd=fixture,env=env,capture_output=True);assert p.returncode==0,p.stderr
 commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=fixture).decode().strip();(out/'synthetic-provenance.json').write_text(json.dumps({'scope':'synthetic-source-history/test-only','commit':commit})+'\n')
 release=out/'synthetic-release';invoke('synthetic-release',fixture,['build','--output',str(release),'--platform',a.platform,'--mode','release','--version','0.1.0','--source-commit',commit]);rm=json.loads((release/'manifest.json').read_text());record({'id':'synthetic-release-identity','passed':rm['identity']['sourceCommit']==commit and rm['identity']['dirty'] is False and rm['identity']['buildMode']=='release'})
 # Ignored checkout inputs must not enter the validated compilation snapshot.
 ignored=[('go','cmd/argocd-ls/ignored_delivery.go','package main\nfunc init(){println("UNCOMMITTED DELIVERY INPUT")}\n'),('embed','internal/assets/schemas/ignored-delivery.json','{}\n'),('native','vendor/github.com/lazysnail1024/k8s-crd-toolkit/yamlview/internal/parser/grammar/ignored_delivery.c','#error UNCOMMITTED_DELIVERY_INPUT\n')]
 exclude=fixture/'.git/info/exclude';savedExclude=exclude.read_text()
 for label,name,value in ignored:
  (fixture/name).write_text(value)
 exclude.write_text(savedExclude+'\n'+''.join('/'+name+'\n' for _,name,_ in ignored));assert not subprocess.check_output(['git','status','--porcelain'],cwd=fixture)
 result=invoke('ignored-inputs',fixture,['build','--output',str(out/'ignored-inputs'),'--platform',a.platform,'--mode','release','--version','0.1.0','--source-commit',commit])
 actual=json.loads((out/'ignored-inputs/manifest.json').read_text()) if result.get('passed') else {}
 # These independent release builds already compile identical declared inputs
 # from different temporary snapshots. Use them for reproducibility as well as
 # the ignored-input controls, avoiding another full development-mode rebuild.
 record({'id':'repeat-native-bytes','firstBuild':'synthetic-release','repeatBuild':'ignored-inputs','passed':actual.get('binarySHA256')==rm['binarySHA256'] and actual.get('identity')==rm['identity'] and actual.get('sourceInputs')==rm['sourceInputs']})
 for label,name,_ in ignored:
  record({'id':'ignored-'+label+'-not-compiled','passed':actual.get('binarySHA256')==rm['binarySHA256'] and name not in actual.get('sourceInputs',{})})
  (fixture/name).unlink()
 exclude.write_text(savedExclude)
 # A declared source that requires an undeclared input must fail, not read through the snapshot boundary.
 required=[('embed','package main\nimport _ "embed"\n//go:embed required.txt\nvar required string\nfunc init(){println(required)}\n','required.txt','UNDECLARED EMBED'),('include','package main\n// #include "required.h"\nimport "C"\n','required.h','/* UNDECLARED NATIVE HEADER */\n')]
 for label,code,missing,value in required:
  isolated=out/('required-'+label+'-source');shutil.copytree(fixture,isolated);source=isolated/'cmd/argocd-ls/required_delivery.go';source.write_text(code);dependency=source.parent/missing;dependency.write_text(value);(isolated/'.git/info/exclude').write_text('/cmd/argocd-ls/'+missing+'\n')
  for command in [['git','add','cmd/argocd-ls/required_delivery.go'],['git','-c','commit.gpgsign=false','commit','-m','Synthetic required input only']]:
   r=subprocess.run(command,cwd=isolated,env=env,capture_output=True);assert r.returncode==0,r.stderr
  requiredCommit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=isolated).decode().strip();invoke('required-'+label,isolated,['build','--output',str(out/('required-'+label+'-build')),'--platform',a.platform,'--mode','release','--version','0.1.0','--source-commit',requiredCommit],2)
 invoke('commit-mismatch',fixture,['build','--output',str(out/'mismatch'),'--platform',a.platform,'--mode','release','--version','0.1.0','--source-commit','0'*40],2)
 tracked=fixture/'README.md';original=tracked.read_bytes();tracked.write_bytes(original+b'\nchanged\n');invoke('dirty-tracked',fixture,['build','--output',str(out/'dirty-tracked'),'--platform',a.platform,'--mode','release','--version','0.1.0','--source-commit',commit],2);tracked.write_bytes(original)
 extra=fixture/'untracked.txt';extra.write_text('untracked');invoke('dirty-untracked',fixture,['build','--output',str(out/'dirty-untracked'),'--platform',a.platform,'--mode','release','--version','0.1.0','--source-commit',commit],2);extra.unlink()
 # Missing source history must not be promoted to a release.
 gitdir=fixture/'.git';gitdir.rename(fixture/'.git-held')
 invoke('missing-git-history',fixture,['build','--output',str(out/'missing-history'),'--platform',a.platform,'--mode','release','--version','0.1.0','--source-commit',commit],2)
 (fixture/'.git-held').rename(gitdir)
 # A package must not need command source or invoke another build.
 shutil.rmtree(fixture/'cmd')
 for label in ['one','two']:invoke('package-'+label,fixture,['package','--manifest',str(release/'manifest.json'),'--output',str(out/('package-'+label+'-result'))])
 p1=json.loads((out/'package-one-result/package.json').read_text());p2=json.loads((out/'package-two-result/package.json').read_text());archive=out/'package-one-result'/p1['archive'];record({'id':'deterministic-archive','passed':p1['archiveSHA256']==p2['archiveSHA256']==sha(archive)})
 extracted=out/'extracted';extracted.mkdir()
 if archive.suffix=='.zip':
  with zipfile.ZipFile(archive) as z:names=z.namelist();z.extractall(extracted)
 else:
  with tarfile.open(archive) as t:names=t.getnames();assert all(x.isfile() for x in t.getmembers());t.extractall(extracted,filter='data')
 record({'id':'safe-layout','passed':all(not n.startswith('/') and '..' not in pathlib.PurePosixPath(n).parts for n in names) and len({n.split('/')[0] for n in names})==1 and any(n.endswith('/LICENSE') for n in names) and any('/licenses/go/' in n for n in names) and any('/licenses/argo-cd/' in n for n in names)})
 if a.platform=='windows/amd64':
  pinned=json.loads((ROOT/'tools/windows-runtime.json').read_text())
  record({'id':'windows-runtime-notices','passed':all(sha(extracted/p1['root']/'licenses/windows-runtime'/name)==expected for name,expected in pinned['legalFiles'].items())})
  for label,corrupt in [('missing-runtime-notice',False),('rewritten-runtime-notice',True)]:
   copied=release/'legal/licenses/windows-runtime/winpthreads/COPYING';original=copied.read_bytes();bad=json.loads((release/'manifest.json').read_text());key='licenses/windows-runtime/winpthreads/COPYING'
   if corrupt:copied.write_bytes(b'altered runtime notice');bad['legalInputs'][key]=sha(copied)
   else:del bad['legalInputs'][key]
   altered=release/(label+'.json');altered.write_text(json.dumps(bad));invoke(label,fixture,['package','--manifest',str(altered),'--output',str(out/label)],2);copied.write_bytes(original)
 record({'id':'go-patent-grant','passed':(extracted/p1['root']/'licenses/go/PATENTS').is_file() and (extracted/p1['root']/'licenses/go/PATENTS').read_bytes()==(pathlib.Path(rm['goEnvironment']['GOROOT'])/'PATENTS').read_bytes()})
 binary=extracted/p1['root']/('argocd-ls.exe' if a.platform.startswith('windows/') else 'argocd-ls');binary.chmod(0o755);r=subprocess.run([str(binary),'version','--json'],capture_output=True,timeout=10);(out/'extracted.stdout').write_bytes(r.stdout);(out/'extracted.stderr').write_bytes(r.stderr);record({'id':'extracted-native','passed':r.returncode==0 and json.loads(r.stdout)==rm['identity'] and sha(binary)==rm['binarySHA256']})
 invoke('existing-output',fixture,['package','--manifest',str(release/'manifest.json'),'--output',str(out/'package-one-result')],2)
 bad=json.loads((release/'manifest.json').read_text());bad['binarySHA256']='0'*64;(release/'tampered.json').write_text(json.dumps(bad));invoke('tampered-hash',fixture,['package','--manifest',str(release/'tampered.json'),'--output',str(out/'bad-hash')],2)
 bad=json.loads((release/'manifest.json').read_text());bad['platform']='windows/amd64' if a.platform!='windows/amd64' else 'linux/amd64';(release/'wrong-platform.json').write_text(json.dumps(bad));invoke('package-platform-mismatch',fixture,['package','--manifest',str(release/'wrong-platform.json'),'--output',str(out/'package-wrong-platform')],2)
 bad=json.loads((release/'manifest.json').read_text());removed=next(k for k in bad['legalInputs'] if k.endswith('/NOTICE'));del bad['legalInputs'][removed];(release/'missing-notice.json').write_text(json.dumps(bad));invoke('missing-dependency-notice',fixture,['package','--manifest',str(release/'missing-notice.json'),'--output',str(out/'missing-notice')],2)
 bad=json.loads((release/'manifest.json').read_text());bad['product']='argo-workflows';(release/'wrong-product.json').write_text(json.dumps(bad));invoke('wrong-product',fixture,['package','--manifest',str(release/'wrong-product.json'),'--output',str(out/'wrong-product')],2)
 bad=json.loads((release/'manifest.json').read_text());bad['legalInputs']['../escape']='0'*64;(release/'unsafe-legal.json').write_text(json.dumps(bad));invoke('unsafe-legal',fixture,['package','--manifest',str(release/'unsafe-legal.json'),'--output',str(out/'unsafe-legal')],2)
 directory=release/'directory-manifest';directory.mkdir();invoke('nonregular-manifest',fixture,['package','--manifest',str(directory),'--output',str(out/'nonregular')],2)
 if os.name!='nt':
  link=release/'linked-manifest.json';link.symlink_to(release/'manifest.json');invoke('symlink-manifest',fixture,['package','--manifest',str(link),'--output',str(out/'symlink')],2)
 # Editing a copied legal file together with its claimed digest must still fail.
 for label,name in [('go-license','licenses/go/LICENSE'),('go-patents','licenses/go/PATENTS'),('crd-license',next(n for n in rm['legalInputs'] if n.startswith('licenses/argo-cd/')))]:
  path=release/'legal'/name;original=path.read_bytes();path.write_bytes(b'Altered legal grant\n');bad=json.loads((release/'manifest.json').read_text());bad['legalInputs'][name]=sha(path);changed=release/(label+'.json');changed.write_text(json.dumps(bad));invoke('altered-'+label,fixture,['package','--manifest',str(changed),'--output',str(out/('altered-'+label))],2);path.write_bytes(original)
 import audit_repair_e2e as wire
 workspace=out/'extracted-workspace';workspace.mkdir();app=wire.write(workspace,'app.yaml',wire.APP);wire.write(workspace,'project.yaml',wire.PROJECT);wire.write(workspace,'.argocd-ls.yaml',wire.CONFIG);wire.BINARY=binary
 r=subprocess.run([str(binary),'check','--root',str(workspace),'--format','json'],capture_output=True,timeout=10);(out/'extracted-check.stdout').write_bytes(r.stdout);(out/'extracted-check.stderr').write_bytes(r.stderr);value=json.loads(r.stdout);record({'id':'extracted-check','passed':r.returncode==0 and value['resourcesAnalyzed']==2 and value['analysisComplete'] and value['diagnostics']==[]})
 lsp=out/'extracted-lsp';lsp.mkdir();client=wire.LSP(lsp,[workspace])
 try:
  client.open(app,wire.APP,1);ds=client.diag(app,1);hover=client.request('textDocument/hover',{'textDocument':{'uri':app.as_uri()},'position':{'line':5,'character':4}});record({'id':'extracted-lsp','passed':ds==[] and '3.0.0' in json.dumps(hover)})
 finally:client.close();record({'id':'extracted-lsp-shutdown','passed':client.proc.returncode==0})
 legal=fixture/'LICENSE';legal.write_text('incorrect license');invoke('modified-license',fixture,['package','--manifest',str(release/'manifest.json'),'--output',str(out/'bad-license')],2)
 completed=True
except Exception as e:record({'id':'runner','passed':False,'error':repr(e)})
finally:
 report=save();print(json.dumps({'rows':len(rows),'passed':report['passed']}))
sys.exit(0 if report['passed'] else 1)
