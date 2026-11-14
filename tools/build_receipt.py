#!/usr/bin/env python3
"""Build once with cold caches, or verify exact same-attempt ordinary bytes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]

def sha(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Expected regular input: '+str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()

def verify(directory, source, platform, mode, version, run, attempt, receipt_sha=None, check_source=False):
    if receipt_sha is not None and sha(directory/"receipt.json")!=receipt_sha:
        raise ValueError("Receipt differs from trusted producer output digest")
    receipt=json.loads((directory/'receipt.json').read_text())
    expected={'schemaVersion':1,'buildRole':'ordinary','sourceCommit':source,'platform':platform,
              'buildMode':mode,'version':version,'runID':run,'attempt':attempt,'initialBuildCachesEmpty':True}
    if any(type(receipt.get(k)) is not type(v) or receipt[k]!=v for k,v in expected.items()):
        raise ValueError('Producer identity mismatch; rerun the producer and consumers in the same attempt')
    if not receipt.get('job') or sha(directory/'manifest.json')!=receipt.get('manifestSHA256'):
        raise ValueError('Missing producer job or mismatched manifest')
    manifest=json.loads((directory/'manifest.json').read_text())
    name=manifest['binary']
    if Path(name).name!=name or sha(directory/name)!=receipt.get('binarySHA256') or manifest['binarySHA256']!=receipt['binarySHA256']:
        raise ValueError('Binary differs from producer bytes')
    identity=manifest['identity']
    if manifest['platform']!=platform or any(identity.get(k)!=v for k,v in {'sourceCommit':source,'buildMode':mode,'serverVersion':version,'dirty':False}.items()):
        raise ValueError('Native build identity differs from expected ordinary build')
    if check_source:
        if subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()!=source:
            raise ValueError('Checkout differs from producer source')
        names=subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode().split('\0')
        actual={name:sha(ROOT/name) for name in names if name}
        if manifest['sourceInputs']!=actual:raise ValueError('Build input inventory differs from checked-out source')
        binary=directory/manifest['binary']
        if os.name!='nt':binary.chmod(0o755)
        observed=json.loads(subprocess.check_output([str(binary),'version','--json'],timeout=20))
        if observed!=identity:raise ValueError('Executed binary handshake differs from producer manifest')
        expected_go='go'+next(line.split()[1] for line in (ROOT/'go.mod').read_text().splitlines() if line.startswith('go '))
        if manifest['goEnvironment']['GOVERSION']!=expected_go or manifest['buildEnvironment']['CGO_ENABLED']!='1':
            raise ValueError('Producer toolchain or CGO mode differs')
    return receipt

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['produce','verify'])
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--source-commit',required=True)
    parser.add_argument('--receipt-sha')
    parser.add_argument('--check-source',action='store_true')
    parser.add_argument('--platform',required=True)
    parser.add_argument('--mode',choices=['release','development'],required=True)
    parser.add_argument('--version',required=True)
    parser.add_argument('--run-id',default=os.environ.get('GITHUB_RUN_ID'))
    parser.add_argument('--attempt',default=os.environ.get('GITHUB_RUN_ATTEMPT'))
    args=parser.parse_args()
    try:
        if not args.run_id or not args.attempt:
            raise ValueError('Explicit workflow run and attempt are required')
        directory=args.directory.resolve()
        if args.command=='produce':
            directory.mkdir(parents=True,exist_ok=False)
            cache=directory.parent/(directory.name+'-cache')
            modules=directory.parent/(directory.name+'-modules')
            cache.mkdir(); modules.mkdir()
            env=dict(os.environ,GOCACHE=str(cache),GOMODCACHE=str(modules),GOWORK='off',GOTOOLCHAIN='local',CGO_ENABLED='1')
            command=[sys.executable,str(ROOT/'tools/native.py'),'build','--platform',args.platform,'--mode',args.mode,'--source-commit',args.source_commit,'--output',str(directory/'build')]
            command += ['--version',args.version]
            started=time.monotonic()
            result=subprocess.run(command,cwd=ROOT,env=env,capture_output=True,timeout=600)
            (directory/'build.stdout').write_bytes(result.stdout);(directory/'build.stderr').write_bytes(result.stderr)
            (directory/'command.json').write_text(json.dumps({'command':command,'exit':result.returncode,'elapsedSeconds':time.monotonic()-started}))
            if result.returncode:raise ValueError('Producer native build failed')
            for path in (directory/'build').iterdir():
                shutil.move(str(path),str(directory/path.name))
            (directory/'build').rmdir()
            manifest=json.loads((directory/'manifest.json').read_text())
            receipt={'schemaVersion':1,'buildRole':'ordinary','sourceCommit':args.source_commit,'platform':args.platform,
                     'buildMode':args.mode,'version':args.version,'runID':args.run_id,'attempt':args.attempt,
                     'job':os.environ.get('GITHUB_JOB','local-producer'),'initialBuildCachesEmpty':True,
                     'manifestSHA256':sha(directory/'manifest.json'),'binarySHA256':sha(directory/manifest['binary'])}
            (directory/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
        if args.command=='produce' and os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'],'a') as output:output.write('receiptSHA256='+sha(directory/'receipt.json')+'\n')
        if args.command=='verify' and not args.receipt_sha:raise ValueError('Trusted producer receipt digest is required')
        print(json.dumps(verify(directory,args.source_commit,args.platform,args.mode,args.version,args.run_id,args.attempt,args.receipt_sha,args.check_source)))
        return 0
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as error:
        print(json.dumps({'passed':False,'error':str(error)}),file=sys.stderr)
        return 1

if __name__=='__main__':sys.exit(main())
