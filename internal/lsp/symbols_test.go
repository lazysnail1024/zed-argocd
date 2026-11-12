package lsp

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/lazysnail1024/k8s-crd-toolkit/index"
	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/zed-argocd/internal/workspace"
)

func symbolIndexFixture(t *testing.T, count int) *workspaceIndex {
	t.Helper()
	state := newWorkspaceIndex()
	t.Cleanup(state.close)
	root := t.TempDir()
	catalog := &workspace.Catalog{Input: workspace.Input{Root: root, Targets: []workspace.Target{{Scope: "root"}}}}
	state.rootOrder = []string{root}
	state.roots[root] = &indexedRoot{catalog: catalog, discovered: true, scanComplete: true}
	path := filepath.Join(root, "symbols.yaml")
	uri := source.URI(workspace.FileURI(path))
	facts := index.Facts{}
	for n := 0; n < count; n++ {
		facts.Symbols = append(facts.Symbols, index.Symbol{ID: index.SymbolID(fmt.Sprint(n)), Key: "resource:fixture", Name: fmt.Sprintf("item-%06d", n), Span: source.Span{Start: n, End: n + 1}, Selection: source.Span{Start: n, End: n + 1}})
	}
	ticket, err := state.facts.Begin(uri, indexScope(root, "root"), 1)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := state.facts.Commit(ticket, facts); err != nil {
		t.Fatal(err)
	}
	state.files[uri] = &indexedFile{uri: uri, path: path, spelling: string(uri), root: root, target: workspace.Target{Scope: "root"}, revision: 1}
	return state
}

func TestWorkspaceSymbolNoMatchWork(t *testing.T) {
	const count = 4096
	state := symbolIndexFixture(t, count)
	collected := symbolCollection{complete: true}
	err := state.collectSymbols(context.Background(), state.rootOrder[0], "root", "never-matches", &collected)
	if err != nil || len(collected.retained) != 0 || !collected.complete {
		t.Fatalf("zero-match query: items=%d complete=%t err=%v", len(collected.retained), collected.complete, err)
	}
	if collected.work != 2*(count+1) {
		t.Fatalf("zero matches did not charge every file and symbol twice: work=%d", collected.work)
	}
}

// Pausing a cancellation checkpoint is a deterministic lock probe, not a
// replacement scanner or clock. The stdio consumer separately checks real YAML,
// didChange, hover, and cancellation through the original process boundary.
type pausedSymbolContext struct {
	context.Context
	calls   int
	at      int
	paused  chan struct{}
	release chan struct{}
	once    sync.Once
}

func (c *pausedSymbolContext) Err() error {
	c.calls++
	if c.calls == c.at {
		c.once.Do(func() { close(c.paused) })
		<-c.release
	}
	return c.Context.Err()
}

func TestWorkspaceSymbolCheckpointsReleaseOwner(t *testing.T) {
	state := symbolIndexFixture(t, 4096)
	base, cancel := context.WithCancel(context.Background())
	defer cancel()
	ctx := &pausedSymbolContext{Context: base, at: 8, paused: make(chan struct{}), release: make(chan struct{})}
	var release sync.Once
	unblock := func() { release.Do(func() { close(ctx.release) }) }
	defer unblock()
	done := make(chan error, 1)
	go func() {
		items, proof, complete, err := state.workspaceSymbols(ctx, "never-matches")
		if len(items) != 0 || proof != nil || complete {
			done <- fmt.Errorf("cancelled query exposed partial results: %d, %v, %t", len(items), proof, complete)
			return
		}
		done <- err
	}()
	select {
	case <-ctx.paused:
	case <-time.After(2 * time.Second):
		t.Fatal("query did not reach a cancellation checkpoint during traversal")
	}
	accepted := make(chan struct{})
	go func() {
		state.mu.Lock()
		state.mu.Unlock()
		close(accepted)
	}()
	select {
	case <-accepted:
	case <-time.After(2 * time.Second):
		t.Error("symbol traversal held the workspace owner lock")
	}
	cancel()
	unblock()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("cancelled zero-match query: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("cancelled symbol query did not terminate")
	}
}

type observedSymbolContext struct {
	context.Context
	observed chan struct{}
	once     sync.Once
}

func (c *observedSymbolContext) Err() error {
	err := c.Context.Err()
	c.once.Do(func() { close(c.observed) })
	return err
}

func TestWorkspaceSymbolOwnerWaitCancels(t *testing.T) {
	state := symbolIndexFixture(t, 1)
	state.mu.Lock()
	base, cancel := context.WithCancel(context.Background())
	defer cancel()
	ctx := &observedSymbolContext{Context: base, observed: make(chan struct{})}
	done := make(chan error, 1)
	go func() {
		_, _, _, err := state.workspaceSymbols(ctx, "never-matches")
		done <- err
	}()
	// The first Err check has observed a live context. Cancellation must now
	// interrupt lock acquisition, not merely reject an already-cancelled call.
	select {
	case <-ctx.observed:
	case <-time.After(2 * time.Second):
		state.mu.Unlock()
		t.Fatal("symbol query did not check its context before lock acquisition")
	}
	cancel()
	select {
	case err := <-done:
		state.mu.Unlock()
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("owner lock wait ignored cancellation: %v", err)
		}
	case <-time.After(2 * time.Second):
		state.mu.Unlock()
		t.Fatal("query waited for the owner lock despite cancellation")
	}
}
