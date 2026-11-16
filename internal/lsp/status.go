package lsp

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"path/filepath"
	"strings"

	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/zed-argocd/internal/analysis"
	"github.com/lazysnail1024/zed-argocd/internal/workspace"
)

const analysisStatusMethod = "argocd/analysisStatus"
const explainCommand = "argocd.explainAnalysis"
const explainActionKind = "source.argocd.explainAnalysis"

type explainArgument struct {
	URI      string            `json:"uri"`
	Version  int               `json:"version"`
	Revision uint64            `json:"revision"`
	Position analysis.Position `json:"position"`
}

type operationAvailability struct {
	Operation string `json:"operation"`
	Allowed   bool   `json:"allowed"`
	*analysis.Refusal
	ResultCount *int `json:"resultCount,omitempty"`
}

type analysisStatus struct {
	Root          string   `json:"root"`
	Roots         []string `json:"roots"`
	Scope         string   `json:"scope"`
	Phase         string   `json:"phase"`
	InputIdentity struct {
		Source        string `json:"source"`
		URI           string `json:"uri"`
		Version       int    `json:"version"`
		Revision      uint64 `json:"revision"`
		SHA256        string `json:"sha256"`
		ConfigHash    string `json:"configHash"`
		ConfigSource  string `json:"configSource"`
		TargetVersion string `json:"targetVersion"`
		TargetSource  string `json:"targetSource"`
	} `json:"inputIdentity"`
	Completeness struct {
		Document  bool `json:"document"`
		Workspace bool `json:"workspace"`
	} `json:"completeness"`
	Reasons             []*analysis.Refusal       `json:"reasons"`
	Operations          []operationAvailability   `json:"operations"`
	ReferenceCandidates *referenceCandidateReport `json:"referenceCandidates,omitempty"`
}

// explainAction depends only on the accepted current buffer. It remains useful
// while indexing is incomplete, and it cannot authorize an edit or disk write.
func explainActions(m message, buffers map[string]buffer, roots []string) []any {
	var p struct {
		TextDocument struct {
			URI string `json:"uri"`
		} `json:"textDocument"`
		Range   analysis.Range `json:"range"`
		Context struct {
			Only []string `json:"only"`
		} `json:"context"`
	}
	if json.Unmarshal(m.Params, &p) != nil {
		return nil
	}
	b, opened := buffers[p.TextDocument.URI]
	path, err := workspace.PathFromURI(p.TextDocument.URI)
	if !opened || b.unavailable != "" || err != nil || workspace.Owner(roots, path) == "" {
		return nil
	}
	result := []any{}
	for _, kind := range []string{explainActionKind, compatibilityActionKind} {
		allowed := len(p.Context.Only) == 0
		for _, selected := range p.Context.Only {
			allowed = allowed || kind == selected || strings.HasPrefix(kind, selected+".")
		}
		if allowed {
			result = append(result, explanationAction(explainArgument{p.TextDocument.URI, b.version, uint64(b.snapshot.Revision()), p.Range.Start}, kind))
		}
	}
	return result
}

func explanationAction(arg explainArgument, kind string) any {
	title, command := "Explain current Argo CD analysis", explainCommand
	if kind == compatibilityActionKind {
		title, command = "Explain Argo CD compatibility candidates", compatibilityCommand
	}
	return map[string]any{"title": title, "kind": kind,
		"command": map[string]any{"title": title, "command": command, "arguments": []explainArgument{arg}}}
}

func explainOnly(m message) bool {
	var p struct {
		Context struct {
			Only []string `json:"only"`
		} `json:"context"`
	}
	if json.Unmarshal(m.Params, &p) != nil || len(p.Context.Only) == 0 {
		return false
	}
	for _, kind := range p.Context.Only {
		if kind != explainActionKind && kind != compatibilityActionKind {
			return false
		}
	}
	return true
}

func explainRequest(m message, buffers map[string]buffer) (message, *analysis.Refusal, bool) {
	var p struct {
		Command   string            `json:"command"`
		Arguments []explainArgument `json:"arguments"`
	}
	if json.Unmarshal(m.Params, &p) != nil || (p.Command != explainCommand && p.Command != compatibilityCommand) {
		return m, nil, false
	}
	if len(p.Arguments) != 1 {
		return m, analysis.Refuse("INPUT_UNAVAILABLE", "explain analysis requires one current document argument"), true
	}
	arg := p.Arguments[0]
	b, opened := buffers[arg.URI]
	if !opened || b.unavailable != "" || b.version != arg.Version || uint64(b.snapshot.Revision()) != arg.Revision {
		return m, analysis.Refuse("STALE_INPUT", "analysis explanation action is stale; request it again"), true
	}
	params, err := json.Marshal(map[string]any{"textDocument": map[string]any{"uri": arg.URI}, "position": arg.Position})
	if err != nil {
		return m, analysis.ExplainRefusal(err), true
	}
	m.Method, m.Params = analysisStatusMethod, params
	if p.Command == compatibilityCommand {
		m.Method = compatibilityMethod
	}
	return m, nil, true
}

