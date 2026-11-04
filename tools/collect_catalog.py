#!/usr/bin/env python3
"""Explicit build-time upstream fetch; never used by the server runtime."""
import concurrent.futures,datetime,hashlib,json,pathlib,re,urllib.request
root=pathlib.Path(__file__).resolve().parents[1];raw=root/'artifacts/cd-2/upstream';raw.mkdir(parents=True,exist_ok=True)
cutoff='2026-11-02T23:59:59Z'
def get(url):
 req=urllib.request.Request(url,headers={'User-Agent':'zed-argocd-local-development'})
 return urllib.request.urlopen(req,timeout=60).read()
def sha(b):return hashlib.sha256(b).hexdigest()
releases=[];pages=[]
for page in range(1,10):
 url=f'https://api.github.com/repos/argoproj/argo-cd/releases?per_page=100&page={page}'
 b=get(url);(raw/f'releases-{page}.json').write_bytes(b);batch=json.loads(b);pages.append({'url':url,'sha256':sha(b),'file':f'artifacts/cd-2/upstream/releases-{page}.json'})
 releases.extend(x for x in batch if re.fullmatch(r'v3\.\d+\.\d+',x['tag_name']) and not x['draft'] and not x['prerelease'] and x['published_at']<=cutoff)
 if len(batch)<100 or all(not x['tag_name'].startswith('v3.') for x in batch):break
versions=sorted(releases,key=lambda r:tuple(map(int,r['tag_name'][1:].split('.'))))
entries=[{'version':x['tag_name'][1:],'releaseURL':x['html_url'],'publishedAt':x['published_at'],'assetStatus':'not-collected','semanticsVerified':False,'ruleset':None} for x in versions]
# Initial source acquisition is deliberately bounded; remaining inventory is explicit.
selected={'3.0.0',entries[-1]['version']}
for e in entries:
 if e['version'] not in selected:continue
 tag='v'+e['version'];ref=json.loads(get(f'https://api.github.com/repos/argoproj/argo-cd/git/ref/tags/{tag}'))['object']
 if ref['type']=='tag':ref=json.loads(get(ref['url']))['object']
 assert ref['type']=='commit';commit=ref['sha'];e['upstreamCommit']=commit;e['sources']=[]
 paths=['manifests/crds/application-crd.yaml','manifests/crds/appproject-crd.yaml','manifests/crds/applicationset-crd.yaml','LICENSE']
 for path in paths:
  url=f'https://raw.githubusercontent.com/argoproj/argo-cd/{commit}/{path}';b=get(url);digest=sha(b);dest=root/'schemas'/digest;dest.write_bytes(b)
  e['sources'].append({'path':path,'url':url,'sha256':digest,'localPath':'schemas/'+digest})
 e['assetStatus']='raw-collected'
lock={'formatVersion':1,'product':'argo-cd','cutoff':cutoff,'supportPolicy':'all stable major 3 patches before cutoff; acquisition and behavior acceptance tracked independently','defaultVersion':None,'ready':False,'inventorySources':pages,'versions':entries}
(root/'compat/catalog.lock.json').write_text(json.dumps(lock,indent=2)+'\n')
print(json.dumps({'inventory':len(entries),'collected':len(selected),'defaultVersion':None}))
