// Package workspace owns filesystem scope, configuration and immutable inputs.
package workspace

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/zed-argocd/internal/assets"
	"io"
	"io/fs"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"sigs.k8s.io/yaml"
	"sort"
	"strings"
)

// MaxRetainedText bounds logical snapshot text, not process RSS.
const MaxRetainedText = 128 * 1024 * 1024
const MaxFileText = 5 * 1024 * 1024

type Scope struct {
	Name          string   `json:"name"`
	Include       []string `json:"include"`
	TargetVersion string   `json:"targetVersion"`
}
type Repository struct {
	RepoURL        string `json:"repoURL"`
	TargetRevision string `json:"targetRevision"`
	Path           string `json:"path"`
}
type Config struct {
	SchemaVersion         int          `json:"schemaVersion"`
	TargetVersion         string       `json:"targetVersion"`
	ReferencePolicy       string       `json:"referencePolicy"`
	ControlPlaneNamespace string       `json:"controlPlaneNamespace"`
	Exclude               []string     `json:"exclude"`
	Scopes                []Scope      `json:"scopes"`
	Repositories          []Repository `json:"repositories"`
}
type Target struct {
	SchemaHash string `json:"schemaHash"`
	RulesetID  string `json:"rulesetId"`
	ID         string `json:"id"`
	Root       string `json:"root"`
	Scope      string `json:"scope"`
	Version    string `json:"version"`
	Source     string `json:"source"`
	ConfigHash string `json:"configHash"`
	Complete   bool   `json:"complete"`
}
type File struct {
	Snapshot source.Snapshot
	Target   Target
	Output   bool
}
type Input struct {
	LocalPaths map[string]map[string]bool
	Root       string
	Config     Config
	Targets    []Target
	Files      []File
	Complete   bool
}
type Error struct {
	Code    string
	Message string
}

func (e *Error) Error() string           { return e.Message }
func failure(code, message string) error { return &Error{code, message} }

func Load(ctx context.Context, root, selectedScope, selectedFile, override string) (Input, error) {
	remaining := MaxRetainedText
	return load(ctx, root, selectedScope, selectedFile, override, nil, nil, nil, &remaining)
}
func LoadWithConfig(ctx context.Context, root, scope, file, target string, config *string) (Input, error) {
	remaining := MaxRetainedText
	return load(ctx, root, scope, file, target, config, nil, nil, &remaining)
}

// LoadBuffers applies immutable open text before disk reads, using one file policy.
func LoadBuffers(ctx context.Context, root, target string, config *string, buffers map[string]source.Snapshot, roots []string, remaining *int) (Input, error) {
	return load(ctx, root, "", "", target, config, buffers, roots, remaining)
}

