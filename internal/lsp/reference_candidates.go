package lsp

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"reflect"
	"sort"
	"strings"

	"github.com/lazysnail1024/k8s-crd-toolkit/index"
	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/zed-argocd/internal/analysis"
	"github.com/lazysnail1024/zed-argocd/internal/workspace"
)

const referenceCandidateKind = "source.openReferenceCandidate"
const referenceCandidateCommand = "argocd.openReferenceCandidate"
const referenceCandidateMethod = "argocd/internal/openReferenceCandidate"
const referenceCandidateResponsePrefix = "argocd-reference-candidate:"
const referenceCandidateLimit = 20

type referenceCandidateReport struct {
	Label         string                      `json:"label"`
	Selection     analysis.Location           `json:"selection"`
	ScopeComplete bool                        `json:"scopeComplete"`
	Truncated     bool                        `json:"truncated"`
	Complete      bool                        `json:"complete"`
	Items         []analysis.ProjectCandidate `json:"items"`
	*analysis.Refusal
}

type candidateFileIdentity struct {
	SHA256   string `json:"sha256"`
	Opened   bool   `json:"opened"`
	Version  int    `json:"version"`
	Revision uint64 `json:"revision"`
}

type referenceCandidateArgument struct {
	Source      explainArgument           `json:"source"`
	SourceHash  string                    `json:"sourceHash"`
	Root        string                    `json:"root"`
	RootEvent   uint64                    `json:"rootEvent"`
	Override    string                    `json:"override"`
	InputTarget workspace.Target          `json:"inputTarget"`
	Candidate   analysis.ProjectCandidate `json:"candidate"`
	Target      candidateFileIdentity     `json:"target"`
}

type referenceCandidateParams struct {
	TextDocument struct {
		URI string `json:"uri"`
	} `json:"textDocument"`
	Position analysis.Position          `json:"position"`
	Range    analysis.Range             `json:"range"`
	Argument referenceCandidateArgument `json:"argument"`
	Resolve  bool                       `json:"resolve"`
}

type referenceCandidateRead struct {
	report    *referenceCandidateReport
	arguments []referenceCandidateArgument
	proof     *readProof
}

type referenceCandidateSource struct {
	path string
	hash [32]byte
}

type referenceCandidateOpen struct {
	Location analysis.Location `json:"location"`
	Message  string            `json:"message"`
}

func candidateHash(text string) string {
	hash := sha256.Sum256([]byte(text))
	return hex.EncodeToString(hash[:])
}

func candidateActionAllowed(m message) bool {
	var params struct {
		Context struct {
			Only []string `json:"only"`
		} `json:"context"`
	}
	if json.Unmarshal(m.Params, &params) != nil {
		return false
	}
	if len(params.Context.Only) == 0 {
		return true
	}
	for _, kind := range params.Context.Only {
		if referenceCandidateKind == kind || strings.HasPrefix(referenceCandidateKind, kind+".") {
			return true
		}
	}
	return false
}

func candidateActionsOnly(m message) bool {
	var params struct {
		Context struct {
			Only []string `json:"only"`
		} `json:"context"`
	}
	if json.Unmarshal(m.Params, &params) != nil || len(params.Context.Only) == 0 {
		return false
	}
	for _, kind := range params.Context.Only {
		if kind != referenceCandidateKind {
			return false
		}
	}
	return true
}

func referenceCandidateAction(argument referenceCandidateArgument) any {
	item := argument.Candidate
	namespace := item.Namespace
	if namespace == "" {
		namespace = "unknown namespace"
	}
	path, err := workspace.PathFromURI(item.Location.URI)
	if err != nil {
		path = item.Location.URI
	} else if relative, relativeErr := filepath.Rel(argument.Root, path); relativeErr == nil {
		path = filepath.ToSlash(relative)
	}
	title := fmt.Sprintf("Candidate: %s %s/%s — %s:%d:%d", item.Kind, namespace, item.Name, path, item.Location.Range.Start.Line+1, item.Location.Range.Start.Character+1)
	return map[string]any{"title": title, "kind": referenceCandidateKind,
		"command": map[string]any{"title": title, "command": referenceCandidateCommand, "arguments": []referenceCandidateArgument{argument}}}
}

