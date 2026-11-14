#!/usr/bin/env python3
"""Replay real hosted reports through production acceptance boundaries."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--orthogonal', type=Path)
    parser.add_argument('--resources', type=Path)
    parser.add_argument('--verifier-path', type=Path, default=ROOT/'tests')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    cases = []
    if args.orthogonal:
        query = json.loads((args.orthogonal / 'schema-repair-queries/report.json').read_text())
        cases.append(('orthogonal-original', 'orthogonal', query, True))
        for name in ('missing-case', 'duplicate-case', 'extra-case', 'false-assertion', 'missing-assertion', 'extra-assertion', 'truthy-assertion', 'wrong-case-type'):
            value = copy.deepcopy(query)
            if name == 'missing-case': value['rows'] = value['rows'][:1]
            if name == 'duplicate-case': value['rows'][1] = copy.deepcopy(value['rows'][0])
            if name == 'extra-case': value['rows'].append(copy.deepcopy(value['rows'][0]))
            if name == 'false-assertion': value['rows'][0]['checks']['terminalResponse'] = False
            if name == 'missing-assertion': value['rows'][0]['checks'].pop('terminalResponse')
            if name == 'extra-assertion': value['rows'][0]['checks']['invented'] = True
            if name == 'truthy-assertion': value['rows'][0]['checks']['terminalResponse'] = 1
            if name == 'wrong-case-type': value['rows'][0]['snippets'] = 0
            cases.append((name, 'orthogonal', value, False))
    for kind in (('completion', 'full-lsp') if args.resources else ()):
        original = json.loads((args.resources / kind / 'report.json').read_text())
        cases.append((kind+'-original', kind, original, True))
        for name, bad in [('negative', -1), ('nan', float('nan')), ('infinity', float('inf')), ('boolean', True), ('string', '0.01')]:
            value = copy.deepcopy(original)
            value['rows'][0]['seconds'][0] = bad
            cases.append((kind+'-'+name, kind, value, False))
        if kind == 'full-lsp':
            for name,bad in [('zero',0),('negative',-1),('boolean',True),('nan',float('nan')),('infinity',float('inf'))]:
                value=copy.deepcopy(original);value['rows'][4]['warmRSSKiB']=bad;value['rows'][4]['samplesRSSKiB']=[bad]*5
                cases.append(('rss-'+name,kind,value,False))
    if args.resources:
        for name, text in [('nan','{"value":NaN}'), ('infinity','{"value":Infinity}'), ('overflow','{"value":1e999}'), ('duplicate-key','{"value":1,"value":2}')]:
            cases.append(('json-'+name,'strict-json',text,False))
        cases.append(('json-original','strict-json','{"value":0.01}',True))
        cases.append(('writer-nan','strict-writer',{'value':float('nan')},False))
        cases.append(('writer-original','strict-writer',{'value':0.01},True))
    if not cases:
        parser.error('At least one original evidence directory is required')
    rows=[]
    for name,kind,value,wanted in cases:
        folder=args.output/name;folder.mkdir();input_path=folder/'report.json'
        input_path.write_text(json.dumps(value,indent=2)+'\n')
        if kind=='strict-json':
            code="import sys;sys.path.insert(0,'tests');from resource_ci import load_json;load_json(__import__('json').load(open(sys.argv[1])))"
        elif kind=='strict-writer':
            code="import sys,json;from pathlib import Path;sys.path.insert(0,'tests');from resource_ci import save;save(Path(sys.argv[1]).with_name('written.json'),json.load(open(sys.argv[1])))"
        elif kind=='orthogonal':
            code="import json,sys;sys.path.insert(0,'tests');from orthogonal_ci import evidence_passed;v=json.load(open(sys.argv[1]));sys.exit(0 if evidence_passed(v,'schema-repair-queries') else 1)"
            # Preserve replay compatibility with the original one-argument boundary.
            code=code.replace("evidence_passed(v,'schema-repair-queries')", "(evidence_passed(v,'schema-repair-queries') if len(__import__('inspect').signature(evidence_passed).parameters)>1 else evidence_passed(v))")
        else:
            script='bounded_completion_e2e.py' if kind=='completion' else 'bounded_lsp_e2e.py'
            code="import json,sys;sys.path.insert(0,'tests');from resource_ci import validate_report;v=json.load(open(sys.argv[1]));validate_report(v,"+repr(kind)+","+repr(script)+",v['binarySHA256'],v.get('runnerSHA256',v.get('testSHA256')))"
        code=code.replace("sys.path.insert(0,'tests');", "sys.path.insert(0,'tests');sys.path.insert(0,"+repr(str(args.verifier_path.resolve()))+");")
        command=[sys.executable,'-c',code,str(input_path.resolve())]
        result=subprocess.run(command,cwd=ROOT,capture_output=True)
        (folder/'stdout').write_bytes(result.stdout);(folder/'stderr').write_bytes(result.stderr)
        rows.append({'id':name,'command':command,'inputSHA256':hashlib.sha256(input_path.read_bytes()).hexdigest(),'exit':result.returncode,'expectedAccepted':wanted,'rejection':result.stderr.decode('utf-8',errors='replace')[-1000:] if result.returncode else None,'passed':(result.returncode==0)==wanted})
    report={'python':sys.version,'verifierSHA256':{name:hashlib.sha256((args.verifier_path/name).read_bytes()).hexdigest() for name in ('orthogonal_ci.py','orthogonal_contracts.py','child_evidence.py','resource_ci.py') if (args.verifier_path/name).is_file()},'testSHA256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'rows':rows,'passed':all(r['passed'] for r in rows)}
    (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report));return 0 if report['passed'] else 1
if __name__=='__main__':sys.exit(main())
