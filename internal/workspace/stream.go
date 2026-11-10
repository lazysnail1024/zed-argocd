package workspace

import (
	"context"
	"path"
	"path/filepath"
	"strings"

	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/zed-argocd/internal/analysis"
)

// Catalog is immutable configuration and file policy. It contains no acquired
// resource text. The LSP owns discovery and individual file lifetimes; the CLI
// continues to use Load's one-shot acquisition and aggregate input budget.
type Catalog struct {
	Input Input
	roots []string
}

func Prepare(ctx context.Context, root, override string, config *string, roots []string) (*Catalog, error) {
	remaining := MaxRetainedText
	input, err := LoadOpenBuffers(ctx, root, override, config, nil, roots, &remaining)
	if err != nil {
		return nil, err
	}
	c := &Catalog{Input: input, roots: append([]string(nil), roots...)}
	// Configuration validity is independent of resource discovery. In particular,
	// open-document analysis alone does not validate repository mapping targets.
	if _, err := c.LocalPaths(ctx, nil); err != nil {
		return nil, err
	}
	return c, nil
}

// Select applies the same scope/exclusion/ownership rules as Load. Open files
// excluded from the workspace retain their isolated document-only scope.
func (c *Catalog) Select(file string, opened bool) (Target, bool, error) {
	selection, included, err := selectFile(c.Input.Root, file, c.Input.Config, c.roots, opened, false)
	if err != nil || !included {
		return Target{}, included, err
	}
	for _, target := range c.Input.Targets {
		if target.Scope == selection.scope {
			if selection.isolated {
				target.Scope = "isolated:" + file
				target.ID += ":" + target.Scope
			}
			return target, true, nil
		}
	}
	return Target{}, false, failure("SCOPE_UNKNOWN", "file scope is unavailable")
}

type fileSelection struct {
	scope    string
	isolated bool
}

// Both one-shot and streaming acquisition use this policy after canonicalizing
// the path. Explicit CLI files may be excluded; open excluded files are isolated.
func selectFile(root, file string, cfg Config, roots []string, opened, selected bool) (fileSelection, bool, error) {
	if !inside(root, file) || (len(roots) > 0 && Owner(roots, file) != root) {
		return fileSelection{}, false, nil
	}
	rel, err := filepath.Rel(root, file)
	if err != nil {
		return fileSelection{}, false, err
	}
	rel = filepath.ToSlash(rel)
	owner := "root"
	if len(cfg.Scopes) != 0 {
		owner = "unassigned"
	}
	matches := 0
	for _, scope := range cfg.Scopes {
		for _, glob := range scope.Include {
			if match(glob, rel) {
				owner = scope.Name
				matches++
				break
			}
		}
	}
	if matches > 1 {
		return fileSelection{}, false, failure("SCOPE_OVERLAP", "file belongs to multiple scopes: "+rel)
	}
	excluded := false
	for _, glob := range cfg.Exclude {
		excluded = excluded || match(glob, rel)
	}
	for _, part := range strings.Split(rel, "/") {
		excluded = excluded || excludedDir(part)
	}
	return fileSelection{scope: owner, isolated: excluded && opened}, !excluded || opened || selected, nil
}

// ScanExclusions only turns rules applying to that basename everywhere into
// scanner pruning. Other globs remain exact file-policy filters in Select;
// a partial match must never hide an included subtree.
func (c *Catalog) ScanExclusions() []string {
	result := []string{".git", "node_modules", "vendor", ".argo-ls"}
	for _, glob := range c.Input.Config.Exclude {
		if strings.HasPrefix(glob, "**/") && strings.HasSuffix(glob, "/**") {
			name := strings.TrimSuffix(strings.TrimPrefix(glob, "**/"), "/**")
			if name != "" && !strings.ContainsAny(name, "/\\*?[]{}") {
				result = append(result, name)
			}
		}
	}
	return result
}

// ReadFile checks the single-file bound before reading. Disk text is admitted
// through Documents.PublishRead by the LSP, rather than kept in Catalog.
func ReadFile(ctx context.Context, file string) (string, error) {
	text, err := readStable(ctx, file, MaxFileText)
	return string(text), err
}

// LocalPaths samples only concrete repository dependencies. A non-YAML watcher
// event can request this again without reacquiring unrelated YAML documents.
func (c *Catalog) LocalPaths(ctx context.Context, refs []analysis.RepositoryReference) (map[string]map[string]bool, error) {
	result := map[string]map[string]bool{}
	for _, repo := range c.Input.Config.Repositories {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		base, err := filepath.EvalSymlinks(filepath.Join(c.Input.Root, repo.Path))
		if err != nil {
			return nil, failure("CONFIG_INVALID", "repository mapping path is unavailable: "+repo.Path)
		}
		if !inside(c.Input.Root, base) {
			return nil, failure("CONFIG_INVALID", "repository mapping escapes root through symlink")
		}
		key := repo.RepoURL + "\x00" + repo.TargetRevision
		if _, exists := result[key]; exists {
			return nil, failure("CONFIG_INVALID", "repository mapping must be unique for URL and revision")
		}
		entries := map[string]bool{}
		result[key] = entries
		for _, ref := range refs {
			if err := ctx.Err(); err != nil {
				return nil, err
			}
			if ref.RepoURL != repo.RepoURL || ref.TargetRevision != repo.TargetRevision {
				continue
			}
			clean := path.Clean(ref.Path)
			if _, exists := entries[clean]; exists {
				continue
			}
			if !filepath.IsAbs(ref.Path) && inside(base, filepath.Join(base, ref.Path)) {
				resolved, err := filepath.EvalSymlinks(filepath.Join(base, ref.Path))
				entries[clean] = err == nil && inside(base, resolved)
			}
		}
	}
	return result, nil
}

// A stable canonical acquisition path and the client's URI spelling are kept
// separately by the caller, just as in File.
func (c *Catalog) File(snapshot source.Snapshot, canonical string, opened bool) (File, bool, bool, error) {
	target, included, err := c.Select(canonical, opened)
	if err != nil || !included {
		return File{}, included, false, err
	}
	file := File{Snapshot: snapshot, Target: target, Output: true, Path: canonical}
	return file, true, IsHelmTemplate(c.Input.Root, canonical, snapshot), nil
}
