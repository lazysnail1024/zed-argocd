#!/usr/bin/env python3
"""Install only the checksum-pinned Linux Helm used by real renderer tests."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import urllib.request

URL = 'https://get.helm.sh/helm-v3.19.0-linux-amd64.tar.gz'
SHA256 = 'a7f81ce08007091b86d8bd696eb4d86b8d0f2e1b9f6c714be62f82f96a594496'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    with urllib.request.urlopen(URL, timeout=60) as response:
        archive = response.read(32 * 1024 * 1024 + 1)
    if len(archive) > 32 * 1024 * 1024 or hashlib.sha256(archive).hexdigest() != SHA256:
        raise ValueError('Helm archive size or SHA256 differs from the fixed test tool')
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as package:
        member = package.getmember('linux-amd64/helm')
        if not member.isfile() or member.size > 100 * 1024 * 1024:
            raise ValueError('Expected the regular pinned Helm executable')
        with package.extractfile(member) as source:
            binary = source.read()
    executable = args.output / 'helm'
    executable.write_bytes(binary)
    executable.chmod(0o755)
    result = subprocess.run([str(executable.resolve()), 'version', '--short'], capture_output=True, check=True, timeout=10)
    if not result.stdout.decode().startswith('v3.19.0+'):
        raise ValueError('Pinned Helm runtime version differs')
    (args.output / 'receipt.json').write_text(json.dumps({'url': URL, 'archiveSHA256': SHA256,
        'binarySHA256': hashlib.sha256(binary).hexdigest(), 'version': result.stdout.decode().strip()}, indent=2) + '\n')


if __name__ == '__main__':
    main()
