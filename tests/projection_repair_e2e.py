#!/usr/bin/env python3
"""Independent authored CLI/LSP behavior consumers for the sealed projection candidate."""
import hashlib,json,pathlib,subprocess,sys,time
HERE=pathlib.Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
import audit_repair_e2e as wire
binary=pathlib.Path(sys.argv[1]).resolve();out=pathlib.Path(sys.argv[2]).resolve();out.mkdir(parents=True,exist_ok=False);wire.BINARY=binary
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
config='schemaVersion: 1\ntargetVersion: "3.5.6"\ncontrolPlaneNamespace: argocd\nreferencePolicy: closed\n'
head='apiVersion: argoproj.io/v1alpha1\nkind: Application\n'
tail='  source: {repoURL: https://example.org/repo}\n  destination: {server: here}\n'
plain=head+'metadata: {name: audit}\nspec:\n  project: team\n'+tail
rows=[];cases=[]
def position(text,n):return {'line':text[:n].count('\n'),'character':len(text[:n].rsplit('\n',1)[-1].encode('utf-16-le'))//2}
def span(text,token,index=0):
 start=-1
 for _ in range(index+1):start=text.index(token,start+1)
 return {'start':position(text,start),'end':position(text,start+len(token))}
def diag(text,code,token,severity=1,derived=False,index=0):return {'code':code,'source':'argocd-ls','severity':severity,'range':span(text,token,index),'derived':derived}
def case(name,text,ds=None,resources=1,complete=True):cases.append({'id':name,'text':text,'diagnostics':ds or [],'resources':resources+1,'files':3 if complete else 2,'complete':complete})
def refused(name,text):case(name,text,[diag(text,'YAML_PROJECTION_INCOMPLETE',text)],0,False)
case('plain-control',plain)
case('indented-plain-control',''.join('  '+line for line in plain.splitlines(keepends=True)))
case('indented-crlf-control',''.join('  '+line for line in plain.splitlines(keepends=True)).replace('\n','\r\n'))
case('marked-indented-crlf-control','---\r\n'+''.join('  '+line for line in plain.splitlines(keepends=True)).replace('\n','\r\n')+'...\r\n')
for name,value,code in [('hex-integer','0x35','SCHEMA_TYPE'),('old-octal','077','SCHEMA_TYPE'),('yaml11-bool','yes','SCHEMA_TYPE'),('quoted-bool','"yes"','CD_PROJECT_UNRESOLVED'),('explicit-string','!!str 42','CD_PROJECT_UNRESOLVED')]:
 text=head+'metadata: {name: audit, annotations: {value: &v '+value+'}}\nspec:\n  project: *v\n'+tail
 case(name,text,[diag(text,code,'*v',derived=True)])
text=head+'metadata:\n  name: audit\n  annotations:\n    first: &first {project: team}\n    second: &second {project: absent}\n    maps: &maps [*first, *second]\nspec:\n  <<: *maps\n'+tail
case('alias-of-merge-sequence',text)
rev=text.replace('maps: &maps [*first, *second]','maps: &maps [*second, *first]')
case('alias-of-merge-sequence-reversed',rev,[diag(rev,'CD_PROJECT_UNRESOLVED','*maps',derived=True)])
for order in ['before','after']:
 replacement='  project: team\n  <<: *maps\n' if order=='before' else '  <<: *maps\n  project: team\n'
 case('explicit-wins-'+order,rev.replace('  <<: *maps\n',replacement))
text=head+'metadata: {name: audit, annotations: {value: &v team}}\nspec:\n  project: *v\n'+tail
other=text.replace('&v team','&v absent')
stream=text+'---\n'+other
case('document-local-anchor-redefinition',stream,[diag(stream,'CD_PROJECT_UNRESOLVED','*v',derived=True,index=1)],2)
for identity in ['plain','alias','merge']:
 if identity=='plain':text=head+'metadata: {name: audit}\n'
 elif identity=='alias':text='metadata: {name: audit, annotations: {api: &api argoproj.io/v1alpha1, kind: &kind Application}}\napiVersion: *api\nkind: *kind\n'
 else:text='metadata: {name: audit, annotations: &identity {apiVersion: argoproj.io/v1alpha1, kind: Application}}\n<<: *identity\n'
 refused(identity+'-identity-unresolved-alias',text+'spec: {project: *missing}\n')
 refused(identity+'-identity-custom-tag',text+'spec: {project: !custom team}\n')
 foreign=text.replace('argoproj.io/v1alpha1','v1').replace('Application','ConfigMap')+'spec: {project: *missing}\n'
 case(identity+'-foreign-unresolved-alias',foreign,resources=0)
# A metadata anchor with the spelling << remains a literal key if explicitly tagged string.
text=head+'metadata: {name: audit, annotations: {k: &k !!str "<<"}}\nspec:\n  *k : {project: team}\n'+tail
case('alias-string-merge-marker-is-ordinary-key',text,[diag(text,'SCHEMA_REQUIRED',text[text.index('*k'):]),diag(text,'SCHEMA_UNKNOWN_FIELD','*k',2,True)])
for identity in ['plain','alias']:
 text='metadata: {name: ghost, annotations: {api: &api argoproj.io/v1alpha1, kind: &kind AppProject}}\napiVersion: *api\nkind: *kind\nspec: {sourceRepos: *missing}\n'
 if identity=='plain':text=text.replace('apiVersion: *api','apiVersion: argoproj.io/v1alpha1').replace('kind: *kind','kind: AppProject')
 apptext=plain.replace('project: team','project: ghost');stream=text+'---\n'+apptext
 case(identity+'-unparsed-project-affects-scope',stream,[diag(stream,'YAML_PROJECTION_INCOMPLETE',text),diag(stream,'CD_PROJECT_UNRESOLVED','ghost',3,index=1)],resources=1,complete=False)
# Ownership recovery must not change anchor ordering or invent identity.
identity='metadata: {name: audit, annotations: {api: &api argoproj.io/v1alpha1, kind: &kind Application}}\napiVersion: *api\nkind: *kind\n'
indented=''.join('  '+line for line in (identity+'spec: {project: *missing}\n').splitlines(keepends=True))
case('indented-aliased-identity-failure',indented,[diag(indented,'YAML_PROJECTION_INCOMPLETE',indented[2:])],0,False)
refused('undefined-before-valid-redefinition','unused: *api\n'+identity+'spec: {project: *missing}\n')
refused('latest-preceding-anchor',identity.replace('api: &api argoproj.io/v1alpha1','old: &api v1, api: &api argoproj.io/v1alpha1')+'spec: {project: *missing}\n')
unknown='apiVersion: *api\nkind: Application\nmetadata: {name: audit, annotations: {later: &api argoproj.io/v1alpha1}}\nspec: {project: *missing}\n'
case('forward-identity-stays-unknown',unknown,resources=0,complete=False)
unknown='metadata:\n  note: |\n    &api argoproj.io/v1alpha1\napiVersion: *api\nkind: Application\nspec: {project: *missing}\n'
case('block-scalar-is-not-anchor',unknown,resources=0,complete=False)
foreign='metadata: {name: audit, annotations: {first: &api v1}}\napiVersion: *api\nkind: ConfigMap\nlater: &api argoproj.io/v1alpha1\nspec: {project: *missing}\n'
case('later-anchor-cannot-change-foreign-identity',foreign,resources=0)
body=identity+'spec: {project: *missing}\n'
for name,prefix,suffix in [('document-markers','---\n','...\n'),('yaml-directive','%YAML 1.1\n---\n','...\n')]:
 text=prefix+body+suffix;case(name,text,[diag(text,'YAML_PROJECTION_INCOMPLETE',body.rstrip('\n'))],0,False)
unknown='metadata: {name: audit, annotations: &known {apiVersion: argoproj.io/v1alpha1, kind: Application}}\n<<: [*missing, *known]\nspec: {}\n'
case('unknown-earlier-merge-stays-unknown',unknown,resources=0,complete=False)
refused('explicit-identity-wins-unknown-merge',head+'metadata: {name: audit}\n<<: *missing\nspec: {}\n')
stream=identity+'spec:\n  project: team\n'+tail+'---\napiVersion: *api\nkind: Application\nspec: {}\n'
case('anchors-do-not-cross-documents',stream,resources=1,complete=False)
# An early foreign hint must be unique, not whichever duplicate key comes first.
for field,owned,foreign in [('apiVersion','argoproj.io/v1alpha1','v1'),('kind','Application','ConfigMap')]:
 other='kind: Application\n' if field=='apiVersion' else 'apiVersion: argoproj.io/v1alpha1\n'
 for order,values in [('foreign-first',[foreign,owned]),('owned-first',[owned,foreign])]:
  text=''.join(field+': '+v+'\n' for v in values)+other+'metadata: {name: audit}\nspec: {}\n'
  case(field+'-duplicate-'+order,text,resources=0,complete=False)
 for order,values in [('foreign-first',[foreign,owned]),('owned-first',[owned,foreign])]:
  text='metadata: {name: audit, annotations: {key: &key '+field+'}}\n'+field+': '+values[0]+'\n*key : '+values[1]+'\n'+other+'spec: {}\n'
  case(field+'-aliased-key-duplicate-'+order,text,resources=0,complete=False)
 text=field+': '+foreign+'\n'+other+'metadata: {name: audit}\nspec: {project: *missing}\n'
 case(field+'-unique-foreign',text,resources=0)
# Placeholder values are unknown facts, including when an alias is a key.
for classification,api,kind in [('owned','argoproj.io/v1alpha1','Application'),('foreign','v1','ConfigMap')]:
 text='apiVersion: '+api+'\nkind: '+kind+'\nmetadata: {name: audit}\n*missing : ignored\nspec: {}\n'
 case(classification+'-undefined-key-identity-unknown',text,resources=0,complete=False)
 text='apiVersion: '+api+'\nkind: '+kind+'\nmetadata: {name: audit, annotations: {key: &key other}}\n*key : ignored\nspec: {project: *missing}\n'
 if classification=='owned':refused('owned-known-other-key-control',text)
 else:case('foreign-known-other-key-control',text,resources=0)
# CST scalar text omits tag wrappers; it cannot alone prove foreign identity.
for field,foreign,other in [('apiVersion','v1','kind: Application\n'),('kind','ConfigMap','apiVersion: argoproj.io/v1alpha1\n')]:
 for tag,complete in [('!custom',False),('!!str',True)]:
  text=field+': '+tag+' '+foreign+'\n'+other+'metadata: {name: audit}\nspec: {}\n'
  case(field+'-tag-'+tag.replace('!',''),text,resources=0,complete=complete)
for tag,complete in [('!custom',False),('!!map',True)]:
 text=tag+' {apiVersion: v1, kind: ConfigMap, metadata: {name: audit}}\n'
 case('root-tag-'+tag.replace('!',''),text,resources=0,complete=complete)
# Standard binary tags decode to strings in the pinned strict YAML consumer.
encoded='apiVersion: !!binary YXJnb3Byb2ouaW8vdjFhbHBoYTE=\nkind: !!binary QXBwbGljYXRpb24=\n'
case('binary-identity-valid-control',plain.replace(head,encoded))
refused('binary-identity-unresolved-remainder',encoded+'metadata: {name: audit}\nspec: {project: *missing}\n')
for order in ['foreign-first','owned-first']:
 members=['apiVersion: v1\n','!!binary YXBpVmVyc2lvbg== : argoproj.io/v1alpha1\n']
 if order=='owned-first':members.reverse()
 text=''.join(members)+'kind: Application\nmetadata: {name: audit}\nspec: {}\n'
 case('binary-key-duplicate-'+order,text,resources=0,complete=False)
text='apiVersion: v1\nkind: ConfigMap\n!custom unknown: ignored\nmetadata: {name: audit}\n'
case('custom-key-cannot-prove-foreign-uniqueness',text,resources=0,complete=False)
(out/'expected-cli.json').write_text(json.dumps(cases,indent=2)+'\n')

def record(name,checks,observed):rows.append({'id':name,'checks':checks,'observed':observed,'passed':all(checks.values())})
for c in cases:
 folder=out/c['id'];folder.mkdir();root=folder/'workspace';root.mkdir();app=root/'input.yaml';app.write_text(c['text']);(root/'.argocd-ls.yaml').write_text(config);(root/'project.yaml').write_text(wire.PROJECT);cmd=[str(binary),'check','--root',str(root),'--format','json']
 try:
  p=subprocess.run(cmd,capture_output=True,timeout=10);(folder/'stdout').write_bytes(p.stdout);(folder/'stderr').write_bytes(p.stderr);d=json.loads(p.stdout)
  actual=[{k:x[k] for k in ['code','source','severity','range']}|{'derived':'derived YAML value' in x['message']} for x in d['diagnostics']]
  sort=lambda a:sorted(a,key=lambda x:json.dumps(x,sort_keys=True))
  checks={'exit':p.returncode==(2 if not c['complete'] else 1 if any(x['severity']==1 for x in c['diagnostics']) else 0),'complete':d['analysisComplete']==c['complete'],'envelope':d['product']=='argo-cd' and d['reportSchemaVersion']==1 and d['inputSource']=='filesystem' and d['validationScope']=='local-editing-contract','configuration':d['configurationErrors']==[],'targets':len(d['targets'])==1 and d['targets'][0]['version']=='3.5.6' and d['targets'][0]['complete']==c['complete'],'diagnostics':sort(actual)==sort(c['diagnostics']),'allDiagnosticURIs':all(x['uri']==app.as_uri() for x in d['diagnostics']),'files':d['filesAnalyzed']==c['files'],'resources':d['resourcesAnalyzed']==c['resources']}
  record(c['id'],checks,{'command':cmd,'exit':p.returncode,'report':d})
 except Exception as e:
  (folder/'stdout').write_bytes((e.stdout or b'') if isinstance(e,subprocess.TimeoutExpired) else b'')
  (folder/'stderr').write_bytes((e.stderr or b'') if isinstance(e,subprocess.TimeoutExpired) else repr(e).encode())
  record(c['id'],{'completed':False},{'command':cmd,'status':'timeout' if isinstance(e,subprocess.TimeoutExpired) else 'failed','error':repr(e)})
# Graph expansion cannot turn an ambiguous ref into a unique reference binding.
lsp=out/'lsp';root=lsp/'workspace';root.mkdir(parents=True);(root/'.argocd-ls.yaml').write_text(config);project=root/'project.yaml';project.write_text(wire.PROJECT);app=root/'app.yaml'
base=head+'metadata: {name: audit}\nspec:\n  project: team\n  sources:\n  - {repoURL: https://example.org/a, ref: values}\n  - {repoURL: https://example.org/b, ref: values}\n  - {repoURL: https://example.org/chart, helm: {valueFiles: ["$values/file.yaml"]}}\n  destination: {server: here}\n'
graph=base.replace('metadata: {name: audit}','metadata: {name: audit, annotations: {source: &source {repoURL: https://example.org/b, ref: values}}}').replace('  - {repoURL: https://example.org/b, ref: values}','  - <<: *source')
spec={'plain-duplicate':{'text':base,'references':[],'diagnosticCodes':['CD_SOURCE_REF_DUPLICATE']},'derived-duplicate':{'text':graph,'references':[],'diagnosticCodes':['CD_SOURCE_REF_DUPLICATE']}}
spec['shared-duplicate']={'text':base.replace('https://example.org/b, ref: values','https://example.org/b, ref: &ref values').replace('repoURL: https://example.org/chart','repoURL: *ref'),'references':[],'diagnosticCodes':['CD_SOURCE_REF_DUPLICATE']}
(lsp/'expected.json').write_text(json.dumps(spec,indent=2)+'\n');client=None
try:
 client=wire.LSP(lsp,[root])
 for version,(name,c) in enumerate(spec.items(),1):
  text=c['text'];(lsp/(name+'.yaml')).write_text(text)
  if version==1:client.open(app,text,version)
  else:client.change(app,text,version)
  ds=client.diag(app,version);positionRef=position(text,text.index('  - {repoURL: https://example.org/a, ref: values}')+len('  - {repoURL: https://example.org/a, ref: va'))
  refs={str(include):client.request('textDocument/references',{'textDocument':{'uri':app.as_uri()},'position':positionRef,'context':{'includeDeclaration':include}}) for include in [False,True]}
  expect=[diag(text,'CD_SOURCE_REF_DUPLICATE','values',index=0),diag(text,'CD_SOURCE_REF_DUPLICATE','values',index=1)] if name in ['plain-duplicate','shared-duplicate'] else [diag(text,'CD_SOURCE_REF_DUPLICATE','values',index=1),diag(text,'CD_SOURCE_REF_DUPLICATE','*source',derived=True)]
  actual=[{k:d[k] for k in ['code','source','severity','range']}|{'derived':'derived YAML value' in d['message']} for d in ds]
  record(name,{'fullDiagnostic':actual==expect,'ambiguityPreserved':all(x.get('result')==[] for x in refs.values())},{'diagnostics':ds,'references':refs})
 # Positive control: one exact ref returns exact uses and optionally its declaration.
 unique=base.replace('https://example.org/b, ref: values','https://example.org/b, ref: other')
 client.change(app,unique,4);ds=client.diag(app,4)
 point=position(unique,unique.index('  - {repoURL: https://example.org/a, ref: values}')+len('  - {repoURL: https://example.org/a, ref: va'))
 expectedUse={'uri':app.as_uri(),'range':span(unique,'"$values/file.yaml"')};expectedDeclaration={'uri':app.as_uri(),'range':span(unique,'values')}
 replies={str(include):client.request('textDocument/references',{'textDocument':{'uri':app.as_uri()},'position':point,'context':{'includeDeclaration':include}}) for include in [False,True]}
 record('unique-exact-ref',{'noDiagnostics':ds==[],'withoutDeclaration':replies['False'].get('result')==[expectedUse],'withDeclaration':replies['True'].get('result')==[expectedUse,expectedDeclaration]},replies)
 # Fresh valid replacement recovers current diagnostics and exact definition.
 client.change(app,plain,5);ds=client.diag(app,5);definition=client.request('textDocument/definition',{'textDocument':{'uri':app.as_uri()},'position':position(plain,plain.index('project: team')+11)});expected={'uri':project.as_uri(),'range':span(wire.PROJECT,'team')}
 record('exact-recovery',{'noDiagnostics':ds==[],'exactDefinition':definition.get('result')==[expected]},{'diagnostics':ds,'definition':definition})
except Exception as e:record('lsp-runner',{'completed':False},repr(e))
finally:
 if client:client.close();record('shutdown',{'exit0':client.proc.returncode==0},client.proc.returncode)
report={'binarySHA256':sha(binary),'runnerSHA256':sha(pathlib.Path(__file__)),'wireSHA256':sha(pathlib.Path(wire.__file__)),'casesSHA256':sha(out/'expected-cli.json'),'rows':rows,'passed':len(rows)==len(cases)+6 and all(x['passed'] for x in rows)};(out/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'rows':len(rows),'failed':[(r['id'],[k for k,v in r['checks'].items() if not v]) for r in rows if not r['passed']]}));sys.exit(0 if report['passed'] else 1)
