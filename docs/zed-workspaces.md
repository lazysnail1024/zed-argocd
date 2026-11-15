# Zed workspaces and multiple repositories

Open a common parent directory when Argo CD resources in several repositories
need to resolve within one product scope. That parent is an explicit indexing
boundary. A nested Git repository does not automatically create a workspace root,
configuration boundary or scope.

The native server's root support and Zed's process allocation are separate
contracts. Use the current-buffer explanation to check the file's actual owner;
use process identities and LSP traffic when checking whether two visible folders
share a server session.

## Choose the intended parent and scope

For example, open `/work/argo-workspace` in Zed:

| Path relative to the opened directory | Purpose |
| --- | --- |
| `.argocd-ls.yaml` | Configuration for the parent workspace root |
| `applications-repo/app.yaml` | An Application with a `spec.project` reference |
| `projects-repo/project.yaml` | Its AppProject declaration |
| `applications-repo/.git/` and `projects-repo/.git/` | Independent Git histories, excluded from resource indexing |

Without configured scopes, admitted files belong to this root's `root` scope.
To describe the intended shared inventory explicitly, put this configuration at
the opened parent:

```yaml
schemaVersion: 1
targetVersion: auto
referencePolicy: open
controlPlaneNamespace: argocd
exclude:
  - "**/generated/**"
scopes:
  - name: shared
    include:
      - "applications-repo/**"
      - "projects-repo/**"
```

Here `argocd` is the example namespace containing the AppProjects; use the actual
control-plane namespace for this inventory. It is distinct from an Application's
destination namespace. The two include patterns belong to the same scope.
Giving each repository a separate scope would isolate their bindings. Matching
names, known namespaces and supported static resource relationships are still
required; directory membership alone does not establish a Project binding.

Keep `referencePolicy: open` for a partial checkout. Choose `closed` only when
that policy describes the intended inventory. Cross-file AppProject Rename also
requires a complete current scope, a unique exact binding, an explicit
control-plane namespace and every affected file open at an accepted client
version. A Definition candidate does not authorize a cross-file edit.

Scope entries can select different `targetVersion` values. Root-level
`referencePolicy` and `controlPlaneNamespace` still apply to that root's scopes.
If repositories need independent root policies, use deliberately separate roots
and account for the host's process behavior below. See the
[configuration reference](configuration.md) for the supported keys and strict
scope/glob rules. Opening a larger parent increases discovery work; choose the
intended parent and describe its inventory with includes and exclusions.

## Distinguish the four boundaries

| Boundary | Meaning |
| --- | --- |
| Visible worktree | A folder shown by Zed as a project root. Host policy assigns language-server instances to it. |
| Native process session | One `argocd-ls serve --stdio` process with its own buffers, roots, configuration override and index lifecycle. |
| Client-supplied workspace root | A local root received through `initialize` or workspace-folder notifications in that session. |
| Product scope | A root-qualified membership and reference boundary. Identical scope names in different owning roots remain separate. |

The native server advertises `workspaceFolders` support and folder-change
notifications in [server.go](../internal/lsp/server.go). That capability alone
does not establish how a particular host allocates visible roots to processes.

The ownership and configuration rules in
[workspace.go](../internal/workspace/workspace.go) apply to the roots actually
received by a native session:

- The server reads `.argocd-ls.yaml` at each owning root. A nested configuration
  file does not become active merely because it is inside another Git repository.
  When opening a common parent, put the intended policy at that parent or use
  explicitly configured scopes.
- Includes and exclusions are relative to the owning root and use `/` in globs.
  Files matching two named scopes produce `SCOPE_OVERLAP`. Unmatched files use
  `unassigned` when named scopes exist; otherwise they use `root`.
- An explicitly opened excluded document can receive isolated document analysis.
  It does not lend definitions to the workspace or establish a complete reference
  inventory. Opening the file and including it in workspace discovery are
  different actions.
- If the **same session** receives overlapping roots, the deepest containing root
  owns the file, independently of root-list order. A nested folder visible in
  Zed is not proof that the parent process received that child root. Different
  processes have independent root lists and indexes.
- Target precedence is the LSP override, then scope target, then root target,
  then `auto`. The current LSP `settings.targetVersion` override is session-wide;
  it cannot represent different overrides for two roots in one session. Product
  configuration files still belong to their individual roots.

Root removal/re-addition is a lifecycle change even when the path string is
unchanged. The server invalidates the removed root and re-evaluates ownership
against the roots it still has. In particular, a retained parent can still own
files under a removed child folder. Removing a visible row is not itself a new
filesystem exclusion.

## Explain the actual buffer and configuration

In the YAML buffer, choose **editor: toggle code actions → Explain current Argo CD
analysis**. At a supported Project/source-reference symbol, position the cursor
on its name to inspect reference certainty and Rename availability as well.
This is an explicit read-only action.

