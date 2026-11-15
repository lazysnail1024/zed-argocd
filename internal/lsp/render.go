package lsp

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"sort"
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
type Renderer func(context.Context, workspace.RenderRequest) (RenderResult, error)
type renderArgument struct {
	URI         string `json:"uri"`
	Version     int    `json:"version"`
	Revision    uint64 `json:"revision"`
	RootEvent   uint64 `json:"rootEvent"`
	Profile     string `json:"profile"`
	Fingerprint string `json:"fingerprint"`
}
type renderResponse struct {
	ctx    context.Context
	id     json.RawMessage
	result RenderResult
	proof  *renderProof
	err    error
}

type renderProof struct {
	argument renderArgument
	request  workspace.RenderRequest
	override string
	opened   map[string]uint64
}

func (s *workspaceIndex) renderEvents(roots []string) map[string]uint64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	events := map[string]uint64{}
	for _, root := range roots {
		events[root] = s.configEvents[root]
	}
	return events
}

func renderConfiguration(root string, buffers map[string]buffer) (*string, error) {
	var result *string
	for uri, b := range buffers {
		file, err := workspace.PathFromURI(uri)
		if err != nil || file != filepath.Join(root, ".argocd-ls.yaml") {
			continue
		}
		if b.unavailable != "" {
			return nil, fmt.Errorf("configuration buffer is unavailable")
		}
		text := b.snapshot.Text()
		if result != nil && *result != text {
			return nil, fmt.Errorf("configuration aliases disagree")
		}
		result = &text
	}
	return result, nil
}

func renderSource(ctx context.Context, uri string, buffers map[string]buffer, roots []string) (string, string, bool) {
	b, ok := buffers[uri]
	if !ok || b.unavailable != "" || !analysis.HelmSourceOwned(ctx, b.snapshot) {
		return "", "", false
	}
	path, err := workspace.PathFromURI(uri)
	if err != nil {
		return "", "", false
	}
	root := workspace.Owner(roots, path)
	if root == "" {
		return "", "", false
	}
	if _, err := workspace.FindChart(root, path); err != nil {
		return "", "", false
	}
	return root, path, true
}

func resolveRender(ctx context.Context, uri, profile string, buffers map[string]buffer, roots []string, target string) (workspace.RenderRequest, error) {
	root, path, ok := renderSource(ctx, uri, buffers, roots)
	if !ok {
		return workspace.RenderRequest{}, fmt.Errorf("render requires an owned open chart source inside the workspace")
	}
	configuration, err := renderConfiguration(root, buffers)
	if err != nil {
		return workspace.RenderRequest{}, err
	}
	return workspace.ResolveRenderRequest(ctx, workspace.RenderOptions{Root: root, File: path, Profile: profile, Target: target, Config: configuration, Roots: roots})
}

func renderAction(request workspace.RenderRequest, arg renderArgument) any {
	title := "Render chart and check Argo CD (saved files)"
	if request.Profile != "" {
		title = "Render profile " + request.Profile + " and check Argo CD (saved files)"
	}
	return map[string]any{"title": title, "kind": "source", "command": map[string]any{"title": title, "command": renderCommand, "arguments": []any{arg}}}
}

func renderActions(ctx context.Context, uri string, buffers map[string]buffer, roots []string, target string, events map[string]uint64) []any {
	root, _, ok := renderSource(ctx, uri, buffers, roots)
	if !ok {
		return nil
	}
	configuration, err := renderConfiguration(root, buffers)
	if err != nil {
		return nil
	}
	catalog, err := workspace.Prepare(ctx, root, target, configuration, roots)
	if err != nil {
		return nil
	}
	profiles := []string{""}
	for name := range catalog.Input.Config.RenderProfiles {
		profiles = append(profiles, name)
	}
	sort.Strings(profiles)
	actions := []any{}
	b := buffers[uri]
	for _, name := range profiles {
		request, err := resolveRender(ctx, uri, name, buffers, roots, target)
		if err != nil {
			continue
		}
		arg := renderArgument{URI: uri, Version: b.version, Revision: uint64(b.revision), RootEvent: events[root], Profile: name, Fingerprint: request.Fingerprint}
		actions = append(actions, renderAction(request, arg))
	}
	return actions
}

