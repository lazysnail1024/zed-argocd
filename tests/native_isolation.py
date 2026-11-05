#!/usr/bin/env python3
"""Build, package and execute the native release with all editor files removed."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {'passed': False, 'boundary': 'native build and release without any editor files', 'commands': []}
    def run(name, command, cwd, env=None):
        p = subprocess.run([str(x) for x in command], cwd=cwd, env=env, capture_output=True, timeout=600)
        (output/(name+'.stdout')).write_bytes(p.stdout)
        (output/(name+'.stderr')).write_bytes(p.stderr)
        report['commands'].append({'name':name, 'command':[str(x) for x in command], 'exit':p.returncode})
        if p.returncode:
            raise RuntimeError(name+': '+p.stderr.decode(errors='replace')[-2500:])
        return p.stdout
    try:
        product = 'argocd' if (ROOT/'tools/native.py').exists() else 'argo-workflows'
        binary_name = product+'-ls'+('.exe' if os.name=='nt' else '')
        go_env = json.loads(run('host', ['go','env','-json','GOHOSTOS','GOHOSTARCH'], ROOT))
        platform = go_env['GOHOSTOS']+'/'+go_env['GOHOSTARCH']
        with tempfile.TemporaryDirectory(prefix='native-without-editors-') as temporary:
            source = Path(temporary)/'source'
            source.mkdir()
            names = subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT).decode().split('\0')
            removed=[]
            for name in sorted(set(names)-{''}):
                path=ROOT/name
                if not path.exists():continue
                if name.startswith('editors/') or name.startswith('.github/workflows/zed'):
                    removed.append(name)
                    continue
                if path.is_symlink() or not path.is_file():
                    raise ValueError('Source must be a regular file: '+name)
                dest=source/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,dest)
            if not removed or (source/'editors').exists() or (source/'Cargo.toml').exists():
                raise ValueError('Isolation fixture did not remove the adapter')
            env=os.environ|{'GIT_AUTHOR_NAME':'Native isolation fixture','GIT_AUTHOR_EMAIL':'fixture@example.invalid',
                            'GIT_COMMITTER_NAME':'Native isolation fixture','GIT_COMMITTER_EMAIL':'fixture@example.invalid'}
            run('git-init',['git','init'],source,env)
            run('git-add',['git','-c','core.autocrlf=false','add','.'],source,env)
            run('git-commit',['git','-c','commit.gpgsign=false','commit','-m','Isolated native source fixture'],source,env)
            commit=run('source-commit',['git','rev-parse','HEAD'],source).decode().strip()
            version=(source/'VERSION').read_text().strip()
            build=output/'build'
            folder='tools' if product=='argocd' else 'scripts'
            release=source/folder/'release.py'
            run('independent-version',[sys.executable,release,'version','--source-commit',commit],source)
            if product=='argocd':
                run('native-build',[sys.executable,source/'tools/native.py','build','--platform',platform,'--mode','release',
                                    '--version',version,'--source-commit',commit,'--output',build],source)
                stage=['--manifest',build/'manifest.json']
            else:
                run('native-build',[sys.executable,source/'tests/ci/run.py','build','--platform',platform,
                                    '--release-version',version,'--output',build],source)
                stage=['--build',build/'build.json']
            staged=output/'release'
            run('native-stage',[sys.executable,release,'stage',*stage,'--version',version,'--source-commit',commit,'--output',staged],source)
            record=json.loads(next(staged.glob('release-*.json')).read_text())
            raw=staged/record['binaryAsset']
            identity=json.loads(run('native-identity',[raw,'version','--json'],source))
            archive=staged/record['archiveAsset']
            if archive.suffix=='.zip':
                with zipfile.ZipFile(archive) as z: members=z.namelist()
            else:
                with tarfile.open(archive) as t: members=t.getnames()
            checks={'analysisReady':identity['analysisReady'] is True,'exactSource':identity['sourceCommit']==commit,
                    'nativeVersion':identity['serverVersion']==version,'autoTarget':identity.get('defaultTargetVersion')=='auto',
                    'noEditorInArchive':not any('/extension/' in name or '/editors/' in name or '/Cargo.' in name for name in members),
                    'hasExecutable':any(name.endswith('/'+binary_name) for name in members),
                    'hasOriginalLicenses':any('/licenses/go/LICENSE' in name for name in members)}
            report.update(sourceCommit=commit, removedEditorFiles=len(removed), binarySha256=hashlib.sha256(raw.read_bytes()).hexdigest(),
                          archiveSha256=hashlib.sha256(archive.read_bytes()).hexdigest(),checks=checks,passed=all(checks.values()))
    except Exception as error:
        report['error']=repr(error)
    (output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))
    return 0 if report['passed'] else 1

if __name__=='__main__':sys.exit(main())
