package lsp

import (
	"context"
	"encoding/json"

	"github.com/lazysnail1024/zed-argocd/internal/analysis"
)

type renameParams struct {
	TextDocument struct {
		URI string `json:"uri"`
	} `json:"textDocument"`
	Position analysis.Position `json:"position"`
	NewName  string            `json:"newName"`
}

// The last return value selects a document-private proof. Resource renames
// acquire the entire selected scope and retain its generation instead.
func (s *workspaceIndex) renameRequest(ctx context.Context, m message, input *completed, buffers map[string]buffer, canEdit bool) (any, int, string, bool) {
	if !canEdit {
		input.refusal = analysis.Refuse("CLIENT_CAPABILITY_MISSING", "client does not support versioned documentChanges")
		return nil, -32602, "client does not support versioned documentChanges", false
	}
	var params renameParams
	if err := json.Unmarshal(m.Params, &params); err != nil {
		return nil, -32602, "invalid rename parameters", false
	}
	b, exists := buffers[params.TextDocument.URI]
	if !exists || b.unavailable != "" || input.err != nil {
		input.refusal = analysis.Refuse("INPUT_UNAVAILABLE", "rename requires the current accepted open document")
		return nil, -32801, "rename requires the current accepted open document", false
	}
	for _, scope := range input.scopes {
		for _, doc := range scope.documents {
			if string(doc.Snapshot.URI()) != params.TextDocument.URI || doc.Snapshot.Revision() != b.snapshot.Revision() {
				continue
			}
			if !analysis.SourceRefRenamePosition(doc, params.Position) {
				continue
			}
			target, err := analysis.PrepareSourceRefRename(ctx, doc, params.Position)
			if err != nil {
				input.refusal = analysis.ExplainRefusal(err)
				if m.Method == "textDocument/prepareRename" {
					return nil, 0, "", true
				}
				return nil, -32602, err.Error(), true
			}
			if m.Method == "textDocument/prepareRename" {
				return target, 0, "", true
			}
			plan, err := analysis.RenameSourceRef(ctx, doc, params.Position, params.NewName)
			if err != nil {
				input.refusal = analysis.ExplainRefusal(err)
				return nil, -32602, err.Error(), true
			}
			edits := []analysis.TextEdit{}
			for _, replacement := range plan.Replacements {
				edits = append(edits, replacementEdit(b.snapshot, replacement.Span, replacement.NewText))
			}
			return map[string]any{"documentChanges": []any{map[string]any{"textDocument": map[string]any{"uri": params.TextDocument.URI, "version": b.version}, "edits": edits}}}, 0, "", true
		}
	}
	answer, code, why := s.projectRenameRequest(ctx, m, input, buffers, params)
	return answer, code, why, false
}
