package lsp

import (
	"context"
	"encoding/json"
	"github.com/lazysnail1024/k8s-crd-toolkit/edit"
	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/zed-argocd/internal/analysis"
	"github.com/lazysnail1024/zed-argocd/internal/workspace"
	"path/filepath"
)

// Configuration actions use the same immutable workspace and post-edit error gate.
func pinRequest(ctx context.Context, m message, latest completed, buffers map[string]buffer, epoch uint64, canEdit bool, roots []string, override string) (any, int, string, bool) {
	var p struct {
		TextDocument struct {
			URI string `json:"uri"`
		} `json:"textDocument"`
		Range analysis.Range `json:"range"`
		Data  fixData        `json:"data"`
	}
	if json.Unmarshal(m.Params, &p) != nil {
		return nil, 0, "", false
	}
	uri := p.TextDocument.URI
	if m.Method == "codeAction/resolve" {
		if p.Data.FixCode != "FIX_PIN_VERSION" {
			return nil, 0, "", false
		}
		uri = p.Data.URI
	}
	file, err := workspace.PathFromURI(uri)
	if err != nil || filepath.Base(file) != ".argocd-ls.yaml" {
		return nil, 0, "", false
	}
	empty := []any{}
	b, open := buffers[uri]
	if !canEdit || !open {
		if m.Method == "codeAction/resolve" {
			return nil, -32801, "configuration is no longer open for versioned editing", true
		}
		return empty, 0, "", true
	}
	if latest.epoch != epoch || latest.err != nil {
		return nil, -32801, "analysis changed", true
	}
	if m.Method == "codeAction/resolve" && (p.Data.Epoch != latest.editGeneration || p.Data.Revision != uint64(b.snapshot.Revision())) {
		return nil, -32801, "pin action is stale", true
	}
	var input *workspace.Input
	for i := range latest.inputs {
		if latest.inputs[i].Root == filepath.Dir(file) {
			input = &latest.inputs[i]
			break
		}
	}
	if input == nil {
		return empty, 0, "", true
	}
	actions := []any{}
	for _, pin := range workspace.Pins(ctx, *input, b.snapshot) {
		r := pin.Plan.Replacements[0]
		if m.Method == "textDocument/codeAction" {
			start, e1 := b.snapshot.Offset(source.Position{Line: p.Range.Start.Line, UTF16Column: p.Range.Start.Character})
			end, e2 := b.snapshot.Offset(source.Position{Line: p.Range.End.Line, UTF16Column: p.Range.End.Character})
			if e1 != nil || e2 != nil || pin.Selection.Start > end || pin.Selection.End < start {
				continue
			}
		} else if p.Data.Target != pin.Version || p.Data.Start != r.Span.Start || p.Data.End != r.Span.End || p.Data.NewText != r.NewText {
			continue
		}
		next, e := edit.Apply(b.snapshot, b.snapshot.Revision()+1, pin.Plan)
		if e != nil {
			continue
		}
		copied := map[string]buffer{}
		for uri, value := range buffers {
			copied[uri] = value
		}
		copied[uri] = buffer{snapshot: next, version: b.version, revision: next.Revision()}
		after := analyzeRoots(ctx, epoch, roots, copied, override)
		if ctx.Err() != nil || after.err != nil || !pinSafe(latest, after, uri, pin) {
			continue
		}
		if m.Method == "textDocument/codeAction" {
			data := fixData{URI: uri, Epoch: latest.editGeneration, Revision: uint64(b.snapshot.Revision()), Target: pin.Version, FixCode: "FIX_PIN_VERSION", Start: r.Span.Start, End: r.Span.End, NewText: r.NewText}
			actions = append(actions, map[string]any{"title": "Pin " + pin.Scope + " to " + pin.Version, "kind": "quickfix", "data": data})
		} else {
			var action map[string]any
			_ = json.Unmarshal(m.Params, &action)
			action["edit"] = map[string]any{"documentChanges": []any{map[string]any{"textDocument": map[string]any{"uri": uri, "version": b.version}, "edits": []analysis.TextEdit{replacementEdit(b.snapshot, r.Span, r.NewText)}}}}
			return action, 0, "", true
		}
	}
	if m.Method == "codeAction/resolve" {
		return nil, -32801, "pin action is no longer safe", true
	}
	return actions, 0, "", true
}

func pinSafe(before, after completed, uri string, pin workspace.Pin) bool {
	config, err := workspace.PathFromURI(uri)
	if err != nil {
		return false
	}
	root := filepath.Dir(config)
	// Completeness is the normal analyzer's scope fact, including zero documents.
	for _, result := range []completed{before, after} {
		found := false
		for _, scope := range result.scopes {
			if scope.root != root || (!pin.Root && scope.scope != pin.Scope) {
				continue
			}
			found = true
			if !scope.options.Complete {
				return false
			}
		}
		if !found {
			return false
		}
	}
	counts := map[string]int{}
	for _, p := range before.publications {
		for _, d := range p.diagnostics {
			if d.Severity == 1 {
				counts[d.URI+"\n"+d.Code+"\n"+d.Message]++
			}
		}
	}
	for _, p := range after.publications {
		for _, d := range p.diagnostics {
			if d.Severity == 1 {
				key := d.URI + "\n" + d.Code + "\n" + d.Message
				counts[key]--
				if counts[key] < 0 {
					return false
				}
			}
		}
	}
	for _, input := range after.inputs {
		config, _ := workspace.PathFromURI(uri)
		if input.Root != filepath.Dir(config) {
			continue
		}
		if pin.Root {
			return input.Config.TargetVersion == pin.Version
		}
		for _, s := range input.Config.Scopes {
			if s.Name == pin.Scope {
				return s.TargetVersion == pin.Version
			}
		}
	}
	return false
}
