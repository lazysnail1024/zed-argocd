# Native distribution and Zed store readiness

Status reviewed on **2026-11-05**: the distribution code has been changed for the
store requirements, but no first native Release or completed Zed store
publication is claimed. The pinned `v0.1.0` Release was absent during the review.
A precompiled developer ZIP, a passing native process test, and a compiled WASM
adapter do not establish real Zed editor acceptance or registry approval.

## Server selection and trust boundary

The adapter selects an executable in this order:

1. `lsp.argocd-ls.settings.serverPath`, if explicitly present. It must be an
   absolute path for the host OS and must pass the product, adapter-contract,
   analysis-readiness and supported-schema identity checks. Development builds
   remain supported. A wrong type, invalid path or failed identity stops startup.
2. `argocd-ls` found by Zed's worktree shell `PATH`, with the same identity checks.
   A discovered but incompatible executable produces an error rather than being
   silently replaced by a download.
3. The exact native Release pinned by the adapter's `CARGO_PKG_VERSION`.

Custom paths are validated by Zed's process API. They are never read through
WASI filesystem calls outside the extension sandbox. Zed's generic `binary.path`
setting bypasses the adapter; use `settings.serverPath` when testing its checks.

The extension contains no native payload and has no `bundled-server` feature.
Both the checksum manifest and native executable are fetched through
`zed_extension_api::download_file(..., Uncompressed)`, so Zed's download policies
apply to both. No alternative HTTP client bypasses those policies. The identity
probe uses the existing narrowly scoped `process:exec` capability for
`version --json`. A denied download or process capability remains an installation
error and must be resolved in the editor's permissions.

## Release asset contract

The extension and native server currently pin version **0.1.0**. Release tag
`v0.1.0`, `Cargo.toml`, `extension.toml`, and `identity.serverVersion` must agree.
Managed native identity must additionally have `product=argo-cd`,
`adapterContractVersion=1`, `analysisReady=true`, `buildMode=release`,
`dirty=false`, and a full 40-character lowercase source commit. The release
pipeline verifies that the native source commit is the tagged checkout.

| Zed host | Raw executable asset |
| --- | --- |
| Linux x86_64 | `argocd-ls-0.1.0-linux-amd64` |
| Linux aarch64 | `argocd-ls-0.1.0-linux-arm64` |
| macOS x86_64 | `argocd-ls-0.1.0-darwin-amd64` |
| macOS aarch64 | `argocd-ls-0.1.0-darwin-arm64` |
| Windows x86_64 | `argocd-ls-0.1.0-windows-amd64.exe` |

Downloads use the fixed repository and tag:
`https://github.com/lazysnail1024/zed-argocd/releases/download/v0.1.0/<asset>`.
There is no latest-release lookup on startup. Unsupported hosts need a compatible
native build selected through `serverPath` or `PATH`.

A release also provides `SHA256SUMS` and the existing deterministic native
archives (`.tar.gz` on Linux/macOS, `.zip` on Windows). The archives retain the
native binary, MIT license, actual Go/module/CRD/native-runtime legal texts and
notices, and complete native build provenance produced by `tools/native.py`.
Their hashes, as well as the five raw binary hashes, are included in the global
checksum manifest. Preserve these companion legal archives when redistributing
native binaries; the raw asset is used by the installer to avoid unverified
archive extraction.

Each checksum line is exactly a lowercase SHA-256, two spaces, a safe asset
filename, and a newline. The installer rejects malformed entries, duplicate
filenames, missing exact assets and unsafe names; it never selects a substring or
another platform's checksum. It bounds manifest reads and streamed binary hashes,
rejects empty and non-regular files, and verifies the SHA-256 before setting
executable permissions or invoking any downloaded bytes.

Checksums from the same HTTPS Release provide integrity and mismatch detection.
They are not independent publisher signatures, attestations, or proof against a
compromised publisher. The workflow refuses to replace an existing pinned
Release. Publish corrections under a new version and update the adapter pin.

## Cache, offline operation and recovery

All managed files live under Zed's extension work directory. A slot such as
`servers/v0.1.0-linux-amd64/` contains the executable and its exact `SHA256SUMS`.
The adapter derives the **absolute host executable path** from the extension work
directory for both its identity probe and language-server command; it does not
assume that the Zed host process shares the WASI working directory.

