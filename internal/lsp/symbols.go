package lsp

import (
	"container/heap"
	"context"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/lazysnail1024/k8s-crd-toolkit/index"
	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/zed-argocd/internal/analysis"
	"github.com/lazysnail1024/zed-argocd/internal/workspace"
)

type workspaceSymbol struct {
	Name          string            `json:"name"`
	Kind          int               `json:"kind"`
	Location      analysis.Location `json:"location"`
	ContainerName string            `json:"containerName"`
}

type symbolReadProof struct {
	catalogs map[string]*workspace.Catalog
	events   map[string]uint64
	files    map[source.URI]source.Revision
}

// The two disjoint key prefixes may each visit every retained file and symbol.
// This preserves the existing index capacity while bounding zero-match queries.
const workspaceSymbolWork = 2 * (indexFileLimit + 500000)

type symbolEntry struct {
	symbol   workspaceSymbol
	uri      source.URI
	revision source.Revision
}

func symbolLess(a, b workspaceSymbol) bool {
	if a.Location.URI != b.Location.URI {
		return a.Location.URI < b.Location.URI
	}
	if a.Location.Range.Start.Line != b.Location.Range.Start.Line {
		return a.Location.Range.Start.Line < b.Location.Range.Start.Line
	}
	if a.Location.Range.Start.Character != b.Location.Range.Start.Character {
		return a.Location.Range.Start.Character < b.Location.Range.Start.Character
	}
	if a.Name != b.Name {
		return a.Name < b.Name
	}
	return a.ContainerName < b.ContainerName
}

// A max heap retains the first global limit across roots and target scopes.
// Each partition uses the same source order, so its first limit is sufficient.
type symbolHeap []symbolEntry

func (h symbolHeap) Len() int           { return len(h) }
func (h symbolHeap) Less(i, j int) bool { return symbolLess(h[j].symbol, h[i].symbol) }
func (h symbolHeap) Swap(i, j int)      { h[i], h[j] = h[j], h[i] }
func (h *symbolHeap) Push(v any)        { *h = append(*h, v.(symbolEntry)) }
func (h *symbolHeap) Pop() any {
	old := *h
	v := old[len(old)-1]
	*h = old[:len(old)-1]
	return v
}

type symbolCollection struct {
	retained symbolHeap
	work     int
	complete bool
}

// A queued owner write must not make a cancelled foreground query wait for its
// entire lifetime. The normal uncontended path does not allocate a timer.
func (s *workspaceIndex) queryReadLock(ctx context.Context) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if s.mu.TryRLock() {
		return nil
	}
	tick := time.NewTicker(time.Millisecond)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-tick.C:
			if err := ctx.Err(); err != nil {
				return err
			}
			if s.mu.TryRLock() {
				return nil
			}
		}
	}
}

// Borrow one immutable file record under a short lock. Mapping retained source
// ranges and merging results never holds the workspace owner lock.
func (s *workspaceIndex) symbolEntry(ctx context.Context, match index.Definition, root, scope string) (symbolEntry, bool, error) {
	if err := s.queryReadLock(ctx); err != nil {
		return symbolEntry{}, false, err
	}
	file := s.files[match.URI]
	s.mu.RUnlock()
	if file == nil || file.root != root || file.target.Scope != scope || file.invalidated || file.incomplete || file.err != nil || file.revision != match.Revision {
		return symbolEntry{}, false, nil
	}
	ordinal, ok := strings.CutPrefix(string(match.Symbol.ID), "document:")
	ordinal, _, _ = strings.Cut(ordinal, ":")
	n, err := strconv.Atoi(ordinal)
	if !ok || err != nil || n < 0 || n >= len(file.summary.Documents) {
		return symbolEntry{}, false, nil
	}
	doc := file.summary.Documents[n]
	items := analysis.Symbols([]analysis.Document{doc}, string(doc.Snapshot.URI()))
	if len(items) != 1 || items[0].Name != match.Symbol.Name {
		return symbolEntry{}, false, nil
	}
	item := items[0]
	return symbolEntry{workspaceSymbol{item.Name, item.Kind, analysis.Location{URI: string(doc.Snapshot.URI()), Range: item.SelectionRange}, doc.Kind}, match.URI, match.Revision}, true, nil
}

