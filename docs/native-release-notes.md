# Argo CD language server - 0.0.1-alpha.7

This prerelease upgrades the pinned shared `k8s-crd-toolkit` dependency to `v0.0.1-alpha.2`, adds explicit analysis/compatibility explanations, render profiles and candidate inspection, and improves LSP performance and lifecycle reliability.

- Upgrades `github.com/lazysnail1024/k8s-crd-toolkit` to published release `v0.0.1-alpha.2` (`toolkit-design-6`), retaining vendored C parser sources and verified lock/snapshot digests.
- Adds read-only analysis-status and guarded refusal explanations (`argocd/analysisStatus`, `argocd.explainAnalysis`), whole-resource compatibility candidate explanations (`argocd/compatibility`, `explain-compatibility`), explicit `schemaVersion: 2` render profiles with owned artifact caching, and explicit unbound `AppProject` candidate inspection and opening.
- Reduces LSP startup time, hover/symbol query overhead, and steady-state memory via a shared compiled schema cache, pass-local canonical path resolution, and document reuse across discovery transitions.
- Fixes worker slot release ordering on the single-slot edit lane so sequential follow-up requests cannot observe `-32803` busy from their own completed predecessor, and defers valid root-reset clears so 40ms batch yields cannot emit false empty workspace diagnostics before open buffers are re-analyzed.

The five-second CLI deadline and existing memory, cancellation and foreground-query limits remain unchanged. The compatibility window remains **2.0.0-3.6.1**.

Native archives target Linux amd64/arm64, macOS amd64/arm64 and Windows amd64, with build/source records, original legal notices and `SHA256SUMS`. The independently versioned `zed-v0.0.1-alpha.7` adapter pins native `v0.0.1-alpha.7`.

Previous prerelease tags and downloads remain unchanged. Native, editor and distribution evidence remain separate acceptance boundaries; this release does not claim publication to the Zed extension store.
