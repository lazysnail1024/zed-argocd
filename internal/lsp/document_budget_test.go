package lsp

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/zed-argocd/internal/workspace"
)

func TestDocumentLaneFileBudgetAndReuse(t *testing.T) {
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	config := "schemaVersion: 1\ntargetVersion: '3.5.6'\nreferencePolicy: open\n"
	if err := os.WriteFile(filepath.Join(root, ".argocd-ls.yaml"), []byte(config), 0600); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(filepath.Join("..", "..", "tests", "alias-projection-cases.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fixtures struct {
		Cases []struct{ ID, Text string }
	}
	if err := json.Unmarshal(data, &fixtures); err != nil {
		t.Fatal(err)
	}
	large := ""
	for _, fixture := range fixtures.Cases {
		if fixture.ID == "expanded-boundary--1" {
			large = fixture.Text
		}
	}
	if large == "" {
		t.Fatal("missing independently sized 99,999-node fixture")
	}
	buffers := map[string]buffer{}
	put := func(name, text string, version int) string {
		t.Helper()
		uri := workspace.FileURI(filepath.Join(root, name))
		snapshot, err := source.New(source.URI(uri), source.Revision(version), text)
		if err != nil {
			t.Fatal(err)
		}
		buffers[uri] = buffer{uri: uri, version: version, revision: snapshot.Revision(), snapshot: snapshot}
		return uri
	}
	a, b := put("a.yaml", large, 1), put("b.yaml", large, 1)
	check := func(result completed, budgetURI string) {
		t.Helper()
		if result.err != nil || len(result.failures) != 0 {
			t.Fatalf("document pass failed: %v, %v", result.err, result.failures)
		}
		graphs, checked := map[source.URI]bool{}, 0
		for _, scope := range result.scopes {
			checked += len(scope.checked)
			for _, doc := range scope.documents {
				graphs[doc.Snapshot.URI()] = true
			}
			for _, result := range scope.checked {
				for _, doc := range result.Documents {
					graphs[doc.Snapshot.URI()] = true
				}
			}
		}
		if len(graphs) != 1 || checked != 1 {
			t.Fatalf("latest document result retained %d files' full values and %d full checks", len(graphs), checked)
		}
		budgets := 0
		for _, p := range result.publications {
			for _, diagnostic := range p.diagnostics {
				if diagnostic.Code == "INPUT_BUDGET_EXCEEDED" {
					budgets++
					if p.uri != budgetURI {
						t.Fatalf("unrelated file received a budget diagnosis: %s", p.uri)
					}
				}
			}
		}
		if (budgetURI == "" && budgets != 0) || (budgetURI != "" && budgets != 1) {
			t.Fatalf("budget diagnostics=%d, expected URI %q", budgets, budgetURI)
		}
	}
	ctx := context.Background()
	first := analyzeDocuments(ctx, 1, []string{root}, buffers, "", completed{}, "")
	check(first, "")
	if first.stats.ParsedFiles != 2 {
		t.Fatalf("initial pass parsed %d files", first.stats.ParsedFiles)
	}

	// The publication-only file still needs full values for a catalog fallback.
	state := newWorkspaceIndex()
	defer state.close()
	params, _ := json.Marshal(map[string]any{"textDocument": map[string]string{"uri": b}})
	query := state.query(ctx, message{Method: "textDocument/hover", Params: params}, completed{}, first, buffers, []string{root}, "", 2)
	check(query, "")
	if query.stats.ParsedFiles != 1 || len(query.scopes) != 1 || len(query.scopes[0].documents) != 1 || query.scopes[0].documents[0].Snapshot.URI() != source.URI(b) {
		t.Fatalf("selected fallback did not materialize only the requested file: %+v", query.stats)
	}

	put("a.yaml", large+"# changed\n", 2)
	next := analyzeDocuments(ctx, 3, []string{root}, buffers, "", first, "")
	check(next, "")
	if next.stats.ParsedFiles != 1 {
		t.Fatalf("editing A re-expanded unchanged B: parsed %d files", next.stats.ParsedFiles)
	}
	unchanged := analyzeDocuments(ctx, 4, []string{root}, buffers, "", next, "")
	check(unchanged, "")
	if unchanged.stats.ParsedFiles != 0 {
		t.Fatalf("unchanged publications re-expanded %d files", unchanged.stats.ParsedFiles)
	}
	put("a.yaml", large+"---\n"+large, 3)
	refused := analyzeDocuments(ctx, 5, []string{root}, buffers, "", unchanged, "")
	check(refused, a)

	// Publication limits must not truncate the full checked result used by edits.
	text := "apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata: {name: unknown}\nspec:\n  project: team\n  source: {repoURL: https://example.org/repo}\n  destination: {server: here}\n"
	for i := 0; i < 300; i++ {
		text += fmt.Sprintf("  unknown%d: value\n", i)
	}
	put("a.yaml", text, 4)
	limited := analyzeDocuments(ctx, 6, []string{root}, buffers, "", refused, "")
	check(limited, "")
	full := limited.scopes[0].checked[source.URI(a)]
	if len(full.Diagnostics) <= 256 {
		t.Fatalf("full edit check was truncated: %d diagnostics", len(full.Diagnostics))
	}
	for _, p := range limited.publications {
		if len(p.diagnostics) > 256 {
			t.Fatalf("unbounded publication: %d diagnostics", len(p.diagnostics))
		}
		for _, diagnostic := range p.diagnostics {
			if len(diagnostic.Message) > 4099 {
				t.Fatalf("unbounded diagnostic message: %d bytes", len(diagnostic.Message))
			}
		}
	}

	put(".argocd-ls.yaml", strings.Replace(config, "3.5.6", "3.0.0", 1), 1)
	configured := state.query(ctx, message{Method: "textDocument/hover", Params: params}, completed{}, limited, buffers, []string{root}, "", 7)
	check(configured, "")
	if configured.scopes[0].documents[0].Target != "3.0.0" {
		t.Fatal("selected fallback ignored the opened root configuration")
	}

	// Repeated immutable padding has distinct URI authority but little test
	// memory. Narrowing a query must not omit these bytes from admission.
	padding := "#" + strings.Repeat("x", 1024*1024-1)
	for i := 0; i < 128; i++ {
		put(fmt.Sprintf("padding-%d.yaml", i), padding, 1)
	}
	over := state.query(ctx, message{Method: "textDocument/hover", Params: params}, completed{}, configured, buffers, []string{root}, "", 8)
	failure, ok := over.err.(*workspace.Error)
	if !ok || failure.Code != "INPUT_BUDGET_EXCEEDED" || over.stats.ParsedFiles != 0 {
		t.Fatalf("selected fallback bypassed all-open input admission: %v, %+v", over.err, over.stats)
	}
}
