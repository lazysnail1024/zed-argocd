# Argo CD language server — 0.0.1-alpha.6

This prerelease reduces CPU and allocation costs when checking large local workspaces.

- Strict YAML mapping-key validation avoids redundant scalar decoding while preserving duplicate-key, alias, merge, tag and value semantics.
- Diagnostic source lookup avoids decoding a small set of exact field-name spellings; all other spellings retain strict decoding and existing source ranges.

The five-second CLI deadline and existing memory, cancellation and foreground-query limits remain unchanged. The compatibility window remains **2.0.0–3.6.1**.

Native archives target Linux amd64/arm64, macOS amd64/arm64 and Windows amd64, with build/source records, original legal notices and `SHA256SUMS`. The independently versioned `zed-v0.0.1-alpha.6` adapter pins native `v0.0.1-alpha.6`.

Previous prerelease tags and downloads remain unchanged. Native, editor and distribution evidence remain separate acceptance boundaries; this release does not claim publication to the Zed extension store.
