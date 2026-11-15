#!/usr/bin/env python3
"""Captured-action lifetimes and actual stdio render completion fences.

The controlled renderer observes calls and owns a real descendant. It is not a
Helm/schema oracle; render_profiles_e2e separately uses both actual Helm majors.
"""
import argparse
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import time

import audit_repair_e2e as wire
from render_profiles_e2e import setup, config, TEMPLATE
from support.session import Session


class Client(Session):
    def __init__(self, folder, roots, binary, show=True):
        self.show = show
        super().__init__(folder, roots, binary=binary, server_request=lambda message: {'success': True})

    def send(self, method, params=None, ident=None):
        if method == 'initialize':
            params['capabilities']['window'] = {'showDocument': {'support': self.show}}
        return super().send(method, params, ident)


def helper(folder):
    binary = folder / 'bin/helm'
    binary.parent.mkdir()
    binary.write_text('#!' + sys.executable + '\n' + '''import json,os,pathlib,subprocess,sys,time
folder=pathlib.Path(''' + repr(str(folder)) + ''')
with (folder/'calls.jsonl').open('a') as out:out.write(json.dumps(sys.argv[1:])+'\\n')
if sys.argv[1]=='version':print('v3.19.0');sys.exit(0)
mode=(folder/'mode').read_text()
if mode=='tree':
 child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'])
 (folder/'pids.json').write_text(json.dumps([os.getpid(),child.pid]))
if mode in ['hold','tree']:
 print('# partial stdout',flush=True);print('held stderr',file=sys.stderr,flush=True)
(folder/'started').write_text('template')
if mode in ['hold','tree']:
 while not (folder/'release').exists():time.sleep(.01)
print('apiVersion: argoproj.io/v1alpha1\\nkind: Application\\nmetadata: {name: rendered}\\nspec: {project: default, source: {repoURL: x}, destination: {server: x, namespace: default}}',flush=True)
''')
    binary.chmod(0o700)
    (folder / 'mode').write_text('pass')
    return binary


def wait_file(path):
    end = time.monotonic() + 6
    while not path.exists() and time.monotonic() < end: time.sleep(.01)
    return path.exists()


def actions(client, file):
    params = {'textDocument': {'uri': file.as_uri()}, 'range': {'start': {'line': 0, 'character': 0}, 'end': {'line': 0, 'character': 0}}, 'context': {'diagnostics': []}}
    deadline = time.monotonic() + 2
    while True:
        response = client.request('textDocument/codeAction', params)
        if response.get('error', {}).get('code') != -32801 or time.monotonic() >= deadline: break
        time.sleep(.02)
    return [item for item in response.get('result', []) if item.get('command', {}).get('command') == 'argocd.renderCheck']


def selected(items, name=''):
    return next((item for item in items if item['command'].get('arguments', [{}])[0].get('profile', '') == name), None)


def execute(client, action):
    return client.request('workspace/executeCommand', action['command'], timeout=12) if action else {}


def refused(response, phrase):
    return 'error' in response and phrase in response['error'].get('message', '').lower()