// Arguments carry observations, never authority. Both resolve and execute
// enumerate again and compare a newly derived action before returning a target.
func referenceCandidateRequest(m message, buffers map[string]buffer) (message, *analysis.Refusal, bool) {
	if m.Method != "workspace/executeCommand" && m.Method != "codeAction/resolve" {
		return m, nil, false
	}
	raw := m.Params
	if m.Method == "codeAction/resolve" {
		var envelope struct {
			Command json.RawMessage `json:"command"`
		}
		if json.Unmarshal(raw, &envelope) != nil {
			return m, nil, false
		}
		raw = envelope.Command
	}
	var command struct {
		Command   string                       `json:"command"`
		Arguments []referenceCandidateArgument `json:"arguments"`
	}
	if json.Unmarshal(raw, &command) != nil || command.Command != referenceCandidateCommand {
		return m, nil, false
	}
	stale := analysis.Refuse("STALE_INPUT", "Reference candidate action changed; request current candidates again.")
	if len(command.Arguments) != 1 {
		return m, stale, true
	}
	argument := command.Arguments[0]
	b, opened := buffers[argument.Source.URI]
	if !opened || b.unavailable != "" || b.version != argument.Source.Version || uint64(b.snapshot.Revision()) != argument.Source.Revision || candidateHash(b.snapshot.Text()) != argument.SourceHash {
		return m, stale, true
	}
	params := referenceCandidateParams{Position: argument.Source.Position, Argument: argument, Resolve: m.Method == "codeAction/resolve"}
	params.TextDocument.URI = argument.Source.URI
	encoded, err := json.Marshal(params)
	if err != nil {
		return m, analysis.ExplainRefusal(err), true
	}
	m.Method, m.Params = referenceCandidateMethod, encoded
	return m, nil, true
}

func candidateSnapshot(ctx context.Context, path string, buffers map[string]buffer) (source.Snapshot, candidateFileIdentity, error) {
	var snapshot source.Snapshot
	identity := candidateFileIdentity{}
	for uri, b := range buffers {
		canonical, err := workspace.PathFromURI(uri)
		if err != nil || canonical != path {
			continue
		}
		if identity.Opened || b.unavailable != "" {
			return snapshot, identity, analysis.Refuse("INPUT_UNAVAILABLE", "A candidate has ambiguous or unavailable open input.")
		}
		snapshot = b.snapshot
		identity.Opened, identity.Version, identity.Revision = true, b.version, uint64(b.snapshot.Revision())
	}
	if !identity.Opened {
		text, err := workspace.ReadFile(ctx, path)
		if err != nil {
			return snapshot, identity, err
		}
		snapshot, err = source.New(source.URI(workspace.FileURI(path)), 1, text)
		if err != nil {
			return snapshot, identity, err
		}
	}
	identity.SHA256 = candidateHash(snapshot.Text())
	return snapshot, identity, nil
}

// Collect index identities under a generation sampled before enumeration. Only
// fresh full documents below supply locations. Index truncation remains visible
// even when exact-name filtering removes most of the scanned symbols.
func (s *workspaceIndex) candidateIndex(ctx context.Context, target workspace.Target, name string, buffers map[string]buffer, override string, disk map[string]uint64) ([]referenceCandidateSource, *readProof, uint64, bool, bool, error) {
	if err := s.queryReadLock(ctx); err != nil {
		return nil, nil, 0, false, false, err
	}
	root := s.roots[target.Root]
	if root == nil || root.catalog == nil {
		s.mu.RUnlock()
		return nil, nil, 0, false, false, nil
	}
	catalog, generation, event := root.catalog, root.projectGeneration[target.Scope], s.configEvents[target.Root]
	complete := s.scopeComplete(root, target.Scope) && root.pending == 0
	s.mu.RUnlock()
	matches, err := s.facts.SymbolsWithPrefixContext(ctx, indexScope(target.Root, target.Scope), analysis.ProjectKey("", ""), name, 1000, workspaceSymbolWork/2)
	if err != nil && !errors.Is(err, index.ErrQueryBudget) {
		return nil, nil, 0, false, false, err
	}
	if err := s.queryReadLock(ctx); err != nil {
		return nil, nil, 0, false, false, err
	}
	defer s.mu.RUnlock()
	root = s.roots[target.Root]
	if root == nil || root.catalog != catalog || root.projectGeneration[target.Scope] != generation || s.configEvents[target.Root] != event {
		return nil, nil, 0, false, false, index.ErrQueryChanged
	}
	proof := &readProof{engine: s, catalog: catalog, root: target.Root, scopeName: target.Scope,
		scope: indexScope(target.Root, target.Scope), complete: complete, allProjects: true, projectGeneration: generation,
		override: override, diskGeneration: disk[target.Root], buffers: buffers, files: map[string]bool{}, knownFiles: map[string]bool{}}
	paths := map[string][32]byte{}
	for _, match := range matches.Items {
		if match.Symbol.Kind != "AppProject" || match.Symbol.Name != name || match.Symbol.Key != analysis.ProjectKey("", name) {
			continue
		}
		file := s.files[match.URI]
		if file == nil || file.root != target.Root || file.target.Scope != target.Scope || file.revision != match.Revision {
			complete = false
			continue
		}
		paths[file.path] = file.hash
	}
	result := make([]referenceCandidateSource, 0, len(paths))
	for path, hash := range paths {
		result = append(result, referenceCandidateSource{path: path, hash: hash})
	}
	sort.Slice(result, func(i, j int) bool { return result[i].path < result[j].path })
	return result, proof, event, complete, matches.Truncated || !matches.Complete, nil
}

