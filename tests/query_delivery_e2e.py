#!/usr/bin/env python3
"""In-flight read validity across independent scopes and changed dependencies."""
import argparse,json,pathlib,sys
import audit_repair_e2e as w
p=argparse.ArgumentParser();p.add_argument('binary',type=pathlib.Path);p.add_argument('output',type=pathlib.Path);a=p.parse_args();w.BINARY=a.binary.resolve();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);r=out/'workspace';r.mkdir();w.write(r,'.argocd-ls.yaml',w.CONFIG+'scopes:\n  - name: a\n    include: ["a/**"]\n  - name: b\n    include: ["b/**"]\n');project=w.write(r,'a/project.yaml',w.PROJECT);other=w.write(r,'b/app.yaml',w.APP);expected=set()
for i in range(2000):expected.add(w.write(r,'a/'+str(i)+'.yaml',w.APP.replace('name: app','name: app'+str(i))).as_uri())
rows=[];c=w.LSP(out,[r]);params={'textDocument':{'uri':project.as_uri()},'position':{'line':3,'character':12},'context':{'includeDeclaration':False}}
try:
 c.open(project,w.PROJECT);c.open(other,w.APP);c.diag(project,1);response=c.request('textDocument/references',params);assert {x['uri'] for x in response.get('result',[])}==expected;rows.append({'id':'exact-positive-control','passed':True})
 for i in range(10):
  ident=100+i;c.send('textDocument/references',params,ident);c.change(other,w.APP+'# unrelated '+str(i)+'\n',i+2);response=c.wait(lambda x:x.get('id')==ident);passed={x['uri'] for x in response.get('result',[])}==expected;rows.append({'id':'independent-scope-'+str(i),'passed':passed,'response':response});assert passed
 c.send('textDocument/references',params,200);c.change(project,w.PROJECT.replace('name: team','name: renamed'),2);response=c.wait(lambda x:x.get('id')==200);rows.append({'id':'changed-dependency-refused','passed':response.get('error',{}).get('code')==-32801,'response':response})
except Exception as e:rows.append({'id':'runner','passed':False,'error':repr(e)})
finally:c.close();(out/'report.json').write_text(json.dumps({'binarySHA256':w.hash_file(w.BINARY),'runnerSHA256':w.hash_file(pathlib.Path(__file__)),'rows':rows},indent=2)+'\n')
sys.exit(len(rows)!=12 or not all(x['passed'] for x in rows))
