# Version coverage and automatic compatibility

The native server defaults to `auto`. A user does not need an editor setting or
`.argocd-ls.yaml` merely to obtain version coverage. An explicitly configured exact
version still selects that version's collected schema and local static rules.

## Collected window

The checked-in inventory covers every numeric stable tag from **2.0.0 through
3.6.1**, across majors 2 and 3: **370 tags**, containing **58 distinct schemas** for
Application, ApplicationSet and AppProject. Release metadata was captured at the
2026-11-05 cutoff. There are 364 matching GitHub Release records and six additional
numeric tags without a corresponding Release record in the captured API pages:

- 2.1.13
- 3.0.7, 3.0.8, 3.0.9 and 3.0.10
- 3.4.0

These six tags are included. Absence of Release metadata is not interpreted as
proof of why a release is missing. RC and other prerelease suffixes are excluded.
The official Release list determines the latest stable release and major window;
the official tag list supplies the complete numeric-tag inventory through that
release. Tags are resolved through annotated tag objects to immutable commits.
Every source CRD, normalized schema, inventory response and tag object has a
recorded SHA-256 digest. `compat/catalog.lock.json` retains the complete provenance;
`internal/assets/catalog.json` contains only the runtime projection.

ApplicationSet was a separate upstream project before it was bundled in Argo CD.
The earlier Argo CD tags that do not contain its CRD are recorded without that
schema. We do not silently substitute a newer ApplicationSet schema into an older
exact target. Automatic mode includes every ApplicationSet CRD present in the
covered Argo CD tags. This catalog is not a separate version catalog for the old
standalone ApplicationSet project.

## What automatic mode means

Argo CD manifests retain `argoproj.io/v1alpha1` across multiple product versions,
so the document alone does not identify the installed controller release.
Automatic mode checks whether a document satisfies a complete schema variant
from at least one covered release. Completion and hover use the whole release
branches compatible with the document's existing field names. An unconstrained
document can discover every covered field; an existing old `ksonnet` field narrows
suggestions so they do not introduce the incompatible newer `enabled` field, and
vice versa. The active field is excluded from that selection. Missing required
fields and ordinary value errors do not suppress useful suggestions while
editing. If existing names already match no complete branch, queries fall back
to the covered declarations so the document can still be repaired. Validation
continues to diagnose that incompatible document independently.

The validator uses a root `anyOf` of whole release schemas. It does not merge
individual fields into one permissive schema. For example, old ksonnet source
options and the newer `syncPolicy.automated.enabled` field are individually
supported; a combination that never occurs in a covered schema is diagnosed as
`SCHEMA_VERSION_COMBINATION`. Malformed known fields retain `SCHEMA_TYPE` and
missing mandatory values retain `SCHEMA_REQUIRED`.

Structured objects in automatic schemas have an explicit additional-property
boundary. Otherwise an older schema could accept a malformed newer field merely
because that older schema did not know its name. Kubernetes preserve-unknown
objects and genuinely open maps keep their declared extension behavior. Unknown
names outside every covered schema remain `SCHEMA_UNKNOWN_FIELD` warnings. The
original exact-version schemas are kept unchanged.

The generated compatibility schemas share repeated nodes using local `$defs`.
The three automatic assets total **854,610 bytes**. The server loads only the
resource kind it needs, and query traversal deduplicates shared compiled nodes.
It does not compile 370 independent schema sets at startup, fetch the network at
runtime, or maintain a second constraint interpreter for completion.

The identity response reports `defaultTargetVersion: "auto"` and
`supportedVersions: {min, max, majors, releaseCount}`. An automatic target does
not offer a pin action; explicit scoped pins remain available.

## Scope of the evidence

The coverage claim concerns the collected CRD declarations, editor schema
queries and the implemented local static checks. It does not claim that all
controller executions, remote Helm/Kustomize rendering, runtime admission rules,
Kubernetes versions or every combination of dynamic ApplicationSet expressions
have been executed. Existing dynamic-field and external-reference limitations
remain visible in diagnostics and the report's `notEvaluated` entries.

`semanticsVerified` is the existing catalog readiness field: it records passage
of this local contract, not an assertion of complete controller equivalence.
The historical deeper controller-source audit remains in
`compat/local-rules-sources.json`; its domain matrix selects only versions with
that source evidence. The broader current catalog is exercised by the new
independent CRD and editor-query gates.

## Refresh and acceptance

Run from the repository root with Python/PyYAML and the project's Go toolchain:

