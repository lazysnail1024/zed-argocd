package lsp

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/lazysnail1024/k8s-crd-toolkit/index"
	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	shared "github.com/lazysnail1024/k8s-crd-toolkit/workspace"
	"github.com/lazysnail1024/zed-argocd/internal/analysis"
	"github.com/lazysnail1024/zed-argocd/internal/workspace"
)

func finishIndex(t *testing.T, ctx context.Context, state *workspaceIndex, root string, events workspaceEvents) (reads, parses int) {
	t.Helper()
	for {
		result := state.advance(ctx, 1, []string{root}, nil, "", events)
		events = workspaceEvents{}
		reads += result.stats.DiskReads
		parses += result.stats.ParsedFiles
		if ctx.Err() != nil || len(result.failures) != 0 {
			t.Fatalf("index did not complete: context=%v failures=%v", ctx.Err(), result.failures)
		}
		if !result.more {
			if !result.stats.Complete {
				t.Fatalf("incomplete final state: %+v", result.stats)
			}
			return
		}
	}
}

// The retained domain payload and the shared index must not keep two copies of
// the same navigable facts. Real file events also verify that unchanged input
// reuses those facts and a later semantic edit still reaches its dependents.
func TestIncrementalFactsOwnershipAndReuse(t *testing.T) {
	root := t.TempDir()
	root, err := filepath.EvalSymlinks(root)
	if err != nil {
		t.Fatal(err)
	}
	const project = "apiVersion: argoproj.io/v1alpha1\nkind: AppProject\nmetadata: {name: team, namespace: argocd}\nspec: {sourceRepos: ['*'], destinations: [{server: '*', namespace: '*'}]}\n"
	const app = "apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata: {name: app}\nspec:\n  project: team\n  source: {repoURL: https://example.org/repo}\n  destination: {server: https://cluster, namespace: default}\n"
	for name, text := range map[string]string{
		".argocd-ls.yaml": "schemaVersion: 1\ntargetVersion: '3.0.0'\ncontrolPlaneNamespace: argocd\nreferencePolicy: closed\n",
		"project.yaml":    project,
		"app.yaml":        app,
	} {
		if err := os.WriteFile(filepath.Join(root, name), []byte(text), 0600); err != nil {
			t.Fatal(err)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	state := newWorkspaceIndex()
	defer state.close()
	advance := func(events workspaceEvents) (reads, parses int) {
		t.Helper()
		return finishIndex(t, ctx, state, root, events)
	}
	advance(workspaceEvents{})
	projectURI := source.URI(workspace.FileURI(filepath.Join(root, "project.yaml")))
	appURI := source.URI(workspace.FileURI(filepath.Join(root, "app.yaml")))
	scope, key := indexScope(root, "root"), analysis.ProjectKey("argocd", "team")
	before, _ := state.facts.Definitions(scope, key)
	if len(before) != 1 {
		t.Fatalf("initial project definitions: %+v", before)
	}
	for _, uri := range []source.URI{projectURI, appURI} {
		if !reflect.DeepEqual(state.files[uri].summary.Facts, index.Facts{}) {
			t.Fatalf("private summary retained a second facts copy for %s", uri)
		}
	}
	state.withdraw(string(projectURI))
	reads, parses := advance(workspaceEvents{files: map[string]bool{string(projectURI): true}})
	after, _ := state.facts.Definitions(scope, key)
	if reads != 1 || parses != 0 || len(after) != 1 || after[0].Revision <= before[0].Revision || after[0].Symbol != before[0].Symbol {
		t.Fatalf("unchanged file did not reuse fresh facts: reads=%d parses=%d before=%+v after=%+v", reads, parses, before, after)
	}
	if err := os.WriteFile(filepath.Join(root, "project.yaml"), []byte(strings.Replace(project, "name: team", "name: gone", 1)), 0600); err != nil {
		t.Fatal(err)
	}
	state.withdraw(string(projectURI))
	reads, parses = advance(workspaceEvents{files: map[string]bool{string(projectURI): true}})
	unresolved := false
	for _, diagnostic := range state.files[appURI].diagnostics {
		unresolved = unresolved || diagnostic.Code == "CD_PROJECT_UNRESOLVED"
	}
	uses, _ := state.facts.References(scope, key)
	if reads != 1 || parses != 1 || !unresolved || len(uses) != 1 || uses[0].URI != appURI {
		t.Fatalf("committed dependency was lost: reads=%d parses=%d unresolved=%t uses=%+v", reads, parses, unresolved, uses)
	}
}

func TestWatchCreationBetweenEnumerationAndSweep(t *testing.T) {
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	const project = "apiVersion: argoproj.io/v1alpha1\nkind: AppProject\nmetadata: {name: %s, namespace: argocd}\nspec: {}\n"
	write := func(name, text string) string {
		t.Helper()
		path := filepath.Join(root, name)
		if err := os.WriteFile(path, []byte(text), 0600); err != nil {
			t.Fatal(err)
		}
		return path
	}
	write(".argocd-ls.yaml", "schemaVersion: 1\ntargetVersion: '3.0.0'\ncontrolPlaneNamespace: argocd\n")
	removed := write("removed.yaml", strings.Replace(project, "%s", "removed", 1))
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	state := newWorkspaceIndex()
	defer state.close()
	finishIndex(t, ctx, state, root, workspaceEvents{})
	if err := os.Remove(removed); err != nil {
		t.Fatal(err)
	}
	indexed := state.roots[root]
	state.startScan(indexed)
	// Pause at a real cursor's completion, before the coordinator reconciles
	// membership. A watcher may add a file after its directory has closed.
	batch, err := indexed.scanner.Next(ctx, indexScanWork)
	if err != nil || !batch.Done || !batch.Complete {
		t.Fatalf("fixture discovery did not finish: batch=%+v err=%v", batch, err)
	}
	for _, path := range batch.Files {
		state.enqueue(source.URI(workspace.FileURI(path)), true, shared.Background)
	}
	created := write("created.yaml", strings.Replace(project, "%s", "created", 1))
	state.withdraw(workspace.FileURI(created))
	finishIndex(t, ctx, state, root, workspaceEvents{})
	scope := indexScope(root, "root")
	old, _ := state.facts.Definitions(scope, analysis.ProjectKey("argocd", "removed"))
	fresh, _ := state.facts.Definitions(scope, analysis.ProjectKey("argocd", "created"))
	if len(old) != 0 || len(fresh) != 1 || state.files[source.URI(workspace.FileURI(removed))] != nil || state.files[source.URI(workspace.FileURI(created))] == nil {
		t.Fatalf("completed discovery lost the new event or kept the deleted file: old=%+v fresh=%+v", old, fresh)
	}
}

// A file's domain publication can arrive before the root finishes its final
// domain pass. Exercise that actual batch boundary without clocks or sleeps.
func TestConfigPinWaitsForRootReady(t *testing.T) {
	for _, spelling := range []string{"canonical", "localhost"} {
		t.Run(spelling, func(t *testing.T) {
			root, err := filepath.EvalSymlinks(t.TempDir())
			if err != nil {
				t.Fatal(err)
			}
			const config = "schemaVersion: 1\ntargetVersion: \"3.0.0\"\ncontrolPlaneNamespace: argocd\nreferencePolicy: closed\n"
			const app = "apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata: {name: app}\nspec:\n  project: team\n  source: {repoURL: https://example.org/repo}\n  destination: {server: https://cluster, namespace: default}\n"
			const project = "apiVersion: argoproj.io/v1alpha1\nkind: AppProject\nmetadata: {name: team, namespace: argocd}\nspec: {sourceRepos: ['*'], destinations: [{server: '*', namespace: '*'}]}\n"
			files := map[string]string{".argocd-ls.yaml": config, "app.yaml": app, "project.yaml": project}
			for i := 0; i < 64; i++ {
				name := fmt.Sprintf("other-%02d", i)
				files[name+".yaml"] = strings.Replace(project, "name: team", "name: "+name, 1)
			}
			for name, text := range files {
				if err := os.WriteFile(filepath.Join(root, name), []byte(text), 0600); err != nil {
					t.Fatal(err)
				}
			}
			state := newWorkspaceIndex()
			defer state.close()
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			configURI := workspace.FileURI(filepath.Join(root, ".argocd-ls.yaml"))
			if spelling == "localhost" {
				configURI = strings.Replace(configURI, "file://", "file://localhost", 1)
			}
			appURI := workspace.FileURI(filepath.Join(root, "app.yaml"))
			opened := strings.Replace(config, "3.0.0", "3.5.6", 1) + "scopes:\n  - name: dev\n    include: [\"**\"]\n"
			buffers := map[string]buffer{}
			for uri, text := range map[string]string{configURI: opened, appURI: app} {
				snapshot, err := state.documents.Open(source.URI(uri), text)
				if err != nil {
					t.Fatal(err)
				}
				buffers[uri] = buffer{uri: uri, snapshot: snapshot, version: 1, revision: snapshot.Revision()}
			}
			params, _ := json.Marshal(map[string]any{"textDocument": map[string]any{"uri": configURI}, "range": analysis.Range{Start: analysis.Position{Line: 5, Character: 4}, End: analysis.Position{Line: 5, Character: 13}}})
			request := message{Method: "textDocument/codeAction", Params: params}
			pins := func(latest completed) []any {
				input := state.query(ctx, request, latest, completed{}, buffers, []string{root}, "", 1)
				answer, code, reason, handled := pinRequest(ctx, request, input, buffers, 1, true, []string{root}, "")
				actions, ok := answer.([]any)
				if code != 0 || !handled || !ok {
					t.Fatalf("pin query failed: code=%d reason=%s handled=%t answer=%#v", code, reason, handled, answer)
				}
				return actions
			}
			var latest completed
			earlyPublication := false
			for {
				latest = state.advance(ctx, 1, []string{root}, buffers, "", workspaceEvents{})
				if ctx.Err() != nil || len(latest.failures) != 0 {
					t.Fatalf("index failed: context=%v failures=%v", ctx.Err(), latest.failures)
				}
				for _, publication := range latest.publications {
					if !earlyPublication && publication.uri == appURI && publication.phase == "workspace" && latest.more {
						earlyPublication = true
						if actions := pins(latest); len(actions) != 0 {
							t.Fatalf("pin offered before root Ready: %#v", actions)
						}
					}
				}
				if !latest.more {
					break
				}
			}
			if !earlyPublication || !latest.stats.Complete {
				t.Fatalf("fixture did not cover publication before Ready: early=%t stats=%+v", earlyPublication, latest.stats)
			}
			actions := pins(latest)
			if len(actions) != 1 {
				t.Fatalf("complete root must offer the pin: %#v", actions)
			}
			data := actions[0].(map[string]any)["data"].(fixData)
			if data.URI != configURI || data.Target != "3.5.6" || data.NewText != "    targetVersion: \"3.5.6\"\n" {
				t.Fatalf("pin lost client identity or exact insertion: %+v", data)
			}
		})
	}
}
