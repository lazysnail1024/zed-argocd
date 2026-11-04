#!/usr/bin/env python3
"""Complete immutable raw source acquisition from the reviewed release inventory."""
import concurrent.futures,hashlib,json,pathlib,urllib.request,time
root=pathlib.Path(__file__).resolve().parents[1];lockpath=root/'compat/catalog.lock.json';lock=json.loads(lockpath.read_text())
def get(url):
 for attempt in range(3):
  try:return urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'zed-argocd-development'}),timeout=90).read()
  except Exception:
   if attempt==2:raise
   time.sleep(1)
url='https://api.github.com/repos/argoproj/argo-cd/git/matching-refs/tags/v3.';raw=get(url);(root/'artifacts/cd-2/upstream/tag-refs.json').write_bytes(raw);refs={r['ref'].split('/')[-1]:r['object'] for r in json.loads(raw)}
def collect(e):
 if e['assetStatus']=='raw-collected':return e
 obj=refs['v'+e['version']]
 if obj['type']=='tag':obj=json.loads(get(obj['url']))['object']
 assert obj['type']=='commit';commit=obj['sha'];sources=[]
 for path in ['manifests/crds/application-crd.yaml','manifests/crds/appproject-crd.yaml','manifests/crds/applicationset-crd.yaml','LICENSE']:
  url=f'https://raw.githubusercontent.com/argoproj/argo-cd/{commit}/{path}';b=get(url);digest=hashlib.sha256(b).hexdigest();(root/'schemas'/digest).write_bytes(b)
  sources.append({'path':path,'url':url,'sha256':digest,'localPath':'schemas/'+digest})
 return {**e,'upstreamCommit':commit,'sources':sources,'assetStatus':'raw-collected'}
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
 for e in pool.map(collect,lock['versions']):
  for i,old in enumerate(lock['versions']):
   if e['version']==old['version']:lock['versions'][i]=e;break
  lockpath.write_text(json.dumps(lock,indent=2)+'\n');print(e['version'],e['assetStatus'],flush=True)
