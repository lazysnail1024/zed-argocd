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
| [compat/rule-evolution.json](../compat/rule-evolution.json) and [compat/semantic-sources](../compat/semantic-sources/) | Reviewed model inventory, sampled rule boundaries, exact function/caller source identities and behavior scenario IDs |
| [internal/assets/compatibility.json](../internal/assets/compatibility.json) | Generated whole-branch release sets, finite modeled-rule profiles and explicit source-evidence coverage |
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

## Explicit compatibility explanation

`argocd-ls explain-compatibility --root . --file app.yaml` emits a read-only JSON
report. The equivalent editor source action is **Explain Argo CD compatibility
candidates**. `--target-version 3.4.13` identifies the intended pin and explains
its membership; it does not limit the candidate universe to that release.

Each resource has independent `schemaCandidates`, `semanticCandidates` and
`candidates` lists, grouped Schema/local-rule exclusions with source ranges,
`coveredRuleIds`, `modeledRuleCoverage`, dynamic `unknowns` and `selectedTarget`.
The report includes buffer/disk and configuration identity, the owning root and
scope, catalog and evidence hashes, and `scopeSemantics: "independent-resources"`.
Separate resources in a YAML stream have separate results; neither a file nor a
workspace is silently assumed to share one deployment target.

The label is **candidates under modeled static constraints**. `complete` means
the bounded explanation covered the input; it does not mean dynamic or unmodeled
controller behavior was proved. `notEvaluated` includes controller/admission
behavior, generator expansion, source hydration, cross-resource Project policy,
repository existence and dynamic effects. AppProject currently has Schema-only
document coverage in this interface; policy needs referencing resources and is
explicitly omitted. Raw Helm templates require an explicit render first.

For example, an Application with a sole `sources` entry containing both
`ref: bad.ref` and `tagPrefix: component/` has Schema candidates from 3.5.0, but
that same version boundary activates sole-ref validation and rejects the dot in
the ref name. The two nonempty sets have no joint candidate. This is an
explanation of existing modeled constraints, not a change to the ordinary checker.
Fields removed from historical schemas, such as `source.ksonnet` at 2.4.0, remain
independent from later additions such as `syncPolicy.automated.enabled` at 3.1.0.

The explanation validates the complete projected resource against each compiled
normalized auto branch once: 25 Application, 22 ApplicationSet or 12 AppProject
branches. It uses compact release bitsets and reuses the existing `Domain` rules
with cross-resource checks withheld. Application has two modeled-rule profiles.
ApplicationSet has three because static `templatePatch` deletion checks also read
the exact Schema's required fields: source is required through 2.5 and optional
from 2.6; the sole-source ref gate changes again at 3.5. AppProject has one profile.
Profiles group current product-rule inputs, not proven equivalent upstream
controllers. Immutable compiled schemas are reused; source snapshots and results
are not retained by the explanation cache.

The normalized whole-schema criterion can exclude a target whose ordinary exact
check emits only an unknown-field warning. Neither that warning policy nor
automatic mode's existing success/failure semantics changes. Use `check` for its
established validation exit status. `explain-compatibility` exits 0 when it computed
the explanation, including an empty candidate set; 2 means invalid or incomplete
input/output, and 3 means cancellation or deadline. At most 32 resources are
explained per request; omitted resources and truncated reason details are marked.
The five-second request and existing input/projection budgets still apply.

### Source evidence and maintenance

The 370-release Schema inventory and the historical 88-release local-rule source
manifest are different sets. Additional pinned GetRefSources/GetRepoObjs samples
cover 3.4.11/3.4.13, 3.5.0 and 3.6.1; they do not establish a full 370-release
controller audit. Reports expose the historical version list, boundary sample
versions and `allCatalogSemanticsAudited: false` independently from the modeled
candidate lists. The sampled sole-source ref introduction is 3.5.0. No removal or
earlier-line backport is demonstrated by these samples; that is not an exhaustive
absence claim. Source function and caller hashes remain separate from CRD hashes.

