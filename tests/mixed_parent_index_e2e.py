#!/usr/bin/env python3
"""Independent stdio checks for mixed roots and live resource locations."""
import argparse
import importlib.util
import json
import os
import pathlib
import shutil
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('binary', type=pathlib.Path)
parser.add_argument('output', type=pathlib.Path)
parser.add_argument('--ordinary-files', type=int, default=2048)
parser.add_argument('--padding-bytes', type=int, default=0)
parser.add_argument('--background-edits', action='store_true',
                    help='Require a local quick fix while discovery is still progressing; use a large fixture.')
args = parser.parse_args()
os.environ['CD_AUDIT_BINARY'] = str(args.binary.resolve())
os.environ['CD_AUDIT_OUTPUT'] = str(args.output.resolve())
if args.background_edits:
    os.environ['ARGOCD_LS_TRACE'] = '1'
spec = importlib.util.spec_from_file_location('wire', pathlib.Path(__file__).with_name('audit_repair_e2e.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
out = args.output.resolve()
out.mkdir(parents=True, exist_ok=True)
root = out / 'input'
if root.exists():
    shutil.rmtree(root)
root.mkdir()
m.write(root, '.argocd-ls.yaml', m.CONFIG)
foreign = 'apiVersion: argoproj.io/v1alpha1\nkind: Workflow\nmetadata: {name: other}\nspec: {entrypoint: absent}\n'
for i in range(args.ordinary_files):
    text = foreign if i % 100 == 0 else 'apiVersion: v1\nkind: ConfigMap\nmetadata: {name: ordinary-' + str(i) + '}\n'
    if args.padding_bytes:
        text += '# ' + 'x' * args.padding_bytes + '\n'
    m.write(root, f'a-background/repo-{i % 8:02d}/file-{i:05d}.yaml', text)
consumer_text = m.APP + '---\n' + foreign
consumer = m.write(root, 'z-related/consumer.yaml', consumer_text)
target = m.write(root, 'z-related/project.yaml', m.PROJECT)
other = m.write(root, 'z-related/workflow.yaml', foreign)
checks, observed = {}, {}
client = None
traces = None


def position(text, needle):
    offset = text.index(needle)
    before = text[:offset]
    return {'line': before.count('\n'), 'character': len(before.rsplit('\n', 1)[-1])}


def definition():
    response = client.request('textDocument/definition', {
        'textDocument': {'uri': consumer.as_uri()},
        'position': position(consumer_text, 'team'),
    })
    if response.get('error', {}).get('code') == -32801:
        return None  # ContentModified is safe while a fresh result is pending.
    assert 'error' not in response, response
    return response.get('result') or []


def await_definition(expected, timeout=10):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = definition()
        if last is not None and expected(last):
            return last
        time.sleep(.02)
    raise AssertionError({'definitionDeadline': timeout, 'last': last})


def at_target(locations, text=m.PROJECT):
    return len(locations) == 1 and locations[0]['uri'] == target.as_uri() and locations[0]['range']['start'] == position(text, 'team')


def changed(path, kind):
    client.send('workspace/didChangeWatchedFiles', {'changes': [{'uri': path.as_uri(), 'type': kind}]})


try:
    started = time.monotonic()
    if args.background_edits:
        import persistent_workspace_e2e as progress
        client = progress.Client(out, [root])
        traces = progress.Traces(client)
    else:
        client = m.LSP(out, [root])
    observed['initializeSeconds'] = time.monotonic() - started
    began = time.monotonic()
    client.open(consumer, consumer_text)
    first = client.wait(lambda value: value.get('method') == 'textDocument/publishDiagnostics'
                        and value['params']['uri'] == consumer.as_uri()
                        and value['params'].get('version') == 1, timeout=5)
    observed['firstPublicationSeconds'] = time.monotonic() - began
    observed['startupToFirstPublicationSeconds'] = time.monotonic() - started
    observed['firstDiagnostics'] = first['params']['diagnostics']
    checks['current_file_publication_under_two_seconds'] = observed['firstPublicationSeconds'] < 2
    observed['initialDefinition'] = await_definition(at_target)
    checks['mixed_document_cross_file_definition'] = True

    if args.background_edits:
        typo = consumer_text.replace('  destination:', '  destinatoin:')
        client.change(consumer, typo, 2)
        client.wait(lambda value: value.get('method') == 'textDocument/publishDiagnostics'
                    and value['params']['uri'] == consumer.as_uri()
                    and value['params'].get('version') == 2)
        start = position(typo, 'destinatoin')
        end = dict(start, character=start['character'] + len('destinatoin'))
        actions = client.request('textDocument/codeAction', {
            'textDocument': {'uri': consumer.as_uri()}, 'range': {'start': start, 'end': end},
        })
        choice = next((action for action in actions.get('result', [])
                       if action.get('title') == 'Rename field to destination'), None)
        assert choice is not None, actions
        traces.refresh()
        before = max((row['stats'].get('knownFiles', 0) for row in traces.rows), default=0)
        time.sleep(.12)  # Let unrelated background files commit between the two requests.
        resolved = client.request('codeAction/resolve', choice)
        traces.refresh()
        after = max((row['stats'].get('knownFiles', 0) for row in traces.rows), default=0)
        checks['local_edit_runs_during_progress'] = after > before and not any(
            row['stats'].get('state') == 'Ready' for row in traces.rows)
        edits = (resolved.get('result') or {}).get('edit', {}).get('documentChanges', [])
        checks['unrelated_background_commits_preserve_local_edit'] = (
            len(edits) == 1
            and edits[0]['textDocument'] == {'uri': consumer.as_uri(), 'version': 2}
            and edits[0]['edits'] == [{'range': {'start': start, 'end': end}, 'newText': 'destination'}])
        observed['backgroundEdit'] = {'knownBefore': before, 'knownAfter': after, 'resolve': resolved}
        client.change(consumer, consumer_text, 3)
        stale = client.request('codeAction/resolve', choice)
        checks['current_buffer_change_rejects_old_edit'] = stale.get('error', {}).get('code') == -32801
        observed['staleEditResponse'] = stale

    client.open(other, foreign)
    ds = client.wait(lambda value: value.get('method') == 'textDocument/publishDiagnostics'
                     and value['params']['uri'] == other.as_uri())['params']['diagnostics']
    checks['foreign_resource_has_no_argocd_diagnostics'] = ds == []

    client.open(target, m.PROJECT.replace('name: team', 'name: different'))
    await_definition(lambda locations: locations == [])
    target.write_bytes(m.PROJECT.encode())
    changed(target, 2)
    # A response after the notification must not restore the disk definition
    # while the renamed buffer remains authoritative.
    checks['open_target_overrides_disk_watch'] = definition() in (None, [])
    client.send('textDocument/didClose', {'textDocument': {'uri': target.as_uri()}})
    await_definition(at_target)
    checks['close_restores_current_disk_target'] = True

    shifted = '# location changed without changing the project\n\n' + m.PROJECT
    target.write_bytes(shifted.encode())
    changed(target, 2)
    immediate = definition()
    checks['watch_does_not_return_old_position'] = not immediate or at_target(immediate, shifted)
    observed['shiftedDefinition'] = await_definition(lambda locations: at_target(locations, shifted))
    checks['position_only_change_refreshes_definition'] = True

    target.unlink()
    changed(target, 3)
    checks['delete_does_not_return_old_target'] = definition() in (None, [])
    await_definition(lambda locations: locations == [])
    checks['deleted_target_is_withdrawn'] = True
    target.write_bytes(m.PROJECT.encode())
    changed(target, 1)
    await_definition(at_target)
    checks['recreated_target_resolves'] = True
except Exception as exc:
    checks['exception'] = repr(exc)
finally:
    if client is not None:
        client.close()
        checks['clean_shutdown'] = client.proc.returncode == 0
        if traces is not None:
            traces.refresh()
            (out / 'traces.json').write_text(json.dumps(traces.rows, indent=2) + '\n')
    report = {'command': __import__('sys').argv,
              'binarySHA256': m.hash_file(args.binary),
              'testSHA256': m.hash_file(pathlib.Path(__file__)),
              'ordinaryFiles': args.ordinary_files,
              'paddingBytesPerFile': args.padding_bytes,
              'checks': checks, 'observed': observed,
              'pass': bool(checks) and all(value is True for value in checks.values()),
              'scope': 'Native stdio behavior; this is not the full 50k or Zed GUI acceptance.'}
    (out / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
raise SystemExit(0 if report['pass'] else 1)
