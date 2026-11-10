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
		return nil, -32602, "client does not support versioned documentChanges", false
	}
	var params renameParams
	if err := json.Unmarshal(m.Params, &params); err != nil {
		return nil, -32602, "invalid rename parameters", false
	}
	b, exists := buffers[params.TextDocument.URI]
	if !exists || b.unavailable != "" || input.err != nil {
		return nil, -32801, "rename requires the current accepted open document", false
	}
	for _, scope := range input.scopes {
		for _, doc := range scope.documents {
			if string(doc.Snapshot.URI()) != params.TextDocument.URI || doc.Snapshot.Revision() != b.snapshot.Revision() {
				continue
			}
			if m.Method == "textDocument/prepareRename" {
				target, err := analysis.PrepareSourceRefRename(ctx, doc, params.Position)
				if err == nil {
					return target, 0, "", true
				}
				continue
			}
			if _, err := analysis.PrepareSourceRefRename(ctx, doc, params.Position); err != nil {
				continue
			}
			plan, err := analysis.RenameSourceRef(ctx, doc, params.Position, params.NewName)
			if err != nil {
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
