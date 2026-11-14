#!/usr/bin/env python3
"""Compare real incremental sessions with fresh final-state sessions."""
import argparse
import hashlib
import json
import random
from pathlib import Path
import subprocess
import sys

import audit_repair_e2e as wire


def normalized(value, roots):
    if isinstance(value,dict):return {key:normalized(item,roots) for key,item in value.items()}
    if isinstance(value,list):return [normalized(item,roots) for item in value]
    if isinstance(value,str):
        for index,root in enumerate(roots):value=value.replace(root.as_uri(),'root'+str(index))
    return value


def observe(client, app, revision, roots):
    diagnostics=client.diag(app,revision)
    position={'line':5,'character':12}
    result={'version':revision,'diagnostics':diagnostics}
    for method,extra in [('definition',{}),('references',{'context':{'includeDeclaration':True}}),('completion',{})]:
        response=client.request('textDocument/'+method,{'textDocument':{'uri':app.as_uri()},'position':position,**extra})
        if 'error' in response:raise ValueError('Incomplete query: '+repr(response))
        result[method]=response['result']
    actions=client.request('textDocument/codeAction',{'textDocument':{'uri':app.as_uri()},'range':{'start':{'line':5,'character':2},'end':{'line':5,'character':6}},'context':{'diagnostics':diagnostics}})
    if 'error' in actions:raise ValueError('Code-action state is incomplete')
    result['actions']=[]
    for action in actions.get('result',[]):
        resolved=client.request('codeAction/resolve',action)
        if 'error' in resolved:raise ValueError('Current action could not resolve')
        result['actions'].append({'title':action['title'],'kind':action.get('kind'),'edit':resolved['result'].get('edit')})
    return normalized(result,roots)


