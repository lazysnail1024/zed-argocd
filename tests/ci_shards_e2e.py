#!/usr/bin/env python3
"""Real gate CLI positive/negative controls; synthetic reports are not native proof."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from native_ci import BUILD_ROLES, COMPILER_CASES, PLATFORM_COMPILERS, SUITES, inventory_digest, required_steps

ROOT = Path(__file__).resolve().parents[1]


def fixture_source(output):
    fixture = output / "synthetic-source"
    fixture.mkdir()
    names = {spec["runner"] for spec in json.loads((ROOT/"tests/child_contracts.json").read_text())["consumers"].values() if "runner" in spec}
    names.update(("tests/child_evidence.py", "tools/ci_gate.py", "tests/native_ci.py", "go.mod", "VERSION", ".gitignore", "tests/inventory.json", "tests/child_contracts.json"))
    for name in sorted(names):
        target = fixture / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    environment = dict(os.environ, GIT_AUTHOR_NAME="Synthetic CI gate fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                       GIT_COMMITTER_NAME="Synthetic CI gate fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid",
                       GIT_AUTHOR_DATE="2000-01-01T00:00:00Z", GIT_COMMITTER_DATE="2000-01-01T00:00:00Z")
    for command in (["git", "init"], ["git", "-c", "core.autocrlf=false", "add", "."],
                    ["git", "-c", "commit.gpgsign=false", "commit", "-m", "Synthetic gate controls only"]):
        subprocess.run(command, cwd=fixture, env=environment, check=True, capture_output=True, timeout=15)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=fixture, text=True).strip()
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=fixture, text=True).strip()
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=fixture).decode().split("\0")
    source_hash = inventory_digest({name: hashlib.sha256((fixture / name).read_bytes()).hexdigest() for name in names if name})
    return fixture, head, tree, source_hash


def child_fixture(spec):
    def row(item):
        result=copy.deepcopy(item['identity'])
        if 'passKey' in item:result[item['passKey']]=True
        for key in ('checks','assertions'):
            if key not in item:continue
            desc=item[key]
            if 'keys' in desc:result[key]={k:True for k in desc['keys']}
            elif 'rows' in desc:result[key]=[row(x) for x in desc['rows']]
            else:result[key]=copy.deepcopy(desc['literal'])
        for key in item.get('empty',[]):result[key]=[] if key=='errors' else None
        for path,value in item.get('required',{}).items():
            parts=path.split('.');destination=result
            for key in parts[:-1]:destination=destination.setdefault(key,{})
            destination[parts[-1]]=copy.deepcopy(value)
        return result
    values=[row(x) for x in spec['rows']]
    result=values[0] if spec['rootRow'] else ({spec['rowsKey']:values} if spec['rowsKey'] else values)
    if isinstance(result,dict):
        if 'passKey' in spec:result[spec['passKey']]=True
        if 'runnerKey' in spec:result[spec['runnerKey']]=hashlib.sha256((ROOT/spec['runner']).read_bytes()).hexdigest()
        if 'binaryKey' in spec:result[spec['binaryKey']]='c'*64
    if spec.get('applicability'):result['applicability']={'symlinks':True,'fifo':True,'socket':True}
    if spec.get('performance'):
        for item in result['rows']:
            item['samplesMs']=[1]*30
            item['traces']=[{'fullScan':False,'diskReads':0,'parsedFiles':1,'domainDocuments':2,'repositoryDecodes':0,'acquisitionFallbacks':0} for _ in range(30)]
    definitions=json.loads((ROOT/'tests/child_contracts.json').read_text())['consumers']
    if spec.get('kind')=='audit-mutations':
        from audit_regression_mutations import MUTATIONS
        result['completed']=True
        for item,mutation in zip(result['rows'],MUTATIONS):
            failed={'id':mutation['expectedFailure'],'passed':False,'checks':{**{k:False for k in mutation['failureChecks']},**{k:True for k in mutation['requiredChecks']}}}
            extra=[{'id':ident,'passed':False,'checks':{k:False for k in keys}} for ident,keys in mutation.get('additionalFailures',{}).items()]
            item['oracle']['report']={'passed':False,'rows':[failed,*extra,*[{'id':ident,'passed':True} for ident in mutation['positiveCases']]]}
    if spec.get('kind')=='parent-faults':
        result['completed']=True;result['control']={'exit':0,'report':child_fixture(definitions['parent-workspace'])}
        required=['background-failure-was-exercised','zero-config-unsaved-buffer-type-error','local-diagnostics-precede-background-failure','local-schema-hover-survives-background-failure','local-field-completion-survives-background-failure','background-failure-cannot-claim-complete','partial-scope-refuses-exhaustive-references','permanent-deadline-still-allows-valid-local-buffer','clean-server-exit']
        deadline=copy.deepcopy(definitions['parent-workspace'])
        deadline['rows']=deadline['rows'][:1]
        deadline['rows'][0]['identity']={'id':'six-repositories-deadline'}
        for check in deadline['rows'][0]['checks']['rows']:
            if check['identity']['id']=='cross-repository-reference-analysis-recovers-without-restart':check['identity']['id']='permanent-deadline-still-allows-valid-local-buffer'
        positive=child_fixture(deadline);positive['timeoutBuildRequired']=True
        result['rows'][0]['oracle']={'exit':0,'report':positive}
        result['rows'][1]['oracle']={'exit':1,'report':{'passed':False,'rows':[{'id':'six-repositories-deadline','passed':False,'checks':[{'id':k,'passed':True} for k in required]+[{'id':'background-failure-cannot-clear-local-type-error','passed':False}]}]}}
    return result


def companion_fixture(spec):
    definitions=json.loads((ROOT/'tests/child_contracts.json').read_text())['consumers']
    if spec.get('kind')=='project-rename-ablation':
        return {'lsp/report.json':{'rows':[{'id':'duplicate','checks':{'refusedWithoutEdits':False,'cleanExit':True},'renamed':{'result':{'documentChanges':[{}]*4}}}]}}
    if spec.get('kind')=='shared-schemaquery-ablation':
        return {'lsp/report.json':{'rows':[{'id':name,'checks':{'fourEnumActions':False,'sharedCompletionCandidates':False,'constraintObserved':True}} for name in ['auto-enum-repair','3.5.6-enum-repair']]}}
    if spec.get('kind')=='document-budget-ablation':
        baseline=child_fixture(definitions['projection-aggregate']);mutant=copy.deepcopy(baseline)
        row=next(r for r in mutant['rows'] if r['id']=='False-publication-lifecycle');row['checks']['noFalseBudgetInAnyPhase']=False;row['observed']={'unexpectedBudget':[{'message':{'params':{'x-argocd-analysis-scope':'document'}}}]}
        return {'baseline/report.json':baseline,'mutant/report.json':mutant}
    return {}


def fixtures(head, tree, source_hash):
    version = (ROOT / "VERSION").read_text().strip()
    go_version = "go" + next(line.split()[1] for line in (ROOT / "go.mod").read_text().splitlines() if line.startswith("go "))
    runner = hashlib.sha256((ROOT / "tests/native_ci.py").read_bytes()).hexdigest()
    reports, assets = {}, {}
    catalog=json.loads((ROOT/"tests/child_contracts.json").read_text())
    for platform, compiler in PLATFORM_COMPILERS.items():
        host, architecture = platform.split("/")
        contracts=catalog["consumers"].copy()
        for name,variants in catalog.get("platforms",{}).items():
            if platform in variants:contracts[name]=variants[platform]
        for suite in SUITES:
            identity = {"product": "argo-cd", "dirty": False, "sourceCommit": head, "buildMode": "release", "serverVersion": version}
            if suite == "provenance":
                identity.update(buildMode="development", serverVersion=version + "-dev")
            if suite == "compiler-environment":
                identity.update(sourceCommit="b" * 40, serverVersion="0.1.0")
            build = {"platform": platform, "identity": identity, "binarySHA256": "c" * 64, "sourceInputsSHA256": source_hash,
                     "goEnvironment": {"GOVERSION": go_version, "GOHOSTOS": host, "GOHOSTARCH": architecture, "GOOS": host, "GOARCH": architecture},
                     "compiler": compiler, "toolSHA256": {"go": "d" * 64, "cc": "e" * 64}}
            steps = required_steps(platform, suite)
            report = {"schemaVersion": 2, "buildRole": BUILD_ROLES[suite], "contractDigest":hashlib.sha256((ROOT/"tests/inventory.json").read_bytes()).hexdigest(), "suite": suite, "expectedNativePlatform": platform, "releaseVersion": version,
                      "workflowRun": {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}, "runnerSHA256": runner,
                      "expectedProvenance": {"sourceCommit": head, "sourceTree": tree, "dirty": False, "buildMode": "release"},
                      "initialBuildCachesEmpty": True, "host": {"linux": "linux", "darwin": "darwin", "windows": "win32"}[host],
                      "completed": True, "passed": True, "activeCommand": None, "expectedSteps": steps,
                      "rows": [{"id": step, "passed": True, "exit": 0, "cleanupError": None} for step in steps],
                      "buildEvidence": [copy.deepcopy(build) for _ in range(5 if suite == "compiler-environment" else 1)], "securityCases": []}
            report['fixtureChildren']={step:child_fixture(contracts[step]) for step in steps if step in contracts}
            report['childEvidence']={step:{'path':'child-evidence/'+step+'.json'} for step in report['fixtureChildren']}
            report['fixtureCompanions']={step:companion_fixture(contracts[step]) for step in report['fixtureChildren']}
            if suite == "compiler-environment":
                checks = {name: True for name in ("buildExit0", "initialBuildCacheEmpty", "nativeExit0", "identity", "binary", "sourceInputs", "noMarker", "packageExit0", "packagedBinary")}
                report["securityCases"] = [{"id": name, "passed": True, "checks": checks.copy(), "observed": {
                    "initialBuildCacheEmpty": True, "cachePath": "/synthetic/environment/" + name + "-cache", "markerBeforeNative": False,
                    "markerAfterNative": False, "nativeSHA256": build["binarySHA256"]}} for name in COMPILER_CASES]
            reports["argocd-acceptance-" + platform.replace("/", "-") + "-" + suite + "-123-1"] = report
        assets[platform] = {"platform": platform, "sourceCommit": head, "version": version, "package": {"binarySHA256": "c" * 64}}
    return reports, assets


def invoke(fixture, name, reports, assets, head, expected):
    folder = fixture / "artifacts" / name
    folder.mkdir(parents=True)
    for artifact, report in reports.items():
        path = folder / "reports" / artifact / "report.json"
        path.parent.mkdir(parents=True)
        report=copy.deepcopy(report)
        companions=report.pop('fixtureCompanions',{})
        for step,raw in report.pop('fixtureChildren',{}).items():
            entry=report.get('childEvidence',{}).get(step)
            if not isinstance(entry,dict) or 'path' not in entry:continue
            child=path.parent/entry['path'];child.parent.mkdir(parents=True,exist_ok=True)
            child.write_text(json.dumps(raw),encoding='utf-8')
            entry['sha256']=hashlib.sha256(child.read_bytes()).hexdigest()
            entry['companions']={}
            for index,(relative,body) in enumerate(companions.get(step,{}).items()):
                nested=path.parent/'child-evidence'/(step+'-child-'+str(index)+'.json');nested.write_text(json.dumps(body),encoding='utf-8')
                entry['companions'][relative]={'path':'child-evidence/'+nested.name,'sha256':hashlib.sha256(nested.read_bytes()).hexdigest()}
        path.write_text(json.dumps(report), encoding="utf-8")
    for platform, record in assets.items():
        path = folder / "assets" / platform.replace("/", "-") / ("release-" + platform.replace("/", "-") + ".json")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(record), encoding="utf-8")
    command = [sys.executable, str(fixture / "tools/ci_gate.py"), "--inputs", str(folder / "reports"), "--assets", str(folder / "assets"),
               "--source-commit", head, "--version", (ROOT / "VERSION").read_text().strip(), "--run-id", "123", "--run-attempt", "2" if name == "previous-successful-attempt" else "1",
               "--jobs-result", "failure" if name == "failed-job" else "success", "--output", str(folder / "accepted.json")]
    result = subprocess.run(command, cwd=fixture, capture_output=True, timeout=15)
    (folder / "stdout").write_bytes(result.stdout)
    (folder / "stderr").write_bytes(result.stderr)
    checks = {"exit": result.returncode == (0 if expected else 1), "acceptedFile": (folder / "accepted.json").exists() == expected}
    if expected:
        accepted = json.loads((folder / "accepted.json").read_text())
        checks["allNativeLanes"] = len(accepted["acceptedLanes"]) == 15 and len(accepted["nativePlatforms"]) == 5
    else:
        checks["structuredFailure"] = bool(result.stderr) and json.loads(result.stderr).get("passed") is False
    return {"id": name, "command": command, "exit": result.returncode, "checks": checks, "passed": all(checks.values())}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    try:
        fixture, head, tree, source_hash = fixture_source(output)
        original, original_assets = fixtures(head, tree, source_hash)
        cases = ("complete", "missing-child-evidence", "empty-child-evidence", "false-child-assertion", "missing-child-case", "duplicate-child-case", "empty-child-assertions", "missing-child-assertion", "truthy-child-assertion", "unexpected-child-case", "wrong-child-binary", "previous-successful-attempt", "boolean-schema", "failed-job", "missing-lane", "duplicate-lane", "wrong-platform", "wrong-suite", "wrong-source", "wrong-tree",
                 "wrong-contract", "wrong-mode", "wrong-role", "unknown-schema", "wrong-version", "wrong-runner", "wrong-run", "wrong-attempt", "warm-cache", "wrong-host", "incomplete", "active-command",
                 "row-failure", "row-boolean-exit", "cleanup-error", "missing-step", "duplicate-step", "self-approved-step-omission",
                 "missing-build", "wrong-inventory", "cross-compiled", "different-compiler", "different-tool", "missing-compiler-case",
                 "failed-compiler-check", "missing-compiler-check", "warm-compiler-cache", "compiler-marker", "compiler-binary-mismatch",
                 "synthetic-is-real-source", "missing-release", "wrong-release-source", "wrong-release-binary")
        key = "argocd-acceptance-linux-amd64-consumers-123-1"
        compiler_key = "argocd-acceptance-windows-amd64-compiler-environment-123-1"
        for name in cases:
            reports, assets = copy.deepcopy(original), copy.deepcopy(original_assets)
            target, compiler = reports[key], reports[compiler_key]
            if name == "missing-child-evidence": target.pop("childEvidence", None)
            elif name == "empty-child-evidence": target["childEvidence"] = {}
            elif name == "false-child-assertion": target['fixtureChildren']['targets']['rows'][0]['checks']['exit']=False
            elif name == "missing-child-case": target['fixtureChildren']['targets']['rows'].pop()
            elif name == "duplicate-child-case": target['fixtureChildren']['targets']['rows'].append(copy.deepcopy(target['fixtureChildren']['targets']['rows'][0]))
            elif name == "empty-child-assertions": target['fixtureChildren']['targets']['rows'][0]['checks']={}
            elif name == "missing-child-assertion": target['fixtureChildren']['targets']['rows'][0]['checks'].pop('exit')
            elif name == "truthy-child-assertion": target['fixtureChildren']['targets']['rows'][0]['checks']['exit']=1
            elif name == "unexpected-child-case": target['fixtureChildren']['targets']['rows'][0]['case']['id']='unknown'
            elif name == "wrong-child-binary": target['fixtureChildren']['targets']['binarySHA256']='0'*64
            elif name == "missing-lane": reports.pop(key)
            elif name == "boolean-schema": target["schemaVersion"] = True
            elif name == "duplicate-lane": reports[key + "-duplicate"] = copy.deepcopy(target)
            elif name == "wrong-platform": target["expectedNativePlatform"] = "linux/arm64"
            elif name == "wrong-suite": target["suite"] = "all"
            elif name == "wrong-source": target["expectedProvenance"]["sourceCommit"] = "0" * 40
            elif name == "wrong-tree": target["expectedProvenance"]["sourceTree"] = "0" * 40
            elif name == "wrong-contract": target["contractDigest"] = "0"*64
            elif name == "unknown-schema": target["schemaVersion"] = 999
            elif name == "wrong-role": target["buildRole"] = "mutation"
            elif name == "wrong-mode": target["buildEvidence"][0]["identity"]["buildMode"] = "development"
            elif name == "wrong-version": target["releaseVersion"] = "0.0.1"
            elif name == "wrong-runner": target["runnerSHA256"] = "0" * 64
            elif name == "wrong-run": target["workflowRun"]["GITHUB_RUN_ID"] = "124"
            elif name == "wrong-attempt": target["workflowRun"]["GITHUB_RUN_ATTEMPT"] = "2"
            elif name == "warm-cache": target["initialBuildCachesEmpty"] = False
            elif name == "wrong-host": target["host"] = "win32"
            elif name == "incomplete": target["completed"] = False
            elif name == "active-command": target["activeCommand"] = {"id": "build"}
            elif name == "row-failure": target["rows"][0]["exit"] = 7
            elif name == "row-boolean-exit": target["rows"][0]["exit"] = False
            elif name == "cleanup-error": target["rows"][0]["cleanupError"] = "child survived"
            elif name == "missing-step": target["rows"].pop()
            elif name == "duplicate-step": target["rows"].append(copy.deepcopy(target["rows"][-1]))
            elif name == "self-approved-step-omission": target["rows"].pop(); target["expectedSteps"].pop()
            elif name == "missing-build": target["buildEvidence"] = []
            elif name == "wrong-inventory": target["buildEvidence"][0]["sourceInputsSHA256"] = "0" * 64
            elif name == "cross-compiled": target["buildEvidence"][0]["goEnvironment"]["GOHOSTARCH"] = "arm64"
            elif name == "different-compiler": target["buildEvidence"][0]["compiler"] = "unapproved compiler"
            elif name == "different-tool": compiler["buildEvidence"][0]["toolSHA256"]["cc"] = "f" * 64
            elif name == "missing-compiler-case": compiler["securityCases"].pop()
            elif name == "failed-compiler-check": compiler["securityCases"][0]["checks"]["binary"] = False
            elif name == "missing-compiler-check": compiler["securityCases"][0]["checks"].pop("binary")
            elif name == "warm-compiler-cache": compiler["securityCases"][0]["observed"]["initialBuildCacheEmpty"] = False
            elif name == "compiler-marker": compiler["securityCases"][0]["observed"]["markerAfterNative"] = True
            elif name == "compiler-binary-mismatch": compiler["securityCases"][0]["observed"]["nativeSHA256"] = "0" * 64
            elif name == "synthetic-is-real-source": compiler["buildEvidence"][0]["identity"]["sourceCommit"] = head
            elif name == "missing-release": assets.pop("windows/amd64")
            elif name == "wrong-release-source": assets["windows/amd64"]["sourceCommit"] = "0" * 40
            elif name == "wrong-release-binary": assets["windows/amd64"]["package"]["binarySHA256"] = "0" * 64
            rows.append(invoke(fixture, name, reports, assets, head, name == "complete"))
        checks = {platform: len(required_steps(platform, "all")) == len(set(required_steps(platform, "all"))) and
                  set(required_steps(platform, "all")) == {step for suite in SUITES for step in required_steps(platform, suite)}
                  for platform in PLATFORM_COMPILERS}
        rows.append({"id": "suite-partition-preserves-every-step", "checks": checks, "passed": all(checks.values())})
    except Exception as error:
        rows.append({"id": "runner", "passed": False, "error": repr(error)})
    report = {"scope": "synthetic validator controls only; not native execution evidence", "rows": rows,
              "passed": len(rows) == 54 and all(row["passed"] for row in rows)}
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "rows": len(rows), "failed": [row["id"] for row in rows if not row["passed"]]}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