func (s *workspaceIndex) referenceCandidates(ctx context.Context, m message, buffers map[string]buffer, roots []string, override string, disk map[string]uint64) (referenceCandidateRead, error) {
	read := referenceCandidateRead{}
	var params referenceCandidateParams
	if err := json.Unmarshal(m.Params, &params); err != nil {
		return read, err
	}
	if m.Method == "textDocument/codeAction" {
		params.Position = params.Range.Start
	}
	uri := params.TextDocument.URI
	b, opened := buffers[uri]
	path, err := workspace.PathFromURI(uri)
	if err != nil || !opened || b.unavailable != "" {
		return read, nil
	}
	root := workspace.Owner(roots, path)
	if root == "" {
		return read, nil
	}
	if _, _, err := candidateSnapshot(ctx, path, buffers); err != nil {
		return read, err
	}
	configuration, err := renderConfiguration(root, buffers)
	if err != nil {
		return read, err
	}
	catalog, err := workspace.Prepare(ctx, root, override, configuration, roots)
	if err != nil || catalog.Input.Config.ControlPlaneNamespace != "" {
		return read, err
	}
	file, included, skipped, err := catalog.File(b.snapshot, path, false)
	if err != nil || !included || skipped {
		return read, err
	}
	analyzer := new(analysis.Analyzer)
	checked, err := analyzer.Analyze(ctx, b.snapshot, file.Target.Version)
	if err != nil || checked.Incomplete {
		return read, err
	}
	name, selection, supported := analysis.ProjectCandidateSelection(checked.Documents, uri, params.Position)
	if !supported {
		return read, nil
	}
	paths, proof, event, complete, truncated, err := s.candidateIndex(ctx, file.Target, name, buffers, override, disk)
	if err != nil {
		return read, err
	}
	read.report = &referenceCandidateReport{Label: "Same-name resource candidates; binding unproven", Selection: selection,
		ScopeComplete: complete, Truncated: truncated, Items: []analysis.ProjectCandidate{},
		Refusal: analysis.Refuse("TARGET_UNKNOWN", "The control-plane namespace is unknown. These same-name declarations are candidates for inspection, not proved bindings.")}
	read.proof = proof
	if proof == nil {
		return read, nil
	}
	indexedTarget, indexedIncluded, selectErr := proof.catalog.Select(path, false)
	indexedTarget.Complete, file.Target.Complete = false, false
	read.report.ScopeComplete = complete && selectErr == nil && indexedIncluded && indexedTarget == file.Target
	proof.files[uri], proof.knownFiles[uri] = true, true
	argument := referenceCandidateArgument{Source: explainArgument{uri, b.version, uint64(b.snapshot.Revision()), params.Position},
		SourceHash: candidateHash(b.snapshot.Text()), Root: root, RootEvent: event, Override: override, InputTarget: file.Target}
	for i, candidatePath := range paths {
		if i > referenceCandidateLimit || len(read.report.Items) >= referenceCandidateLimit {
			read.report.Truncated = true
			break
		}
		if err := s.appendReferenceCandidates(ctx, &read, analyzer, catalog, file.Target.Scope, candidatePath, name, argument, buffers); err != nil {
			return read, err
		}
	}
	read.report.Complete = read.report.ScopeComplete && !read.report.Truncated
	return read, nil
}

