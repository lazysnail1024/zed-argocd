# Argo CD adapter for Zed — 0.0.1-alpha.2

This precompiled adapter pins the native `v0.0.1-alpha.2` language server. That
release restores the standalone ApplicationSet CRDs referenced by Argo CD 2.3.x's
optional stable addons, including legacy `ksonnet` fields in ApplicationSet templates.
Automatic compatibility still covers 2.0.0–3.6.1 without project configuration.

Download `zed-argocd-0.0.1-alpha.2.zip` and follow the
[installation guide](https://github.com/lazysnail1024/zed-argocd/blob/main/README.md).
The adapter verifies and downloads its pinned native server when needed. If you
use a separate `serverPath` or PATH installation, update that executable to the
alpha.2 native release as well.

Published alpha.1 tags, releases and assets remain immutable and unchanged.
Alpha.2 supplies a new adapter and native pin; it does not overwrite alpha.1.

The ZIP includes the compiled WASM, snippets and original Rust dependency notices;
`SHA256SUMS` and `zed-adapter.json` accompany it. Adapter releases have their own
version lifecycle and Rust installer/package acceptance. This release does not
publish to the Zed extension store or claim a real Zed GUI acceptance session.
