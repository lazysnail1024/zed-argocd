#!/usr/bin/env python3
"""Process-level issue acceptance; raw wire and CLI artifacts survive failures."""
import argparse,hashlib,json,pathlib,platform,subprocess,sys,time
import audit_repair_e2e as w
p=argparse.ArgumentParser();p.add_argument('binary',type=pathlib.Path);p.add_argument('output',type=pathlib.Path);p.add_argument('--large',action='store_true');a=p.parse_args();w.BINARY=a.binary.resolve();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);rows=[]
def pos(text,needle):
 s=text[:text.index(needle)+len(needle)];return {'line':s.count('\n'),'character':len(s.rsplit('\n',1)[-1].encode('utf-16-le'))//2}
def setup(name,text=w.APP,config=w.CONFIG):
 d=out/name;r=d/'workspace';r.mkdir(parents=True);w.write(r,'.argocd-ls.yaml',config);w.write(r,'project.yaml',w.PROJECT);f=w.write(r,'app.yaml',text);return d,r,f
def query(c,f,method,text,needle,**extra):
 return c.request('textDocument/'+method,dict(textDocument={'uri':f.as_uri()},position=pos(text,needle),**extra))
def record(name,fn):
 before=len(w.CLIENTS);start=time.monotonic();obs={};err=None
 try:fn(obs)
 except Exception as e:err=repr(e)
 finally:
  for c in w.CLIENTS[before:]:c.close()
 row={'id':name,'passed':err is None,'error':err,'observed':obs,'seconds':time.monotonic()-start};rows.append(row);print(json.dumps(row),flush=True)
def refs(obs):
 text=w.APP.replace('  source: {repoURL: https://example.org/repo}','  sources:\n    - {repoURL: values, ref: values}\n    - repoURL: chart\n      helm:\n        valueFiles: ["$values/config.yaml"]\n        fileParameters:\n          - name: config\n            path: "$values/config\u4e2d.yaml"')
 d,r,f=setup('references',text);c=w.LSP(d,[r]);c.open(f,text);c.diag(f,1)
 for needle in ['valueFiles: ["$values','path: "$values']:
  response=query(c,f,'definition',text,needle);obs[needle]=response;assert len(response.get('result',[]))==1,response
  items=query(c,f,'completion',text,needle).get('result',[]);assert [x['label'] for x in items]==['values'],items
 response=query(c,f,'references',text,'ref: values',context={'includeDeclaration':False});obs['references']=response;assert len(response.get('result',[]))==2
record('CD-10-both-source-ref-fields',refs)
def incomplete(obs):
 text=w.APP.replace('  source: {repoURL: https://example.org/repo}\n','')+'  sou\n  syncPolicy: {\n';d,r,f=setup('incomplete',text);c=w.LSP(d,[r]);c.open(f,text);c.diag(f,1)
 obs['definition']=query(c,f,'definition',text,'project: team');assert len(obs['definition'].get('result',[]))==1
 obs['completion']=query(c,f,'completion',text,'  sou');assert {'source','sources'} <= {x['label'] for x in obs['completion'].get('result',[])}
record('CD-09-reliable-incomplete-region',incomplete)
def typing(obs):
 d,r,f=setup('typing');c=w.LSP(d,[r]);c.open(f,w.APP);c.diag(f,1);obs['responses']=[]
 for i in range(10):
  text=w.APP+'# '+str(i)+'\n';c.change(f,text,i+2);response=query(c,f,'completion',text,'project: team');obs['responses'].append(response);time.sleep(.08)
 assert all([x['label'] for x in z.get('result',[])]==['team'] for z in obs['responses'])
record('CD-02-continuous-current-buffer-query',typing)
def multiroot(obs):
 d,r,f=setup('multi',w.APP+'  revisionHistoryLimit: wrong\n');b=d/'other';b.mkdir();cfg=w.write(b,'.argocd-ls.yaml',w.CONFIG);w.write(b,'project.yaml',w.PROJECT);c=w.LSP(d,[r,b]);c.open(f,f.read_text());assert any(x['code']=='SCHEMA_TYPE' for x in c.diag(f,1));c.open(cfg,w.CONFIG.replace('3.0.0','99.0.0'));c.diag(cfg,1)
 response=query(c,f,'definition',f.read_text(),'project: team');obs['query']=response;assert len(response.get('result',[]))==1 and response['result'][0]['uri']==(r/'project.yaml').as_uri();assert [x['label'] for x in query(c,f,'completion',f.read_text(),'project: team').get('result',[])]==['team'];refs=query(c,r/'project.yaml','references',w.PROJECT,'name: team',context={'includeDeclaration':False});assert len(refs.get('result',[]))==1 and refs['result'][0]['uri']==f.as_uri()
 publications=[x['message']['params']['diagnostics'] for x in c.messages if x['message'].get('method')=='textDocument/publishDiagnostics' and x['message']['params']['uri']==f.as_uri()];obs['publications']=publications;assert any(x['code']=='SCHEMA_TYPE' for x in publications[-1])
record('CD-05-independent-root-survives-failure',multiroot)
def quick(obs):
 d,r,f=setup('quickfix',w.APP.replace('project: team','project: missing'));(r/'project.yaml').unlink()
 for i in range(300):w.write(r,'p'+str(i)+'.yaml',w.PROJECT.replace('name: team','name: team'+str(i)))
 c=w.LSP(d,[r]);c.open(f,f.read_text());c.diag(f,1);point=pos(f.read_text(),'project: missing');start=time.monotonic();response=c.request('textDocument/codeAction',{'textDocument':{'uri':f.as_uri()},'range':{'start':point,'end':point},'context':{'diagnostics':[]}});obs['listSeconds']=time.monotonic()-start;obs['list']=response;assert len(response.get('result',[]))==300
 start=time.monotonic();resolved=c.request('codeAction/resolve',response['result'][0]);obs['resolveSeconds']=time.monotonic()-start;obs['resolve']=resolved;assert resolved.get('result',{}).get('edit')
record('CD-03-300-candidates-and-one-resolve',quick)
def cli(d,r):
 cmd=[str(w.BINARY),'check','--root',str(r),'--format','json'];q=subprocess.run(cmd,capture_output=True);(d/'stdout').write_bytes(q.stdout);(d/'stderr').write_bytes(q.stderr);(d/'command.json').write_text(json.dumps({'command':cmd,'exit':q.returncode}));return q.returncode,json.loads(q.stdout)
def patch(obs):
 text='apiVersion: argoproj.io/v1alpha1\nkind: ApplicationSet\nmetadata: {name: set}\nspec:\n  goTemplate: true\n  generators: []\n  template:\n    metadata: {name: child}\n    spec:\n      project: team\n      source: {repoURL: x}\n      destination: {name: in-cluster, server: https://kubernetes.default.svc, namespace: prod}\n  templatePatch: |\n    spec:\n      destination:\n        name: null\n'
 d,r,f=setup('patch',text);rc,v=cli(d,r);obs['report']=v;assert rc==0 and v['analysisComplete'] and not any('CD_DESTINATION_CONFLICT' in x['message'] for x in v['diagnostics'])
 f.write_text(text[:text.index('  templatePatch:')]);rc,v=cli(d,r);assert rc==1 and any('CD_DESTINATION_CONFLICT' in x['message'] for x in v['diagnostics'])
record('CD-06-static-patch-delete-and-positive-control',patch)
def helm(obs):
 d,r,f=setup('helm');f.unlink();w.write(r,'Chart.yaml','apiVersion: v2\nname: sample\nversion: 1.0.0\n');w.write(r,'templates/app.yaml',w.APP.replace('project: team','project: 42'));rc,v=cli(d,r);obs['report']=v;assert rc==1 and any(x['code']=='SCHEMA_TYPE' for x in v['diagnostics'])
record('CD-08-pure-chart-YAML',helm)
def guidance(obs):
 d,r,f=setup('guidance',config=w.CONFIG.replace('controlPlaneNamespace: argocd\n',''));rc,v=cli(d,r);obs['report']=v;messages=' '.join(x['message'] for x in v['diagnostics']);assert '.argocd-ls.yaml' in messages and 'controlPlaneNamespace' in messages
record('CD-12-actionable-config-guidance',guidance)
if a.large:
 def exclusion(obs):
  d,r,f=setup('exclusion',config=w.CONFIG+'exclude: ["generated/**"]\n');g=r/'generated';g.mkdir()
  for i in range(50001):(g/str(i)).touch()
  rc,v=cli(d,r);obs['report']=v;assert rc==0 and v['analysisComplete'] and v['resourcesAnalyzed']==2
 record('CD-04-excluded-subtree-budget',exclusion)
manifest={'command':sys.argv,'binarySHA256':w.hash_file(w.BINARY),'runnerSHA256':w.hash_file(pathlib.Path(__file__)),'python':sys.version,'platform':platform.platform(),'cases':rows,'filesSHA256':{str(f.relative_to(out)):w.hash_file(f) for f in out.rglob('*') if f.is_file()}};(out/'report.json').write_text(json.dumps(manifest,indent=2)+'\n');sys.exit(not rows or not all(x['passed'] for x in rows))
