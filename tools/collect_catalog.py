#!/usr/bin/env python3
"""Refresh the rolling two-major CRD inventory at build time (never at runtime)."""
import argparse
import concurrent.futures
import hashlib
import json
import pathlib
import re
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO = 'argoproj/argo-cd'
API = 'https://api.github.com/repos/' + REPO
RAW = 'https://raw.githubusercontent.com/' + REPO


def get(url, optional=False):
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'argocd-ls-catalog'})
            with urllib.request.urlopen(request, timeout=45) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if optional and error.code == 404:
                return None
            if attempt == 3 or error.code not in (403, 429, 500, 502, 503, 504):
                raise
        except (OSError, TimeoutError):
            if attempt == 3:
                raise
        time.sleep(attempt + 1)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cutoff', required=True, help='Inclusive UTC publication cutoff (ISO 8601)')
    args = parser.parse_args()
    inventory_dir = ROOT / 'compat/upstream'
    inventory_dir.mkdir(parents=True, exist_ok=True)
    existing_path = ROOT / 'compat/catalog.lock.json'
    existing = json.loads(existing_path.read_text()) if existing_path.exists() else {}
    old = {row['version']: row for row in existing.get('versions', [])}
    releases = []
    pages = []
    for page in range(1, 30):
        url = API + f'/releases?per_page=100&page={page}'
        batch = json.loads(get(url))
        rows = [{k: row[k] for k in ('tag_name', 'html_url', 'published_at', 'draft', 'prerelease')} for row in batch]
        raw = (json.dumps(rows, sort_keys=True, separators=(',', ':')) + '\n').encode()
        local = f'compat/upstream/releases-{page}.json'
        (ROOT / local).write_bytes(raw)
        pages.append({'url': url, 'sha256': digest(raw), 'file': local, 'projection': 'release identity and publication metadata'})
        releases.extend(row for row in rows if re.fullmatch(r'v\d+\.\d+\.\d+', row['tag_name']) and not row['draft'] and not row['prerelease'] and row['published_at'] <= args.cutoff)
        if len(batch) < 100:
            break
    newest = max(tuple(map(int, row['tag_name'][1:].split('.'))) for row in releases)
    majors = [newest[0] - 1, newest[0]]
    release_by_tag = {row['tag_name']: row for row in releases}
    refs = {}
    tag_sources = []
    for major in majors:
        url = API + f'/git/matching-refs/tags/v{major}.'
        raw = get(url)
        local = f'compat/upstream/tag-refs-v{major}.json'
        (ROOT / local).write_bytes(raw)
        tag_sources.append({'url': url, 'sha256': digest(raw), 'file': local, 'type': 'refs'})
        for ref in json.loads(raw):
            refs[ref['ref'].split('/')[-1]] = ref['object']
    # Some final numeric tags have no GitHub Release object. They still contain
    # historical API content and must not disappear from the compatibility window.
    selected = sorted((tag for tag in refs if re.fullmatch(r'v\d+\.\d+\.\d+', tag)
                       and tuple(map(int, tag[1:].split('.'))) <= newest),
                      key=lambda tag: tuple(map(int, tag[1:].split('.'))))
    commits = {}
    for tag in selected:
        obj = refs[tag]
        while obj['type'] == 'tag':
            raw = get(obj['url'])
            local = f"compat/upstream/tag-object-{obj['sha']}.json"
            (ROOT / local).write_bytes(raw)
            tag_sources.append({'url': obj['url'], 'sha256': digest(raw), 'file': local, 'type': 'tag'})
            obj = json.loads(raw)['object']
        if obj['type'] != 'commit':
            raise ValueError('Tag does not resolve to a commit: ' + tag)
        commits[tag] = obj['sha']
    print(json.dumps({'versions': len(selected), 'majors': majors, 'latest': newest,
                      'tagOnly': [tag for tag in selected if tag not in release_by_tag]}), flush=True)

    def collect(tag):
        version = tag[1:]
        commit = commits[tag]
        release = release_by_tag.get(tag)
        metadata = {'releaseURL': release['html_url'] if release else None,
                    'publishedAt': release['published_at'] if release else None,
                    'tagURL': f'https://github.com/{REPO}/tree/{tag}',
                    'inventoryStatus': 'release-and-tag' if release else 'tag-only'}
        if version in old and old[version].get('assetStatus') == 'raw-collected' and old[version]['upstreamCommit'] == commit:
            return {**old[version], **metadata}
        sources = []
        for path in ['manifests/crds/application-crd.yaml', 'manifests/crds/appproject-crd.yaml', 'manifests/crds/applicationset-crd.yaml', 'LICENSE']:
            # ApplicationSet was a separate upstream project before it was
            # bundled in Argo CD. Never fill an absent CRD with a future schema.
            data = get(f'{RAW}/{commit}/{path}', optional=path.endswith('applicationset-crd.yaml'))
            if data is None:
                continue
            hashed = digest(data)
            local = 'schemas/' + hashed
            (ROOT / local).write_bytes(data)
            sources.append({'path': path, 'url': f'{RAW}/{commit}/{path}', 'sha256': hashed, 'localPath': local})
        # The local compatibility mode accepts a single-source ref when some
        # covered release accepts it. Exact old releases retain the old rule.
        return {'version': version, **metadata, 'assetStatus': 'raw-collected', 'semanticsVerified': False, 'ruleset': 'argocd-local-static-1', 'upstreamCommit': commit, 'sources': sources, 'localRules': {'contract': 'argocd-local-static-1', 'singleSourceRefs': tuple(map(int, version.split('.'))) >= (3, 5, 0)}}

    entries = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        for entry in pool.map(collect, selected):
            entries.append(entry)
            if len(entries) % 25 == 0:
                print(json.dumps({'collected': len(entries), 'of': len(selected)}), flush=True)
    lock = {'formatVersion': 1, 'product': 'argo-cd', 'cutoff': args.cutoff, 'supportPolicy': 'all stable numeric tags in the latest two majors through the latest stable release; default auto validates a whole known CRD variant', 'supportedMajors': majors, 'latestVersion': '.'.join(map(str, newest)), 'defaultVersion': 'auto', 'ready': False, 'inventorySources': pages, 'tagSources': tag_sources, 'versions': entries}
    existing_path.write_text(json.dumps(lock, indent=2) + '\n')
    print(json.dumps({'versions': len(entries), 'sources': len({source['sha256'] for entry in entries for source in entry['sources']})}), flush=True)


if __name__ == '__main__':
    main()