A valid slot is reused without network access. Each startup still verifies the
checksum and identity, so a cached filename or old in-memory path cannot bypass
verification. Offline first installation requires a previously installed
compatible executable through `serverPath` or `PATH`; a missing or damaged cache
cannot become verified while offline.

Each attempt downloads into a uniquely reserved staging directory. Interrupted
or failed attempts remove only their own staging data. Abandoned staging data
from process termination is never a candidate executable and does not prevent a
new attempt. A complete staging directory is activated only after checksum,
executable-permission and identity checks succeed. If another Zed process has
already installed the same version, its verified slot is reused. A broken current
slot is moved aside before promotion; an unsuccessful promotion attempts to
restore it. Permission errors, file locks and cleanup errors remain visible.

The installer does not overwrite a running binary in place. Windows antivirus or
file locks may still prevent replacement; close Zed and retry if the error asks
for it. It does not remove unrelated files or all older slots in an attempt to
recover. Cache directory and file symlinks are refused. Local permission and
symlink defenses are not a guarantee against a process that can concurrently
modify the user's extension work directory.

Version changes use separate slots. A failed upgrade preserves previous versions
but returns an error instead of silently running an old server. Roll back by
installing the previous adapter version or deliberately selecting a compatible
known-good server through `serverPath`. Old cache slots are retained for that
purpose; close Zed before removing obsolete slots manually. Restart the language
server after restoring network access to retry a failed install.

## Reused build and publication pipeline

`tools/native.py build` and `tools/native.py package` remain the only native build
and archive producers. They already validate exact inputs, compiler/toolchain,
actual native identity, platform, runtime dependencies and legal files. Release
mode requires a clean, exact committed checkout and an explicit stable version.
The new release helper stages their verified raw executables and native archives,
then aggregates a complete, mutually consistent five-platform asset set with
checksums. It does not cross-compile or replace the existing native consumers.

The reusable native workflow runs on real Linux amd64/arm64, macOS amd64/arm64 and
Windows amd64 hosts. Each job executes the existing required native CLI/LSP
consumers, production Rust installer tests, separate developer-package tests,
release packaging acceptance tests, and the actual adapter-only WASM build.
Release requests use the same workflow with a release version rather than a
second, reduced platform matrix. Asset staging happens only after that platform's
required steps pass. The release workflow aggregates all five successful native
artifacts before preparing its release output.

The first release is still a maintainer action: review the intended source and
version before pushing its tag or dispatching the workflow on that tag. Once all
gates pass, the workflow creates a draft with the complete verified assets, then
publishes it after upload succeeds. It has no additional manual approval job. An
interrupted draft must be inspected before retrying; the workflow refuses to
clobber an existing Release. Check its exact tag/commit, complete asset and
checksum inventory, native evidence, license contents and platform outcomes.
The installer needs a public published Release: private drafts cannot satisfy
its URLs. Do not substitute arbitrary workflow artifacts for the public release
contract, and do not upload new bytes over an already published version.

## Automated acceptance and its limits

The Rust acceptance suite calls the production installer with real temporary
files and fake Zed transport, permission and process boundaries. It checks:

- Exact mapping for all five platforms; unsupported host and unsafe pin refusal.
- Strict checksum parsing, wrong or missing asset, duplicate/truncated manifest,
  empty/corrupt binary, and zero process execution before integrity validation.
- Interrupted downloads, cleanup, missing releases, retry, chmod and process
  failures, exact release identity and permissive valid developer identity.
- Offline cache reuse, per-startup revalidation, corrupted or absent cache repair,
  wrong cached version, upgrade failure, explicit rollback and path priority.
- Concurrent completed installation reuse, preserved cache on failed replacement,
  and abandoned staging. Unix tests additionally exercise unsafe symlinks; Windows
  GUI/junction/antivirus behavior remains a manual acceptance concern.

The Python developer-package suite reuses real native packaging and executes the
actual separate Go executable. It checks deterministic archives, exact hashes,
legal text, adapter-only contents, install folder layout and rejected native
manifest corruption. Release tests exercise the real producer and rejected
incomplete or inconsistent platform sets. Fixture data for aggregation rules is
not evidence that a foreign native platform ran.