def run(binary, out):
    rows = []
    original_path = os.environ.get('PATH', '')
    original_cache = os.environ.get('XDG_CACHE_HOME')

    def record(name, checks, observed):
        rows.append({'id': name, 'assertions': checks, 'observed': observed, 'passed': bool(checks) and all(checks.values())})
        (out / 'report.json').write_text(json.dumps({'rows': rows, 'passed': all(row['passed'] for row in rows), 'binarySHA256': wire.hash_file(binary), 'runnerSHA256': wire.hash_file(Path(__file__)), 'command': sys.argv}, indent=2) + '\n')
        print(name, 'PASS' if rows[-1]['passed'] else 'FAIL', flush=True)

    def start(name, profiles=True, show=True):
        folder = out / name
        folder.mkdir()
        root = folder / 'workspace'
        file = setup(root, config() if profiles else {'schemaVersion': 1, 'targetVersion': '3.6.1'})
        helm = helper(folder)
        os.environ['PATH'] = str(helm.parent) + os.pathsep + original_path
        os.environ['XDG_CACHE_HOME'] = str(folder / 'cache')
        client = Client(folder, [root], binary, show)
        client.open(file, TEMPLATE, 4)
        return folder, root, file, client

    folder, root, file, client = start('profiles')
    try:
        listed = actions(client, file)
        dev = selected(listed, 'dev')
        resolved = client.request('codeAction/resolve', dev) if dev else {}
        record('collect-and-resolve-never-run-helm', {'defaultAndNamedOptions': {a['command']['arguments'][0].get('profile', '') for a in listed} == {'', 'dev', 'prod', 'reverse'}, 'resolvePreservesCapturedInput': bool(dev) and resolved.get('result') == dev, 'zeroRendererCalls': not (folder / 'calls.jsonl').exists()}, {'listed': listed, 'resolved': resolved})

        values = root / 'env/dev.yaml'
        client.open(values, 'environment: unsaved\n', 1)
        rejected = execute(client, dev)
        notice = None
        try: notice = client.wait(lambda item: item.get('method') == 'window/showMessage' and item.get('params', {}).get('type') == 2, timeout=.5)
        except TimeoutError: pass
        record('external-values-must-be-saved', {'refusedBeforeHelm': refused(rejected, 'save') and not (folder / 'calls.jsonl').exists(), 'diskUnchanged': values.read_text() == 'environment: dev\n', 'visibleSaveReason': bool(notice) and 'save' in notice['params']['message'].lower() and 'values' in notice['params']['message'].lower() and str(values) in notice['params']['message']}, {'response': rejected, 'notice': notice})
        client.send('textDocument/didClose', {'textDocument': {'uri': values.as_uri()}})

        fresh = selected(actions(client, file), 'dev')
        values.write_text('environment: changed\n')
        rejected = execute(client, fresh)
        record('ordered-values-disk-identity', {'oldValueHashRefused': refused(rejected, 'stale') and not (folder / 'calls.jsonl').exists()}, rejected)
        values.write_text('environment: dev\n')

        fresh = selected(actions(client, file), 'dev')
        changed = config()
        changed['renderProfiles']['dev']['values'].reverse()
        configuration = root / '.argocd-ls.yaml'
        configuration.write_text(json.dumps(changed, indent=2) + '\n')
        rejected = execute(client, fresh)
        record('profile-order-change-invalidates-action', {'orderAndConfigurationRefused': refused(rejected, 'stale') and not (folder / 'calls.jsonl').exists()}, rejected)
        configuration.write_text(json.dumps(config(), indent=2) + '\n')

        fresh = selected(actions(client, file), 'dev')
        client.open(configuration, json.dumps(changed), 8)
        rejected = execute(client, fresh)
        record('unsaved-profile-configuration-refused', {'noRendererForDirtyConfig': 'error' in rejected and not (folder / 'calls.jsonl').exists(), 'savedConfigurationPreserved': json.loads(configuration.read_text()) == config()}, rejected)
        client.send('textDocument/didClose', {'textDocument': {'uri': configuration.as_uri()}})

        alias = root / 'dev-alias.yaml'
        alias.symlink_to(values)
        client.open(alias, 'environment: alias-unsaved\n', 3)
        fresh = selected(actions(client, file), 'dev')
        rejected = execute(client, fresh)
        record('values-alias-buffer-is-authoritative', {'aliasDirtyInputRefused': refused(rejected, 'save') and not (folder / 'calls.jsonl').exists()}, rejected)
        client.send('textDocument/didClose', {'textDocument': {'uri': alias.as_uri()}})

        fresh = selected(actions(client, file), 'dev')
        rendered = execute(client, fresh)
        if fresh and 'error' not in rendered: client.wait(lambda x: x.get('method') == 'window/showMessage')
        # The transport returns a real JSON-RPC result to showDocument. A normal
        # next request proves the response was not parsed as a new request.
        symbols = client.request('textDocument/documentSymbol', {'textDocument': {'uri': file.as_uri()}})
        shown = [x['message'] for x in client.messages if x['message'].get('method') == 'window/showDocument']
        record('saved-profile-recovers-and-show-response-is-valid', {'explicitRenderSucceeded': bool(rendered.get('result', {}).get('output')) and Path(rendered['result']['output']).is_file(), 'oneOutputRequest': len(shown) == 1, 'protocolRemainsUsable': 'result' in symbols and not any(x['message'].get('error') and x['message'].get('id') == 'argocd-render-output' for x in client.messages)}, {'rendered': rendered, 'symbols': symbols, 'shown': shown})
    finally:
        client.close()

    for name in ['reopened-source', 'readded-root', 'no-show-capability']:
        folder, root, file, client = start(name, False, name != 'no-show-capability')
        try:
            action = selected(actions(client, file))
            if name == 'reopened-source':
                client.send('textDocument/didClose', {'textDocument': {'uri': file.as_uri()}})
                client.open(file, TEMPLATE, 4)
            elif name == 'readded-root':
                client.send('workspace/didChangeWorkspaceFolders', {'event': {'removed': [{'uri': root.as_uri(), 'name': root.name}], 'added': []}})
                client.send('workspace/didChangeWorkspaceFolders', {'event': {'removed': [], 'added': [{'uri': root.as_uri(), 'name': root.name}]}})
            response = execute(client, action)
            if name == 'no-show-capability':
                client.request('textDocument/documentSymbol', {'textDocument': {'uri': file.as_uri()}})
                checks = {'explicitResultStillAvailable': bool(response.get('result', {}).get('output')), 'noUnsupportedShowDocument': not any(x['message'].get('method') == 'window/showDocument' for x in client.messages)}
            else:
                checks = {'capturedLifetimeRefused': refused(response, 'stale'), 'zeroRendererCalls': not (folder / 'calls.jsonl').exists()}
            record(name, checks, response)
        finally:
            client.close()

    for name in ['edit-during-render', 'remove-root-during-render', 'cancel-descendant-render']:
        folder, root, file, client = start(name, False)
        try:
            action = selected(actions(client, file))
            (folder / 'mode').write_text('tree' if name == 'cancel-descendant-render' else 'hold')
            if action:
                client.seq += 1
                ident = client.seq
                client.send('workspace/executeCommand', action['command'], ident)
                started = wait_file(folder / 'started')
                if name == 'edit-during-render': client.change(file, TEMPLATE + '# changed while renderer was running\n', 5)
                elif name == 'remove-root-during-render': client.send('workspace/didChangeWorkspaceFolders', {'event': {'added': [], 'removed': [{'uri': root.as_uri(), 'name': root.name}]}})
                else: client.send('$/cancelRequest', {'id': ident})
                if name != 'cancel-descendant-render': (folder / 'release').write_text('continue')
                response = client.wait(lambda x: x.get('id') == ident, timeout=8)
            else:
                started, response = False, {}
            client.request('textDocument/documentSymbol', {'textDocument': {'uri': file.as_uri()}})
            checks = {'actualRendererStarted': started, 'obsoleteSuccessRefused': 'error' in response, 'noLateOutputNotifications': not any(x['message'].get('method') in ['window/showDocument', 'window/showMessage'] for x in client.messages)}
            if name == 'cancel-descendant-render':
                pids = json.loads((folder / 'pids.json').read_text()) if (folder / 'pids.json').exists() else []
                def exited(pid):
                    status = Path('/proc') / str(pid) / 'stat'
                    return not status.exists() or status.read_text().split()[2] == 'Z'
                end = time.monotonic() + 3
                while pids and not all(exited(p) for p in pids) and time.monotonic() < end: time.sleep(.02)
                checks['descendantReclaimed'] = len(pids) == 2 and all(exited(p) for p in pids)
                retained = False
                end = time.monotonic() + 3
                while time.monotonic() < end:
                    listed = subprocess.run([str(binary), 'render-cache'], capture_output=True, timeout=3)
                    try: entries = json.loads(listed.stdout).get('entries', [])
                    except ValueError: entries = []
                    owned = [entry for entry in entries if entry.get('owned')]
                    if owned:
                        artifact = Path(owned[0]['directory'])
                        report = json.loads((artifact / 'report.json').read_text())
                        retained = '# partial stdout' in (artifact / 'helm.stdout').read_text() and 'held stderr' in (artifact / 'helm.stderr').read_text() and report.get('renderer', {}).get('exit', 0) != 0 and [item['code'] for item in report.get('configurationErrors', [])] == ['RENDER_CANCELLED']
                        break
                    time.sleep(.02)
                checks['cancelledEvidenceRetained'] = retained
            record(name, checks, response)
        finally:
            (folder / 'release').write_text('cleanup')
            client.close()
            # Reclaim deliberately leaked test descendants after recording the
            # outcome, so harness cleanup cannot make the assertion pass.
            if (folder / 'pids.json').is_file():
                for pid in json.loads((folder / 'pids.json').read_text()):
                    try: os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError: pass
    os.environ['PATH'] = original_path
    if original_cache is None: os.environ.pop('XDG_CACHE_HOME', None)
    else: os.environ['XDG_CACHE_HOME'] = original_cache
    return all(row['passed'] for row in rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('binary', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.resolve().mkdir(parents=True, exist_ok=False)
    sys.exit(0 if run(args.binary.resolve(), args.output.resolve()) else 1)
