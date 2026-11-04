#!/usr/bin/env python3
import hashlib,json,pathlib,subprocess,sys
root=pathlib.Path(__file__).resolve().parents[1]; binary=pathlib.Path(sys.argv[1]).resolve();out=pathlib.Path(sys.argv[2]).resolve();out.mkdir(parents=True,exist_ok=True)
good={'product':'argo-cd','serverVersion':'0.1.0-dev','adapterContractVersion':1,'analysisReady':True,'configSchemaVersions':[1],'reportSchemaVersions':[1],'toolkitVersion':'development-snapshot','catalogHash':'a'*64,'sourceCommit':None,'dirty':True,'buildMode':'development'}
cases=[('ready',good,0),('wrong-product',{**good,'product':'argo-workflows'},2),('not-ready',{**good,'analysisReady':False},2),('wrong-contract',{**good,'adapterContractVersion':2},2),('missing-product',{k:v for k,v in good.items() if k!='product'},2),('invalid-json','{',2),('empty-catalog',{**good,'catalogHash':None},2)]
rows=[]
for name,data,expected in cases:
 raw=data if isinstance(data,str) else json.dumps(data)
 (out/(name+'.input')).write_text(raw)
 p=subprocess.run([str(binary)],input=raw.encode(),capture_output=True,timeout=10)
 (out/(name+'.stdout')).write_bytes(p.stdout);(out/(name+'.stderr')).write_bytes(p.stderr)
 rows.append({'id':name,'expectedExit':expected,'exit':p.returncode,'passed':p.returncode==expected})
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
(out/'manifest.json').write_text(json.dumps({'command':sys.argv,'binarySHA256':sha(binary),'testSHA256':sha(pathlib.Path(__file__)),'inputs':{p.name:sha(p) for p in out.glob('*.input')}},indent=2)+'\n')
(out/'results.json').write_text(json.dumps(rows,indent=2)+'\n');print(json.dumps(rows));sys.exit(0 if all(r['passed'] for r in rows) else 1)