// explainAnalysis projects already-owned analysis and runs the existing prepare
// guard path. It does not retain state, reinterpret an error string, change
// reference policy, or execute Rename's post-edit simulation.
func (s *workspaceIndex) explainAnalysis(ctx context.Context, m message, input *completed, buffers map[string]buffer, roots []string, canEdit bool) analysisStatus {
	status := analysisStatus{Roots: append([]string{}, roots...), Phase: "pending", Reasons: []*analysis.Refusal{}, Operations: []operationAvailability{}}
	var params renameParams
	if err := json.Unmarshal(m.Params, &params); err != nil {
		status.Reasons = append(status.Reasons, analysis.ExplainRefusal(err))
		return status
	}
	uri := params.TextDocument.URI
	path, err := workspace.PathFromURI(uri)
	if err == nil {
		status.Root = workspace.Owner(roots, path)
	}
	status.InputIdentity.URI, status.InputIdentity.Source = uri, "buffer"
	if b, opened := buffers[uri]; opened {
		hash := sha256.Sum256([]byte(b.snapshot.Text()))
		status.InputIdentity.Version, status.InputIdentity.Revision = b.version, uint64(b.revision)
		status.InputIdentity.SHA256 = hex.EncodeToString(hash[:])
	} else {
		status.InputIdentity.Source = "disk"
	}
	for _, scope := range input.scopes {
		checked, found := scope.checked[source.URI(uri)]
		if !found {
			continue
		}
		status.Root, status.Scope = scope.root, scope.scope
		status.Completeness.Document = !checked.Incomplete && input.err == nil
		status.Completeness.Workspace = scope.options.Complete && !input.documentOnly
		target := inputTarget(*input, uri)
		status.InputIdentity.ConfigHash, status.InputIdentity.TargetVersion = target.ConfigHash, target.Version
		status.InputIdentity.TargetSource = target.Source
		status.InputIdentity.ConfigSource = "disk-or-defaults"
		for spelling := range buffers {
			configPath, _ := workspace.PathFromURI(spelling)
			if configPath == filepath.Join(scope.root, ".argocd-ls.yaml") {
				status.InputIdentity.ConfigSource = "buffer"
			}
		}
		refs := operationAvailability{Operation: "references", Allowed: status.Completeness.Workspace}
		if refs.Allowed {
			full, err := s.completeQueryScope(ctx, input, uri)
			if err != nil {
				refs.Allowed, refs.Refusal = false, analysis.ExplainRefusal(err)
			} else {
				locations, limitation := analysis.ReferencesExplained(full.documents, full.options, uri, params.Position, false)
				refs.Refusal, refs.Allowed = limitation, limitation == nil
				if refs.Allowed {
					count := len(locations)
					refs.ResultCount = &count
				}
			}
		} else {
			refs.Refusal = incompleteScopeReason(status.Scope)
		}
		status.Operations = append(status.Operations, refs)
		break
	}
	status.Phase = s.analysisPhase(ctx, status.Root, status.Completeness.Workspace)
	if status.Root == "" {
		status.Reasons = append(status.Reasons, analysis.Refuse("OUTSIDE_WORKSPACE", "document is outside the roots supplied by the client"))
	} else if input.err != nil {
		status.Reasons = append(status.Reasons, analysis.ExplainRefusal(input.err))
	} else if !status.Completeness.Document {
		status.Reasons = append(status.Reasons, analysis.Refuse("INPUT_UNAVAILABLE", "the current document has not completed independent analysis"))
	}
	if !status.Completeness.Workspace {
		status.Reasons = append(status.Reasons, incompleteScopeReason(status.Scope))
		if strings.HasPrefix(status.Scope, "isolated:") {
			status.Phase = "isolated"
		}
	}
	prepare := m
	prepare.Method = "textDocument/prepareRename"
	answer, _, _, _ := s.renameRequest(ctx, prepare, input, buffers, canEdit)
	op := operationAvailability{Operation: "rename", Allowed: answer != nil, Refusal: input.refusal}
	if !op.Allowed && op.Refusal == nil {
		op.Refusal = analysis.Refuse("NOT_RENAME_TARGET", "position is not a supported rename binding")
	}
	status.Operations = append(status.Operations, op)
	return status
}

func incompleteScopeReason(scope string) *analysis.Refusal {
	if strings.HasPrefix(scope, "isolated:") {
		return analysis.Refuse("ISOLATED_DOCUMENT", "excluded documents receive independent analysis without entering the workspace index")
	}
	return analysis.Refuse("INDEX_INCOMPLETE", "selected workspace scope is not completely analyzed; absence of references is unproved")
}

func (s *workspaceIndex) analysisPhase(ctx context.Context, rootPath string, complete bool) string {
	if complete {
		return "complete"
	}
	if err := s.queryReadLock(ctx); err != nil {
		return "cancelled"
	}
	defer s.mu.RUnlock()
	root := s.roots[rootPath]
	if root == nil || root.catalog == nil {
		return "acquisition"
	}
	if root.err != nil {
		return "incomplete"
	}
	if !root.discovered || !root.scanComplete {
		return "discovery"
	}
	if root.pending != 0 {
		return "analysis"
	}
	return "incomplete"
}

func (s analysisStatus) message() string {
	lines := []string{fmt.Sprintf("Argo CD analysis — %s input, version %d", s.InputIdentity.Source, s.InputIdentity.Version),
		fmt.Sprintf("Root: %s\nScope: %s\nPhase: %s", s.Root, s.Scope, s.Phase),
		fmt.Sprintf("Current document complete: %t; workspace scope complete: %t", s.Completeness.Document, s.Completeness.Workspace)}
	if s.ReferenceCandidates != nil {
		lines = append(lines, s.ReferenceCandidates.message())
	}
	for _, reason := range s.Reasons {
		lines = append(lines, reason.ReasonCode+": "+reason.Message+" "+reason.NextStep)
	}
	for _, op := range s.Operations {
		if op.Allowed {
			lines = append(lines, op.Operation+": available for this input and selection")
		} else if op.Refusal != nil {
			lines = append(lines, op.Operation+": "+op.ReasonCode+" — "+op.Message+" "+op.NextStep)
			for _, location := range op.RelatedLocations {
				lines = append(lines, "Affected file: "+location.URI)
			}
		}
	}
	return strings.Join(lines, "\n")
}
