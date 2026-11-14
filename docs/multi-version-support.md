# Version catalog and automatic compatibility

The default target is `auto`; no configuration file is required. An explicit exact target uses that version's collected CRDs and the implemented local static rules. See [configuration](configuration.md).

## Bundled offline catalog

The committed [catalog lock](../compat/catalog.lock.json) has a collection cutoff of **2026-11-05T23:59:59Z**. These figures describe that snapshot, not the latest upstream releases.

| Item | Coverage |
| --- | --- |
| Version window | Argo CD 2.0.0–3.6.1, majors 2 and 3 |
| Numeric stable tags | 370 |
| Application and AppProject | 370 targets each |
| ApplicationSet | 330 targets: 312 bundled CRDs and 18 pinned addon references |
| Distinct source schemas | 59 |

The collector uses official releases to determine the newest stable major and the preceding major at the cutoff, then uses tags to complete the numeric stable versions in that window. Prerelease suffixes are excluded. A numeric stable tag may be included without a matching release record. Annotated tags resolve to immutable commits; source responses, tag objects, CRDs and normalized schemas have recorded SHA-256 identities.

ApplicationSet coverage includes pinned standalone addon references in Argo CD 2.3.x: 2.3.0 references ApplicationSet v0.4.0, and the other collected 2.3.x versions reference v0.4.1. Installation references, commits and CRD digests remain in the source records. Exact targets without either a bundled CRD or a pinned installation reference do not borrow a newer schema. Automatic mode combines verified sources; it does not expose a separate standalone ApplicationSet version catalog.

## Catalog inputs

| Path | Purpose |
| --- | --- |
| [compat/catalog.lock.json](../compat/catalog.lock.json) | Versions, immutable tag/commit identities, CRDs, normalized assets and readiness |
| [compat/upstream](../compat/upstream/) | Collected upstream responses and tag objects |
| [compat/local-rules-sources.json](../compat/local-rules-sources.json) | Controller source references for implemented local rules and domain matrix selection |
| [schemas](../schemas/) | Collected and normalized inputs stored by content identity |
| [internal/assets/catalog.json](../internal/assets/catalog.json) | Runtime catalog embedded in the executable |
| [internal/assets/schemas](../internal/assets/schemas/) | Exact-target and automatic-compatibility schemas |
| [tools/collect_catalog.py](../tools/collect_catalog.py) and [tools/normalize_schemas.py](../tools/normalize_schemas.py) | Collection, schema generation, report acceptance and readiness generation |

These files are required build and verification inputs.

## Automatic mode

Argo CD product versions share `argoproj.io/v1alpha1`, so this API version cannot identify the installed controller version. Automatic validation requires a document to satisfy at least one **complete supported schema**, represented by a root `anyOf`. It does not merge unrelated version fields into a permissive union.

For example, older `ksonnet` fields and newer `syncPolicy.automated.enabled` fields are individually supported, but a combination that never existed in one supported version produces `SCHEMA_VERSION_COMBINATION`. Known field type errors remain `SCHEMA_TYPE`; missing required values remain `SCHEMA_REQUIRED`.

Automatic schemas enforce additional-field boundaries on structured objects. An older branch cannot silently accept invalid values within a newer field it does not recognize. Upstream preserve-unknown objects and open maps retain their extension behavior. Fields unknown to every branch produce a `SCHEMA_UNKNOWN_FIELD` warning. Exact-target upstream schemas are not rewritten by these automatic-mode rules.

Completion and hover select complete version branches using existing field names, excluding the field currently being edited. Missing required fields and ordinary value errors do not block repair suggestions. If existing names match no complete branch, queries fall back to the coverage declarations to support repair; validation still reports incompatibility. Queries and validation share compiled schemas. See [schema queries](../internal/analysis/schema_queries.go).

Generated schemas share repeated structures through local `$defs`. The runtime prepares schemas by resource kind and deduplicates traversal; it does not compile 370 separate schemas at startup or fetch schemas when opening a file. The identity response exposes `defaultTargetVersion: "auto"` and `supportedVersions: {min, max, majors, releaseCount}`. Automatic targets have no pin action; explicit scope targets retain verified pin actions.

## Scope of support

Coverage means collected CRDs, editor schema queries and implemented local static checks. It does not establish controller execution, Kubernetes admission, remote rendering, dynamic ApplicationSet expression evaluation or complete runtime policy behavior. Reports retain unknown behavior through diagnostics and `notEvaluated` entries.

`semanticsVerified` is a readiness field for the local verification contract. It does not mean every controller behavior was reproduced for every release. The controller-source domain matrix and the catalog-wide CRD/query matrix are separate checks. Workspace completeness and edit authority remain subject to the [capability limits](../README.md#capabilities-in-the-current-source).

## Updating the catalog

Use the Go/C toolchain specified by the repository and Python with PyYAML. From the repository root, set an explicit UTC collection cutoff. To reproduce the current snapshot:

```sh
catalog_cutoff=2026-11-05T23:59:59Z
python3 tools/collect_catalog.py --cutoff "$catalog_cutoff"
python3 tools/normalize_schemas.py
go test -mod=vendor ./internal/assets ./internal/analysis
go build -mod=vendor -o build/argocd-ls ./cmd/argocd-ls
python3 tests/multi_version_e2e.py build/argocd-ls artifacts/catalog-bootstrap --bootstrap
python3 tools/normalize_schemas.py --accept-report artifacts/catalog-bootstrap/report.json
go build -mod=vendor -o build/argocd-ls ./cmd/argocd-ls
python3 tests/multi_version_e2e.py build/argocd-ls artifacts/catalog-final
```

Use fresh output directories for each verification. Collection clears readiness. Only a passing bootstrap report covering every exact tag with matching digests can restore it. Rebuild afterward, then verify the complete public report and configuration-free LSP behavior. Schema changes invalidate earlier verification markers.

| Verification entry | Scope |
| --- | --- |
| [multi_version_e2e.py](../tests/multi_version_e2e.py) | Every exact tag, inventory, automatic mode and LSP; Linux CI uses the complete catalog |
| The same entry with `--representatives` | Other native platforms select representatives by distinct schema bundle, retaining inventory, automatic mode and LSP checks |
| [multi_version_test.go](../internal/analysis/multi_version_test.go) | Automatic queries for declared fields |
| [appset_overrides_e2e.py](../tests/appset_overrides_e2e.py) with `--all-targets` | Groups eligible targets by exact ApplicationSet schema hash and complete local-rule set; runs deep scenarios once per group and automatic mode separately; records every covered version |
| [ablate_multi_version.py](../tests/ablate_multi_version.py) | Compiles controlled variants that remove historical branches, object boundaries, shared definitions or query branch selection |

Grouped deep ApplicationSet scenarios and the full-tag compatibility matrix establish different boundaries. See the commands in [native_ci.py](../tests/native_ci.py) and [native.yml](../.github/workflows/native.yml), with the complete execution policy in [test contracts](../tests/README.md).
