#!/usr/bin/env python3
"""Prove document diagnostics survive per-file deadlines and detect their loss.

Both variants compile isolated copies of this checkout. The deadline is shortened
only in a test copy, with no production hook. The negative control removes the
real incremental publication recovery under the same per-file timeout. Ordinary
sibling errors are already isolated and cannot exercise that merge guard.
Build/transport/runner failures do not count as detecting the removed recovery.
"""
import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

from audit_regression_mutations import FatalCommandError, command, digest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CONSUMER = ROOT / 'tests/parent_workspace_lsp_e2e.py'
VARIANTS = (
    ('background-file-deadline', False),
    ('publication-recovery-removed', True),
)
DEADLINE = ('const workspaceAnalysisTimeout = 5 * time.Second',
            'const workspaceAnalysisTimeout = time.Nanosecond')
RECOVERY = ('recoverDocumentPublications(result, documents, roots, buffers)',
            'result.publications')


def consumer(binary, folder, env, timeout=False):
    args = [sys.executable, str(CONSUMER), str(binary), str(folder / 'consumer')]
    if timeout:
        args.append('--timeout')
    outcome = command(args, folder, 'consumer', env, timeout=90)
    outcome['report'] = json.loads((folder / 'consumer/report.json').read_text(encoding='utf-8'))
    return outcome


def detected_loss(outcome):
    report = outcome['report']
    if outcome['exit'] != 1 or report.get('passed') is not False:
        return False
    rows = {row['id']: row for row in report.get('rows', [])}
    expected = {'six-repositories-deadline'}
    if set(rows) != expected:
        return False
    required = ('background-failure-was-exercised', 'zero-config-unsaved-buffer-type-error',
                'local-diagnostics-precede-background-failure',
                'local-schema-hover-survives-background-failure',
                'local-field-completion-survives-background-failure',
                'background-failure-cannot-claim-complete',
                'partial-scope-refuses-exhaustive-references',
                'permanent-deadline-still-allows-valid-local-buffer', 'clean-server-exit')
    for row in rows.values():
        checks = {item['id']: item['passed'] for item in row.get('checks', [])}
        if (row.get('error') or row.get('passed') is not False
                or checks.get('background-failure-cannot-clear-local-type-error') is not False
                or not all(checks.get(name) is True for name in required)):
            return False
    return True


