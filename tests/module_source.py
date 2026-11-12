"""Copy the pinned Toolkit for test-only mutation; never edit the module cache."""
import json
import os
from pathlib import Path
import shutil
import subprocess


def toolkit_copy(root, output, relative):
    go = os.environ.get('GO') or (str(Path(os.environ['GOROOT']) / 'bin/go') if os.environ.get('GOROOT') else 'go')
    env = dict(os.environ, GOWORK='off', GOTOOLCHAIN='local', GOFLAGS='')
    data = subprocess.check_output([go, 'list', '-mod=readonly', '-m', '-json',
                                   'github.com/lazysnail1024/k8s-crd-toolkit'], cwd=root, env=env)
    module = json.loads(data)
    if module.get('Replace') or not module.get('Dir'):
        raise ValueError('The pinned module must be downloaded, without a replacement')
    subprocess.check_output([go, 'mod', 'verify'], cwd=root, env=env)
    copied = output / 'toolkit-source'
    shutil.copytree(module['Dir'], copied, copy_function=shutil.copyfile)
    modfile = output / 'ablation.mod'
    shutil.copyfile(root / 'go.mod', modfile)
    shutil.copyfile(root / 'go.sum', modfile.with_suffix('.sum'))
    subprocess.check_call([go, 'mod', 'edit', '-modfile=' + str(modfile),
                           '-replace=github.com/lazysnail1024/k8s-crd-toolkit=' + str(copied)], cwd=root, env=env)
    return copied / relative, '-modfile=' + str(modfile)
