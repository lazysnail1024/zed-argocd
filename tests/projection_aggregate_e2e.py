#!/usr/bin/env python3
"""Independent aggregate graph admission and current-result recovery through CLI/LSP."""
import hashlib,json,pathlib,subprocess,sys
import audit_repair_e2e as wire
wire.BINARY=pathlib.Path(sys.argv[1]).resolve();out=pathlib.Path(sys.argv[2]).resolve();out.mkdir(parents=True,exist_ok=False);rows=[]
cases=json.loads(pathlib.Path(__file__).with_name('alias-projection-cases.json').read_text())['cases'];large=next(c['text'] for c in cases if c['id']=='expanded-boundary--1')
# Each graph is exactly 99,999 projected nodes. Two cannot share a 100,000 allowance.
plain='apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata: {name: plain}\nspec:\n  project: team\n  source: {repoURL: https://example.org/repo}\n  destination: {server: here}\n'
config='targetVersion: 3.5.6\ncontrolPlaneNamespace: argocd\nreferencePolicy: open\n'
def record(name,checks,observed):rows.append({'id':name,'checks':checks,'observed':observed,'passed':all(checks.values())})
def setup(root):root.mkdir(parents=True);(root/'.argocd-ls.yaml').write_text(config);(root/'project.yaml').write_text(wire.PROJECT)
def run(cmd, folder):
 try:
  result=subprocess.run(cmd,capture_output=True,timeout=12)
 except subprocess.TimeoutExpired as e:
  (folder/'stdout').write_bytes(e.stdout or b'');(folder/'stderr').write_bytes(e.stderr or b'');raise
 except OSError as e:
  (folder/'stdout').write_bytes(b'');(folder/'stderr').write_text(repr(e));raise
 return result
try:
 for layout in ['documents','files','scopes']:
  folder=out/layout;root=folder/'workspace';setup(root)
  if layout=='documents':(root/'a.yaml').write_text(large+'---\n'+large+'---\n'+plain)
  else:
   if layout=='scopes':
    (root/'.argocd-ls.yaml').write_text(config+'scopes:\n  - name: a\n    include: [a/**]\n  - name: b\n    include: [b/**]\n');(root/'a').mkdir();(root/'b').mkdir()
   (root/('a/input.yaml' if layout=='scopes' else 'a.yaml')).write_text(large);(root/('b/input.yaml' if layout=='scopes' else 'b.yaml')).write_text(large);(root/'z.yaml').write_text(plain)
  cmd=[str(wire.BINARY),'check','--root',str(root),'--format','json'];r=run(cmd,folder);(folder/'stdout').write_bytes(r.stdout);(folder/'stderr').write_bytes(r.stderr)
  try:
   report=json.loads(r.stdout);budget=[d for d in report['diagnostics'] if d['code']=='INPUT_BUDGET_EXCEEDED'];record(layout,{'exit2':r.returncode==2,'resources3':report['resourcesAnalyzed']==3,'oneRefused':len(budget)==1,'incomplete':report['analysisComplete'] is False,'noDeadline':'deadline' not in r.stdout.decode().lower()},report)
  except Exception as e:record(layout,{'report':False},repr(e))
 # Independently derived formula: 127 fixed nodes + 493 * 101 + 80 = 50,000.
 baseStart=large.index('    expanded: [');baseEnd=large.index(']\n',baseStart)+1
 half=large[:baseStart]+'    expanded: ['+','.join(['*base']*493+['x']*80)+']'+large[baseEnd:]
 for label,text,want,budgets in [('exact-boundary',half+'---\n'+half,3,0),('over-boundary',half+'---\n'+half.replace(',x]\n',',x,x]\n'),2,1)]:
  folder=out/label;root=folder/'workspace';setup(root);(root/'input.yaml').write_text(text);cmd=[str(wire.BINARY),'check','--root',str(root),'--format','json']
  try:
   proc=run(cmd,folder);(folder/'stdout').write_bytes(proc.stdout);(folder/'stderr').write_bytes(proc.stderr);r=json.loads(proc.stdout);record(label,{'exit':proc.returncode==(2 if budgets else 0),'resources':r['resourcesAnalyzed']==want,'budgetCount':sum(d['code']=='INPUT_BUDGET_EXCEEDED' for d in r['diagnostics'])==budgets},r)
  except Exception as e:record(label,{'terminal':False},repr(e))
 for multi in [False,True]:
  folder=out/('roots' if multi else 'reuse');a=folder/'a';b=folder/'b' if multi else a;setup(a)
  if multi:setup(b)
  first=a/'a.yaml';second=b/'b.yaml';first.write_text(plain);second.write_text(large);c=None
  try:
   c=wire.LSP(folder,[b,a] if multi else [a]);c.open(first,large,1);c.open(second,large,1)
   def diagnostics(path,version):return c.diag(path,version)
   initial=diagnostics(second,1);record(str(multi)+'-cold-refusal',{'oneBudget':[d['code'] for d in initial]==['INPUT_BUDGET_EXCEEDED']},initial)
   # Change first graph to plain; unchanged second must be re-admitted, not reuse refusal.
   c.change(first,plain,2);diagnostics(first,2);ds=diagnostics(second,1);record(str(multi)+'-replacement-recovery',{'noErrors':ds==[]},ds)
   # Reused second graph must count again when earlier scope is restored.
   c.change(first,large,3);diagnostics(first,3);ds=diagnostics(second,1);record(str(multi)+'-reuse-refusal',{'oneBudget':[d['code'] for d in ds]==['INPUT_BUDGET_EXCEEDED']},ds)
   c.send('textDocument/didClose',{'textDocument':{'uri':first.as_uri()}});ds=diagnostics(second,1);record(str(multi)+'-close-recovery',{'noErrors':ds==[]},ds)
  except Exception as e:record(str(multi)+'-runner',{'terminal':False},repr(e))
  finally:
   if c:c.close();record(str(multi)+'-shutdown',{'exit':c.proc.returncode==0},c.proc.returncode)
except Exception as e:record("runner",{"terminal":False},repr(e))
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest();report={'binarySHA256':sha(wire.BINARY),'runnerSHA256':sha(pathlib.Path(__file__)),'rows':rows,'passed':len(rows)==15 and all(r['passed'] for r in rows)};(out/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'passed':report['passed'],'failed':[r['id'] for r in rows if not r['passed']]}));sys.exit(0 if report['passed'] else 1)