func (s *workspaceIndex) collectSymbols(ctx context.Context, rootPath, scope, query string, collected *symbolCollection) error {
	const limit = 20000
	// Project's any-namespace key excludes its second exact-namespace lookup key.
	for _, prefix := range []index.Key{"resource:", analysis.ProjectKey("", "")} {
		remaining := workspaceSymbolWork - collected.work
		if remaining <= 0 {
			return index.ErrQueryBudget
		}
		matches, err := s.facts.SymbolsWithPrefixContext(ctx, indexScope(rootPath, scope), prefix, query, limit, remaining)
		collected.work += matches.Work
		if err != nil {
			return err
		}
		collected.complete = collected.complete && !matches.Truncated
		for _, match := range matches.Items {
			if err := ctx.Err(); err != nil {
				return err
			}
			entry, ok, err := s.symbolEntry(ctx, match, rootPath, scope)
			if err != nil {
				return err
			}
			if !ok {
				collected.complete = false
				continue
			}
			if len(collected.retained) < limit {
				heap.Push(&collected.retained, entry)
				continue
			}
			collected.complete = false
			if symbolLess(entry.symbol, collected.retained[0].symbol) {
				collected.retained[0] = entry
				heap.Fix(&collected.retained, 0)
			}
		}
	}
	return nil
}

// Symbol lookup uses retained source ranges and never materializes closed text.
// Root order, catalogs and file records are immutable after publication. Borrow
// them under short locks, query/map outside those locks, then retain the original
// delivery proof. That proof covers returned positions, not complete discovery.
func (s *workspaceIndex) workspaceSymbols(ctx context.Context, query string) ([]workspaceSymbol, *symbolReadProof, bool, error) {
	proof := &symbolReadProof{catalogs: map[string]*workspace.Catalog{}, events: map[string]uint64{}, files: map[source.URI]source.Revision{}}
	collected := symbolCollection{complete: true}
	if err := s.queryReadLock(ctx); err != nil {
		return nil, nil, false, err
	}
	roots := s.rootOrder
	s.mu.RUnlock()
	for _, rootPath := range roots {
		if err := ctx.Err(); err != nil {
			return nil, nil, false, err
		}
		if err := s.queryReadLock(ctx); err != nil {
			return nil, nil, false, err
		}
		root := s.roots[rootPath]
		var catalog *workspace.Catalog
		if root != nil && root.catalog != nil && root.err == nil {
			catalog = root.catalog
			proof.catalogs[rootPath], proof.events[rootPath] = catalog, root.configEvent
		}
		s.mu.RUnlock()
		if catalog == nil {
			collected.complete = false
			continue
		}
		for _, target := range catalog.Input.Targets {
			if err := s.queryReadLock(ctx); err != nil {
				return nil, nil, false, err
			}
			current := s.roots[rootPath]
			ready := current != nil && current.catalog == catalog && s.scopeComplete(current, target.Scope)
			s.mu.RUnlock()
			collected.complete = collected.complete && ready
			if err := s.collectSymbols(ctx, rootPath, target.Scope, query, &collected); err != nil {
				return nil, nil, false, err
			}
		}
	}
	result := make([]workspaceSymbol, len(collected.retained))
	for i, entry := range collected.retained {
		if revision, exists := proof.files[entry.uri]; exists && revision != entry.revision {
			return nil, nil, false, index.ErrQueryChanged
		}
		result[i] = entry.symbol
		proof.files[entry.uri] = entry.revision
	}
	sort.Slice(result, func(i, j int) bool { return symbolLess(result[i], result[j]) })
	if err := ctx.Err(); err != nil {
		return nil, nil, false, err
	}
	return result, proof, collected.complete, nil
}

func (s *workspaceIndex) symbolsValid(proof *symbolReadProof) bool {
	s.mu.RLock()
	defer s.mu.RUnlock()
	for path, catalog := range proof.catalogs {
		root := s.roots[path]
		if root == nil || root.catalog != catalog || s.configEvents[path] != proof.events[path] {
			return false
		}
	}
	for uri, revision := range proof.files {
		file := s.files[uri]
		if file == nil || file.revision != revision || file.err != nil || file.incomplete || file.invalidated {
			return false
		}
	}
	return true
}
