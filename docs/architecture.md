# Current implementation

The native Go server owns Argo CD analysis. The Rust/WASM Zed adapter selects and
verifies an executable, forwards settings and starts `argocd-ls serve --stdio`.
CLI and LSP share analysis packages; neither needs an Argo CD controller or
cluster connection.

## Module map

| Module | Responsibility |
| --- | --- |
| [cmd/argocd-ls](../cmd/argocd-ls/) | Native process entry point. |
| [internal/cli](../internal/cli/) | `version`, `check`, `explain-config`, `render-check` and `serve`; reports, exits, explicit Helm process lifecycle and output handling. |
| [internal/workspace](../internal/workspace/) | Root ownership, strict configuration, targets/scopes, exclusions, bounded acquisition, repository mappings, chart discovery and target pin configuration. |
| [internal/assets](../internal/assets/) | Embedded catalog, exact/automatic schemas, compiled-schema readiness and source identities. |
| [analysis/schema.go](../internal/analysis/schema.go) and [projection.go](../internal/analysis/projection.go) | Parse immutable inputs, identify supported resources, project YAML values/origins and validate schemas. |
| [analysis/domain.go](../internal/analysis/domain.go), [appset.go](../internal/analysis/appset.go), [local_paths.go](../internal/analysis/local_paths.go) | Application/AppProject static rules, supported ApplicationSet variants, source references and explicitly mapped repository checks. |
| [analysis/schema_queries.go](../internal/analysis/schema_queries.go), [queries.go](../internal/analysis/queries.go), [fields.go](../internal/analysis/fields.go), [helm_source.go](../internal/analysis/helm_source.go) | Schema completion/hover, navigation, recovery context and conservative Helm-source projection. |
| [analysis/summary.go](../internal/analysis/summary.go) | Compact resource facts, dependency keys, retained locations and meaning identities without retaining closed-file source or syntax trees. |
| [analysis/fixes.go](../internal/analysis/fixes.go), [rename.go](../internal/analysis/rename.go), [project_rename.go](../internal/analysis/project_rename.go) | Candidate construction, semantic simulation and exact-origin edit planning. |
| [internal/lsp](../internal/lsp/) | Protocol transport, open-buffer authority, incremental orchestration, current-document analysis, publications, request limits, query/edit freshness and explicit render actions. |
| [internal/buildinfo](../internal/buildinfo/) | Product, version, source and contract identity. |
| [editors/zed/src](../editors/zed/src/) | Zed settings/process integration, native selection, download/cache activation and identity verification. |

## Shared toolkit boundary

The current dependency is source commit
`bc3a72298f10731b46eaeb9bf7688c9ad0a52839`, represented by
`v0.0.1-alpha.1.0.20261113165037-bc3a72298f10` in [go.mod](../go.mod).
[toolkit.lock.json](../contracts/toolkit.lock.json) and the hash-checked
[API contract](../contracts/toolkit-design-6.md) identify this pin. The toolkit's
latest `main` can differ without changing this server.

| Shared package | Generic responsibility | Argo CD responsibility retained here |
| --- | --- | --- |
| `source` | Immutable UTF-8 snapshots, revisions, byte spans and UTF-16 positions. | Client versions, URI spelling, resource meaning and publication validity. |
| `yamlview` / `yamlvalue` | Recoverable syntax, YAML projection, aliases/merges and origin classification. | Argo GVK recognition, ApplicationSet interpretation and safe Helm-source selection. |
| `schemaquery` | Queries over the compiled JSON Schema model. | Catalog choice, field context, completion/hover presentation and diagnostics. |
| `edit` | Validated replacement plans against a source revision. | Fix eligibility, namespace/scope authority, simulation and LSP workspace edits. |
| `index` | Versioned opaque facts, definitions/references/dependencies and bounded queries. | Project keys, summaries, configured scopes and completeness decisions. |
| `workspace` | Document/read tickets, queues, resumable discovery and scan membership. | Workers, scheduling, exclusions, root/configuration ownership and protocol lifecycle. |

