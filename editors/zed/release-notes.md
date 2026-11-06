# Argo CD adapter for Zed — 0.0.1-alpha.3

This precompiled adapter pins the native Argo CD LSP
`v0.0.1-alpha.3`. The native update keeps current-file YAML Schema diagnostics
responsive when scanning a parent workspace with multiple Git repositories
times out or encounters malformed sibling files. The default Argo CD support
window remains **2.0.0–3.6.1**, with no project configuration required.

Install or upgrade using the one-command [README instructions](https://github.com/lazysnail1024/zed-argocd/blob/main/README.md).
Alternatively download `zed-argocd-0.0.1-alpha.3.zip` and verify it with the
accompanying `SHA256SUMS`. The adapter downloads and verifies its pinned native
server when needed. A separate `serverPath` or PATH installation must be
upgraded independently.

Existing alpha.1 and alpha.2 releases remain available. The ZIP contains the
precompiled WASM, snippets and original Rust dependency notices;
`zed-adapter.json` records the exact source. Automated acceptance covers
native CLI/LSP and adapter packaging, not real Zed GUI behavior or store
publication.
