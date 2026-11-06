# Diagnostics in large and parent workspaces

Opening a parent directory containing several Git repositories is supported
without a configuration file or version pin. The LSP keeps the editor's declared
workspace roots: a nested `.git` directory is not an implicit analysis boundary.
This preserves parent-level scopes, repository mappings and cross-repository
AppProject references.

## Two analysis phases

1. **Document:** check open buffers first, using the same target, configuration,
   exclusion and Helm-template rules as workspace analysis. This does not walk
   directories or acquire unopened resource YAML. Bounded root configuration
   reads and chart-metadata checks still apply. The result is explicitly partial;
   it provides Schema diagnostics, not a complete reference index.
2. **Workspace:** acquire and analyze the declared workspace, reusing unchanged
   inputs as before. This phase has its own five-second budget, independent of
   the document phase's five-second budget. It adds cross-file diagnostics and
   supplies the existing completeness and freshness checks for navigation and
   edits.

While acquisition is pending or has failed, Schema hover, document symbols and
field completion can use document facts. Reference completion, definition,
references and quick fixes do not gain workspace authority from that partial
view. Both phases remain cancellable and subject to the same epoch/version
delivery checks.

A background timeout or input/parser failure remains visible. It withdraws
unproven workspace results without erasing independently checked current-buffer
diagnostics. Fixing the affected input allows ordinary workspace reconciliation
to recover. Large open buffers and pathological YAML still have their existing
limits; cooperative cancellation is not a hard deadline on arbitrary filesystem
I/O.

## Reading diagnostics and logs

`textDocument/publishDiagnostics` includes the additive
`x-argocd-analysis-scope` field: `document` for the early document phase and
immediate buffer clearing, `workspace` for workspace-phase publication. Existing
clients can ignore it. This is a phase/observability hint, **not a success or
completeness certificate**: a workspace publication can still report incomplete
analysis or retain locally proven diagnostics after failure.

The first diagnostic notification, especially an empty list, does not mean that
cross-file analysis has finished. Consumers requiring workspace results must
wait for the relevant workspace publication and inspect its diagnostics/errors.
An unchanged document result is not repeatedly published for unrelated edits.

`ARGOCD_LS_TRACE=1` enables separate `argocd-document-analysis` and
`argocd-analysis` records, including phase, root, elapsed milliseconds, disk reads
and parsed-file counts. Timeout logs include the phase/root context. Traces do
not include document contents.

## Regression coverage and lessons

`tests/parent_workspace_lsp_e2e.py` starts the actual stdio server against one
parent root containing six real Git repositories. It uses deterministic sibling
failures (an oversized YAML and a YAML stream exceeding the parser limit), not
a machine-dependent guess about how many files take five seconds. It checks
unsaved-buffer diagnostics, hover/completion, new versions, error clearing,
foreign GVKs, cross-repository definition recovery, closing and clean shutdown.
This consumer is required on all five native CI hosts.

On Linux amd64, `tests/parent_workspace_faults.py` additionally compiles isolated
test copies. One shortens only the background deadline and must observe a real
`context deadline exceeded` without losing local checks. Another removes
publication recovery and must fail the precise retention assertion. Compilation
failures, transport timeouts and unrelated runner errors do not count as mutation
detection. No test-only runtime switch is added to the product.

The lessons are to test parent workspaces as well as isolated files, distinguish
early diagnostics from a complete index, and treat exhausted analysis budgets as
incomplete work rather than permission to clear valid local findings. Silently
narrowing analysis at Git boundaries would hide this failure while changing
existing reference semantics.

These are native CLI/LSP and protocol tests, **not real Zed GUI acceptance**.
They do not prove editor rendering, installation or activation on a desktop, and
this change does not itself publish a new adapter or native release.
