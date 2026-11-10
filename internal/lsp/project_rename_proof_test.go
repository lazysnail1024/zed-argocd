package lsp

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/zed-argocd/internal/workspace"
)

// Real index transitions exercise the proof between notification acceptance
// and background work. No deadline race or artificial worker delay is needed.
func TestProjectRenameProofRejectsNewMembersAndRootRecreation(t *testing.T) {
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	const app = "apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata: {name: app}\nspec:\n  project: team\n  source: {repoURL: https://example.org/repo}\n  destination: {server: https://cluster, namespace: default}\n"
	const project = "apiVersion: argoproj.io/v1alpha1\nkind: AppProject\nmetadata: {name: team, namespace: argocd}\nspec: {sourceRepos: ['*'], destinations: [{server: '*', namespace: '*'}]}\n"
	state := newWorkspaceIndex()
	defer state.close()
	buffers := map[string]buffer{}
	for name, text := range map[string]string{
		".argocd-ls.yaml": "schemaVersion: 1\ntargetVersion: '3.0.0'\ncontrolPlaneNamespace: argocd\nreferencePolicy: closed\n",
		"app.yaml":        app, "project.yaml": project,
	} {
		path := filepath.Join(root, name)
		if err := os.WriteFile(path, []byte(text), 0600); err != nil {
			t.Fatal(err)
		}
		if name == ".argocd-ls.yaml" {
			continue
		}
		uri := workspace.FileURI(path)
		snapshot, err := state.documents.Open(source.URI(uri), text)
		if err != nil {
			t.Fatal(err)
		}
		buffers[uri] = buffer{snapshot: snapshot, version: 1, revision: snapshot.Revision()}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	roots := []string{root}
	var latest completed
	finish := func(events workspaceEvents) {
		t.Helper()
		for {
			latest = state.advance(ctx, 1, roots, buffers, "", events)
			events = workspaceEvents{}
			if ctx.Err() != nil || len(latest.failures) > 0 {
				t.Fatalf("index failed: context=%v failures=%v", ctx.Err(), latest.failures)
			}
			if !latest.more {
				if !latest.stats.Complete {
					t.Fatalf("index remained incomplete: %+v", latest.stats)
				}
				return
			}
		}
	}
	finish(workspaceEvents{})
	uri := workspace.FileURI(filepath.Join(root, "app.yaml"))
	params, _ := json.Marshal(map[string]any{"textDocument": map[string]any{"uri": uri}, "position": map[string]int{"line": 4, "character": 12}, "newName": "renamed"})
	request := message{Method: "textDocument/rename", Params: params}
	acquire := func() *readProof {
		t.Helper()
		input := state.query(ctx, request, latest, completed{}, buffers, roots, "", 1)
		answer, code, why, local := state.renameRequest(ctx, request, &input, buffers, true)
		if code != 0 || local || answer == nil {
			t.Fatalf("complete resource rename failed: code=%d why=%s local=%t answer=%#v", code, why, local, answer)
		}
		proof := newReadProof(request, input, buffers, roots, "", nil)
		if proof == nil || !proof.allScope || !proof.valid(buffers, roots, "", nil) {
			t.Fatal("resource rename did not retain a valid complete-scope proof")
		}
		return proof
	}
	proof := acquire()
	created := filepath.Join(root, "created.yaml")
	if err := os.WriteFile(created, []byte("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: ordinary}\n"), 0600); err != nil {
		t.Fatal(err)
	}
	state.withdraw(workspace.FileURI(created))
	if proof.valid(buffers, roots, "", nil) {
		t.Fatal("new member must invalidate complete-scope proof before background work")
	}
	finish(workspaceEvents{})
	if proof.valid(buffers, roots, "", nil) {
		t.Fatal("new member commit must not revive an older scope generation")
	}
	proof = acquire()
	state.invalidateRoot(filepath.Join(root, "unrelated"))
	if !proof.valid(buffers, roots, "", nil) {
		t.Fatal("an unrelated root event invalidated the selected scope")
	}
	state.invalidateRoot(root)
	if proof.valid(buffers, roots, "", nil) {
		t.Fatal("same-path root recreation must revoke proof at notification acceptance")
	}
	finish(workspaceEvents{reset: map[string]bool{root: true}})
	if proof.valid(buffers, roots, "", nil) {
		t.Fatal("rebuilt root must not revive its old catalog proof")
	}
	acquire()
}
