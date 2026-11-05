# Distribution and installation

## Two independent releases

| Component | Tag | Download |
| --- | --- | --- |
| Native LSP / CLI | `v0.0.1-alpha.1` | Platform executable and archive, source/build records, `SHA256SUMS` |
| Zed adapter | `zed-v0.0.1-alpha.1` | `zed-argocd-0.0.1-alpha.1.zip`, adapter record, `SHA256SUMS` |

Both are GitHub **prereleases** before the first `0.0.1`. The adapter uses its
explicit `native-server-version` pin and direct release URL; it never relies on
GitHub's latest-stable-release endpoint. Published versions are not overwritten.
Corrections receive a new alpha number.

## Release automation

Merging a change to root `VERSION` into `main` starts the native release workflow.
Merging a change to `editors/zed/extension.toml` starts the independent Zed release
workflow; its version must also match `editors/zed/Cargo.toml`. Each workflow runs
its complete acceptance gate, then creates its version tag at the exact verified
commit and publishes the checked artifacts. Matching version-tag pushes and
manual runs on `main` or that exact tag are also supported. Existing tags are
never moved, and an existing release or draft stops publication.

A change to `.github/workflows/release.yml` on `main` also retries the native
pipeline, so a publication-workflow repair can complete an unpublished version.
It still runs every gate and refuses an existing release, draft, or tag pointing
at another commit; this retry path never replaces already published artifacts.

The Zed publication step checks its committed `native-server-version` pin before
creating a tag or publishing a ZIP. The native release must already be published
and contain `SHA256SUMS` plus raw executables for Linux amd64/arm64, macOS
amd64/arm64 and Windows amd64. Initial native and adapter releases can start
together: a missing native release (HTTP 404) or a draft is polled every 30 seconds
for at most 45 minutes. Permission errors, other HTTP errors, a mismatched tag or
a published release with missing or incomplete required assets fail immediately.
The adapter does not rebuild the server or require it to come from the adapter's
own source commit. If the wait expires, retry the failed publication job after
the pinned native release becomes available.

## Precompiled Zed installation

The portable ZIP contains `argocd-lsp/extension.toml`, `extension.wasm`, snippets and
licenses. With Zed closed, place that folder below its `extensions/installed`
directory as shown in the root README. This matches Zed's installed extension
layout. Preserve an older folder before replacement; remove this folder while
Zed is closed to uninstall the manual alpha.

The downloaded component is already compiled. A source developer can instead
select `editors/zed` with Zed's **Install Dev Extension** command, which requires
Rust. The extension store is a separate distribution channel and has not been
published by this release.

## Verify downloads

Download `SHA256SUMS` from the same release. On Linux, place the requested assets
alongside it and run `sha256sum --check --ignore-missing SHA256SUMS`. At least the
chosen archive must report `OK`. On macOS, compute `shasum -a 256 <archive>` and
compare the full digest with that archive's exact line in `SHA256SUMS`. On Windows,
use `Get-FileHash <archive> -Algorithm SHA256` and compare the full digest.

A checksum supplied by the same HTTPS release detects corruption and version
mismatch; it is not a separate publisher signature. Archives retain their
original dependency, runtime and schema licenses and exact source identity.

## Server selection and offline use

The adapter chooses:

1. An explicit absolute `lsp.argocd-ls.settings.serverPath`.
2. `argocd-ls` found through Zed's worktree shell PATH.
3. The platform asset from the exact native release pin.

An invalid explicit selection is reported. For managed downloads, SHA-256 is
checked before execution, then the native product, contract, readiness and release
identity are verified. Downloads activate a new complete version/platform cache
directory; a failed download leaves previous versions intact. Existing valid
cache entries are rechecked and reused without network access.

For offline first use, extract the native platform archive and put its executable
on PATH, or configure its absolute path in Zed settings:

```json
{
  "lsp": {
    "argocd-ls": {
      "settings": { "serverPath": "/absolute/path/to/argocd-ls" }
    }
  }
}
```

Use a Windows absolute path and `.exe` suffix on Windows. Restore execute bits
with `chmod +x argocd-ls` after ZIP extraction on POSIX systems when necessary.
Existing custom `serverPath` or PATH installations retain priority on upgrades.

## Troubleshooting and acceptance boundary

Use `zed: open log`, check the selected executable with `version --json`, confirm
the exact pinned release and platform asset exist, and restart the language server
after correcting permissions or network access. Keep the general YAML language
server enabled. Ordinary Argo YAML receives semantic analysis; raw Helm template
sources are not silently rendered. Use explicit `render-check` for rendered input.

Native CI executes five actual operating-system/architecture hosts. Rust tests
exercise installer transport and process boundaries through the production
installer's host interface. Package tests inspect the compiled WASM and archive,
and native tests execute extracted servers. These do not claim a full real Zed
GUI session on every platform. Manual GUI acceptance remains separately tracked
for this alpha; the release is not a Zed extension-store approval.

Upstream installation behavior is defined by Zed's
[extension host](https://github.com/zed-industries/zed/blob/main/crates/extension_host/src/extension_host.rs)
and [platform paths](https://github.com/zed-industries/zed/blob/main/crates/paths/src/paths.rs).
