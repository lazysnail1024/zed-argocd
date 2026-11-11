# Argo CD Zed adapter — 0.0.1-alpha.4

This precompiled Zed adapter pins the native Argo CD language server `v0.0.1-alpha.4`, with zero-configuration compatibility for Argo CD **2.0.0–3.6.1**.

The new native LSP shares reusable CRD parsing/query primitives and improves current-file responsiveness in large multi-repository workspaces. It adds bounded workspace symbols, safe source-aware edits, and conditional cross-file AppProject renaming. Operations requiring an incomplete or ambiguous scope remain deliberately unavailable.

Install or upgrade using the [README instructions](https://github.com/lazysnail1024/zed-argocd/blob/main/README.md), or download `zed-argocd-0.0.1-alpha.4.zip` with the accompanying `SHA256SUMS`. The adapter fetches and verifies its pinned native server when needed; users who override `serverPath` or PATH must update that executable separately.

The ZIP contains the precompiled WASM, snippets, and original Rust dependency notices. `zed-adapter.json` records exact source identity. CI covers native CLI/LSP, installer behavior and package verification, not real Zed GUI behavior or extension-store publication.
