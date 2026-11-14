# Lessons enforced by the current implementation

This document retains problem, cause, correction and regression knowledge.
It does not track PR progress, temporary audit results or CI run status.

## Large parent workspaces must preserve current-document service

- **Problem:** Acquisition can remain incomplete while editing one file beneath
  a parent containing many repositories.
- **Cause:** Complete workspace acquisition as a prerequisite for foreground
  answers couples editing latency to unrelated files.
- **Correction:** Independent document analysis, prioritized resumable discovery,
  compact facts and bounded foreground workers. Partial discovery remains visible
  and cannot establish missing-reference or edit safety.
- **Regression:** [mixed_parent_index_e2e.py](../tests/mixed_parent_index_e2e.py),
  [persistent_workspace_e2e.py](../tests/persistent_workspace_e2e.py),
  [incremental_equivalence_e2e.py](../tests/incremental_equivalence_e2e.py) and
  [bounded_lsp_e2e.py](../tests/bounded_lsp_e2e.py).

## Stale work must not restore superseded state

- **Problem:** Reads, edits, root recreation and analysis finish in a different
  order from the events that requested them.
- **Cause:** A URI or successful worker completion alone is not input authority;
  its text, configuration or ownership may already have changed.
- **Correction:** Document/read tickets, fact revisions, configuration events,
  scope generations and delivery proofs fence results. Queue completion releases
  a reservation without authorizing publication.
- **Regression:** [publication_delivery_e2e.py](../tests/publication_delivery_e2e.py),
  [query_delivery_e2e.py](../tests/query_delivery_e2e.py),
  [reconciliation_e2e.py](../tests/reconciliation_e2e.py) and
  [shared_lifecycle_mutations.py](../tests/shared_lifecycle_mutations.py).

## Partial-state tests must establish the intended batch boundary

- **Problem:** The configuration-pin regression can reach `Ready` without
  observing the intended earlier application publication.
- **Cause:** Final domain work is queued from a Go map. File counts alone do not
  determine which batch contains the application, and the time slice can also
  change a batch's size.
- **Correction:** After discovery and reads finish, verify that the real final
  domain queue exceeds one batch and already contains the application. Promote
  that existing job through the queue's normal priority API before advancing.
  Keep the assertions that pin actions are absent while work remains and that
  the completed root offers exactly the expected edit for both URI spellings.
- **Regression:** `TestConfigPinWaitsForRootReady` in
  [incremental_test.go](../internal/lsp/incremental_test.go), included in the
  Linux consumer lane's `go test -race` step.

## A candidate is not a unique project binding

- **Problem:** Identical AppProject names can exist in several namespaces, and
  unfinished discovery cannot establish the complete candidate set.
- **Cause:** One name match is insufficient evidence for policy, references or
  cross-file rename.
- **Correction:** Unconfigured navigation returns namespace-labelled candidates.
  Authority requires an explicit control-plane namespace; project rename also
  requires a complete closed scope and versioned affected buffers.
- **Regression:** [navigation_defaults_e2e.py](../tests/navigation_defaults_e2e.py),
  [navigation_editing_guards_e2e.py](../tests/navigation_editing_guards_e2e.py) and
  [project_rename_e2e.py](../tests/project_rename_e2e.py).

## Effective YAML values need exact editable origins

- **Problem:** Aliases, merges, malformed input and templates can expose a value
  without a unique safe source replacement.
- **Cause:** Value equality does not establish a physical span; a text replacement
  can change other consumers or introduce new errors.
- **Correction:** Preserve origin classes, restrict edits to eligible exact spans,
  simulate changes and revalidate client versions. Static Helm-source navigation
  offers no semantic edits.
- **Regression:** [alias_provenance_edges_e2e.py](../tests/alias_provenance_edges_e2e.py),
  [projection_repair_e2e.py](../tests/projection_repair_e2e.py),
  [helm_source_e2e.py](../tests/helm_source_e2e.py) and
  [shared_schemaquery_e2e.py](../tests/shared_schemaquery_e2e.py).

## Renderer checks need actual tools and process boundaries

- **Problem:** Chart discovery, unsaved buffers and selected-file scope affect the
  actual rendered input; a successful subprocess does not prove correct cleanup.
- **Cause:** Mock output and broad success checks omit acquisition, tool-version
  compatibility, descendants and generated paths.
- **Correction:** Explicit rendering, saved-buffer checks, bounded acquisition and
  process/output handling, plus checksum-pinned Helm 3 and 4 consumers.
- **Regression:** [render_defaults_e2e.py](../tests/render_defaults_e2e.py),
  [render_scope_e2e.py](../tests/render_scope_e2e.py),
  [bounded_process_e2e.py](../tests/bounded_process_e2e.py) and
  [ablate_config_helm.py](../tests/ablate_config_helm.py).

## Failure and green summaries need independent evidence

- **Problem:** Compiler errors, timeouts, partial case lists and stale reports can
  resemble successful fault detection or passing acceptance.
- **Cause:** A nonzero mutant exit or top-level `passed` flag omits the expected
  reason, complete observations and executed binary identity.
- **Correction:** Passing controls precede named faults. Gates check exact cases,
  assertions, raw observations, hashes and normal completion. Reused ordinary
  binaries are bound to the same workflow run and attempt by build receipts.
- **Regression:** [legacy_mutations.py](../tests/legacy_mutations.py),
  [acceptance_evidence_e2e.py](../tests/acceptance_evidence_e2e.py),
  [observation_evidence_e2e.py](../tests/observation_evidence_e2e.py) and
  [build_receipt_e2e.py](../tests/build_receipt_e2e.py).

## Packaging and file activation do not prove editor behavior

- **Problem:** Valid archives and filesystem installation can coexist with stale
  overrides, cache activation problems or a failing real editor session.
- **Cause:** Native, WASM, file-install and desktop boundaries execute different
  paths; an adapter upgrade does not replace an explicit `serverPath` executable.
- **Correction:** Independent versions, checked native pins, transactional cache
  activation and separate host evidence. Source pins, contract snapshots and
  native-parser provenance advance together.
- **Regression:** [adapter tests](../editors/zed/tests/distribution.rs),
  [install_e2e.py](../editors/zed/tests/install_e2e.py),
  [native_package_e2e.py](../tests/native_package_e2e.py) and the
  [real Zed procedure](../editors/zed/tests/HOST_ACCEPTANCE.md).