def session(binary,folder,case,incremental,seed):
    folder.mkdir(parents=True);roots=[folder/'root-a',folder/'root-b']
    for root in roots:
        root.mkdir();wire.write(root,'.argocd-ls.yaml',wire.CONFIG);wire.write(root,'project.yaml',wire.PROJECT);wire.write(root,'app.yaml',wire.APP)
    app=roots[0]/'app.yaml';project=roots[0]/'project.yaml';final=wire.APP;revision=10
    if case=='overlay':final=wire.APP.replace('project: team','project: missing')
    if case=='edit-candidates':final=wire.APP.replace('project: team','proj: team')
    if case=='delete':project.unlink()
    client=wire.LSP(folder,roots)
    try:
        if incremental:
            client.open(app,wire.APP,1);client.diag(app,1)
            if case in ('edit-undo','overlay'):
                client.change(app,wire.APP.replace('project: team','project: missing'),2);client.diag(app,2)
            if case=='edit-candidates':
                client.change(app,final,2);diagnostics=client.diag(app,2)
                previous=client.request('textDocument/codeAction',{'textDocument':{'uri':app.as_uri()},'range':{'start':{'line':5,'character':2},'end':{'line':5,'character':6}},'context':{'diagnostics':diagnostics}})['result']
                if not previous:raise ValueError('Independent edit candidate control is empty')
            if case=='cancel-recovery':
                large='---\n'.join(wire.APP.replace('name: app','name: app'+str(i)).replace('project: team','proj: team') for i in range(60))
                client.change(app,large,2);client.diag(app,2)
                client.send('textDocument/codeAction',{'textDocument':{'uri':app.as_uri()},'range':{'start':{'line':5,'character':2},'end':{'line':5,'character':6}},'context':{'diagnostics':[]}},200)
                client.send('$/cancelRequest',{'id':200})
                cancelled=client.wait(lambda message:message.get('id')==200,.2)
                if cancelled.get('error',{}).get('code')!=-32800:raise ValueError('No in-flight cancellation observed')
            if case=='delete':
                # Restore then remove inside the live session, not only before startup.
                project.write_text(wire.PROJECT)
                client.send('workspace/didChangeWatchedFiles',{'changes':[{'uri':project.as_uri(),'type':1}]})
                client.change(app,wire.APP,2);client.diag(app,2)
                project.unlink();client.send('workspace/didChangeWatchedFiles',{'changes':[{'uri':project.as_uri(),'type':3}]})
            if case=='target-roundtrip':
                config=roots[0]/'.argocd-ls.yaml'
                for index,target in enumerate(['3.5.6','3.0.0'],2):
                    config.write_text(wire.CONFIG.replace('3.0.0',target))
                    client.send('workspace/didChangeWatchedFiles',{'changes':[{'uri':config.as_uri(),'type':2}]})
                    version_text=wire.APP.replace('  source: {repoURL: https://example.org/repo}', '  sources: [{repoURL: a, ref: values, helm: {valueFiles: ["$values/v.yaml"]}}]')
                    client.change(app,version_text,index);version_diagnostics=client.diag(app,index)
                    if [d['code'] for d in version_diagnostics] != ([] if target=='3.5.6' else ['CD_VERSION_FEATURE']):raise ValueError('Target A/B/A independent semantic oracle failed')
            if case=='repeated-events':
                for _ in range(3):client.send('workspace/didChangeWatchedFiles',{'changes':[{'uri':app.as_uri(),'type':2}]})
            if case=='seeded-undo':
                randomizer=random.Random(seed)
                for index in range(2,6):
                    client.change(app,wire.APP+'# seed '+str(randomizer.randrange(100000))+'\n',index);client.diag(app,index)
            client.change(app,final,revision)
        else:client.open(app,final,revision)
        observed=observe(client,app,revision,roots)
        other=roots[1]/'app.yaml';client.open(other,wire.APP,revision);observed['otherRoot']=observe(client,other,revision,roots)
        if 'root1/project.yaml' not in json.dumps(observed['otherRoot']['definition']):raise ValueError('Second root navigation escaped ownership')
        if case=='edit-candidates':
            if not observed['actions']:raise ValueError('Final edit candidates were not restored')
            if incremental and client.request('codeAction/resolve',previous[0]).get('error',{}).get('code')!=-32801:raise ValueError('Old edit token remained valid')
        # Run the CLI against the effective overlay, independently of LSP response shape.
        app.write_text(final)
        command=[str(binary),'check','--root',str(roots[0]),'--format','json']
        result=subprocess.run(command,capture_output=True,timeout=20)
        (folder/'cli.stdout').write_bytes(result.stdout);(folder/'cli.stderr').write_bytes(result.stderr)
        report=json.loads(result.stdout)
        if report.get('analysisComplete') is not True or result.returncode not in (0,1):raise ValueError('Final state is incomplete')
        observed['analysisComplete']=True
        expected_codes=['CD_PROJECT_UNRESOLVED'] if case in ('overlay','delete') else ['SCHEMA_REQUIRED','SCHEMA_UNKNOWN_FIELD'] if case=='edit-candidates' else []
        if sorted(d['code'] for d in observed['diagnostics'])!=sorted(expected_codes):raise ValueError('Independent diagnostics oracle failed')
        if case not in ('overlay','delete','edit-candidates') and 'root0/project.yaml' not in json.dumps(observed['definition']):raise ValueError('Navigation escaped root ownership')
        return observed
    finally:client.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('binary',type=Path);parser.add_argument('output',type=Path);parser.add_argument('--seed',type=int,default=20261114);args=parser.parse_args()
    wire.BINARY=args.binary.resolve();args.output=args.output.resolve();args.output.mkdir(parents=True,exist_ok=False);rows=[]
    for case in ('edit-undo','overlay','delete','target-roundtrip','repeated-events','root-isolation','seeded-undo','cancel-recovery','edit-candidates'):
        row={'id':case,'passed':False}
        try:
            a=session(wire.BINARY,args.output/(case+'-incremental'),case,True,args.seed)
            b=session(wire.BINARY,args.output/(case+'-fresh'),case,False,args.seed)
            row.update(incremental=a,fresh=b,passed=a==b)
        except Exception as error:row['error']=repr(error)
        rows.append(row)
        (args.output/'report.json').write_text(json.dumps({'rows':rows,'passed':False},indent=2)+'\n')
    report={'schemaVersion':1,'binarySHA256':hashlib.sha256(wire.BINARY.read_bytes()).hexdigest(),'runnerSHA256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'seed':args.seed,'command':sys.argv,'cacheEvidence':'Semantic repeatability only; work-count cache proof remains in incremental_performance_e2e.py.',
            'cancellationEvidence':'cancel-recovery requires -32800 within 200ms before restoring final state and comparing a fresh session.',
            'rows':rows,'passed':all(row['passed'] for row in rows)}
    (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'passed':report['passed'],'failures':[(r['id'],r.get('error')) for r in rows if not r['passed']]}));return 0 if report['passed'] else 1
if __name__=='__main__':sys.exit(main())
