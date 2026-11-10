# Diagnostics and incremental indexing in parent workspaces

Opening a parent directory containing several Git repositories is supported
without a configuration file or version pin. The LSP retains the editor's declared
workspace roots. Nested `.git` directories are exclusions, not analysis boundaries;
parent-level scopes, repository mappings and cross-repository AppProject references
continue to use the declared root and configuration.

## Current documents and persistent workspace facts

Open buffers receive an independent document analysis. It reads bounded root
configuration and chart metadata, but does not discover or read unopened YAML.
Schema diagnostics, hover, document symbols, field completion and local schema
quick fixes can therefore respond during background discovery. A newer editor
version supersedes the old document job without restarting background discovery.

The background worker owns one persistent session. It resumes directory discovery
in batches, queues file identities, reads only admitted files, and commits detached
facts. The shared `k8s-crd-toolkit/workspace` package supplies buffer/read tickets,
the bounded priority queue and scanner. `k8s-crd-toolkit/index` supplies revisioned
facts and reverse dependencies. Argo CD target selection, AppProject keys,
namespace rules, ApplicationSet variants, policy checks and edit safety remain in
this repository.

The scanner gives the current document's directory priority over unrelated
background directories. The queue deduplicates URI notifications and uses the
shared weighted priorities for current, related and background work. Queue overflow
leaves bounded dirty state on registered files. The notification ingress also has
a bound; excess distinct events request reconciliation rather than dropping the
need to rediscover membership.

Each background file operation has its own five-second cooperative budget. There
is no five-second deadline for completing the entire workspace. A scan may take
longer while continuing to publish current-document results. Cancellation retains
the discovery cursor; ordinary edits and watched-file changes do not reset it.
A blocking operating-system read is still outside a hard cancellation guarantee.

## What is retained

Closed files retain detached domain values, candidate identities, dependency keys,
byte spans and UTF-16 ranges. They do not retain original YAML, CST nodes or the
full projection graph. ApplicationSet summaries retain the effective variants
needed by domain checks; unknown and non-exact values remain unknown or non-exact.
Comments can move positions without changing the semantic digest, and new ranges
are installed even when reverse dependents do not need semantic revalidation.

The current limits are:

| Input or state | Bound |
| --- | --- |
| One YAML input | 5 MiB |
| Retained open-buffer text | 128 MiB |
| Open-document entries | 2,048, with URI length limited to 4,096 bytes |
| Registered candidate files | 100,000 across the session |
| Pending queue identities | 512 |
| Distinct file events before reconciliation coalescing | 512 |
| Projection and ApplicationSet expansion | 100,000 logical nodes in the analysis budget; 64 MiB of expanded strings |
| Retained file summaries, metadata and diagnostics | 64 MiB of logical charges |
| Shared index facts | 64 MiB of logical charges, 500,000 symbols, 1,000,000 references/dependencies |
| Scanner directories | 100,000 per declared root |

Logical charges are admission limits, not process RSS promises. Parser, schema,
query, Go runtime and allocator memory is additional. A workspace containing more
than 128 MiB of **cumulative closed input** can finish because those bytes are
consumed one file at a time. The existing one-shot CLI retains its separate
aggregate acquisition and projection limits.

ApplicationSet admission counts the effective templates before constructing all
generator variants. Large repeated `valuesObject` or static `templatePatch` values
cannot bypass that construction limit merely because the final summary would
discard them. A refusal reports `INPUT_BUDGET_EXCEEDED` and leaves the scope
incomplete.

Default directory exclusions are pruned by the scanner. Configured rules of the
form `**/directory/**` can also prune that basename everywhere. More specific glob
rules are applied exactly when selecting discovered files; they do not gain a
broader pruning interpretation that could hide included descendants. Symbolic-link
directory entries are not recursively discovered. Explicit open-file aliases keep
their client URI while using the canonical file identity for membership.

## Events and freshness

`didOpen` and `didChange` establish authoritative snapshots through the shared
Documents store. Disk notifications cannot replace an open buffer. `didClose`
releases that overlay and requests a fresh disk read; an earlier cached disk text
is never restored. All accepted buffer and disk snapshots use the same revision
authority, including close/reopen and deletion/recreation.

A watched YAML notification withdraws its prior navigation facts immediately,
before debounce or a disk read. A running worker must pass the event fence and
index ticket before installing a result. A late read or parse cannot revive an
older position or deleted target. Repeated notifications for the same content
reuse its summary without parsing unrelated files. Non-YAML events affecting
configured repository paths or chart ownership recheck those inputs and their
known consumers.

