# Test contracts and execution boundaries

The required tests exercise the current native executable, its CLI/stdio protocol,
packaged bytes, editor adapter and actual resource limits. The complete file-to-runner
map is [inventory.json](inventory.json). `python3 tests/test_inventory.py` discovers
first-party Python, Go and Rust tests and rejects missing, duplicate or obsolete
classifications, dead runner references, and a candidate gate that omits a required
boundary. Its negative control removes one real classification and requires rejection.
The inventory describes wiring; it does not replace execution evidence.

## Required coverage matrix

| Boundary | Executable entry | Independent dimensions and oracle |
| --- | --- | --- |
| Native portability | `native_ci.py`, five actual hosts in [native.yml](../.github/workflows/native.yml) | Linux amd64/arm64, macOS amd64/arm64 and Windows amd64 each build and execute real native CLI/LSP consumers. Filesystem, process cleanup, platform compiler, archive and executable behavior remain native tests. |
| Source and binary provenance | `native_package_e2e.py`, `native_environment_e2e.py`, `native_process_e2e.py`, `release_e2e.py`, `native_isolation.py` | Clean source identity, vendored input locks, compiler/environment contamination, extracted binary identity, original licenses and source inventories. Positive builds precede isolated corruptions. The candidate gate requires all fifteen platform/suite reports from the same workflow run and matches the release bytes to the tested binary. |
| CLI, configuration and readiness | `cli_e2e.py`, `target_e2e.py`, `readiness_e2e.py`, `config_uri_e2e.py`, `config_input_e2e.py`, `explain_terminal_e2e.py`, `workspace_e2e.py`, `audit_repair_e2e.py cli` | Configuration URI/target/scope selection, exact exits, completeness, diagnostic codes and terminal failures. An incomplete scope withholds an unprovable missing-project diagnosis. |
| Real catalog and domain semantics | `catalog_e2e.py`, `multi_version_e2e.py`, `domain_matrix_e2e.py`, `domain_matrix_lsp_e2e.py`, `appset_overrides_e2e.py`, `cd3_e2e.py`, `domain_e2e.py`, `appset_e2e.py` | Linux executes every exact catalog tag. Other native hosts use representatives. Deep AppSet cases group only identical schema/rule inputs; domain CLI selects behavior boundaries while LSP covers target transitions. The catalog integrity check is data-only and makes no native execution claim. |
| Explicit compatibility explanation | `compatibility_e2e.py`, `ablate_compatibility_intersection.py` | Independent whole-resource/introduced/removed-field and sole-ref boundaries; nonempty Schema but empty joint candidates; dynamic unknowns; unsaved LSP input and explicit messages. Frozen pre-feature reports compare ordinary auto/exact behavior. Source maintenance validates exact function/caller evidence separately from CRD identity and rejects a changed unaudited pin. A compiled ablation must fail the named CLI/LSP intersection cases while ordinary checks still pass. |
| Schema constraints and queries | Go `internal/assets` and `internal/analysis` tests; `schema_queries_e2e.py`, `shared_schemaquery_e2e.py`, `schema_fixture_e2e.py`, `schema_query_values_e2e.py`, `schema_query_comment_e2e.py` | Raw upstream JSON independently supplies type/required/enum expectations for compiled nodes. Real catalog queries and deliberately synthetic schema branches are separate. Every query profile must emit the exact ordered target × snippet-mode × case set, including shutdown. Synthetic value cases run against their own fixture binary. |
| Projection and source mapping | `alias_projection_cli_e2e.py`, `alias_projection_lsp_e2e.py`, `alias_provenance_edges_e2e.py`, `projection_repair_e2e.py`, `projection_aggregate_e2e.py`, `dependency_depth_e2e.py` | YAML alias/merge semantics, mapped locations, recovery, aggregate work accounting and actual depth-budget rejection followed by same-process recovery. |
| Workspace and lifecycle | `parent_workspace_lsp_e2e.py`, `mixed_parent_index_e2e.py`, `persistent_workspace_e2e.py`, `workspace_regressions_e2e.py`, `scope_glob_e2e.py`, `scope_refresh_e2e.py`, `repository_exclusion_e2e.py`, `reconciliation_e2e.py` | Root/scope ownership, exclusions, overlays, discovery, periodic repair and release of stale state. Larger Linux samples add workload scale without duplicating all semantic dimensions on every host. |
| LSP delivery and concurrency | `lsp_e2e.py`, `lsp_regressions_e2e.py`, `publication_e2e.py`, `publication_delivery_e2e.py`, `query_delivery_e2e.py`, `candidate_concurrency_e2e.py`, `host_capabilities_e2e.py` | Actual ordered protocol events, current-version publications, cancellation, client capabilities and stale-result rejection. The lifecycle entry requires all 25 named cases; a successful prefix cannot pass. |
| Analysis explanations | `analysis_status_e2e.py` | Cold-index document independence, separate scope completeness, exact guard reasons and recovery, explicit-only messaging, stale/reopened input identity, excluded and nested roots, disk configuration provenance, and unknown versus proved-zero references, including dynamic ApplicationSet and implicit default Project uses. |
| Navigation and verified edits | `navigation_editing_e2e.py`, `navigation_editing_guards_e2e.py`, `bounded_symbol_queries_e2e.py`, `project_rename_e2e.py`, `interaction_remediation_e2e.py`, `save_action_e2e.py`, `semantic_regressions_e2e.py`, `candidates_e2e.py`, `candidate_review_e2e.py`, `pin_e2e.py`, `pin_review_e2e.py`, `audit_repair_e2e.py lsp` | Exact text/ranges, UTF-16, version binding, named symbols, ownership and before/after semantic safety. Edit consumers apply returned edits. Version 2 pin fixtures require exact profile and values-order byte preservation. Pin stale-revision checks first prove a fresh pin action is currently available, so an unready index cannot mask a removed revision guard. |
| Rendering and process boundaries | `render_profiles_e2e.py`, `render_profile_guards_e2e.py`, `render_cache_e2e.py`, `renderer_input_e2e.py`, `render_acquisition_e2e.py`, `bounded_process_e2e.py`, `helm_e2e.py`, `readiness_render_e2e.py`, `output_failure_e2e.py`, `bounded_accounting_e2e.py` | Real Helm 3.19.0 and Helm 4.3.1 with pinned archive SHA-256; separate controlled renderers for invalid input, blocked acquisition, descendant cleanup and output failures. Ordered named-profile outputs, configuration migration, source/root/config/values lifetime fences, real descendant cancellation, negotiated output display, verified cache ownership, rendered paths/ranges and completed work are checked independently. |
| Performance and retention | Existing native performance consumers plus [resource.yml](../.github/workflows/resource.yml) and `resource_ci.py` | Work-count/incremental regressions remain in native Linux CI. The additional resource lanes enforce cgroup memory, no swap, four allowed CPUs and CPU quota, then check the original precise case sets and thresholds. See below. |
| Go owner contracts | `go test` for `internal/assets`, `internal/analysis`, `internal/cli`; `-race -count=1` for `internal/lsp` and `internal/workspace` | Schema compile constraints, derived AppSet budgets, summary identity, CLI behavior, real index state transitions, rename proofs and document budgets. These supplement executable consumers where ownership/synchronization cannot be directly observed through the protocol. |
| Zed adapter and package | Cargo tests plus `adapter_e2e.py`, `package_e2e.py`, `ablate_identity.py`, `ablate_distribution.py` in [zed.yml](../.github/workflows/zed.yml) | Rust distribution selection and runtime identity, compiled WASM and packaged assets, exact pin isolation, valid alternate-version packages, and named runtime rejection of single corruptions. The adapter builds without the native repository contents. |
| Editor content and installation | `editors/zed/tests/snippets_e2e.py`; `install_e2e.py` in [zed-install.yml](../.github/workflows/zed-install.yml) | All three shipped snippets execute through the current native checker. Installation tests download real published release assets on their configured hosts. The installer workflow is path-triggered for installer changes; package/WASM validation runs in adapter CI. Neither boundary claims GUI interaction with Zed. |
| CI evidence and controls | `native_ci_process_e2e.py`, `ci_evidence_e2e.py`, `ci_evidence_cas_e2e.py`, `ci_shards_e2e.py`, `resource_gate_e2e.py`, `test_inventory.py` | Cleanup, typed archive/export and verified fallback, source/tool/step/job mismatch and missing evidence. Synthetic reports test validators only; native and resource jobs must independently execute the real workload. |

