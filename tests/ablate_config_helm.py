#!/usr/bin/env python3
"""Prepare isolated deletion variants; execute only with the explicit --run flag."""
import argparse, hashlib, json, os, pathlib, shutil, subprocess, sys
ROOT=pathlib.Path(__file__).resolve().parents[1]
VARIANTS=[
 dict(name='unbound-definition-candidates',file='internal/analysis/queries.go',old='if project == "" {',new='if project == "" || options.ControlPlaneNamespace == "" {',oracle='navigation_defaults_e2e.py',failure='unbound-project-all-local-candidates-no-authority',loss='Without namespace configuration, definition loses both local AppProject candidates; explicit namespace still filters normally.'),
 dict(name='dynamic-key-proof',file='internal/analysis/helm_source.go',old='len(parts) == 0 || key || node.Kind()',new='len(parts) == 0 || (key && false) || node.Kind()',oracle='helm_source_e2e.py',failure='dynamic-key-refuses-projection',loss='A dynamic spec mapping key that can collide with project is incorrectly admitted, returning a project definition from unproved structure.'),
 dict(name='saved-buffer-authority',file='internal/lsp/render.go',old='e != nil || opened.unavailable != "" || text != opened.snapshot.Text()',new='e != nil || opened.unavailable != "" || (text != opened.snapshot.Text() && false)',oracle='render_defaults_e2e.py',failure='defaults-and-explicit-action',loss='Explicit render succeeds from saved project team while the open buffer requests unsaved-team, instead of refusing unsaved input.')]
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def hashes(root):return {str(p.relative_to(root)):sha(p) for p in sorted(root.rglob('*')) if p.is_file()}
def capture(command,cwd,env,folder,stem,timeout):
 try:p=subprocess.run(command,cwd=cwd,env=env,capture_output=True,timeout=timeout)
 except subprocess.TimeoutExpired as ex:p=subprocess.CompletedProcess(command,124,ex.stdout or b'',ex.stderr or b'')
 (folder/(stem+'.stdout')).write_bytes(p.stdout);(folder/(stem+'.stderr')).write_bytes(p.stderr)
 return dict(command=command,cwd=str(cwd),exit=p.returncode,stdoutSHA256=sha(folder/(stem+'.stdout')),stderrSHA256=sha(folder/(stem+'.stderr')))
def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--binary',type=pathlib.Path,default=ROOT/'build/argocd-ls');parser.add_argument('--helm',type=pathlib.Path);parser.add_argument('--run',action='store_true',help='Build and execute controlled variants after coordinator authorization');parser.add_argument('--output',type=pathlib.Path,default=ROOT/'artifacts/config-helm-ablation');args=parser.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=True);snapshot=out/'source';planpath=out/'plan.json'
 if not planpath.exists():
  snapshot.mkdir()
  for part in ['go.mod','go.sum','cmd','internal','vendor']:
   p=ROOT/part
   if p.is_dir():shutil.copytree(p,snapshot/part)
   else:shutil.copy2(p,snapshot/part)
  (snapshot/'tests').mkdir()
  for name in {v['oracle'] for v in VARIANTS}|{'audit_repair_e2e.py','native_ci.py'}:shutil.copy2(ROOT/'tests'/name,snapshot/'tests'/name)
  shutil.copytree(ROOT/'tests/support',snapshot/'tests/support',ignore=shutil.ignore_patterns('__pycache__'))
  shutil.copy2(args.binary.resolve(),out/'control-argocd-ls')
  for v in VARIANTS:assert (snapshot/v['file']).read_text().count(v['old'])==1,(v['name'],'mutation anchor must be unique')
  plan=dict(status='prepared-not-executed',candidateSHA256=sha(out/'control-argocd-ls'),sourceHashes=hashes(snapshot),variants=VARIANTS,prepareCommand=sys.argv,runnerSHA256=sha(pathlib.Path(__file__)),python=sys.version)
  planpath.write_text(json.dumps(plan,indent=2)+'\n')
 else:
  plan=json.loads(planpath.read_text());assert hashes(snapshot)==plan['sourceHashes'],'prepared inputs changed';assert sha(out/'control-argocd-ls')==plan['candidateSHA256'],'control binary changed'
 if not args.run:
  print(json.dumps({'status':'prepared-not-executed','plan':str(planpath),'variants':len(VARIANTS)}));return 0
 env=os.environ.copy();env.update(GOCACHE=str(out/'go-cache'),GOWORK='off',GOPROXY='off');rows=[]
 if args.helm:env['PATH']=str(args.helm.resolve().parent)+os.pathsep+env.get('PATH','')
 for v in VARIANTS:
  folder=out/v['name'];folder.mkdir();oracle=snapshot/'tests'/v['oracle'];control=folder/'control';control.mkdir();control_env=env|dict(CD_AUDIT_BINARY=str(out/'control-argocd-ls'),CD_AUDIT_OUTPUT=str(control/'oracle'))
  baseline=capture([sys.executable,str(oracle)],snapshot,control_env,control,'run',120)
  if baseline['exit']!=0:
   rows.append(dict(variant=v['name'],control=baseline,detected=False,blocked='control failed; mutation not built'))
   (out/'report.json').write_text(json.dumps(dict(candidateSHA256=plan['candidateSHA256'],cases=rows),indent=2)+'\n');continue
  copy=folder/'source';shutil.copytree(snapshot,copy);path=copy/v['file'];before=path.read_text();path.write_text(before.replace(v['old'],v['new'],1));(folder/'deletion.json').write_text(json.dumps(v,indent=2)+'\n')
  binary=folder/'argocd-ls';build=capture(['go','build','-mod=vendor','-buildvcs=false','-o',str(binary),'./cmd/argocd-ls'],copy,env,folder,'build',180);row=dict(variant=v['name'],expectedLoss=v['loss'],control=baseline,build=build,detected=False,mutationSourceSHA256=sha(path))
  if build['exit']==0:
   test_env=env|dict(CD_AUDIT_BINARY=str(binary),CD_AUDIT_OUTPUT=str(folder/'oracle'));run=capture([sys.executable,str(copy/'tests'/v['oracle'])],copy,test_env,folder,'run',120);report=json.loads((folder/'oracle/report.json').read_text());failed=[c['id'] for c in report['cases'] if not c['passed']];row.update(run=run,binarySHA256=sha(binary),failedCases=failed,detected=run['exit']==1 and v['failure'] in failed)
  rows.append(row);(out/'report.json').write_text(json.dumps(dict(candidateSHA256=plan['candidateSHA256'],environment={k:env[k] for k in ['GOCACHE','GOWORK','GOPROXY']},cases=rows),indent=2)+'\n');print(json.dumps(row),flush=True)
 return 0 if len(rows)==3 and all(r['detected'] for r in rows) else 1
if __name__=='__main__':sys.exit(main())
