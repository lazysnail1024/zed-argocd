# Argo CD language server — 0.0.1-alpha.4

This prerelease distributes the shared CRD editing, bounded current-file analysis and navigation improvements merged into main after alpha.3.

- Shared toolkit YAML value/source projection and Schema queries replace duplicate private implementations; Argo CD resource/version semantics and safety proofs remain in the language server.
- Current-file diagnostics and light queries use bounded scheduling, independent of slow or incomplete workspace indexing. Per-file input budgets avoid false errors when other open YAML buffers are large.
- Workspace symbols and source-aware navigation are expanded. Versioned field/enum code actions and multi-source Application references can be resolved and edited.
- Static AppProject names and their Application/ApplicationSet references can be renamed together only with a complete, closed scope, explicit namespace, unique sources, current document versions and validated binding preservation. Ambiguous or incomplete edits are refused.
- Incremental workspace readiness and stable diagnostic publication reduce unnecessary full-file rescans and transient errors in parent workspaces containing many Git repositories.

The zero-configuration Argo CD compatibility window remains **2.0.0–3.6.1**. No target cluster version is inferred.

Native binaries and archives cover Linux amd64/arm64, macOS amd64/arm64 and Windows amd64, with source/build records, original legal notices and `SHA256SUMS`. Independently versioned `zed-v0.0.1-alpha.4` pins this `v0.0.1-alpha.4` server.

These CLI/LSP, resource, and distribution tests do **not** constitute real Zed GUI acceptance or publication to the Zed extension store. Previous prerelease tags and downloads remain unchanged.
