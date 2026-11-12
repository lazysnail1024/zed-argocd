package lsp

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"path/filepath"
	"reflect"

	"github.com/lazysnail1024/k8s-crd-toolkit/index"
	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/zed-argocd/internal/analysis"
	"github.com/lazysnail1024/zed-argocd/internal/workspace"
)

func queryURI(m message) string {
	var params struct {
		TextDocument struct {
			URI string `json:"uri"`
		} `json:"textDocument"`
		Data fixData `json:"data"`
	}
	if json.Unmarshal(m.Params, &params) != nil {
		return ""
	}
	if m.Method == "codeAction/resolve" {
		return params.Data.URI
	}
	return params.TextDocument.URI
}

func inputTarget(input completed, uri string) workspace.Target {
	for _, scope := range input.scopes {
		for _, file := range scope.files {
			if string(file.Snapshot.URI()) == uri {
				target := file.Target
				target.Complete = false // Discovery state is not configuration identity.
				return target
			}
		}
	}
	return workspace.Target{}
}

func localFixAnswer(m message, answer any) bool {
	local := func(code string) bool { return code == "FIX_FIELD_NAME" || code == "FIX_ENUM_VALUE" }
	if m.Method == "codeAction/resolve" {
		var params struct {
			Data fixData `json:"data"`
		}
		return json.Unmarshal(m.Params, &params) == nil && local(params.Data.FixCode)
	}
	actions, ok := answer.([]any)
	if !ok {
		return false
	}
	for _, action := range actions {
		value, ok := action.(map[string]any)
		if !ok {
			return false
		}
		data, ok := value["data"].(fixData)
		if !ok || !local(data.FixCode) {
			return false
		}
	}
	return true
}

func (proof *readProof) localize(uri string) {
	proof.localOnly, proof.complete, proof.allProjects = true, false, false
	proof.allScope = false
	proof.candidates, proof.uses = nil, nil
	proof.files, proof.knownFiles = map[string]bool{uri: true}, map[string]bool{uri: true}
	proof.hashes = nil // Local fixes require an open authoritative buffer.
}

