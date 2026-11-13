package lsp

import (
	"context"
	"encoding/json"
	"fmt"
	"path/filepath"
	"strings"

	"github.com/lazysnail1024/zed-argocd/internal/analysis"
	"github.com/lazysnail1024/zed-argocd/internal/workspace"
)

const renderCommand = "argocd.renderCheck"

type RenderResult struct {
	Output  string `json:"output"`
	Message string `json:"message"`
	Report  string `json:"report"`
}
type Renderer func(context.Context, string, string, string) (RenderResult, error)
type renderArgument struct {
	URI     string `json:"uri"`
	Version int    `json:"version"`
}
type renderResponse struct {
	ctx    context.Context
	id     json.RawMessage
	result RenderResult
	err    error
}

func renderAction(ctx context.Context, uri string, buffers map[string]buffer, roots []string) (any, bool) {
	b, ok := buffers[uri]
	if !ok || b.unavailable != "" {
		return nil, false
	}
	path, err := workspace.PathFromURI(uri)
	if err != nil {
		return nil, false
	}
	root := workspace.Owner(roots, path)
	if root == "" {
		return nil, false
	}
	if _, err = workspace.FindChart(root, path); err != nil {
		return nil, false
	}
	if !analysis.HelmSourceOwned(ctx, b.snapshot) {
		return nil, false
	}
	return map[string]any{"title": "Render chart and check Argo CD (saved files)", "kind": "source", "command": map[string]any{"title": "Render chart and check Argo CD", "command": renderCommand, "arguments": []any{renderArgument{uri, b.version}}}}, true
}

func renderSaved(ctx context.Context, renderer Renderer, arg renderArgument, buffers map[string]buffer, roots []string, target string) (RenderResult, error) {
	b, ok := buffers[arg.URI]
	if !ok || b.version != arg.Version {
		return RenderResult{}, fmt.Errorf("render action is stale; request it again")
	}
	if _, ok := renderAction(ctx, arg.URI, buffers, roots); !ok {
		return RenderResult{}, fmt.Errorf("render requires an owned open chart source inside the workspace")
	}
	path, _ := workspace.PathFromURI(arg.URI)
	root := workspace.Owner(roots, path)
	chart, err := workspace.FindChart(root, path)
	if err != nil {
		return RenderResult{}, err
	}
	for uri, opened := range buffers {
		file, e := workspace.PathFromURI(uri)
		if e != nil {
			continue
		}
		rel, e := filepath.Rel(chart, file)
		inChart := e == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
		if !inChart && file != filepath.Join(root, ".argocd-ls.yaml") {
			continue
		}
		text, e := workspace.ReadFile(ctx, file)
		if e != nil || opened.unavailable != "" || text != opened.snapshot.Text() {
			return RenderResult{}, fmt.Errorf("save all open chart and configuration files before render-check")
		}
	}
	return renderer(ctx, root, path, target)
}
