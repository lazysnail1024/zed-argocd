#!/usr/bin/env python3
"""Restore C source directories excluded by go mod vendor for the pinned parsers."""
import hashlib,json,pathlib,shutil,subprocess
root=pathlib.Path(__file__).resolve().parents[1];records=[]
for module,dirs in [('github.com/tree-sitter/go-tree-sitter',['src','include']),('github.com/lazysnail1024/k8s-crd-toolkit',['yamlview/internal/parser/grammar/tree_sitter'])]:
 info=json.loads(subprocess.check_output(['go','list','-mod=mod','-m','-json',module],cwd=root));source=pathlib.Path(info['Dir'])
 for directory in dirs:
  dest=root/'vendor'/module/directory;shutil.copytree(source/directory,dest,dirs_exist_ok=True)
  dest.chmod(0o755)
  for p in dest.rglob('*'):p.chmod(0o755 if p.is_dir() else 0o644)
  records.extend({'module':module,'version':info['Version'],'path':str(p.relative_to(root)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(dest.rglob('*')) if p.is_file())
(root/'contracts/vendor-c-sources.json').write_text(json.dumps(records,indent=2)+'\n')
print(json.dumps({'copiedFiles':len(records)}))

# Verify all retained provider files against its content snapshot, including native grammar.
manifest=json.loads((root/'contracts/toolkit-snapshot.json').read_text())
module=manifest['module'];checked=[]
for relative,expected in manifest['files'].items():
 p=root/'vendor'/module/relative
 if not p.exists():
  if relative in ['CONTRACT.md','go.mod','go.sum']:continue
  raise RuntimeError('vendor source missing: '+relative)
 actual=hashlib.sha256(p.read_bytes()).hexdigest()
 if actual!=expected:raise RuntimeError('vendor content mismatch: '+relative)
 checked.append({'path':relative,'sha256':actual})
(root/'contracts/vendor-toolkit-sources.json').write_text(json.dumps(checked,indent=2)+'\n')

# Preserve upstream character values while keeping the source representation ASCII
# for CJK data. Go interpreted strings accept these equivalent Unicode escapes.
p=root/'vendor/golang.org/x/text/internal/number/tables.go'
p.write_text(''.join('\\u%04x'%ord(c) if '\u3400'<=c<='\u9fff' else c for c in p.read_text()))
p=root/'vendor/golang.org/x/text/internal/number/number.go'
p.write_text(p.read_text().replace("'\u4e09'", "'\\u4e09'"))