The visible message shows **Root**, **Scope**, **Phase**, separate document and
workspace completeness, and operation refusals with recovery steps. The full
structured response, also available through `argocd/analysisStatus`, carries
additional identity fields:

| Field | What it establishes |
| --- | --- |
| `root`, `roots`, `scope` | The selected owner, the roots received by this session, and its reference boundary |
| `inputIdentity.source`, `uri`, `version`, `revision`, `sha256` | Which accepted buffer lifetime and bytes were analyzed |
| `inputIdentity.configSource`, `configHash` | Whether the owning root's configuration came from an open buffer or `disk-or-defaults`, and its content identity |
| `inputIdentity.targetVersion`, `targetSource` | The selected target and whether it came from `override`, `scope`, `root` or `default` |
| `phase`, `completeness.document`, `completeness.workspace` | Current document analysis versus completeness of the selected workspace scope |
| `operations` | Position-specific reference and Rename availability, with a reference count only when it is proved |

The visible notification does not display every structured identity field.
`roots` is a session-local fact, not a process identifier. Two messages naming
similar paths do not establish that the same process produced them.

`INDEX_INCOMPLETE` asks you to wait or resolve unavailable inputs;
`AFFECTED_FILE_NOT_OPEN` identifies files whose versions are needed for an edit;
`TARGET_UNKNOWN` can identify an unknown relevant namespace. Dynamic, ambiguous
or incomplete reference coverage remains unknown rather than proving zero uses.
After changing a buffer, configuration or root, request a new action. See
[the status interface and refusal codes](configuration.md#analysis-explanations).

For saved configuration, the terminal provides a different view:

```sh
argocd-ls explain-config --root /work/argo-workspace
```

It reports disk/default configuration with `coverage: "configuration-only"`.
It does not inspect an unsaved editor configuration, apply Zed's session settings
or certify a completed resource index. `auto` also does not discover a cluster's
controller version.

## Observed Zed 1.28.1 sessions

Ten logical flows were checked on Linux on 2026-11-15 with Zed
`1.28.1+stable.390.e8bc16acee5ae1937600d571d441c4b5d7b0d288`.
The fixture had two Git repositories, `repo-a` containing declarations and
`repo-b` containing references, beneath `common`. Released WASM adapters used
explicit development-native overrides. These are source-development host
observations; they do not establish delivery in a newly published native package
or a registry installation.

Live UI observations were paired with actual process IDs, running executable
hashes, initialization roots and raw LSP traffic. The native identities were:

| Native build | Source commit prefix | Executable SHA-256 prefix | Observed interval |
| --- | --- | --- | --- |
| Argo CD before the wording fix | `ec32e5d5285b` | `cee4d6efff9e` | Initial common-parent and same-name-before-fix checkpoints |
| Argo CD with the wording fix | `42f251ea5cdb` | `a1e79c4022ba` | Same-name-after-fix, common-parent recovery and subsequent checkpoints |
| Workflows | `0788830d3533` | `a99749a7bce5` | All ten flows in this matrix |

The Argo CD change replaces one existing static ambiguity message. Earlier
observations retain their earlier binary identity. Full hashes and per-session
records are retained with the validation evidence.

| Flow | Actual result and boundary | Checkpoint names, prefixed `step8-` |
| --- | --- | --- |
| Common parent | CD PID `1697` received only `common`; F12 from `repo-b/app.yaml` reached `repo-a/project.yaml`. Explain reported a complete `common/root` scope and `AFFECTED_FILE_NOT_OPEN` after the declaration's preview tab closed. In the later recovery, CD PID `1849` again resolved the unique Project; keeping the declaration open produced one proved reference and available Rename. | `common-parent`, `common-recovery` |
| Separately added roots | CD used PID `2196` for A and `2319` for B, each initialized with only its own root and matching cwd. With two Workflows sessions too, four native processes existed. A's open declaration did not resolve B's reference: B's explanation reported `BINDING_NOT_FOUND`. | `separately-added` |
| Nested folder selected through Add Folder | Selecting `common/repo-b` left one top-level `common` worktree. CD PID `1965` received no new initialization or workspace-folder event; its directory watched-file notification was not a root addition. This did not establish two overlapping visible roots or exercise native deepest-root reassignment. | `nested-visible-root` |
| Remove and re-add the same path | Removing B retired its CD PID `2319` while A PID `2196` survived. Re-adding B with its old tabs retained produced no new B session or request. Closing and reopening those files started CD PID `2411`, initialized with B alone, and restored fresh explanations. This was process replacement, not a same-process folder epoch transition. | `remove-b-open-tabs`, `readd-retained-tabs`, `remove-readd` |
| Same name and namespace | Two AppProject declarations with the same name in the configured `argocd` namespace made both reference certainty and Rename refuse with `AMBIGUOUS_BINDING`. The fixed build visibly described duplicate declarations; `CD_PROJECT_AMBIGUOUS`, severity 2, its value range and the refusal remained unchanged. | `same-name-before-fix`, `same-name-after-fix` |
| Excluded declaration still open | Both declarations remained open while the root configuration excluded `repo-a/**`. CD's included Application reported `CD_PROJECT_UNRESOLVED`; its complete scope still had `BINDING_NOT_FOUND` for references and Rename. This checks that an excluded open document does not lend workspace binding or edit authority; it does not deny isolated document analysis. | `exclude` |
| Child symlinks to outside declarations | A child directory link and file link pointed to matching declarations outside `common`. Only the in-root references were opened. CD did not resolve those targets and reported `BINDING_NOT_FOUND`. This covers child-symlink disk discovery, not an explicitly opened alias or a separately registered alias root. | `symlink` |
| Named scope isolation | The common-root configuration assigned `repo-a/**` to `a` and `repo-b/**` to `b`, with both declarations still open and no exclusion. CD Definition and References returned empty lists; Explain reported complete scope `b` and `BINDING_NOT_FOUND`, preserving the distinction between an unresolved binding and a proved zero reference count. | `named-scope-isolation` |
| Both YAML server preference orders | The preferred server received `prepareRename`. Its foreign-resource result did not cause an observed fallback to the other server. Exact results are separated below. Successful preparation opened the inline editor, then Escape canceled it. | `routing-wf-first`, `routing-cd-first` |
| Invalid root and surviving root | After A's external config change reached CD through a watched-file event, A reported `CONFIG_INVALID`, incomplete analysis and unavailable Rename. B PID `2411` still published its correct unresolved-Project diagnostic for newly edited version 12. Removing A left that same B process alive; further saved edits produced fresh version-24 diagnostics. This establishes isolation between these separate host sessions. | `invalid-before-config-open`, `invalid-root-survivor`, `close-root` |

The two routing results were different:

| YAML preference order | Supported selected resource | Foreign selected resource | Observed fallback |
| --- | --- | --- | --- |
| Workflows, then Argo CD | WF PID `1857` prepared the Workflow resource name after an initial `INDEX_INCOMPLETE` refusal cleared. | Application Project preparation reached WF and returned `-32602 / UNMODELED_BINDING`. | CD received no `prepareRename` in the captured interval. |
| Argo CD, then Workflows | CD PID `1965` prepared `shared-project` at the Application's exact Project value range. | Workflow resource-name preparation reached CD and returned `null`. | WF received no `prepareRename` in the captured interval. |

No Rename was applied in this matrix. The survivor case used real saved comment
edits to establish fresh diagnostic versions. The raw records also retain
transient busy/stale responses; for example, the CD restart initially returned
`-32803` for code actions before a fresh cursor request and Explain succeeded.
Navigation, a visible declaration, a successful preparation and an applied edit
are separate observations.

For the observed remove/re-add behavior, close and reopen affected retained files
if their actions do not reconnect. This recovered fresh sessions here; it is not
a claim that every host version needs that recovery. Closing the final test
window left zero test-native processes in the actual process snapshot. Process
absence and shutdown/exit frames do not supply an unrecorded native exit code.

All captured flows had zero `workspace/didChangeWorkspaceFolders` events. Common
parent indexing and separate visible-root sessions therefore explain these
results; native multi-root support is still a separate capability. Configuration
delivery also differed between the products in this profile: CD received the
external config watch event, while WF's early explanation used its previous
effective configuration and a later refresh detected the invalid disk config
before that config was opened. The observations do not establish instantaneous
external-configuration delivery to both servers.

The checkpoint names identify retained `host/evidence/step8-*` snapshots, with
input hashes, UI observations and protocol/process records. The independent
review snapshot is `research/step8-host-review-before-wf-guard.snapshot.json`
(SHA-256 `3010650fd670370f3d317d8e2d82f5ce8bc40cc82646dc3fcd23f6d02b3fe6be`);
`research/step8-config-refresh-timing-correction.json` records the configuration
ordering clarification. These are validation-bundle identifiers, not additional
source-controlled test fixtures. Later builds require their own identities and
observations.

## Pinned host and extension API evidence

The reviewed Zed 1.28.1 source is commit
`e8bc16acee5ae1937600d571d441c4b5d7b0d288`. Its
[`reuse_existing_language_server`](https://github.com/zed-industries/zed/blob/e8bc16acee5ae1937600d571d441c4b5d7b0d288/crates/project/src/lsp_store.rs#L3375)
returns `None` for a visible worktree. The remaining path searches existing
visible-worktree servers for reuse by an invisible worktree. This source fact
must be paired with actual process identities and initialization/folder traffic;
it is not, by itself, an observation of a particular running session.

The host has
[`add_workspace_folder`](https://github.com/zed-industries/zed/blob/e8bc16acee5ae1937600d571d441c4b5d7b0d288/crates/project/src/lsp_store.rs#L16725)
paths for starting/running servers. The adjacent `_remove_workspace_folder`
helper has no call site in the inspected snapshot, and
[`remove_worktree`](https://github.com/zed-industries/zed/blob/e8bc16acee5ae1937600d571d441c4b5d7b0d288/crates/project/src/lsp_store.rs#L4101)
removes mappings and servers according to remaining owners. Deleting only the
visible-root early return would leave lifecycle work unresolved.

The CD adapter pins
[`zed_extension_api` 0.7.0](https://docs.rs/zed_extension_api/0.7.0/zed_extension_api/trait.Extension.html)
in [Cargo.lock](../editors/zed/Cargo.lock). Its current
[implementation](../editors/zed/src/lib.rs) selects a command and returns workspace
configuration; it does not implement a process-sharing policy. The inspected
public trait and pinned
[extension manifest](https://github.com/zed-industries/zed/blob/e8bc16acee5ae1937600d571d441c4b5d7b0d288/crates/extension/src/extension_manifest.rs#L250)
do not expose a visible-root sharing declaration. The host's
[Rust API source](https://github.com/zed-industries/zed/blob/e8bc16acee5ae1937600d571d441c4b5d7b0d288/crates/extension_api/src/extension_api.rs)
contains newer API methods; its
[`since_v0.6.0` WIT](https://github.com/zed-industries/zed/blob/e8bc16acee5ae1937600d571d441c4b5d7b0d288/crates/extension_api/wit/since_v0.6.0/extension.wit)
is a compatibility interface. Neither is presented as the published 0.7.0 crate
source. These statements apply to the inspected versions, not every current or
future Zed API.

Request routing is another host decision. The pinned
[`request_lsp`](https://github.com/zed-industries/zed/blob/e8bc16acee5ae1937600d571d441c4b5d7b0d288/crates/project/src/lsp_store.rs#L6250)
path selects a capable server in preference order. Process sharing would not
itself add resource-aware routing between the Argo CD and Workflows YAML servers.
Keep the receiving server, a foreign-resource `null` response or typed refusal,
and whether the host retries another server as separate observations.

## Minimal upstream proposal: explicit visible-root sharing

This is a draft host contract for upstream review. It has not been submitted or
implemented as an adapter setting.

1. **Keep default isolation.** An explicit host opt-in may share one language-server
   ID within one project and trust domain when the resolved executable, arguments,
   environment, cwd, initialization options and effective host settings are
   compatible. Different product IDs stay separate even when both serve YAML.
   A changed launch identity or incompatible session-wide override must select a
   different session or an explicit restart, rather than silently changing another
   root's settings. Root-local product configuration remains root-local and need
   not have identical contents.
2. **Require workspace-folder support.** Register only explicitly selected roots;
   do not infer a common ancestor or merge product scopes. Wait for the server's
   folder capability before attaching another visible root to an existing session.
   Roots requiring incompatible host settings remain isolated.
3. **Handle starting and running states.** Coalesce add/remove events against the
   pending initialization set, including removal before startup finishes. Once
   running, deliver the corresponding folder notifications in order, before
   sending new-root document work. A path removed and re-added needs a new
   membership identity so earlier results cannot be reused as current evidence.
   Reattach any retained editor buffers to their new owning session with their
   current accepted text and versions; the folder row alone must not leave them
   attached to a retired server.
4. **Remove ownership completely.** Detach the removed owner's watchers and
   diagnostics and cancel/discard its pending responses and edits. Re-evaluate
   buffers under overlapping retained roots using the current owner/configuration;
   close server-side documents that lose all ownership even if editor tabs remain
   open. Preserve other owners' sessions and work, and shut down when the last
   owner is gone. A later project/session must initialize afresh and cannot accept
   replies from the retired session.
5. **Test lifecycle and routing independently.** Record process/session IDs,
   initialization roots, actual add/remove traffic and accepted buffer versions.
   Cover common-parent and nested roots, same-path re-addition, one failed root
   alongside a valid root, incompatible executable/settings/trust, retained-root
   operation after removal, late old-owner replies and close/reopen. Test both
   product IDs and both YAML preference orders; Rename routing needs its own
   explicit contract and is not fixed by lifecycle sharing alone.

The current common-parent workflow does not depend on this proposal. Native
scope and edit guards continue to define what the server may resolve or change.
