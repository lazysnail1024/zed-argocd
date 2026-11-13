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
| Schema constraints and queries | Go `internal/assets` and `internal/analysis` tests; `schema_queries_e2e.py`, `shared_schemaquery_e2e.py`, `schema_fixture_e2e.py`, `schema_query_values_e2e.py`, `schema_query_comment_e2e.py` | Raw upstream JSON independently supplies type/required/enum expectations for compiled nodes. Real catalog queries and deliberately synthetic schema branches are separate. Every query profile must emit the exact ordered target × snippet-mode × case set, including shutdown. Synthetic value cases run against their own fixture binary. |
| Projection and source mapping | `alias_projection_cli_e2e.py`, `alias_projection_lsp_e2e.py`, `alias_provenance_edges_e2e.py`, `projection_repair_e2e.py`, `projection_aggregate_e2e.py`, `dependency_depth_e2e.py` | YAML alias/merge semantics, mapped locations, recovery, aggregate work accounting and actual depth-budget rejection followed by same-process recovery. |
| Workspace and lifecycle | `parent_workspace_lsp_e2e.py`, `mixed_parent_index_e2e.py`, `persistent_workspace_e2e.py`, `workspace_regressions_e2e.py`, `scope_glob_e2e.py`, `scope_refresh_e2e.py`, `repository_exclusion_e2e.py`, `reconciliation_e2e.py` | Root/scope ownership, exclusions, overlays, discovery, periodic repair and release of stale state. Larger Linux samples add workload scale without duplicating all semantic dimensions on every host. |
| LSP delivery and concurrency | `lsp_e2e.py`, `lsp_regressions_e2e.py`, `publication_e2e.py`, `publication_delivery_e2e.py`, `query_delivery_e2e.py`, `candidate_concurrency_e2e.py`, `host_capabilities_e2e.py` | Actual ordered protocol events, current-version publications, cancellation, client capabilities and stale-result rejection. The lifecycle entry requires all 25 named cases; a successful prefix cannot pass. |
| Navigation and verified edits | `navigation_editing_e2e.py`, `navigation_editing_guards_e2e.py`, `bounded_symbol_queries_e2e.py`, `project_rename_e2e.py`, `interaction_remediation_e2e.py`, `save_action_e2e.py`, `semantic_regressions_e2e.py`, `candidates_e2e.py`, `candidate_review_e2e.py`, `pin_e2e.py`, `pin_review_e2e.py`, `audit_repair_e2e.py lsp` | Exact text/ranges, UTF-16, version binding, named symbols, ownership and before/after semantic safety. Edit consumers apply returned edits. Pin stale-revision checks first prove a fresh pin action is currently available, so an unready index cannot mask a removed revision guard. |
| Rendering and process boundaries | `renderer_input_e2e.py`, `render_acquisition_e2e.py`, `bounded_process_e2e.py`, `helm_e2e.py`, `readiness_render_e2e.py`, `output_failure_e2e.py`, `bounded_accounting_e2e.py` | Real Helm 3.19.0 with pinned archive SHA-256; separate controlled renderers for invalid input, blocked acquisition, descendant cleanup and output failures. Rendered paths/ranges and completed work are checked independently. |
| Performance and retention | Existing native performance consumers plus [resource.yml](../.github/workflows/resource.yml) and `resource_ci.py` | Work-count/incremental regressions remain in native Linux CI. The additional resource lanes enforce cgroup memory, no swap, four allowed CPUs and CPU quota, then check the original precise case sets and thresholds. See below. |
| Go owner contracts | `go test` for `internal/assets`, `internal/analysis`, `internal/cli`; `-race -count=1` for `internal/lsp` and `internal/workspace` | Schema compile constraints, derived AppSet budgets, summary identity, CLI behavior, real index state transitions, rename proofs and document budgets. These supplement executable consumers where ownership/synchronization cannot be directly observed through the protocol. |
| Zed adapter and package | Cargo tests plus `adapter_e2e.py`, `package_e2e.py`, `ablate_identity.py`, `ablate_distribution.py` in [zed.yml](../.github/workflows/zed.yml) | Rust distribution selection and runtime identity, compiled WASM and packaged assets, exact pin isolation, valid alternate-version packages, and named runtime rejection of single corruptions. The adapter builds without the native repository contents. |
| Editor content and installation | `editors/zed/tests/snippets_e2e.py`; `install_e2e.py` in [zed-install.yml](../.github/workflows/zed-install.yml) | All three shipped snippets execute through the current native checker. Installation tests download real published release assets on their configured hosts. The installer workflow is path-triggered for installer changes; package/WASM validation runs in adapter CI. Neither boundary claims GUI interaction with Zed. |
| CI evidence and controls | `native_ci_process_e2e.py`, `ci_evidence_e2e.py`, `ci_shards_e2e.py`, `resource_gate_e2e.py`, `test_inventory.py` | Cleanup, archive refusal, source/tool/step/job mismatch and missing evidence. Synthetic reports test validators only; native and resource jobs must independently execute the real workload. |

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
The candidate job explicitly requires successful native, orthogonal and resource
jobs; skipped jobs cannot satisfy it.

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
python3 tests/orthogonal_ci.py contracts --binary build/argocd-ls --output artifacts/contracts
python3 tools/install_test_helm.py --output artifacts/test-tools
python3 tests/orthogonal_ci.py boundaries --binary build/argocd-ls --helm artifacts/test-tools/helm --output artifacts/boundaries
python3 tests/orthogonal_ci.py mutations --binary build/argocd-ls --output artifacts/mutations
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