A rolling reconciliation starts every 30 seconds. It reads actual content instead
of trusting only modification time and size, so missed notifications and changes
with identical file metadata converge. A new reconciliation requested during an
active pass is retained until that pass completes. Directory-only notifications
without individual file changes may require this reconciliation to discover the
new membership.

A parser or input failure removes that file's authority and marks the affected
scope incomplete. Unknown scope ownership errors prevent a whole-root completeness
claim. A failed or partial discovery pass cannot prove deletion of unseen files.
Current-buffer Schema diagnostics survive a background failure for the same input;
recovery compares the accepted snapshot identity, not an unrelated notification
counter.

When an unrelated notification supersedes a completed background batch before
its diagnostics are delivered, its surviving URI records are queued again for
domain checking and publication. Already accepted source bytes are not reread.
Clears for removed closed files are retained without replacing a newer open
buffer's diagnostics.

## Partial results and safe edits

Known positive definition and completion candidates are useful before the whole
scope is ready. They preserve all known duplicates and exactness restrictions;
an empty partial result does not prove that a target is missing. Missing-target
and uniqueness-dependent domain conclusions, reference binding and explicit
Project replacement edits require a complete scope. Invalid, unreadable or
budget-limited files prevent that proof.

Read responses carry proofs for the configuration, selected input and candidate
keys actually used. Reference queries also retain their use set; candidate-list
queries account for newly added Project names. The serial protocol owner checks
the proof again before replying. Unrelated scope changes do not invalidate a
query, while changed dependencies and positions do.

Field-name and enum corrections validate the current document before and after
the proposed change, including its local domain errors. They require the same
accepted buffer revision and target/configuration identity at resolution and
reply. Background discovery of unrelated files does not make such a correction
permanently stale. Project candidate selection and configuration pinning retain
complete-scope checks and revalidate the proposed edit against current inputs.

## Diagnostics, Ready and evidence

`textDocument/publishDiagnostics` retains the additive
`x-argocd-analysis-scope` field. `document` identifies early local findings;
`workspace` identifies a workspace calculation or terminal limited result. The
field is a publication phase, not a completeness certificate. An empty diagnostic
list alone also does not mean that discovery has finished.

With `ARGOCD_LS_TRACE=1`, `argocd-document-analysis` and `argocd-analysis` records
report work without source contents. Persistent records include `state`
(`Discovering`, `Indexing`, `Ready` or `Limited`), `complete`, `knownFiles`,
`queueDepth`, `discoveryWork`, `summaryBytes`, `diskReads` and `parsedFiles`.
Acceptance consumers wait for `Ready`, `complete: true` and an empty queue when
they need complete membership. An error, exhausted budget or incomplete scan
cannot be relabeled Ready because the work queue is empty.

Required native consumers include:

- `mixed_parent_index_e2e.py`: mixed Argo/foreign YAML, related-directory priority,
  overlay/watch/close behavior, immediate position withdrawal, deletion/recreation.
  Its large-workspace mode also resolves a field correction while discovery is
  advancing and rejects that action after the buffer changes.
- `persistent_workspace_e2e.py`: terminal Ready, unchanged save work, exact single
  watched-file work, same-size/same-mtime missed-watch recovery, a 640-file burst
  exceeding queue capacity, and clean exit. Linux also runs 50,000 padded YAML
  inputs with an approximately 90/9/1 ConfigMap/Application/Workflow mix, plus
  100,000 non-YAML entries. The YAML totals about 195 MiB; kernel peak RSS must
  stay within 512 MiB. This checks discovery and cumulative-input retention;
  arbitrary numbers of dense Argo CD summaries remain subject to admission.
- `lsp_regressions_e2e.py`: persistent event progress, scope changes, query proofs
  and parser failures that leave independent roots usable.
- `publication_delivery_e2e.py`: Linux file-open observation forces a real edit
  during a background parse and verifies that the committed diagnostic reaches
  the client without another disk read or a periodic rescan.
- `parent_workspace_faults.py`: buildable isolated faults with a real per-file
  deadline and a removed diagnostic-recovery guard. Compilation failures and
  runner errors are not accepted as behavior-loss evidence.
- Existing domain, ApplicationSet, alias/projection, local-path, Helm, URI,
  cancellation, fix and CLI consumers preserve their semantic checks.

These are native CLI/LSP protocol tests. They do not constitute Zed GUI installation
or rendering acceptance, nor do these source changes publish a native or adapter
release.

Recorded workload results and controlled failures are described in
[incremental validation](incremental-index-validation.md).