func renderInput(request workspace.RenderRequest, file string) bool {
	rel, err := filepath.Rel(request.Chart, file)
	if err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return true
	}
	if file == filepath.Join(request.Root, ".argocd-ls.yaml") {
		return true
	}
	var info os.FileInfo
	for _, input := range request.Inputs {
		if input.Path == file {
			return true
		}
		if info == nil {
			info, _ = os.Stat(file)
		}
		if info != nil {
			other, _ := os.Stat(input.Path)
			if other != nil && os.SameFile(info, other) {
				return true
			}
		}
	}
	return false
}

func renderInputsSaved(ctx context.Context, request workspace.RenderRequest, buffers map[string]buffer) error {
	for uri, opened := range buffers {
		file, e := workspace.PathFromURI(uri)
		if e != nil || !renderInput(request, file) {
			continue
		}
		text, e := workspace.ReadFile(ctx, file)
		if e != nil || opened.unavailable != "" || text != opened.snapshot.Text() {
			return fmt.Errorf("save all open chart, configuration and selected values files before render-check: %s", file)
		}
	}
	return nil
}

func renderCurrent(ctx context.Context, arg renderArgument, buffers map[string]buffer, roots []string, target string, events map[string]uint64, requireSaved bool) (workspace.RenderRequest, error) {
	b, ok := buffers[arg.URI]
	path, err := workspace.PathFromURI(arg.URI)
	root := workspace.Owner(roots, path)
	if !ok || err != nil || root == "" || b.version != arg.Version || uint64(b.revision) != arg.Revision || events[root] != arg.RootEvent {
		return workspace.RenderRequest{}, fmt.Errorf("render action is stale; request it again")
	}
	request, err := resolveRender(ctx, arg.URI, arg.Profile, buffers, roots, target)
	if err != nil {
		return request, fmt.Errorf("render action is stale: %w", err)
	}
	if requireSaved {
		if err := renderInputsSaved(ctx, request, buffers); err != nil {
			return request, err
		}
	}
	if request.Fingerprint != arg.Fingerprint {
		return request, fmt.Errorf("render action is stale; inputs changed, request it again")
	}
	return request, nil
}

func renderSaved(ctx context.Context, renderer Renderer, arg renderArgument, buffers map[string]buffer, roots []string, target string, events map[string]uint64) (RenderResult, *renderProof, error) {
	request, err := renderCurrent(ctx, arg, buffers, roots, target, events, true)
	if err != nil {
		return RenderResult{}, nil, err
	}
	proof := &renderProof{argument: arg, request: request, override: target, opened: map[string]uint64{}}
	for uri, b := range buffers {
		path, err := workspace.PathFromURI(uri)
		if err == nil && renderInput(request, path) {
			proof.opened[uri] = uint64(b.revision)
		}
	}
	b := buffers[arg.URI]
	hash := sha256.Sum256([]byte(b.snapshot.Text()))
	request.Source = &workspace.RenderSourceIdentity{URI: arg.URI, Version: arg.Version, Revision: arg.Revision, SHA256: hex.EncodeToString(hash[:])}
	result, err := renderer(ctx, request)
	return result, proof, err
}

func (proof *renderProof) valid(ctx context.Context, buffers map[string]buffer, roots []string, target string, events map[string]uint64) bool {
	if proof == nil || ctx.Err() != nil || proof.override != target {
		return false
	}
	currentRoots := append([]string{}, roots...)
	sort.Strings(currentRoots)
	if !reflect.DeepEqual(currentRoots, proof.request.Roots) {
		return false
	}
	path, err := workspace.PathFromURI(proof.argument.URI)
	if err != nil || workspace.Owner(roots, path) != proof.request.Root || events[proof.request.Root] != proof.argument.RootEvent {
		return false
	}
	for uri, revision := range proof.opened {
		b, ok := buffers[uri]
		if !ok || uint64(b.revision) != revision {
			return false
		}
	}
	return renderInputsSaved(ctx, proof.request, buffers) == nil
}
