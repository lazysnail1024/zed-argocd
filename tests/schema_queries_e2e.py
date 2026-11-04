#!/usr/bin/env python3
"""Real LSP query oracle; marked positions and intended full bytes are independent."""
import argparse, hashlib, json, pathlib, sys, time
import audit_repair_e2e as wire

def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def position(text, offset):
    before=text[:offset]
    return {'line':before.count('\n'),'character':len(before.rsplit('\n',1)[-1].encode('utf-16-le'))//2}
def offset(text, p):
    lines=text.splitlines(keepends=True); line=lines[p['line']]
    return sum(map(len,lines[:p['line']]))+len(line.encode('utf-16-le')[:p['character']*2].decode('utf-16-le'))
class Client(wire.LSP):
    def send(self, method, params=None, ident=None):
        if method=='initialize': params['capabilities']['textDocument']={'completion':{'completionItem':{'snippetSupport':self.snippets}}}
        super().send(method,params,ident)

def main():
    ap=argparse.ArgumentParser();ap.add_argument('binary',type=pathlib.Path);ap.add_argument('output',type=pathlib.Path);ap.add_argument('--cases',type=pathlib.Path,default=pathlib.Path(__file__).with_name('schema-query-cases.json'));args=ap.parse_args()
    wire.BINARY=args.binary.resolve();out=args.output.resolve();out.mkdir(parents=True,exist_ok=True);cases=json.loads(args.cases.read_text());rows=[]
    try:
      for version in ['3.0.0','3.5.6']:
       for snippets in [False,True]:
        folder=out/(version+('-snippets' if snippets else '-plain'));work=folder/'workspace';work.mkdir(parents=True,exist_ok=True)
        (work/'.argocd-ls.yaml').write_text('schemaVersion: 1\ntargetVersion: "'+version+'"\n')
        path=work/'input.yaml';Client.snippets=snippets;client=None
        try:
          client=Client(folder,[work]);revision=0
          for case in cases:
            row={'id':case['id'],'target':version,'snippets':snippets,'passed':False};rows.append(row)
            try:
              marked=case['text'];marker=case.get('marker','|');point=marked.index(marker);text=marked.replace(marker,'',1);revision+=1
              (folder/(case['id']+'.yaml')).write_bytes(text.encode())
              if revision==1:client.open(path,text,revision)
              else:client.change(path,text,revision)
              diagnostics=client.diag(path,revision)
              response=client.request('textDocument/'+case.get('method','completion'),{'textDocument':{'uri':path.as_uri()},'position':position(text,point)})
              result=response.get('result');checks={'terminalResponse':'error' not in response};row.update(response=response,diagnostics=diagnostics)
              if case.get('method')=='hover':
                value=(result or {}).get('contents',{}).get('value','');checks['hover']=all(x.replace('$TARGET',version).replace('$COMMIT',{'3.0.0':'e98f483bfd5781df2592fef1aeed1148f150d9c9','3.5.6':'c2b1e3017e2cec86b122172e861dcd026453ae44'}[version]) in value for x in case.get('contains',[]));checks['noInventedType']='<nil>' not in value
                if case.get('empty'):checks['empty']=result is None
              else:
                items=result or [];labels=[x['label'] for x in items];checks['required']=set(case.get('labels',[]))<=set(labels);checks['forbidden']=not set(case.get('forbidden',[]))&set(labels)
                if case.get('empty'):checks['empty']=not items
                if 'exactLabels' in case:checks['exactLabels']=sorted(labels)==sorted(case['exactLabels'])
                checks['plainText']=all(x.get('insertTextFormat',1)==1 and '${' not in x.get('textEdit',{}).get('newText','') for x in items)
                if 'candidateLabels' in case:checks['branchCandidates']=all('candidate' in x.get('detail','').lower() for x in items if x['label'] in case['candidateLabels'])
                if 'diagnosticCodes' in case:checks['validationKept']=set(case['diagnosticCodes'])<=set(d.get('code') for d in diagnostics)
                if 'apply' in case:
                  selected=next((x for x in items if x['label']==case['apply']),{});edit=selected.get('textEdit');checks['explicitEdit']=bool(edit)
                  if edit:
                    a=offset(text,edit['range']['start']);b=offset(text,edit['range']['end']);actual=text[:a]+edit['newText']+text[b:];row['applied']=actual;checks['exactBytes']=actual==case['expected']
                    if 'rangeText' in case:checks['exactRange']=text[a:b]==case['rangeText']
                    revision+=1;client.change(path,actual,revision);row['afterDiagnostics']=client.diag(path,revision)
                    checks['noDuplicateProjection']=all(d.get('code')!='YAML_PROJECTION_INCOMPLETE' for d in row['afterDiagnostics'])
              row['checks']=checks;row['passed']=all(checks.values())
            except Exception as e:row['error']=repr(e)
        except Exception as e:rows.append({'id':'client-start','passed':False,'error':repr(e)})
        finally:
          if client:
            client.close();rows.append({'id':'shutdown','target':version,'passed':client.proc.returncode==0,'exitCode':client.proc.returncode})
    finally:
      report={'scope':'real LSP query consumer; asset provenance is supplied by the invoking build manifest','binarySHA256':sha(wire.BINARY),'runnerSHA256':sha(pathlib.Path(__file__)),'wireSHA256':sha(pathlib.Path(wire.__file__)),'casesSHA256':sha(args.cases),'command':sys.argv,'rows':rows,'passed':bool(rows) and all(r['passed'] for r in rows)}
      (out/'report.json').write_text(json.dumps(report,indent=2)+'\n');(out/'artifact-hashes.json').write_text(json.dumps({str(p.relative_to(out)):sha(p) for p in out.rglob('*') if p.is_file() and p.name!='artifact-hashes.json'},indent=2)+'\n')
    print(json.dumps({'rows':len(rows),'failed':[r['id'] for r in rows if not r['passed']]}));return 0 if report['passed'] else 1
if __name__=='__main__':sys.exit(main())
