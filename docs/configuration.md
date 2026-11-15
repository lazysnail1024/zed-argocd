# Configuration and local analysis

No configuration file is required. The default target is `auto`, the reference
policy is `open`, and each workspace root has its own analysis scope. Schemas are
embedded; opening a file does not download them or contact a cluster. The
[bundled catalog](multi-version-support.md) lists the exact version window and
collection cutoff.

## Target selection

Automatic validation requires a complete schema from at least one supported
release. Fields that never coexisted in a release cannot be combined into a valid
document. Completion and hover still support repair when input has errors.
For a known controller, select an exact target:

```sh
argocd-ls check --root . --target-version 3.6.1 --format json
```

Or create `.argocd-ls.yaml` at the workspace root:

```yaml
schemaVersion: 1
targetVersion: "3.6.1"
referencePolicy: open
```

Target precedence is CLI `--target-version` or the LSP target override, then the
matching scope's target, then root `targetVersion`, then `auto`. Unsupported exact
versions fail instead of falling back. Removing an override restores the next
level; removing all target keys restores `auto`.

Configuration belongs to the root supplied to the server. A nested
`.argocd-ls.yaml` does not create a new root or scope by itself. For several
environments under a common parent, use explicit scopes or separate editor roots.

## File configuration

The loader in [workspace.go](../internal/workspace/workspace.go) rejects unknown
fields, invalid types, unsupported `schemaVersion` and invalid policy values.

| Field | Default and behavior |
| --- | --- |
| `schemaVersion` | `1`; only version 1 is accepted. |
| `targetVersion` | `auto` when omitted; the root's default catalog target. |
| `referencePolicy` | `open`; accepts `open` or `closed`. A complete closed scope can treat an unresolved project as an error. |
| `controlPlaneNamespace` | Unset; the namespace containing AppProjects. It is not the destination namespace or necessarily the Application namespace. |
| `exclude` | Additional root-relative file globs. `.git`, `node_modules`, `vendor` and `.argo-ls` directories are excluded by default. |
| `scopes` | List of `{name, include, targetVersion}`. Names must be nonempty and unique; `unassigned` is reserved. `include` must be nonempty. |
| `repositories` | List of `{repoURL, targetRevision, path}` mapping a specific source/revision to a local checkout below this root. `repoURL` and `path` are required. No clone or network access occurs. |

Example for a common parent containing two environments and a local repository:

```yaml
schemaVersion: 1
referencePolicy: closed
controlPlaneNamespace: argocd
exclude:
  - "**/generated/**"
scopes:
  - name: production
    include: ["environments/prod/**", "projects/prod/**"]
    targetVersion: "3.6.1"
  - name: legacy
    include: ["environments/legacy/**", "projects/legacy/**"]
    targetVersion: "3.0.6"
repositories:
  - repoURL: https://example.org/team/apps.git
    targetRevision: main
    path: repos/apps
```

Use `/` in root-relative globs on every platform. `**` spans path components;
ordinary components use `*`, `?` and character classes. Absolute paths, backslashes
and patterns containing `..` are rejected. A file in multiple named scopes causes
`SCOPE_OVERLAP`. Unmatched files belong to `unassigned`; without named scopes they
belong to `root`. References do not silently cross those boundaries.

An opened excluded file receives isolated document analysis without lending its
definitions to the workspace. Explicit CLI `--file` can select an excluded file;
this does not change the policy for other files. Overlapping editor roots use the
deepest containing root as file owner.

Repository mappings match the literal `repoURL` and `targetRevision` pair. Their
paths must stay inside the workspace and name accessible local checkouts. Static
source paths can then be checked locally. This does not establish that checkout
bytes match a remote Git revision.

## Project bindings and edit authority

Without a control-plane namespace, definitions can show local namespace-labelled
AppProject candidates. That does not establish a binding: project policy, reverse
references and project rename require an explicit `controlPlaneNamespace`. A
complete scope with no namespace produces informational
`CD_PROJECT_NAMESPACE_UNBOUND` diagnostics.

