package lsp

import (
	"context"
	"crypto/sha256"
	"sort"
	"strings"

	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/zed-argocd/internal/analysis"
	"github.com/lazysnail1024/zed-argocd/internal/workspace"
)

// projectRenameScope borrows all current summaries in one configured scope.
// Only affected open files are subsequently materialized. Discovery completeness
// and a scope generation are both necessary: a newly introduced unresolved use
// of the proposed name must invalidate the result, even if no old key changed.
func (s *workspaceIndex) projectRenameScope(ctx context.Context, input *completed, uri string) (*scopeResult, error) {
	if len(input.scopes) == 1 && strings.HasPrefix(input.scopes[0].scope, "isolated:") {
		return nil, analysis.Refuse("ISOLATED_DOCUMENT", "excluded documents cannot authorize cross-resource rename")
	}
	if input.queryProof == nil || input.queryProof.catalog == nil || len(input.scopes) != 1 {
		return nil, analysis.Refuse("INDEX_INCOMPLETE", "Project rename requires a ready configured scope")
	}
	selected := input.scopes[0]
	if selected.options.ControlPlaneNamespace == "" {
		return nil, analysis.Refuse("TARGET_UNKNOWN", "Project rename requires an explicit controlPlaneNamespace")
	}
	if selected.options.ReferencePolicy != "closed" {
		return nil, analysis.Refuse("REFERENCE_SCOPE_OPEN", "Project rename requires an intentionally closed reference scope")
	}
	return s.completeQueryScope(ctx, input, uri)
}

// completeQueryScope borrows the complete configured scope for a read-only
// completeness proof. Policy and writable-buffer conditions belong to callers.
func (s *workspaceIndex) completeQueryScope(ctx context.Context, input *completed, uri string) (*scopeResult, error) {
	proof := input.queryProof
	if proof == nil || proof.catalog == nil || len(input.scopes) != 1 {
		return nil, analysis.Refuse("INDEX_INCOMPLETE", "query requires a ready configured scope")
	}
	selected := input.scopes[0]
	path, err := workspace.PathFromURI(uri)
	if err != nil {
		return nil, err
	}
	canonical := source.URI(workspace.FileURI(path))
	s.mu.RLock()
	defer s.mu.RUnlock()
	root := s.roots[selected.root]
	if root == nil || root.catalog != proof.catalog || !s.scopeComplete(root, selected.scope) {
		return nil, analysis.Refuse("INDEX_INCOMPLETE", "query requires a completely analyzed selected scope")
	}
	files := []*indexedFile{}
	for _, file := range s.files {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if file.root != selected.root || file.target.Scope != selected.scope {
			continue
		}
		if file.err != nil || file.invalidated || file.incomplete {
			return nil, analysis.Refuse("INDEX_INCOMPLETE", "scope contains unavailable or incomplete input")
		}
		files = append(files, file)
	}
	sort.Slice(files, func(i, j int) bool { return files[i].uri < files[j].uri })
	scope := &scopeResult{root: selected.root, scope: selected.scope, options: selected.options, checked: selected.checked}
	scope.options.Complete = true
	proof.hashes = map[source.URI][32]byte{}
	found := false
	for _, file := range files {
		proof.hashes[file.uri] = file.hash
		if file.uri == canonical {
			found = true
			for _, sourceFile := range selected.files {
				if string(sourceFile.Snapshot.URI()) == uri {
					if sha256.Sum256([]byte(sourceFile.Snapshot.Text())) != file.hash {
						return nil, analysis.Refuse("STALE_INPUT", "selected input changed during scope acquisition")
					}
					scope.files = append(scope.files, sourceFile)
				}
			}
			for _, doc := range selected.documents {
				if string(doc.Snapshot.URI()) == uri {
					scope.documents = append(scope.documents, doc)
				}
			}
		} else {
			scope.documents = append(scope.documents, file.summary.Documents...)
			// Empty summaries still participate in buffer/revision proof. They
			// may become a CRD or acquire a reference in the next edit.
			metadata, err := source.New(source.URI(file.spelling), file.revision, "")
			if err != nil {
				return nil, err
			}
			scope.files = append(scope.files, workspace.File{Snapshot: metadata, Path: file.path, Target: file.target})
		}
	}
	if !found {
		return nil, analysis.Refuse("INDEX_INCOMPLETE", "selected file is not committed in its scope")
	}
	_, state := s.facts.Definitions(proof.scope, "")
	proof.allScope, proof.scopeGeneration, proof.complete = true, state.Generation, true
	return scope, nil
}