func (s *workspaceIndex) appendReferenceCandidates(ctx context.Context, read *referenceCandidateRead, analyzer *analysis.Analyzer, catalog *workspace.Catalog, scope string, candidateSource referenceCandidateSource, name string, argument referenceCandidateArgument, buffers map[string]buffer) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	path := candidateSource.path
	target, included, err := catalog.Select(path, false)
	if err != nil || !included || target.Scope != scope {
		read.report.ScopeComplete = false
		return nil
	}
	snapshot, identity, err := candidateSnapshot(ctx, path, buffers)
	if err != nil {
		read.report.ScopeComplete = false
		return nil
	}
	if sha256.Sum256([]byte(snapshot.Text())) != candidateSource.hash {
		read.report.ScopeComplete = false
	}
	if workspace.IsHelmTemplate(catalog.Input.Root, path, snapshot) {
		read.report.ScopeComplete = false
		return nil
	}
	analyzer.BeginFile()
	checked, err := analyzer.Analyze(ctx, snapshot, target.Version)
	if err != nil || checked.Incomplete {
		read.report.ScopeComplete = false
		return nil
	}
	uri := string(snapshot.URI())
	read.proof.files[uri], read.proof.knownFiles[uri] = true, true
	items, complete := analysis.ProjectCandidateDeclarations(checked.Documents, name, referenceCandidateLimit+1)
	read.report.ScopeComplete = read.report.ScopeComplete && complete
	for _, item := range items {
		if len(read.report.Items) >= referenceCandidateLimit {
			read.report.Truncated = true
			break
		}
		argument.Candidate, argument.Target = item, identity
		read.report.Items = append(read.report.Items, item)
		read.arguments = append(read.arguments, argument)
	}
	return nil
}

func (read referenceCandidateRead) actions() []any {
	actions := []any{}
	for _, argument := range read.arguments {
		actions = append(actions, referenceCandidateAction(argument))
	}
	return actions
}

func (s *workspaceIndex) resolveReferenceCandidate(ctx context.Context, m message, buffers map[string]buffer, roots []string, override string, disk map[string]uint64) (any, *referenceCandidateOpen, *readProof, *analysis.Refusal) {
	stale := analysis.Refuse("STALE_INPUT", "Reference candidate source, target or configuration changed; request current candidates again.")
	var params referenceCandidateParams
	if json.Unmarshal(m.Params, &params) != nil {
		return nil, nil, nil, stale
	}
	read, err := s.referenceCandidates(ctx, m, buffers, roots, override, disk)
	if err != nil {
		return nil, nil, nil, stale
	}
	for _, current := range read.arguments {
		if !reflect.DeepEqual(current, params.Argument) {
			continue
		}
		if params.Resolve {
			return referenceCandidateAction(current), nil, read.proof, nil
		}
		result := referenceCandidateOpen{Location: current.Candidate.Location,
			Message: fmt.Sprintf("Candidate binding is unproven. Open %s at line %d, column %d to inspect this AppProject.", current.Candidate.Location.URI, current.Candidate.Location.Range.Start.Line+1, current.Candidate.Location.Range.Start.Character+1)}
		return result, &result, read.proof, nil
	}
	return nil, nil, read.proof, stale
}

func (report *referenceCandidateReport) message() string {
	lines := []string{report.Label, report.Message,
		fmt.Sprintf("Candidate scope complete: %t; candidate list truncated: %t; enumeration complete: %t", report.ScopeComplete, report.Truncated, report.Complete)}
	for _, item := range report.Items {
		lines = append(lines, fmt.Sprintf("Candidate %s %s/%s — %s:%d:%d", item.Kind, item.Namespace, item.Name, item.Location.URI, item.Location.Range.Start.Line+1, item.Location.Range.Start.Character+1))
	}
	return strings.Join(lines, "\n")
}

func deliverReferenceCandidate(send func(any) error, id json.RawMessage, result *referenceCandidateOpen, canShowDocument bool) error {
	if canShowDocument {
		return send(map[string]any{"jsonrpc": "2.0", "id": referenceCandidateResponsePrefix + string(id), "method": "window/showDocument",
			"params": map[string]any{"uri": result.Location.URI, "selection": result.Location.Range, "takeFocus": true}})
	}
	return send(map[string]any{"jsonrpc": "2.0", "method": "window/showMessage", "params": map[string]any{"type": 3, "message": result.Message}})
}