With an explicit namespace, a missing local project is informational under `open`
and an error under `closed`; duplicate candidates remain ambiguous. Incomplete
discovery cannot prove absence. Cross-file AppProject rename additionally needs
a complete closed scope, unique exact bindings, accepted client versions for all
affected open files and successful edit simulation. See
[architecture](architecture.md#queries-and-edits) for freshness checks.

## Zed and other LSP clients

Zed forwards native settings from:

```json
{
  "lsp": {
    "argocd-ls": {
      "settings": {
        "targetVersion": "3.6.1",
        "serverPath": "/absolute/path/to/argocd-ls"
      }
    }
  }
}
```

Both fields are optional. `serverPath` is adapter configuration, not a key in
`.argocd-ls.yaml`; an invalid explicit path fails instead of falling back.
Other clients can set `initializationOptions.targetVersion` and later send
`workspace/didChangeConfiguration` with `settings.targetVersion`. The CLI does
not read editor settings. [Distribution](distribution.md#server-selection-and-offline-use)
describes executable selection and identity checks.

## Analysis explanations

The **Explain current Argo CD analysis** source action is available to clients
advertising CodeAction literal support, including clients without versioned edit
support. It invokes `argocd.explainAnalysis` and displays one `window/showMessage`
for that explicit request. Background analysis does not emit these messages.
Actions bind both the client version and the server's input revision, so an old
action is rejected after editing or closing and reopening the same file.

Other clients can request `argocd/analysisStatus` with ordinary
`textDocument.uri` and `position` parameters. The selected document must be an
accepted open buffer. The structured response includes:

- `root`, the deepest owning root, `roots`, exactly the roots supplied by the
  client, and the selected `scope`.
- `inputIdentity`: buffer URI, client version, server revision, text SHA-256,
  configuration hash/source and target version/source.
- `phase` and independent `completeness.document` and
  `completeness.workspace` facts. Completeness describes performed analysis,
  not whether the document is error-free.
- `operations`: reference-query and Rename preparation availability for the
  current position. A reference `resultCount` is emitted only when the selected
  scope and supported static binding establish the count. Dynamic, ambiguous,
  implicit-default and unmapped uses remain unknown. Rename still validates the
  new name and simulates edits when it is actually requested.

The status is a projection of current analysis and existing guards. It does not
create another index, change `referencePolicy` or fill in a namespace. Stable
codes in refusals and Rename `error.data` include:

| Reason code | Meaning and recovery |
| --- | --- |
| `INDEX_INCOMPLETE` | Discovery or relevant analysis is incomplete; wait or correct the reported unavailable inputs. |
| `ISOLATED_DOCUMENT` | The opened file is excluded from workspace indexing; its independent checks do not authorize cross-resource operations. |
| `TARGET_UNKNOWN` | The control-plane namespace or a relevant namespace is unknown; review the intended target. |
| `REFERENCE_SCOPE_OPEN` | Cross-resource Rename requires an intentionally closed scope; an open policy is not changed automatically. |
| `AMBIGUOUS_BINDING` / `BINDING_NOT_FOUND` | Resolve duplicate definitions or the missing binding in the selected scope. |
| `AFFECTED_FILE_NOT_OPEN` | Open the identified affected file so its client version can participate in the edit proof. |
| `DYNAMIC_REFERENCE` / `REFERENCE_COVERAGE_UNKNOWN` | Dynamic or unmapped uses prevent a complete reference count or safe edit. |
| `BUILTIN_BINDING` | The default Project has implicit uses and cannot provide an ordinary complete explicit-reference proof. |
| `STALE_INPUT` | Request a new action against the current buffer and configuration. |

Each refusal includes `message`, `relatedLocations` and `nextStep`. Read-only
reference availability does not require affected files to be open or the scope
to authorize edits. Separately, `explain-config` always reads disk configuration
and reports `inputSource: "disk"` and `coverage: "configuration-only"`; successful
configuration acquisition does not certify Schema or domain analysis.

### Compatibility candidates

`argocd/compatibility` accepts `textDocument.uri` for an accepted open buffer and
returns the independent resources in that file. The source action kind is
`source.argocd.explainCompatibility`, invoking `argocd.explainCompatibility` with
the same URI/client-version/server-revision argument binding as the status action.
The explicit command emits one `window/showMessage` with the candidate label,
counts, sampled versions, exclusion reason and limitations. Direct requests and
background analysis do not emit unsolicited compatibility notifications.

The result's `inputIdentity` records the accepted buffer, actual root/scope,
selected target and disk/default or unsaved-buffer configuration. It is fenced
again before delivery. A cold/incomplete workspace index does not block this
document-only explanation. `explain-compatibility --file PATH --root DIR` provides
the corresponding disk report; paths are relative to that root and excluded files
are isolated. See [candidate meaning and limits](multi-version-support.md#explicit-compatibility-explanation).

## CLI output and limits

```sh
argocd-ls explain-config --root .
argocd-ls explain-compatibility --root . --file environments/prod/app.yaml
argocd-ls check --root . --scope production --format json
argocd-ls check --root . --file environments/prod/app.yaml --warnings-as-errors
argocd-ls version --json
```

`--scope` selects a configured scope. `--file` filters output to that file while
retaining its scope's available dependencies. Selecting a file and a conflicting
scope produces `FILE_SCOPE_MISMATCH`.

| `check` exit code | Meaning |
| --- | --- |
| `0` | Complete analysis with no errors, and no warnings when `--warnings-as-errors` is set. |
| `1` | Complete analysis found an error, or a warning promoted by `--warnings-as-errors`. |
| `2` | Invalid input/configuration, incomplete analysis or output failure. |
| `3` | Cancellation or the five-second analysis deadline. |

JSON retains `analysisComplete`, per-target completeness, `configurationErrors`,
diagnostics and `notEvaluated`. Check completeness and exit code together. Static
validation does not run controllers, admission, source hydration or dynamic
generators. Input and analysis budgets are fixed
[implementation limits](architecture.md#workspace-state-and-budgets), not settings
for suppressing failures. Explicit Helm rendering is described in the
[README](../README.md#helm-sources-and-explicit-rendering).