The toolkit does not depend on Argo resources, Zed, Helm or these repositories'
release workflows. This server does not import the Workflows plugin. Vendor and
contract snapshots are verified dependency inputs; changing them is a dependency
upgrade, not documentation cleanup.

## Workspace state and budgets

The LSP coordinator accepts editor/file events and owns their freshness ordering.
Shared document storage makes accepted open text authoritative over disk reads.
Background discovery uses a resumable scanner and weighted work queue; current
and related files can advance without restarting the parent traversal. The index
retains compact per-file facts, diagnostics and locations. Closed-file text,
syntax trees and projected graphs are acquired again only when needed.

Meaning changes invalidate dependent facts; position changes also invalidate
stale locations. Read tickets, revisions, configuration events, scope generations
and query proofs prevent obsolete publication. A removed/recreated root has a new
interpretation even when its path is unchanged. Watched-file notifications and
30-second reconciliation repair membership; incomplete scanning cannot authorize
a deletion sweep.

Current-document analysis and foreground requests have independent execution
paths from background discovery. An unfinished large workspace therefore does
not automatically remove current-buffer diagnostics. Known candidates remain
useful during discovery, but partial state cannot prove absence, unique bindings
or cross-file edit safety.

| Boundary | Current source limit |
| --- | --- |
| Single input text | 5 MiB. |
| Retained CLI input / accepted LSP open text | 128 MiB logical text, not process RSS. |
| LSP registered files | 100,000. |
| Compact summaries | 96 MiB logical summary accounting; index facts have their own 96 MiB accounting limit. |
| Index facts | 500,000 symbols and 1,000,000 references/dependency entries. |
| Background ready queue | 512 entries; the work queue separately bounds outstanding file work. |
| Foreground concurrency | Four light requests and one heavy request; the explicit renderer has its own single busy state. |
| `check` / ordinary LSP request deadline | Five seconds; explicit editor rendering has a 30-second request context. |

See [incremental.go](../internal/lsp/incremental.go),
[server.go](../internal/lsp/server.go) and
[workspace.go](../internal/workspace/workspace.go) for ownership. These are
admission and work contracts, not hard real-time guarantees or claims that every
workspace completes within the deadline. The CLI uses bounded whole-input
acquisition; it does not inherit the LSP's long-lived resumable index.

## Queries and edits

Schema queries reuse compiled schemas. Automatic mode selects complete version
branches using existing field names and preserves repair suggestions for invalid
values; validation still rejects incompatible combinations. Document symbols use
the selected input. Workspace symbols query fresh compact facts with explicit
work/result limits and cancellation.

Project definitions without a configured control-plane namespace return local
candidates. Policy and reverse references require an authoritative binding.
Private multi-source `ref` rename is confined to its Application. Cross-file
AppProject rename requires a complete closed scope, an explicit namespace, unique
exact bindings and every affected file open at an accepted client version. Edits
are tied to current input and checked again before delivery; clients must support
versioned `documentChanges` for editing capabilities.

Alias-derived, merged or otherwise ambiguous origins do not receive guessed
edits. Recovery supports navigation/completion while a document is being typed
without authorizing unsafe rewrites. Regression consumers apply returned edits
and inspect the resulting bytes and diagnostics.

## Helm and external processes

Ordinary YAML in charts follows the normal path. Raw Helm source can expose
literal navigation only when structure and origins remain safe. Dynamic values
stay unknown; structural control flow, includes and unsafe projections withhold
navigation. This path offers no semantic edits or rendered-validation claim.

Only an explicit `render-check` command or selected editor render action runs
Helm. The editor action requires saved chart/configuration buffers, finds the
nearest ancestor `Chart.yaml` and uses default chart values. CLI overrides are
explicit. Process deadlines/cleanup and reports belong to `internal/cli`;
diagnostic locations point to generated YAML. Opening, saving and code-action
listing never silently run Helm.

## Verification and maintenance

[Test contracts](../tests/README.md) describe native hosts, Linux semantic/resource
lanes, mutation controls and evidence validation. [Distribution](distribution.md)
separates native builds, adapter packages, file installation and real editor
acceptance. [Lessons learned](lessons-learned.md) connects recurring problems to
current guards and regression consumers.