// Owner selects the deepest containing canonical root, independently of list order.
func Owner(roots []string, file string) string {
	owner := ""
	for _, r := range roots {
		if inside(r, file) && len(r) > len(owner) {
			owner = r
		}
	}
	return owner
}
func load(ctx context.Context, root, selectedScope, selectedFile, override string, configOverride *string, buffers map[string]source.Snapshot, roots []string, remaining *int) (Input, error) {
	input := Input{Complete: true, Targets: []Target{}, Files: []File{}}
	if err := ctx.Err(); err != nil {
		return input, err
	}
	root, err := filepath.Abs(root)
	if err != nil {
		return input, err
	}
	root, err = filepath.EvalSymlinks(root)
	if err != nil {
		return input, failure("INPUT_INCOMPLETE", err.Error())
	}
	input.Root = root
	cfg := Config{SchemaVersion: 1, ReferencePolicy: "open"}
	var raw []byte
	if configOverride != nil {
		if len(*configOverride) > 1024*1024 {
			return input, failure("CONFIG_INVALID", "configuration exceeds 1 MiB")
		}
		raw = []byte(*configOverride)
	} else {
		raw, err = readConfig(ctx, filepath.Join(root, ".argocd-ls.yaml"))
	}
	if e := ctx.Err(); e != nil {
		return input, e
	}
	if err != nil && !os.IsNotExist(err) {
		return input, err
	}
	if err == nil {
		if len(raw) > 1024*1024 {
			return input, failure("CONFIG_INVALID", "configuration exceeds 1 MiB")
		}
		if err = yaml.UnmarshalStrict(raw, &cfg); err != nil {
			return input, failure("CONFIG_INVALID", err.Error())
		}
	}
	if cfg.SchemaVersion != 1 || (cfg.ReferencePolicy != "open" && cfg.ReferencePolicy != "closed") {
		return input, failure("CONFIG_INVALID", "unsupported schemaVersion or referencePolicy")
	}
	hash := sha256.Sum256(raw)
	configHash := hex.EncodeToString(hash[:])
	input.Config = cfg
	names := map[string]bool{}
	scopeTargets := map[string]Target{}
	makeTarget := func(name, pin string) (Target, error) {
		version, origin := cfg.TargetVersion, "root"
		if pin != "" {
			version, origin = pin, "scope"
		}
		if override != "" {
			version, origin = override, "override"
		}
		if version == "" {
			version, origin = assets.DefaultVersion(), "default"
			if version == "" {
				return Target{}, failure("TARGET_NOT_VERIFIED", "no verified default target")
			}
		}
		if !assets.Known(version) {
			return Target{}, failure("TARGET_UNKNOWN", "unknown exact target "+version)
		}
		return Target{SchemaHash: assets.BundleHash(version), RulesetID: "argocd-local-static-1", ID: name + ":" + version, Root: root, Scope: name, Version: version, Source: origin, ConfigHash: configHash, Complete: true}, nil
	}
	for _, scope := range cfg.Scopes {
		if scope.Name == "" || scope.Name == "unassigned" || names[scope.Name] || len(scope.Include) == 0 {
			return input, failure("CONFIG_INVALID", "scope name must be unique and include nonempty")
		}
		names[scope.Name] = true
		for _, glob := range scope.Include {
			if err := validateGlob(glob); err != nil {
				return input, failure("CONFIG_INVALID", err.Error())
			}
		}
		t, e := makeTarget(scope.Name, scope.TargetVersion)
		if e != nil {
			return input, e
		}
		scopeTargets[scope.Name] = t
	}
	for _, glob := range cfg.Exclude {
		if err := validateGlob(glob); err != nil {
			return input, failure("CONFIG_INVALID", err.Error())
		}
	}
	implicit := "root"
	if len(cfg.Scopes) > 0 {
		implicit = "unassigned"
	}
	t, err := makeTarget(implicit, "")
	if err != nil {
		return input, err
	}
	scopeTargets[implicit] = t
	if selectedScope != "" {
		if _, ok := scopeTargets[selectedScope]; !ok {
			return input, failure("SCOPE_UNKNOWN", "unknown scope "+selectedScope)
		}
	}
	for _, repo := range cfg.Repositories {
		if repo.RepoURL == "" || repo.Path == "" {
			return input, failure("CONFIG_INVALID", "repository mapping needs repoURL and path")
		}
		if filepath.IsAbs(repo.Path) || !inside(root, filepath.Join(root, repo.Path)) {
			return input, failure("CONFIG_INVALID", "repository path escapes root")
		}
	}
	selected := ""
	if selectedFile != "" {
		selected = selectedFile
		if !filepath.IsAbs(selected) {
			selected = filepath.Join(root, selected)
		}
		selected, err = filepath.EvalSymlinks(selected)
		if err != nil {
			return input, failure("INPUT_INCOMPLETE", err.Error())
		}
		if !inside(root, selected) {
			return input, failure("INPUT_INCOMPLETE", "selected file escapes root")
		}
	}

	input.LocalPaths = map[string]map[string]bool{}
	for _, repo := range cfg.Repositories {
		base := filepath.Join(root, repo.Path)
		canonical, e := filepath.EvalSymlinks(base)
		if e != nil {
			return input, failure("CONFIG_INVALID", "repository mapping path is unavailable: "+repo.Path)
		}
		if !inside(root, canonical) {
			return input, failure("CONFIG_INVALID", "repository mapping escapes root through symlink")
		}
		key := repo.RepoURL + "\x00" + repo.TargetRevision
		if _, exists := input.LocalPaths[key]; exists {
			return input, failure("CONFIG_INVALID", "repository mapping must be unique for URL and revision")
		}
		entries := map[string]bool{}
		count := 0
		e = filepath.WalkDir(canonical, func(p string, d fs.DirEntry, err error) error {
			if err != nil {
				return err
			}
			if err := ctx.Err(); err != nil {
				return err
			}
			count++
			if count > 50000 {
				return failure("INPUT_INCOMPLETE", "repository path budget exceeded")
			}
			if d.Type()&os.ModeSymlink != 0 {
				resolved, e := filepath.EvalSymlinks(p)
				if e != nil || !inside(canonical, resolved) {
					input.Complete = false
					return nil
				}
			}
			relative, _ := filepath.Rel(canonical, p)
			entries[filepath.ToSlash(relative)] = true
			return nil
		})
		if e != nil {
			return input, e
		}
		input.LocalPaths[key] = entries
	}
	paths := []string{}
	count := 0
	err = filepath.WalkDir(root, func(p string, d fs.DirEntry, e error) error {
		if e != nil {
			return e
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		count++
		if count > 50000 {
			return failure("INPUT_INCOMPLETE", "workspace file budget exceeded")
		}
		if d.IsDir() {
			if p != root && excludedDir(d.Name()) {
				return filepath.SkipDir
			}
			return nil
		}
		if strings.HasSuffix(p, ".yaml") || strings.HasSuffix(p, ".yml") {
			paths = append(paths, p)
		}
		return nil
	})
	if err != nil {
		return input, err
	}
	if selected != "" {
		paths = append(paths, selected)
	}
	for p := range buffers {
		if inside(root, p) {
			paths = append(paths, p)
		}
	}
	sort.Strings(paths)
	seen := map[string]bool{}
	selectedOwner := ""
	for _, p := range paths {
		if err := ctx.Err(); err != nil {
			return input, err
		}
		resolved, e := filepath.EvalSymlinks(p)
		if os.IsNotExist(e) {
			if _, opened := buffers[p]; opened {
				resolved = filepath.Clean(p)
				e = nil
			}
		}
		if e != nil {
			input.Complete = false
			continue
		}
		if !inside(root, resolved) || seen[resolved] {
			continue
		}
		seen[resolved] = true
		if len(roots) > 0 && Owner(roots, resolved) != root {
			continue
		}
		rel, _ := filepath.Rel(root, resolved)
		rel = filepath.ToSlash(rel)
		owner := implicit
		matches := 0
		for _, scope := range cfg.Scopes {
			matched := false
			for _, glob := range scope.Include {
				if match(glob, rel) {
					matched = true
					break
				}
			}
			if matched {
				owner = scope.Name
				matches++
			}
		}
		if matches > 1 {
			return input, failure("SCOPE_OVERLAP", "file belongs to multiple scopes: "+rel)
		}
		if resolved == selected {
			selectedOwner = owner
			if selectedScope != "" && owner != selectedScope {
				return input, failure("FILE_SCOPE_MISMATCH", "file does not belong to selected scope")
			}
		}
		excluded := false
		for _, glob := range cfg.Exclude {
			if match(glob, rel) {
				excluded = true
				break
			}
		}
		for _, part := range strings.Split(rel, "/") {
			if excludedDir(part) {
				excluded = true
			}
		}
		_, opened := buffers[resolved]
		if excluded && resolved != selected && !opened {
			continue
		}
		if helmTemplate(root, resolved) {
			continue
		}
		if selectedScope != "" && owner != selectedScope {
			continue
		}
		t := scopeTargets[owner]
		if excluded && opened {
			owner = "isolated:" + resolved
		}
		snap, opened := buffers[resolved]
		if !opened {
			b, e := readStable(ctx, resolved, *remaining)
			if e != nil {
				if ctx.Err() != nil {
					return input, ctx.Err()
				}
				if _, ok := e.(*Error); ok {
					return input, e
				}
				return input, failure("INPUT_INCOMPLETE", e.Error())
			}
			*remaining -= len(b)
			u := url.URL{Scheme: "file", Path: filepath.ToSlash(resolved)}
			snap, e = source.New(source.URI(u.String()), 1, string(b))
			if e != nil {
				return input, e
			}
		}
		if excluded && opened {
			t.Scope = owner
			t.ID += ":" + owner
		}

		input.Files = append(input.Files, File{Snapshot: snap, Target: t, Output: selected == "" || selected == resolved})
	}
	if selected != "" && selectedOwner == "" {
		return input, failure("INPUT_INCOMPLETE", "selected file was not indexed")
	}
	if selected != "" && selectedScope == "" {
		selectedScope = selectedOwner
		filtered := input.Files[:0]
		for _, f := range input.Files {
			if f.Target.Scope == selectedScope {
				filtered = append(filtered, f)
			}
		}
		input.Files = filtered
	}
	for name, t := range scopeTargets {
		if selectedScope == "" || name == selectedScope {
			t.Complete = false
			input.Targets = append(input.Targets, t)
		}
	}
	sort.Slice(input.Targets, func(i, j int) bool { return input.Targets[i].ID < input.Targets[j].ID })
	return input, nil
}
func inside(root, file string) bool {
	rel, e := filepath.Rel(root, file)
	return e == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}
func excludedDir(name string) bool {
	return name == ".git" || name == "node_modules" || name == "vendor" || name == ".argo-ls"
}
func validateGlob(glob string) error {
	if glob == "" || strings.HasPrefix(glob, "/") || strings.Contains(glob, "\\") || strings.Contains(glob, "..") {
		return fmt.Errorf("invalid relative glob %q", glob)
	}
	for _, s := range strings.Split(glob, "/") {
		if _, err := path.Match(s, ""); err != nil {
			return err
		}
	}
	return nil
}
func match(pattern, name string) bool {
	// Most configured scopes have a literal directory prefix. Reject an
	// unequal component before allocating split slices for recursive matching.
	first, _, _ := strings.Cut(pattern, "/")
	component, _, _ := strings.Cut(name, "/")
	if !strings.ContainsAny(first, "*?[\\") && first != component {
		return false
	}
	ps, ns := strings.Split(pattern, "/"), strings.Split(name, "/")
	var walk func(int, int) bool
	walk = func(i, j int) bool {
		if i == len(ps) {
			return j == len(ns)
		}
		if ps[i] == "**" {
			if walk(i+1, j) {
				return true
			}
			return j < len(ns) && walk(i, j+1)
		}
		if j >= len(ns) {
			return false
		}
		ok, _ := path.Match(ps[i], ns[j])
		return ok && walk(i+1, j+1)
	}
	return walk(0, 0)
}
func readStable(ctx context.Context, file string, remaining int) ([]byte, error) {
	for attempt := 0; attempt < 3; attempt++ {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		before, e := os.Stat(file)
		if e != nil {
			return nil, e
		}
		if !before.Mode().IsRegular() || before.Size() > MaxFileText {
			return nil, fmt.Errorf("file exceeds input budget: %s", file)
		}
		if before.Size() > int64(remaining) {
			return nil, failure("INPUT_BUDGET_EXCEEDED", "retained snapshot text exceeds 128 MiB")
		}
		f, e := os.Open(file)
		if e != nil {
			return nil, e
		}
		opened, e := f.Stat()
		if e != nil || !opened.Mode().IsRegular() {
			f.Close()
			return nil, fmt.Errorf("input is not a readable regular file: %s", file)
		}
		b, e := io.ReadAll(io.LimitReader(f, int64(min(remaining, MaxFileText))+1))
		f.Close()
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		if e != nil {
			return nil, e
		}
		if len(b) > remaining {
			return nil, failure("INPUT_BUDGET_EXCEEDED", "retained snapshot text exceeds 128 MiB")
		}
		if len(b) > MaxFileText {
			return nil, fmt.Errorf("file exceeds input budget: %s", file)
		}
		after, e := os.Stat(file)
		if e == nil && before.Size() == after.Size() && before.ModTime() == after.ModTime() && len(b) == int(after.Size()) {
			return b, nil
		}
	}
	return nil, fmt.Errorf("file kept changing: %s", file)
}
func helmTemplate(root, file string) bool {
	for d := filepath.Dir(file); inside(root, d); d = filepath.Dir(d) {
		if filepath.Base(d) == "templates" {
			if _, e := os.Stat(filepath.Join(filepath.Dir(d), "Chart.yaml")); e == nil {
				return true
			}
		}
		if d == root {
			break
		}
	}
	return false
}

// TargetForFile assigns an already canonical local buffer to its root scope.
func TargetForFile(input Input, file string) (Target, error) {
	if !inside(input.Root, file) {
		return Target{}, failure("INPUT_INCOMPLETE", "buffer escapes root")
	}
	rel, _ := filepath.Rel(input.Root, file)
	rel = filepath.ToSlash(rel)
	owner := "root"
	if len(input.Config.Scopes) > 0 {
		owner = "unassigned"
	}
	count := 0
	for _, s := range input.Config.Scopes {
		for _, g := range s.Include {
			if match(g, rel) {
				owner = s.Name
				count++
				break
			}
		}
	}
	if count > 1 {
		return Target{}, failure("SCOPE_OVERLAP", "buffer belongs to multiple scopes")
	}
	for _, t := range input.Targets {
		if t.Scope == owner {
			return t, nil
		}
	}
	return Target{}, failure("SCOPE_UNKNOWN", "buffer scope is unavailable")
}

// Config input is restricted to bounded regular files. Open buffers bypass this
// disk path. This does not promise interruption of arbitrary filesystem I/O.
func readConfig(ctx context.Context, file string) ([]byte, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	info, err := os.Stat(file)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, failure("CONFIG_INVALID", "configuration must be a regular file")
	}
	if info.Size() > 1024*1024 {
		return nil, failure("CONFIG_INVALID", "configuration exceeds 1 MiB")
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	f, err := os.Open(file)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	info, err = f.Stat()
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, failure("CONFIG_INVALID", "configuration must be a regular file")
	}
	if info.Size() > 1024*1024 {
		return nil, failure("CONFIG_INVALID", "configuration exceeds 1 MiB")
	}
	raw, err := io.ReadAll(io.LimitReader(f, 1024*1024+1))
	if e := ctx.Err(); e != nil {
		return nil, e
	}
	if err != nil {
		return nil, err
	}
	if len(raw) > 1024*1024 {
		return nil, failure("CONFIG_INVALID", "configuration exceeds 1 MiB")
	}
	return raw, nil
}