Every test file, including helpers, aliases and consumers invoked by another
consumer, has one primary classification in the machine-readable inventory.
Some tests intentionally serve multiple roles: an unmodified semantic consumer is
also the positive control for its mutation. That necessary control is not a second
independent claim of product coverage.

## Orthogonal Linux suites

`orthogonal_ci.py` supplies semantic and fault dimensions complementary to the five
native hosts. It does not multiply each fixture or source mutation by the platform
matrix. Its three required lanes are:

- `contracts`: catalog integrity, configuration/source boundaries, selected domain
  behavior changes and LSP transitions, real and synthetic schema profiles, scope
  isolation and the shipped editor snippets.
- `boundaries`: completed-work accounting, renderer acquisition/input/process
  boundaries, real Helm integration and output failures.
- `mutations`: oracle controls, named current-source guards, schema constraints,
  schema query branches, shared lifecycle and workspace/edit behavior.

Each command has a fresh output directory and a bounded process group. Success
requires exit zero and nonempty passing structured evidence. Ordinary native
consumers must identify the supplied binary hash; real-renderer receipts must also
identify the supplied Helm hash. A synthetic schema or source mutation records its
own separate build, source and evidence instead of claiming the main binary.
For a full execution plan, the candidate job requires successful native,
orthogonal, resource and ordinary-producer jobs. Only the explicit prose-only
plan permits those lanes to be skipped, after trusted plan recomputation; see
[CI plans and replay](#ci-plans-build-receipts-and-replay).

## Fault controls must test the intended reason

`legacy_mutations.py` compiles the unchanged source once and runs every selected
consumer successfully before compiling one fault at a time. The nine historical
`ablate_*.py` family commands delegate to that runner. They no longer depend on
machine-specific temporary toolchain paths or count exit status 1 as proof.
Each fault must hit its expected named case set, retain the remaining positive
controls and produce the complete evidence shape. The ordered LSP consumer may
stop only at the specified assertion after its complete passing prefix. A compiler
failure, timeout, Python exception or unrelated failure is not a detected mutant.

The guard families distinguish actual-document validation, namespace, dynamic
references, explicit target precedence, stale notifications, new-error rejection,
damaged-reference recovery, literal encoding, source span, client version, pin
revision, post-simulation completeness and root identity. Redundant downstream
namespace/scope checks are not counted as independent mutations when an earlier
live guard already prevents the behavior. Adding an unused traversal function is
also not a semantic fault and is not counted as a test.

`ablate_audit_repair.py` separately mutates cancellation, root selection, the current
incremental buffer-before-disk query path, excluded-file isolation, sequence fields
and returned fix bytes. Each variant must fail exactly its named case; the last
control proves the consumer uses the server's actual edit. Schema-query ablations
compile an unchanged baseline with the same synthetic fixture before their fault.
`schema_constraints_ablation.py` independently removes type, required and enum
compilation, requiring normal completion of the exact named Go test with the
expected assertion text. The raw schema oracle must not decide what to assert by
inspecting whether the compiled implementation retained the constraint.

`oracle_repair_mutation.py` labels report-classifier controls as synthetic. Its
consumer controls replay actual server output and corrupt one of exit status,
diagnostic presence, unexpected Error or completeness; each starts with the
unchanged successful output. The resource and CI evidence validators similarly
use a valid baseline and corrupt one acceptance dimension per negative control.

## Resource contracts

`resource_ci.py` runs these existing workloads under verified cgroup v2 scopes:

| Lane | Workloads | Enforced boundary |
| --- | --- | --- |
| `retention` | Retained CLI input, 100 open buffers, projection retention, 120 repository/scope visits | 512 MiB cgroup ceiling, 384 MiB probe RSS threshold, named budget refusal, full case completion and normal shutdown; repository rotation retains its 10% growth limit. |
| `boundaries` | CLI aggregate, single-root LSP and multiple-root LSP at the retained-input boundary | 1 GiB cgroup ceiling; exact below/at/above cases, refused state isolation and recovery. The existing 128 MiB product retention threshold is unchanged. |
| `lifecycle` | Full CLI workload, 1,000 completion requests, full LSP including 10,000 open/edit/close cycles | 8 GiB cgroup ceiling; original CLI deadline, 100 ms p95 query/completion, 620 ms diagnostic, 10% growth and five-second shutdown assertions remain mandatory. |

The runner checks actual cgroup limits before and after execution and rejects OOM,
missing cases, wrong binary/test hashes, altered assertions or absent RSS samples.
Lack of systemd/cgroup support is a failed acceptance environment, not a skip or a
passing unconstrained substitute. Builds run outside measured resource scopes.
`scope_rotation_e2e.py` remains an optional RSS observation without a threshold;
its lifecycle safety claims are gated by the stricter `repository_rotation_e2e.py`
and `bounded_lsp_e2e.py --full` consumers. It is explicitly classified as observation.

## Run locally

Use the Go version in `go.mod`, a C compiler and fresh output directories. Source
mutations resolve Go from `GO`, `GOROOT` or `PATH`; no fixed local checkout is needed.

```sh
go build -mod=vendor -o build/argocd-ls ./cmd/argocd-ls
python3 tests/test_inventory.py
python3 tests/compatibility_e2e.py build/argocd-ls artifacts/compatibility-explanation
python3 tests/ablate_compatibility_intersection.py artifacts/compatibility-intersection-ablation
python3 tests/orthogonal_ci.py contracts --binary build/argocd-ls --output artifacts/contracts
python3 tools/install_test_helm.py --output artifacts/helm3
python3 tools/install_test_helm.py --major 4 --output artifacts/helm4
python3 tests/orthogonal_ci.py boundaries --binary build/argocd-ls --helm artifacts/helm3/helm --helm4 artifacts/helm4/helm --output artifacts/boundaries
python3 tests/orthogonal_ci.py mutations --binary build/argocd-ls --helm artifacts/helm3/helm --output artifacts/mutations
```

On the matching native host, `native_ci.py --platform <os/arch> --suite consumers`
builds the native consumer set; `provenance` and `compiler-environment` select the
independent source/archive and compiler-security dimensions. Strict release/native
identity checks require a clean committed source tree. The resource workflow builds
that identity before invoking `resource_ci.py --suite <lane> --binary <path>
--output <fresh-path>` on Linux with working cgroup v2 and noninteractive systemd
scope creation. Local success on one host does not replace the other four hosts.

Raw inputs, frames, outputs, reports and hashes remain in each run's output tree.
CI packs and verifies complete evidence, including failure evidence, before upload.
The source commit, workflow job results and controlled build are the trust boundary;
a standalone JSON report or synthetic validator fixture is not proof that an
arbitrary executable or published editor installation passed these contracts.

## CI plans, build receipts and replay


`python tools/ci_baseline.py --commit <full-sha> --output artifacts/baseline.json`
exports immutable test inputs and lane contracts. It does not certify execution.
Native acceptance keeps fifteen native dimensions, all orthogonal suites, race,
source mutations and constrained resource consumers. The ordinary Linux release
producer serves native consumers and orthogonal baselines; a separate ordinary
development producer serves resource lanes. Both start with empty compilation
and module caches. No warm compiler cache is enabled. Compressed evidence is
uploaded without recompression.

### Typed replay transport

`tools/ci_evidence.py` owns both archive formats. `pack` defaults to historical
format 1; `pack --format cas-v2` stores one ordinary content object per SHA-256.
Both use gzip level 1 and keep the designated compiler/module-cache exclusions.
Every logical file still has its own path and mode. The v2 inventory also binds
numeric UID/GID and integer-second mtime, matching the original packer's timestamp
precision; owner/group names stay empty. Directories, symlinks, FIFOs, sockets and
character/block devices remain typed metadata. Physical hardlinks retain the old
behavior of independent regular logical files. The first ordinary TAR entry is
the canonical inventory; the archive hash binds it and all unique objects. This
is a self-contained manifest/archive pair, with no external producer references.

CI invokes `retain`: it packs and verifies `cas-v2/`, then tries `v1/` if the new
codec fails. `retention.json` records `actualFormat`, `fallbackReason`, each real
attempt and verification outcome. A fallback succeeds only after v1 verification.
First-attempt error text, partial outputs and all source evidence remain intact;
no source tree is deleted. If both formats fail, the command fails and marks
retention incomplete. The uploaded directory retains the available partial bytes
and diagnostic report; complete retention is not claimed. Earlier test/job
failures remain failures. Original receipts, child reports and raw streams are
ordinary payload bytes, never reconstructed or restamped as successful evidence.

```sh
python tools/ci_evidence.py retain --root artifacts/native-ci --output artifacts/native-evidence
# Read retention.json for the selected relative manifest path.
python tools/ci_evidence.py verify --manifest artifacts/native-evidence/cas-v2/manifest.json
python tools/ci_evidence.py export --manifest artifacts/native-evidence/cas-v2/manifest.json --output artifacts/offline-replay
```

`export` first verifies its input and writes a complete typed v1 TAR plus manifest
into a new output directory. It only spools ordinary object bytes into an anonymous
temporary file; it never creates logical symlinks, FIFOs, sockets or devices.
V1 input is copied byte for byte, including the original compressed archive and
manifest. V2 expands each logical regular member independently and restores every
recorded header field. Exported v1 is verified again before success. Verification
and export work after source trees are detached. This is a byte-complete offline
export, not evidence of newly executing the original tests or the whole CI plan.

V2 accepts canonical relative portable paths, rejects case/NFC aliases and
file/directory conflicts, and requires exact object membership, ordinary physical
USTAR headers, zero TAR padding and one gzip member with no compressed tail. It
rejects duplicate JSON keys, boolean numeric fields, undeclared fields, hash/size
disagreement and missing raw objects. Historical v1 keeps its original relative
path policy, including Unix names such as `con.txt`, `revision:one.txt` and distinct
`A.txt`/`a.txt` and POSIX non-UTF-8 filename/link bytes; no logical path is extracted
by its verifier. V1 uses the original surrogateescape byte encoding for those
metadata bounds; v2 remains strict UTF-8. Pinned old production
archives exercise those compatibility cases on every native test host.

Both readers bound a manifest to 256 MiB, the logical inventory to 1,000,000 members,
total logical bytes and compressed archive bytes to 64 GiB, encoded names to 4,096
bytes and link metadata to 16 KiB. V1 regular files are bounded to 16 GiB; v2 content
objects are bounded to 8 GiB minus one byte by its canonical USTAR size field.
V2's expanded byte allowance is derived from the exact inventory/object sizes,
headers and TAR padding; the legacy reader separately allows bounded PAX headers.
Packing checks actual file identity, size, mode, owner/group, mtime and ctime
around reads, then checks the entire selected member inventory again. These are
observed stability checks, not an atomic snapshot against a hostile concurrent
writer. Roots and exclusions use sorted/set lookups rather than pairwise scans.
The inventory allowance covers the existing full native lane: its 150,000-file
persistent workspace, 50,000-file mixed parent and 50,001-file repository exclusion
fixtures already exceed the former 250,000-member limit before any other evidence.
These generated inputs remain in the archive. Individual streaming reads remain
bounded to 64 MiB. Device identity is compared when the operating system exposes
it; Windows ordinary files have no `st_rdev` field, while their other identity and
metadata checks remain active.
Windows path and descriptor stat results use different meanings for `ctime` in
the pinned Python runtime ([CPython #157671](https://github.com/python/cpython/issues/157671)).
Their initial identity comparison uses the shared
creation timestamp when available; subsequent path-to-path and
descriptor-to-descriptor checks retain full `ctime` and mode comparisons. This
does not accept changes to either API's metadata during a read.

`ci_evidence_cas_e2e.py` fixes the seven-type header and duplicate-content
expectations independently, tests malformed containers and actual source changes,
and requires a verified fallback under a controlled codec failure. Filesystem
capability limits are recorded separately from the literal metadata checks.
The native lane and its child contract require this consumer alongside the
unchanged v1 and production gate controls. `ci_gate.py`, build receipts, run/attempt
trust and domain acceptance remain separate from this transport verifier.

Transport measurements must count the archive, external manifest and required
retention/index files together. Compare current production v1 with v2 using the
same logical input, compression level, runtime and selection; report pack, verify
and offline export separately. Local historical replay samples do not establish
hosted upload/download/queue savings, critical-path improvements or new domain
execution coverage.

The initial three alternating local pairs used the exact `ec32e5d` production v1
tool and CAS candidate tool SHA-256 `cf4e7e66baa787aaf2c13bee2c39069d9ff81a6e11b8cf1b54fcebc4baf04fbc`,
Python 3.12.14 / zlib 1.3.2 / gzip level 1. The reconstructed historical input
contained 4,220 regular files (206,187,799 bytes), 1,008 directories and one FIFO.
Both tools read the same physical paths and metadata; the original archive's
UID/GID 1001 was recorded separately from the reconstructed owner's 1000.
Every pair preserved the complete typed inventory and produced identical full
offline TAR bytes. Each CAS trial also ran the actual retention wrapper to count
its required metadata, separately from the timed pack/verify/export commands.

| Local measurement | Frozen production v1 | CAS v2 |
| --- | ---: | ---: |
| Archive bytes, each trial | 39,486,139 | 30,249,613 |
| External manifest bytes, each trial | 1,152,329 | 1,366,718 |
| Required retention metadata bytes, each trial | 0 | 927 |
| Total transport bytes, each trial | 40,638,468 | 31,617,258 |
| Pack median, three samples | 3.093 s | 4.750 s |
| Verify median, three samples | 1.266 s | 1.648 s |
| New offline export median, three samples | 3.119 s | 5.202 s |

This is a 22.20% reduction in transport bytes in exchange for **more local packing,
verification and offline recovery time** in this sample. It is not an overall CI
speedup claim. Export is a new interface in both columns; v1 copies its verified
full archive, while v2 restores every logical member from verified unique content.
The historical tests were not reexecuted, and hosted transfer/queue samples are
absent. The scratch comparison and its raw phase observations remain separate
from product test results.
The final follow-up only changes two v1 metadata byte-length expressions to
preserve POSIX surrogateescape names. A frozen old-producer archive tests that
compatibility; all 5,229 measured logical names have identical strict/surrogateescape
bytes, and v2's strict encoding is unchanged. The timings above remain bound to
the stated measured candidate, rather than being restamped onto that follow-up.

### Producer identity and execution plans

Consumers require the producer receipt digest from the same workflow dependency,
run and attempt, then verify source inputs, bytes and the executable identity.
A failed-only job rerun cannot certify inherited earlier-attempt producer bytes:
rerun the producer and all dependent consumers together. Fault, provenance,
synthetic-schema and race builds retain their independent execution paths.

`tools/ci_plan.py` records base, head and tested SHA independently. Only modifications
to README.md and DEVELOPMENT.md are eligible for the prose-only native plan.
Mixed, renamed, deleted, unknown, schema, dependency, test or CI changes select
full; an unavailable diff also selects full. Main and release always select full.
The stable candidate gate recomputes the plan and rejects unexpected skips,
missing jobs, wrong identities and unplanned blocking work. The Zed acceptance
workflow always runs independently, including prose-only PRs.

Transport controls run with `python tests/session_transport_e2e.py --output
artifacts/session-controls`. Domain oracles remain in the real LSP consumers;
the shared transport does not decide Argo semantics. Schema fixtures and their
stable IDs live under `tests/fixtures/contracts/schema-query`; edit expectations
explicitly and review the diff. CI never updates fixtures. Run
`python tests/incremental_equivalence_e2e.py <binary> artifacts/equivalence` to
compare incremental and fresh final states, including overlays and root identity.
The eleven histories include actual UTF-16/CRLF application of Project rename,
field completion and resolved repair to independently expected complete texts.
`work_observability_e2e.py` adds five real interaction workloads and a trace-off
control, including actual byte/parse/schema/domain counters and separate cancel
reply/worker-release observations. See [measurement definitions](../docs/performance.md).
Repeated semantics alone do not prove a cache hit; existing incremental work-count
and cancellation consumers remain required.

Published file-install acceptance validates successful upstream identity, current
adapter source/version/native pin, downloaded asset IDs and digests, then retains
upgrade/rollback and fault controls. It is not real Zed host evidence. To replay:
`python editors/zed/tests/install_e2e.py --source-commit <published-source-sha>
--report artifacts/zed-installer`.

For CI timing, save complete paginated Actions jobs JSON per run/attempt and use
`python tools/ci_trends.py --jobs <jobs.json> --output artifacts/trends.json`.
Keep first-attempt failures and diagnostic reruns separately. Pair only matching
platform/toolchain/selection/cache groups and report missing samples. Timing
summaries describe the supplied runs; they do not relax product thresholds, prove
a performance improvement without comparable observations, or certify desktop
acceptance.

Native acceptance uploads the original consumer JSON reports separately from the lane summary. The aggregate validates their case and assertion inventories against `tests/child_contracts.json`, their recorded runner and binary hashes, and their artifact digests. Changes to consumer scenarios must update this acceptance contract in the same review. Full transcripts remain in the separate evidence archive. Reports with zero cases, missing assertions, or nonboolean success values cannot satisfy the gate.

Linux orthogonal reports use the reviewed case and assertion inventories in
`tests/orthogonal_contracts.py`, including the independent schema query fixtures
and named mutation failures. Resource reports validate every raw timing and RSS
sample before applying the original counts and thresholds. Both runners reject
nonfinite JSON and duplicate object keys. The required orthogonal contracts and
resource lifecycle jobs replay negative controls against their freshly produced
reports before archiving evidence. To replay downloaded reports locally:

```sh
python tests/acceptance_evidence_e2e.py --orthogonal artifacts/orthogonal-ci/tests --output artifacts/orthogonal-controls
python tests/acceptance_evidence_e2e.py --resources artifacts/resources --output artifacts/resource-controls
```

Use a new output directory for each replay. It retains each input, verifier and
input hashes, command, exit status, stdout, stderr, and rejection reason. These
controls establish evidence acceptance; they do not measure product performance.

`tests/observation_evidence_e2e.py` additionally replays ordinary observations,
mutation positive controls and designated raw failures, retained-budget RSS, and
the CLI process deadline. Pass `--contracts` or `--mutations` with the respective
orthogonal output directory, `--retained` with the multiroot retained-budget
report, or `--resources` with the lifecycle output directory, plus a new
`--output` directory. The corresponding required CI jobs run these controls.