def worker(args):
    binary, out = args.binary.resolve(), args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, GOWORK='off', GOTOOLCHAIN='local', CGO_ENABLED='1', GOFLAGS='',
               GOPROXY='off', GOSUMDB='off', GOENV='off', PYTHONUTF8='1')
    go = env.get('GO') or (str(pathlib.Path(env['GOROOT']) / 'bin/go')
                           if env.get('GOROOT') else shutil.which('go'))
    report = {'binarySHA256': digest(binary), 'runnerSHA256': digest(pathlib.Path(__file__)),
              'consumerSHA256': digest(CONSUMER), 'platform': sys.platform, 'rows': [],
              'completed': False, 'passed': False}

    def save():
        pending = out / 'report.pending.json'
        pending.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        pending.replace(out / 'report.json')

    save()
    try:
        if not go:
            raise RuntimeError('Go toolchain is required')
        control = out / 'control'
        control.mkdir()
        report['control'] = consumer(binary, control, env)
        if report['control']['exit'] != 0 or report['control']['report'].get('passed') is not True:
            raise RuntimeError('Unmodified binary did not pass the positive control')
        for ident, remove_recovery in VARIANTS:
            folder = out / ident
            folder.mkdir()
            row = {'id': ident, 'passed': False, 'mutations': []}
            report['rows'].append(row)
            save()
            with tempfile.TemporaryDirectory(prefix='argocd-parent-fault-') as temporary:
                source = pathlib.Path(temporary)
                for name in ('go.mod', 'go.sum', 'cmd', 'internal', 'vendor'):
                    original, destination = ROOT / name, source / name
                    shutil.copytree(original, destination) if original.is_dir() else shutil.copy2(original, destination)
                target = source / 'internal/lsp/server.go'
                text = target.read_text(encoding='utf-8')
                row['sourceSHA256'] = digest(target)
                for anchor, replacement in (DEADLINE, RECOVERY) if remove_recovery else (DEADLINE,):
                    if text.count(anchor) != 1:
                        raise RuntimeError(ident + ': mutation anchor must occur exactly once: ' + anchor)
                    text = text.replace(anchor, replacement, 1)
                    row['mutations'].append({'file': 'internal/lsp/server.go', 'anchor': anchor,
                                             'replacement': replacement})
                target.write_text(text, encoding='utf-8')
                row['variantSourceSHA256'] = digest(target)
                candidate = folder / ('argocd-ls.exe' if os.name == 'nt' else 'argocd-ls')
                row['build'] = command([go, 'build', '-mod=vendor', '-buildvcs=false', '-trimpath',
                                        '-o', str(candidate), './cmd/argocd-ls'], folder, 'build', env, source)
                if row['build']['exit'] != 0:
                    raise RuntimeError(ident + ': fault variant did not compile')
                row['binarySHA256'] = digest(candidate)
                row['oracle'] = consumer(candidate, folder, env, timeout=True)
                if not remove_recovery:
                    row['passed'] = (row['oracle']['exit'] == 0
                                     and row['oracle']['report'].get('passed') is True
                                     and row['oracle']['report'].get('timeoutBuildRequired') is True)
                else:
                    row['detected'] = detected_loss(row['oracle'])
                    row['passed'] = row['detected']
            save()
            print(json.dumps({'id': ident, 'passed': row['passed']}), flush=True)
        report['completed'] = True
        report['passed'] = len(report['rows']) == len(VARIANTS) and all(
            row['passed'] for row in report['rows'])
    except FatalCommandError as exc:
        report['error'], report['fatalCommand'] = repr(exc), exc.execution
    except Exception as exc:
        report['error'] = repr(exc)
    finally:
        save()
    return 0 if report['passed'] else 1


def supervise(args):
    # Reuse the same Linux process-group / Windows job boundary as native CI.
    # --worker is only for a caller that already owns that boundary.
    from native_ci import start_command

    out = args.output.resolve()
    if out.exists():
        raise ValueError('Output must not already exist')
    argv = [sys.executable, str(pathlib.Path(__file__).resolve()), str(args.binary.resolve()),
            str(out), '--worker']
    started = time.monotonic()
    process = close = None
    stdout = stderr = b''
    status, cleanup_error = 'launch-failed', None
    try:
        process, close = start_command(argv, dict(os.environ, PYTHONUTF8='1'))
        try:
            stdout, stderr = process.communicate(timeout=480)
            status = process.returncode
        except subprocess.TimeoutExpired:
            status = 'timeout'
    finally:
        if close:
            try:
                close()
            except OSError as exc:
                cleanup_error = repr(exc)
        if process:
            try:
                stdout, stderr = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
                cleanup_error = 'Task-tree output did not close after cleanup'
            process.stdout.close()
            process.stderr.close()
            if os.name == 'nt':
                process._handle.Close()
        out.mkdir(parents=True, exist_ok=True)
        (out / 'boundary.json').write_text(json.dumps({'command': argv, 'exit': status,
            'cleanupError': cleanup_error, 'elapsedSeconds': time.monotonic() - started,
            'passed': status == 0 and cleanup_error is None}, indent=2) + '\n', encoding='utf-8')
        (out / 'worker.stdout').write_bytes(stdout)
        (out / 'worker.stderr').write_bytes(stderr)
    sys.stdout.buffer.write(stdout)
    sys.stderr.buffer.write(stderr)
    return 0 if status == 0 and cleanup_error is None else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=pathlib.Path)
    parser.add_argument('output', type=pathlib.Path)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    sys.exit(worker(arguments) if arguments.worker else supervise(arguments))