func (s *workspaceIndex) projectRenameRequest(ctx context.Context, m message, input *completed, buffers map[string]buffer, params renameParams) (any, int, string) {
	refuse := func(err error) (any, int, string) {
		input.refusal = analysis.ExplainRefusal(err)
		if m.Method == "textDocument/prepareRename" {
			return nil, 0, ""
		}
		return nil, -32602, err.Error()
	}
	scope, err := s.projectRenameScope(ctx, input, params.TextDocument.URI)
	if err != nil {
		return refuse(err)
	}
	target, err := analysis.PrepareProjectRename(ctx, scope.documents, scope.options, params.TextDocument.URI, params.Position)
	if err != nil {
		return refuse(err)
	}
	byPath := map[string]buffer{}
	ambiguous := map[string]bool{}
	for uri, b := range buffers {
		path, err := workspace.PathFromURI(uri)
		if err != nil {
			continue
		}
		if _, duplicate := byPath[path]; duplicate {
			ambiguous[path] = true
		}
		byPath[path] = b
	}
	full := map[string][]analysis.Document{}
	analyzer := new(analysis.Analyzer)
	analyzer.SetSchemaCache(s.schemas)
	analyzer.ReuseSchemas(scope.documents)
	for _, uri := range target.URIs {
		if err := ctx.Err(); err != nil {
			return refuse(err)
		}
		path, err := workspace.PathFromURI(uri)
		if err != nil {
			return refuse(err)
		}
		b, opened := byPath[path]
		if ambiguous[path] {
			return refuse(analysis.Refuse("AMBIGUOUS_BINDING", "multiple open spellings prevent a unique document-version proof"))
		}
		if !opened || b.unavailable != "" {
			return refuse(analysis.Refuse("AFFECTED_FILE_NOT_OPEN", "every affected Project file must be open with an accepted client version", analysis.Location{URI: uri}))
		}
		if sha256.Sum256([]byte(b.snapshot.Text())) != input.queryProof.hashes[source.URI(workspace.FileURI(path))] {
			return refuse(analysis.Refuse("STALE_INPUT", "affected input changed during scope acquisition"))
		}
		file, included, skipped, err := input.queryProof.catalog.File(b.snapshot, path, true)
		if err != nil || !included || skipped || file.Target.Scope != scope.scope {
			return refuse(analysis.Refuse("STALE_INPUT", "affected input no longer belongs to the selected scope"))
		}
		checked, err := analyzer.Analyze(ctx, b.snapshot, file.Target.Version)
		if err != nil || checked.Incomplete {
			return refuse(analysis.Refuse("INDEX_INCOMPLETE", "an affected Project file cannot be completely analyzed"))
		}
		full[path] = checked.Documents
		for i := range scope.files {
			if scope.files[i].Path == path {
				scope.files[i] = file
			}
		}
	}
	documents := []analysis.Document{}
	replaced := map[string]bool{}
	for _, doc := range scope.documents {
		path, _ := workspace.PathFromURI(string(doc.Snapshot.URI()))
		if fresh, ok := full[path]; ok {
			if !replaced[path] {
				documents = append(documents, fresh...)
				replaced[path] = true
			}
		} else {
			documents = append(documents, doc)
		}
	}
	scope.documents = documents
	input.scopes = []scopeResult{*scope}
	target, err = analysis.PrepareProjectRename(ctx, documents, scope.options, params.TextDocument.URI, params.Position)
	if err != nil {
		return refuse(err)
	}
	if m.Method == "textDocument/prepareRename" {
		return target, 0, ""
	}
	plans, err := analysis.RenameProject(ctx, documents, scope.options, params.TextDocument.URI, params.Position, params.NewName)
	if err != nil {
		return refuse(err)
	}
	changes := []any{}
	for _, planned := range plans {
		path, _ := workspace.PathFromURI(string(planned.Document.Snapshot.URI()))
		b := byPath[path]
		edits := []analysis.TextEdit{}
		for _, replacement := range planned.Plan.Replacements {
			edits = append(edits, replacementEdit(b.snapshot, replacement.Span, replacement.NewText))
		}
		changes = append(changes, map[string]any{"textDocument": map[string]any{"uri": string(b.snapshot.URI()), "version": b.version}, "edits": edits})
	}
	return map[string]any{"documentChanges": changes}, 0, ""
}
