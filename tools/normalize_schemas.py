#!/usr/bin/env python3
"""Canonical OpenAPI source extraction, not a complete JSON Schema conversion."""
import hashlib,json,pathlib,yaml
root=pathlib.Path(__file__).resolve().parents[1];p=root/'compat/catalog.lock.json';catalog=json.loads(p.read_text());out=root/'internal/assets/schemas';out.mkdir(parents=True,exist_ok=True)
for target in catalog['versions']:
 target['schemas']=[]
 for src in target['sources']:
  if not src['path'].endswith('.yaml'):continue
  raw=(root/src['localPath']).read_bytes();assert hashlib.sha256(raw).hexdigest()==src['sha256'];crd=yaml.safe_load(raw)
  assert crd['spec']['group']=='argoproj.io'
  for version in crd['spec']['versions']:
   if not version['served']:continue
   schema=version['schema']['openAPIV3Schema'];data=(json.dumps(schema,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode();digest=hashlib.sha256(data).hexdigest();(out/(digest+'.json')).write_bytes(data)
   target['schemas'].append({'apiVersion':'argoproj.io/'+version['name'],'kind':crd['spec']['names']['kind'],'sha256':digest,'path':'internal/assets/schemas/'+digest+'.json','format':'canonical-openapi-v3-source','validated':False})
p.write_text(json.dumps(catalog,indent=2)+'\n');(root/'internal/assets/catalog.json').write_text(json.dumps(catalog,separators=(',',':'))+'\n')
print(json.dumps({'targets':len(catalog['versions']),'uniqueCanonicalSchemas':len(list(out.glob('*.json'))),'behaviorVerified':False}))