```sh
python3 tools/collect_catalog.py --cutoff 2026-11-05T23:59:59Z
python3 tools/normalize_schemas.py
go test -mod=vendor ./internal/assets ./internal/analysis
go build -mod=vendor -o build/argocd-ls ./cmd/argocd-ls
python3 tests/multi_version_e2e.py build/argocd-ls artifacts/bootstrap --bootstrap
python3 tools/normalize_schemas.py --accept-report artifacts/bootstrap/report.json
go build -mod=vendor -o build/argocd-ls ./cmd/argocd-ls
python3 tests/multi_version_e2e.py build/argocd-ls artifacts/multi-version
```

Use a fresh output directory for each acceptance run. Collection deliberately
clears readiness. Acceptance requires a passing bootstrap report with every exact
tag and the matching catalog digest. The final gate verifies the public complete
report and zero-configuration LSP behavior after readiness is enabled. A changed
schema loses its prior validation marker.

The default E2E runs every exact tag. `--representatives` selects one target per
distinct schema bundle for additional operating systems while retaining the
inventory, automatic-mode and LSP checks. Linux should run the complete inventory.
There is no `--all-targets` option for this new runner. The older AppSet override
runner has its own `--all-targets` option, selecting releases that actually contain
the `templatePatch` and `goTemplate` fields its fixtures exercise. Its deeper
override scenarios run once per distinct ApplicationSet schema hash and complete
`localRules` object, plus `auto` independently. The current 180 eligible exact
targets form 17 groups, so all groups and automatic mode require 198 scenarios.
The report records every group's representative and covered versions and checks
that no eligible target was omitted or duplicated. The separate compatibility
matrix continues to execute every exact tag.

## Verification and ablation

Local checks completed for this change:

| Check | Result |
| --- | --- |
| Official release/tag and immutable asset inventory | 370 stable tags; all digests and commit identities match |
| Exact-target and default real CLI/LSP matrix | 374 rows pass, including every exact tag |
| Compiled constraint traversal | 170,411 nodes across all 58 distinct source schemas pass |
| Automatic field queries | 10,516 field declarations at 2,113 distinct paths pass |
| Automatic whole-version LSP selection | 12 positive/negative scenarios pass, including active-field edits, arrays and incomplete documents |
| Existing readiness and identity | 21 cases pass |
| Existing strict-target schema queries | 100 cases pass |
| AppSet overrides across all schema/rule groups and automatic mode | 198 cases pass; 17 groups cover all 180 eligible exact targets |
| Explicit and default pin behavior | 3 scenarios pass |

Run the isolated counterfactual builds with:

```sh
python3 tests/ablate_multi_version.py build/argocd-ls artifacts/multi-version-ablation
```

| Removed feature | Observed consequence | Decision |
| --- | --- | --- |
| Structured object boundaries | Both old and new wrong-type fields and the impossible combination are accepted | Retain |
| Historical whole-schema branches, keeping only the newest schema | A valid old ksonnet field becomes unknown; historical type checking is lost | Retain |
| Shared local definitions, inlining every branch instead | Application probes still pass; automatic JSON grows from 854,610 to 8,991,596 bytes, and the three-kind CLI fixture exceeds 30 seconds | Retain |
| Whole-release selection for editor queries | The validator still rejects incompatible combinations, but completion again suggests `enabled` beside old `ksonnet` and `ksonnet` beside new `enabled` | Retain |
| Separate legacy `collect_remaining.py` pipeline | The unified collector already discovers and resolves the complete rolling window | Delete |

The final baseline three-kind cold CLI sample had a median of 179.65 ms over five
runs on this Linux host. The inline-definition mutant timed out at 30 seconds.
These observations justify keeping the shared data representation; they are not
portable benchmark guarantees. Full stdout, commands, reports and mutation build
logs are retained under the artifact output directories. A compact evidence
manifest is kept in `compat/multi-version-verification.json`.

The complete local Linux release-mode native gate at `374d214` finished with
30 of 31 steps passing, including all eight regression mutations. Its original
AppSet override step timed out at 480.047 seconds after 1,880 of 1,991 CLI
executions. Repeating the same deep override cases for identical patch schemas
and local-rule contracts provided no additional combination coverage. Grouping
those inputs preserves all 11 scenario assertions and the separate 370-target
compatibility gate. The corrected 198-scenario run passed using the same release
binary; an independent coverage audit verified all 180 eligible exact targets,
all 17 distinct combinations and the separate automatic-mode execution. The
observed span from the first fixture write to the final report was 51.912 seconds,
excluding initial catalog loading. The initial timeout report is retained as a
failure, alongside the passing targeted follow-up; no timeout budget was raised.