// A query acquires only its selected source document. All cross-file values
// and positions come from detached immutable summaries. Query reads never
// advance the background reconciliation baseline or publish diagnostics.
func (s *workspaceIndex) query(ctx context.Context, m message, latest, local completed, buffers map[string]buffer, roots []string, override string, epoch uint64) completed {
	var params struct {
		TextDocument struct {
			URI string `json:"uri"`
		} `json:"textDocument"`
		Data fixData `json:"data"`
	}
	if json.Unmarshal(m.Params, &params) != nil {
		return completed{epoch: epoch, err: fmt.Errorf("invalid query params")}
	}
	uri := params.TextDocument.URI
	if m.Method == "codeAction/resolve" {
		uri = params.Data.URI
	}
	localInput := func() completed {
		result := analyzeDocuments(ctx, epoch, roots, buffers, override, local, uri)
		result.editGeneration = latest.editGeneration
		return result
	}
	path, err := workspace.PathFromURI(uri)
	if err != nil {
		return completed{epoch: epoch, err: err}
	}
	if workspace.Owner(roots, path) == "" {
		return completed{epoch: epoch}
	}
	old, catalog, complete := s.lookup(uri)
	if catalog == nil {
		return localInput()
	}
	if override != "" {
		for _, target := range catalog.Input.Targets {
			if target.Version != override {
				return localInput()
			}
		}
	}
	for spelling, buffer := range buffers {
		configPath, _ := workspace.PathFromURI(spelling)
		if configPath != filepath.Join(catalog.Input.Root, ".argocd-ls.yaml") {
			continue
		}
		hash := sha256.Sum256([]byte(buffer.snapshot.Text()))
		if buffer.unavailable != "" || len(catalog.Input.Targets) == 0 || catalog.Input.Targets[0].ConfigHash != hex.EncodeToString(hash[:]) {
			return localInput()
		}
	}
	result := completed{epoch: epoch, editGeneration: latest.editGeneration, incremental: s}
	// Pin actions keep their existing bounded whole-scope before/after proof.
	// The before view needs diagnostics and completeness, not retained YAML.
	if filepath.Base(path) == ".argocd-ls.yaml" {
		if err := s.queryReadLock(ctx); err != nil {
			result.err = err
			return result
		}
		for rootPath, root := range s.roots {
			if root.catalog == nil {
				continue
			}
			input := root.catalog.Input
			input.Complete = root.pending == 0
			for _, target := range root.catalog.Input.Targets {
				complete := s.scopeComplete(root, target.Scope) && root.pending == 0
				input.Complete = input.Complete && complete
				result.scopes = append(result.scopes, scopeResult{root: rootPath, scope: target.Scope, options: analysis.DomainOptions{Complete: complete}})
			}
			result.inputs = append(result.inputs, input)
		}
		for _, file := range s.files {
			result.publications = append(result.publications, publication{uri: file.spelling, diagnostics: file.diagnostics})
		}
		s.mu.RUnlock()
		return result
	}
	var snapshot source.Snapshot
	opened := false
	for spelling, b := range buffers {
		candidate, _ := workspace.PathFromURI(spelling)
		if candidate != path {
			continue
		}
		if b.unavailable != "" {
			result.err = fmt.Errorf("%s", b.unavailable)
			return result
		}
		snapshot, opened = b.snapshot, true
		break
	}
	if !opened {
		text, readErr := workspace.ReadFile(ctx, path)
		if readErr != nil {
			result.err = readErr
			return result
		}
		revision := source.Revision(1)
		if old != nil {
			revision = old.revision
		}
		snapshot, err = source.New(source.URI(uri), revision, text)
		if err != nil {
			result.err = err
			return result
		}
		if old == nil || old.hash != sha256.Sum256([]byte(text)) {
			complete = false
		}
	}
	file, included, skipped, err := catalog.File(snapshot, path, opened)
	if err != nil {
		result.err = err
		return result
	}
	if !included || skipped {
		return result
	}
	var checked analysis.Result
	reused := false
	for _, scope := range local.scopes {
		for _, previous := range scope.files {
			if previous.Snapshot.URI() == snapshot.URI() && previous.Target == file.Target && previous.Snapshot.Text() == snapshot.Text() && previous.Snapshot.Revision() == snapshot.Revision() {
				if value, ok := scope.checked[snapshot.URI()]; ok {
					checked, reused = value, true
				}
			}
		}
	}
	if !reused {
		analyzer := new(analysis.Analyzer)
		for _, scope := range local.scopes {
			analyzer.ReuseSchemas(scope.documents)
		}
		checked, err = analyzer.Analyze(ctx, snapshot, file.Target.Version)
		if err != nil {
			result.err = err
			return result
		}
	}
	if m.Method == "textDocument/definition" && checked.Incomplete {
		var params struct {
			Position analysis.Position `json:"position"`
		}
		if json.Unmarshal(m.Params, &params) == nil {
			if recovered := analysis.RecoverProjectUse(ctx, snapshot, file.Target.Version, params.Position); recovered != nil {
				// This is a query-local use, never a declaration or a committed
				// index fact. Its key selects already known Project candidates.
				checked.Documents = append(append([]analysis.Document(nil), checked.Documents...), *recovered)
			}
		}
	}
	summary := analysis.Summarize(checked.Documents, catalog.Input.Config.ControlPlaneNamespace)
	selected := &indexedFile{uri: source.URI(workspace.FileURI(path)), path: path, spelling: string(snapshot.URI()), root: catalog.Input.Root, target: file.Target, hash: sha256.Sum256([]byte(snapshot.Text())), summary: summary}
	allProjects := m.Method == "textDocument/completion" || m.Method == "textDocument/codeAction" || m.Method == "codeAction/resolve"
	references := m.Method == "textDocument/references" || m.Method == "textDocument/codeAction" || m.Method == "codeAction/resolve"
	var candidates *index.SymbolQueryResult
	var candidateGeneration uint64
	if allProjects {
		// Query the shared index outside the workspace owner lock. Only a stable
		// Project generation may supply candidates to the later dependency proof.
		if err := s.queryReadLock(ctx); err != nil {
			result.err = err
			return result
		}
		root := s.roots[selected.root]
		valid := root != nil && root.catalog == catalog
		if valid {
			candidateGeneration = root.projectGeneration[selected.target.Scope]
		}
		s.mu.RUnlock()
		if !valid {
			result.err = index.ErrQueryChanged
			return result
		}
		matches, err := s.facts.SymbolsWithPrefixContext(ctx, indexScope(selected.root, selected.target.Scope), analysis.ProjectKey(catalog.Input.Config.ControlPlaneNamespace, ""), "", 1000, workspaceSymbolWork/2)
		if err != nil {
			if ctx.Err() != nil {
				result.err = ctx.Err()
				return result
			}
			// Optional Project candidates must not block local Schema completion
			// while background commits keep changing the scope. No partial scan
			// is consumed or allowed to prove a safe cross-resource modification.
			matches = index.SymbolQueryResult{}
			allProjects = false
		}
		candidates = &matches
	}
	if err := s.queryReadLock(ctx); err != nil {
		result.err = err
		return result
	}
	root := s.roots[selected.root]
	if root == nil || root.catalog != catalog {
		s.mu.RUnlock()
		result.err = index.ErrQueryChanged
		return result
	}
	if allProjects && root.projectGeneration[selected.target.Scope] != candidateGeneration {
		candidates, allProjects = &index.SymbolQueryResult{}, false
	}
	documents, candidatesComplete := s.related(selected, summary.Facts, references, candidates)
	result.candidatesTruncated = !candidatesComplete
	complete = complete && candidatesComplete
	proof := &readProof{engine: s, catalog: catalog, scope: indexScope(selected.root, selected.target.Scope), scopeName: selected.target.Scope, complete: complete, hashes: map[source.URI][32]byte{}, candidates: map[index.Key][]index.Definition{}, uses: map[index.Key][]index.Use{}, allProjects: allProjects}
	if root := s.roots[selected.root]; root != nil {
		proof.projectGeneration = root.projectGeneration[selected.target.Scope]
	}
	if !opened {
		proof.hashes[selected.uri] = selected.hash
	}
	keys := map[index.Key]bool{}
	for _, reference := range summary.Facts.References {
		keys[reference.Target] = true
	}
	for _, key := range summary.Facts.Dependencies {
		keys[key] = true
	}
	if references {
		for _, symbol := range summary.Facts.Symbols {
			keys[symbol.Key] = true
		}
	}
	for key := range keys {
		proof.candidates[key], _ = s.facts.Definitions(proof.scope, key)
		if references {
			proof.uses[key], _ = s.facts.References(proof.scope, key)
		}
	}
	files := []workspace.File{file}
	seen := map[source.URI]bool{selected.uri: true}
	for _, document := range documents {
		path, _ := workspace.PathFromURI(string(document.Snapshot.URI()))
		canonical := source.URI(workspace.FileURI(path))
		if seen[canonical] {
			continue
		}
		seen[canonical] = true
		if related := s.files[canonical]; related != nil {
			proof.hashes[canonical] = related.hash
			files = append(files, workspace.File{Snapshot: document.Snapshot, Path: related.path, Target: related.target})
		}
	}
	s.mu.RUnlock()
	// Replace the selected summary by the full current input for cursor queries
	// and safe edits. The other documents remain detached immutable facts.
	kept := append([]analysis.Document(nil), checked.Documents...)
	for _, document := range documents {
		if document.Snapshot.URI() != snapshot.URI() {
			kept = append(kept, document)
		}
	}
	paths, err := catalog.LocalPaths(ctx, summary.Repositories)
	if err != nil {
		complete = false
	}
	if checked.Incomplete {
		complete = false
	}
	proof.complete = complete
	result.queryProof = proof
	result.scopes = []scopeResult{{root: selected.root, scope: selected.target.Scope, files: files, documents: kept, checked: map[source.URI]analysis.Result{snapshot.URI(): checked}, options: analysis.DomainOptions{Context: ctx, Complete: complete, LocalPaths: paths, ControlPlaneNamespace: catalog.Input.Config.ControlPlaneNamespace, ReferencePolicy: catalog.Input.Config.ReferencePolicy}}}
	result.inputs = []workspace.Input{catalog.Input}
	return result
}

func (s *workspaceIndex) proofValid(proof *readProof) bool {
	s.mu.RLock()
	defer s.mu.RUnlock()
	root := s.roots[proof.root]
	if root == nil || root.catalog != proof.catalog || s.configEvents[proof.root] != root.configEvent {
		return false
	}
	if proof.complete && !s.scopeComplete(root, proof.scopeName) {
		return false
	}
	if proof.allProjects && root.projectGeneration[proof.scopeName] != proof.projectGeneration {
		return false
	}
	if proof.allScope {
		_, state := s.facts.Definitions(proof.scope, "")
		if state.Generation != proof.scopeGeneration || !s.scopeComplete(root, proof.scopeName) {
			return false
		}
	}
	for uri, hash := range proof.hashes {
		file := s.files[uri]
		if file == nil || file.hash != hash || file.err != nil || file.invalidated {
			return false
		}
	}
	for key, candidates := range proof.candidates {
		current, _ := s.facts.Definitions(proof.scope, key)
		if !reflect.DeepEqual(current, candidates) {
			return false
		}
	}
	for key, uses := range proof.uses {
		current, _ := s.facts.References(proof.scope, key)
		if !reflect.DeepEqual(current, uses) {
			return false
		}
	}
	return true
}
