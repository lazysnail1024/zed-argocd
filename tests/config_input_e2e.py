#!/usr/bin/env python3
"""Real config input rejection, authoritative buffers and LSP recovery."""
import hashlib,importlib.util,json,os,pathlib,subprocess,sys,time,shutil
binary=pathlib.Path(sys.argv[1]).resolve();out=pathlib.Path(sys.argv[2]).resolve();out.mkdir(parents=True,exist_ok=True);os.environ['CD_AUDIT_BINARY']=str(binary);spec=importlib.util.spec_from_file_location('client',pathlib.Path(__file__).with_name('audit_repair_e2e.py'));e=importlib.util.module_from_spec(spec);spec.loader.exec_module(e);rows=[]
for kind in ['fifo','directory','oversized','normal']:
 folder=out/('cli-'+kind);root=folder/'workspace'
 if root.exists():shutil.rmtree(root)
 root.mkdir(parents=True,exist_ok=True);config=root/'.argocd-ls.yaml';e.write(root,'app.yaml',e.APP)
 if kind=='fifo':os.mkfifo(config)
 elif kind=='directory':config.mkdir()
 elif kind=='oversized':config.write_text('#'+'x'*1048576)
 else:config.write_text(e.CONFIG)
 cmd=[str(binary),'check','--root',str(root),'--format','json'];start=time.monotonic();stdout=stderr=b'';code=None
 try:
  p=subprocess.run(cmd,capture_output=True,timeout=2);stdout,stderr,code=p.stdout,p.stderr,p.returncode;r=json.loads(stdout);codes=[x['code'] for x in r['configurationErrors']];checks={'terminal':code==(1 if kind=='normal' else 2),'status':codes==([] if kind=='normal' else ['CONFIG_INVALID']),'counts':r['resourcesAnalyzed']==(1 if kind=='normal' else 0),'complete':r['analysisComplete']==(kind=='normal')};row={'name':'cli-'+kind,'assertions':checks,'passed':all(checks.values())}
 except Exception as ex:
  if isinstance(ex,subprocess.TimeoutExpired):stdout,stderr=ex.stdout or b'',ex.stderr or b''
  row={'name':'cli-'+kind,'error':repr(ex),'passed':False}
 row.update(command=cmd,exit=code,seconds=time.monotonic()-start);rows.append(row);(folder/'stdout.raw').write_bytes(stdout);(folder/'stderr.raw').write_bytes(stderr)
for kind in ['fifo','oversized']:
 folder=out/('lsp-'+kind);root=folder/'workspace'
 if root.exists():shutil.rmtree(root)
 root.mkdir(parents=True,exist_ok=True);config=root/'.argocd-ls.yaml';app=e.write(root,'app.yaml',e.APP);e.write(root,'project.yaml',e.PROJECT)
 if kind=='fifo':os.mkfifo(config)
 else:config.write_text('#'+'x'*1048576)
 c=None
 try:
  c=e.LSP(folder,[root]);time.sleep(.2);c.open(config,e.CONFIG,1);c.open(app,e.APP,1);ds=c.diag(app,1);assert ds==[],ds
  c.send('textDocument/completion',{'textDocument':{'uri':app.as_uri()},'position':{'line':5,'character':14}},500);c.send('$/cancelRequest',{'id':500});response=c.wait(lambda m:m.get('id')==500,timeout=1);assert response.get('error',{}).get('code') in [-32800,-32801] or 'result' in response
  config.unlink();config.write_text(e.CONFIG);c.send('textDocument/didClose',{'textDocument':{'uri':config.as_uri()}});c.change(app,e.APP.replace('namespace: default','namespace: default, name: conflict'),2);ds=c.diag(app,2);assert [d['code'] for d in ds]==['CD_DESTINATION_CONFLICT'],ds
  c.close();assert c.proc.returncode==0;assert sum(m['message'].get('id')==500 for m in c.messages)==1
  rows.append({'name':'lsp-'+kind,'passed':True,'expected':'buffer overrides nonregular/oversized disk; one request terminal reply; restored disk config and new diagnostic; clean shutdown'})
 except Exception as ex:rows.append({'name':'lsp-'+kind,'error':repr(ex),'passed':False})
 finally:
  if c:c.close()
report={'binarySHA256':hashlib.sha256(binary.read_bytes()).hexdigest(),'runnerSHA256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'rows':rows,'passed':len(rows)==6 and all(x['passed'] for x in rows)};(out/'report.json').write_text(json.dumps(report,indent=2));print(json.dumps(report));sys.exit(0 if report['passed'] else 1)