These tests do not host the adapter in real Zed. A successful run on a previous
commit also does not establish the submitted commit's behavior.

## Required real Zed verification

All five platforms require a new manual record for this distribution change.
Earlier native CI or Linux development-bundle use does not satisfy these rows.
Record OS version, CPU, Zed version, exact extension commit, release tag, asset
SHA-256, and logs/screenshots or a reproducible transcript.

| Platform | Native CI runner configured | Real Zed installer acceptance |
| --- | --- | --- |
| Linux amd64 | `ubuntu-24.04` | **Not performed for this change** |
| Linux arm64 | `ubuntu-24.04-arm` | **Not performed for this change** |
| macOS amd64 | `macos-15-intel` | **Not performed for this change** |
| macOS arm64 | `macos-15` | **Not performed for this change** |
| Windows amd64 | `windows-2025` | **Not performed for this change** |

For each platform, test a clean installation of the exact proposed submodule
commit with no local override, then complete these cases:

1. Download the public pinned native Release through Zed, approve only the
   documented capabilities, and verify `version --json`, YAML schema diagnostics,
   completion, hover and local navigation.
2. Restart with the network disabled and confirm the verified cache starts
   without download requests. Separately confirm offline first install produces
   a useful error, and an explicit existing `serverPath` works offline.
3. Interrupt a first download; retry. Alter or truncate the cached binary, then
   confirm it does not run offline and is repaired after connectivity returns.
   Exercise a missing checksum/asset, denied download or process permission, and
   platform execution failure without pretending those are successful starts.
4. Install an adapter pinning a newer test release, verify a distinct cache slot,
   and interrupt or fail that upgrade. Confirm no older server starts silently,
   then test deliberate rollback and recovery. Do not overwrite production tags
   to create failure cases; use a maintainer-controlled test build/fixture server.
5. Check valid and invalid `serverPath`, including paths with spaces and Windows
   drive/UNC paths; confirm explicit settings retain priority. On Windows test a
   running server and antivirus/file-lock recovery. On macOS verify downloaded
   executable permissions and any OS security prompts.
6. Confirm diagnostics update after edit/save/watch events and the existing
   reconciliation behavior; keep the general YAML server enabled and note any
   duplicate schema diagnostics.

Native runner OS versions are tested build environments, not a promised minimum
OS range. Establish minimum OS support separately before advertising it.

## Remaining registry review conditions

Follow the current upstream documents:

- [Publishing prerequisites](https://github.com/zed-industries/zed/blob/main/docs/src/extensions/publishing/prerequisites.md)
- [Publishing guide](https://github.com/zed-industries/zed/blob/main/docs/src/extensions/publishing/publishing-guide.md)
- [License requirements](https://github.com/zed-industries/zed/blob/main/docs/src/extensions/publishing/license-requirements.md)

The implementation removes the direct language-server payload and uses the
required download/environment mechanisms. The existing `argocd-lsp` ID is a
unique kebab-case LSP identifier; user-facing manifest strings are English and the
MIT LICENSE is at the adapter root. These code properties do not resolve the
remaining publication steps:

- Publish the complete public `v0.1.0` native Release only after all five native
  gates pass for its exact clean commit, with all checksum and legal assets.
- Finish and record manual real-Zed testing of the exact extension commit. Resolve
  platform startup and UX findings before claiming support.
- Explain the specialized offline, versioned Argo CD catalogs, AppProject and
  ApplicationSet semantics, navigation and safe edits. Generic YAML/Kubernetes
  CRD extensions already cover some schema functionality; registry maintainers
  decide whether the specialized functionality meets their overlap policy.
- Submit the public HTTPS repository as a Git submodule in
  `zed-industries/extensions`, pointing at an existing commit on a branch. Make
  the registry and extension versions match and run the registry's
  `pnpm sort-extensions` step.
- Submit one extension per registry PR and await review/merge. The Argo Workflows
  extension requires its own registry PR. Publication follows registry approval;
  this repository's implementation PR and its tests do not publish either
  extension.
