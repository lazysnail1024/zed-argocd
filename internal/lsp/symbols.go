package lsp

import (
	"container/heap"
	"context"
	"sort"
	"strconv"
	"strings"

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

// Symbol lookup uses retained source ranges and never materializes closed text.
// The proof covers returned positions, not an assertion of complete discovery.
func (s *workspaceIndex) workspaceSymbols(ctx context.Context, query string) ([]workspaceSymbol, *symbolReadProof, bool, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	retained := symbolHeap{}
	proof := &symbolReadProof{catalogs: map[string]*workspace.Catalog{}, events: map[string]uint64{}, files: map[source.URI]source.Revision{}}
	complete := true
	const limit = 20000
	for _, rootPath := range s.rootOrder {
		root := s.roots[rootPath]
		if root == nil || root.catalog == nil || root.err != nil {
			complete = false
			continue
		}
		proof.catalogs[rootPath], proof.events[rootPath] = root.catalog, root.configEvent
		for _, target := range root.catalog.Input.Targets {
			complete = complete && s.scopeComplete(root, target.Scope)
			// Project's any-namespace key has one entry per real declaration;
			// its second exact-namespace key is excluded before limiting results.
			for _, prefix := range []index.Key{"resource:", analysis.ProjectKey("", "")} {
				if err := ctx.Err(); err != nil {
					return nil, nil, false, err
				}
				matches, err := s.facts.SymbolsWithPrefix(indexScope(rootPath, target.Scope), prefix, query, limit)
				if err != nil {
					return nil, nil, false, err
				}
				complete = complete && !matches.Truncated
				for _, match := range matches.Items {
					if err := ctx.Err(); err != nil {
						return nil, nil, false, err
					}
					file := s.files[match.URI]
					if file == nil || file.invalidated || file.incomplete || file.err != nil || file.revision != match.Revision {
						complete = false
						continue
					}
					// Symbol IDs are allocated by Summarize, one per document. The
					// disjoint prefixes above include each resource exactly once.
					ordinal, ok := strings.CutPrefix(string(match.Symbol.ID), "document:")
					ordinal, _, _ = strings.Cut(ordinal, ":")
					n, err := strconv.Atoi(ordinal)
					if !ok || err != nil || n < 0 || n >= len(file.summary.Documents) {
						complete = false
						continue
					}
					doc := file.summary.Documents[n]
					items := analysis.Symbols([]analysis.Document{doc}, string(doc.Snapshot.URI()))
					if len(items) != 1 || items[0].Name != match.Symbol.Name {
						continue
					}
					item := items[0]
					entry := symbolEntry{workspaceSymbol{item.Name, item.Kind, analysis.Location{URI: string(doc.Snapshot.URI()), Range: item.SelectionRange}, doc.Kind}, match.URI, match.Revision}
					if len(retained) < limit {
						heap.Push(&retained, entry)
					} else {
						complete = false
						if symbolLess(entry.symbol, retained[0].symbol) {
							retained[0] = entry
							heap.Fix(&retained, 0)
						}
					}
				}
			}
		}
	}
	result := make([]workspaceSymbol, len(retained))
	for i, entry := range retained {
		result[i] = entry.symbol
		proof.files[entry.uri] = entry.revision
	}
	sort.Slice(result, func(i, j int) bool { return symbolLess(result[i], result[j]) })
	return result, proof, complete, nil
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