Run `python3 tools/compatibility_metadata.py --check` to verify exact retained
source/Git blob bytes, function and caller hashes, historical-manifest linkage,
reviewed model pin identities and reproducible branch/profile metadata. Its JSON
report shows semantic-source changes separately from Schema changes, even when
schemas are equal. Adding or changing a `[version, upstreamCommit]` pin first
requires review of `rule-evolution.json`; regeneration rejects an unreviewed pin
even if its schemas and boolean rule flags are unchanged. The reviewed model
inventory digest records that decision and does not claim an exhaustive source
audit. Update introduction/removal/backport intervals, caller conditions, evidence
coverage and behavioral scenario IDs when new evidence warrants it.

## Scope of support

Coverage means collected CRDs, editor schema queries and implemented local static checks. It does not establish controller execution, Kubernetes admission, remote rendering, dynamic ApplicationSet expression evaluation or complete runtime policy behavior. Reports retain unknown behavior through diagnostics and `notEvaluated` entries.

`semanticsVerified` is a readiness field for the local verification contract. It does not mean every controller behavior was reproduced for every release. The controller-source domain matrix and the catalog-wide CRD/query matrix are separate checks. Workspace completeness and edit authority remain subject to the [capability limits](../README.md#capabilities-in-the-current-source).

## Updating the catalog

Use the Go/C toolchain specified by the repository and Python with PyYAML. From the repository root, set an explicit UTC collection cutoff. To reproduce the current snapshot:

```sh
catalog_cutoff=2026-11-05T23:59:59Z
python3 tools/collect_catalog.py --cutoff "$catalog_cutoff"
python3 tools/normalize_schemas.py
python3 tools/compatibility_metadata.py --check
go test -mod=vendor ./internal/assets ./internal/analysis
go build -mod=vendor -o build/argocd-ls ./cmd/argocd-ls
python3 tests/multi_version_e2e.py build/argocd-ls artifacts/catalog-bootstrap --bootstrap
python3 tools/normalize_schemas.py --accept-report artifacts/catalog-bootstrap/report.json
go build -mod=vendor -o build/argocd-ls ./cmd/argocd-ls
python3 tests/multi_version_e2e.py build/argocd-ls artifacts/catalog-final
```

Use fresh output directories for each verification. Collection clears readiness. Only a passing bootstrap report covering every exact tag with matching digests can restore it. Rebuild afterward, then verify the complete public report and configuration-free LSP behavior. Schema changes invalidate earlier verification markers.

Before normalizing a changed release inventory, perform the semantic-source review
above. Normalization regenerates the explanation sidecar after writing the catalog
and rejects missing model review; it does not infer source equivalence from CRD
equality. The existing historical manifest and exact schema bytes stay immutable.

| Verification entry | Scope |
| --- | --- |
| [multi_version_e2e.py](../tests/multi_version_e2e.py) | Every exact tag, inventory, automatic mode and LSP; Linux CI uses the complete catalog |
| The same entry with `--representatives` | Other native platforms select representatives by distinct schema bundle, retaining inventory, automatic mode and LSP checks |
| [multi_version_test.go](../internal/analysis/multi_version_test.go) | Automatic queries for declared fields |
| [appset_overrides_e2e.py](../tests/appset_overrides_e2e.py) with `--all-targets` | Groups eligible targets by exact ApplicationSet schema hash and complete local-rule set; runs deep scenarios once per group and automatic mode separately; records every covered version |
| [ablate_multi_version.py](../tests/ablate_multi_version.py) | Compiles controlled variants that remove historical branches, object boundaries, shared definitions or query branch selection |
| [compatibility_e2e.py](../tests/compatibility_e2e.py) and [ablate_compatibility_intersection.py](../tests/ablate_compatibility_intersection.py) | Whole-resource CLI/LSP candidates, actual empty intersection, dynamic coverage, source maintenance, frozen ordinary-check reports and a compiled removal of the intersection |

Grouped deep ApplicationSet scenarios and the full-tag compatibility matrix establish different boundaries. See the commands in [native_ci.py](../tests/native_ci.py) and [native.yml](../.github/workflows/native.yml), with the complete execution policy in [test contracts](../tests/README.md).
