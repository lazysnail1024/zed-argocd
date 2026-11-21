package workspace

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

type RenderProfile struct {
	Chart       string   `json:"chart"`
	Values      []string `json:"values,omitempty"`
	ReleaseName string   `json:"releaseName,omitempty"`
	Namespace   string   `json:"namespace,omitempty"`
	KubeVersion string   `json:"kubeVersion,omitempty"`
	APIVersions []string `json:"apiVersions,omitempty"`
}

type RenderFileIdentity struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type RenderSourceIdentity struct {
	URI      string `json:"uri"`
	Version  int    `json:"version"`
	Revision uint64 `json:"revision"`
	SHA256   string `json:"sha256"`
}

type RenderOptions struct {
	Root, Scope, File, Chart, Profile, Target string
	ReleaseName, Namespace, KubeVersion       string
	Values, APIVersions, Roots                []string
	Config                                    *string
}

// RenderRequest is a product-local, immutable preflight shared by CLI and LSP.
// It contains resolved arguments and input identities, never arbitrary commands
// or a rendering process. Ordered ValuesFiles deliberately retain duplicates.
type RenderRequest struct {
	Roots       []string              `json:"roots"`
	Root        string                `json:"root"`
	File        string                `json:"file"`
	Chart       string                `json:"chart"`
	Profile     string                `json:"profile"`
	Target      Target                `json:"target"`
	ReleaseName string                `json:"releaseName"`
	Namespace   string                `json:"namespace"`
	KubeVersion string                `json:"kubeVersion"`
	APIVersions []string              `json:"apiVersions"`
	ValuesFiles []RenderFileIdentity  `json:"valuesFiles"`
	Inputs      []RenderFileIdentity  `json:"inputs"`
	Links       map[string]string     `json:"links"`
	Fingerprint string                `json:"fingerprint"`
	Source      *RenderSourceIdentity `json:"source,omitempty"`
	Options     RenderOptions         `json:"-"`
}

var profileName = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$`)

func rootRelativeRenderPath(path string) bool {
	return path != "" && !filepath.IsAbs(path) && filepath.VolumeName(path) == "" && !strings.Contains(path, "\\") && filepath.Clean(path) != ".." && !strings.HasPrefix(filepath.Clean(path), ".."+string(filepath.Separator))
}

func validateRenderProfiles(profiles map[string]RenderProfile) error {
	for name, profile := range profiles {
		if !profileName.MatchString(name) {
			return fmt.Errorf("render profile name must contain only letters, digits, dot, dash or underscore")
		}
		if !rootRelativeRenderPath(profile.Chart) {
			return fmt.Errorf("profile %s chart must be a root-relative path without traversal", name)
		}
		for _, value := range profile.Values {
			if !rootRelativeRenderPath(value) {
				return fmt.Errorf("profile %s values must be root-relative paths without traversal", name)
			}
		}
		if err := validateRenderArguments(profile.ReleaseName, profile.Namespace, profile.KubeVersion, profile.APIVersions); err != nil {
			return fmt.Errorf("profile %s: %w", name, err)
		}
	}
	return nil
}

func validateRenderArguments(release, namespace, kube string, apis []string) error {
	if strings.HasPrefix(release, "-") || strings.ContainsAny(release, "\x00\r\n") {
		return fmt.Errorf("release name must be a literal Helm name, not an option")
	}
	if strings.HasPrefix(namespace, "-") || strings.ContainsAny(namespace, "\x00\r\n") {
		return fmt.Errorf("namespace must be a literal value, not an option")
	}
	for _, value := range append([]string{kube}, apis...) {
		if strings.HasPrefix(value, "-") || strings.ContainsAny(value, "\x00\r\n") {
			return fmt.Errorf("capability arguments must be literal values, not options")
		}
	}
	return nil
}

func ResolveRenderRequest(ctx context.Context, options RenderOptions) (RenderRequest, error) {
	request := RenderRequest{Profile: options.Profile, APIVersions: []string{}, ValuesFiles: []RenderFileIdentity{}, Inputs: []RenderFileIdentity{}, Links: map[string]string{}}
	catalog, err := Prepare(ctx, options.Root, options.Target, options.Config, options.Roots)
	if err != nil {
		return request, err
	}
	request.Root = catalog.Input.Root
	request.Roots = append([]string{}, options.Roots...)
	sort.Strings(request.Roots)
	resolve := func(path string) (string, error) {
		if !filepath.IsAbs(path) {
			path = filepath.Join(request.Root, path)
		}
		resolved, err := filepath.EvalSymlinks(path)
		if err != nil {
			return "", err
		}
		resolved, err = filepath.Abs(resolved)
		if err != nil || !inside(request.Root, resolved) || len(options.Roots) > 0 && Owner(options.Roots, resolved) != request.Root {
			return "", fmt.Errorf("render inputs must stay inside source root")
		}
		if info, err := os.Lstat(path); err == nil && info.Mode()&os.ModeSymlink != 0 {
			request.Links[path] = resolved
		}
		return resolved, ctx.Err()
	}
	if options.File != "" {
		request.File, err = resolve(options.File)
		if err != nil {
			return request, err
		}
	}
	profile := RenderProfile{Chart: options.Chart, Values: options.Values, ReleaseName: options.ReleaseName, Namespace: options.Namespace, KubeVersion: options.KubeVersion, APIVersions: options.APIVersions}
	if options.Profile != "" {
		var exists bool
		profile, exists = catalog.Input.Config.RenderProfiles[options.Profile]
		if !exists {
			return request, fmt.Errorf("unknown render profile %q", options.Profile)
		}
	}
	if profile.Chart == "" {
		start := request.File
		if start == "" {
			start = request.Root
		}
		profile.Chart, err = FindChart(request.Root, start)
		if err != nil {
			return request, err
		}
	}
	request.Chart, err = resolve(profile.Chart)
	if err != nil {
		return request, err
	}
	if request.File != "" {
		nearest, err := FindChart(request.Root, request.File)
		if err != nil || nearest != request.Chart {
			return request, fmt.Errorf("render profile/chart must match the selected source's nearest owned chart")
		}
	}
	if _, err = ReadFile(ctx, filepath.Join(request.Chart, "Chart.yaml")); err != nil {
		return request, fmt.Errorf("chart metadata is unavailable: %w", err)
	}
	scope := options.Scope
	scopeFile := request.File
	if scopeFile == "" {
		scopeFile = filepath.Join(request.Chart, "Chart.yaml")
	}
	{
		selection, included, err := selectFile(request.Root, scopeFile, catalog.Input.Config, options.Roots, false, true)
		if err != nil || !included {
			return request, fmt.Errorf("selected render source has no owning scope")
		}
		if scope != "" && scope != selection.scope {
			return request, fmt.Errorf("FILE_SCOPE_MISMATCH: selected chart/source belongs to scope %s", selection.scope)
		}
		scope = selection.scope
	}
	for _, target := range catalog.Input.Targets {
		if target.Scope == scope || (scope == "" && len(catalog.Input.Targets) == 1) {
			request.Target = target
		}
	}
	if request.Target.Version == "" {
		return request, fmt.Errorf("select exactly one source scope with --file or --scope")
	}
	request.ReleaseName, request.Namespace, request.KubeVersion = profile.ReleaseName, profile.Namespace, profile.KubeVersion
	if request.ReleaseName == "" {
		request.ReleaseName = "argo-ls"
	}
	if request.Namespace == "" {
		request.Namespace = "default"
	}
	if request.KubeVersion == "" {
		request.KubeVersion = "1.31.0"
	}
	request.APIVersions = append(request.APIVersions, profile.APIVersions...)
	if err := validateRenderArguments(request.ReleaseName, request.Namespace, request.KubeVersion, request.APIVersions); err != nil {
		return request, err
	}
	identities := map[string]RenderFileIdentity{}
	bytes := 0
	acquire := func(path string) (RenderFileIdentity, error) {
		canonical, err := resolve(path)
		if err != nil {
			return RenderFileIdentity{}, err
		}
		if previous, ok := identities[canonical]; ok {
			return previous, nil
		}
		if len(identities) >= 10000 {
			return RenderFileIdentity{}, fmt.Errorf("render input exceeds 10000 files")
		}
		text, err := ReadFile(ctx, canonical)
		if err != nil {
			return RenderFileIdentity{}, err
		}
		bytes += len(text)
		if bytes > MaxRetainedText {
			return RenderFileIdentity{}, fmt.Errorf("render inputs exceed 128 MiB")
		}
		hash := sha256.Sum256([]byte(text))
		identity := RenderFileIdentity{canonical, hex.EncodeToString(hash[:])}
		identities[canonical] = identity
		return identity, nil
	}
	visited := map[string]bool{}
	var walk func(string) error
	walk = func(directory string) error {
		canonical, err := resolve(directory)
		if err != nil {
			return err
		}
		if visited[canonical] {
			return fmt.Errorf("cyclic or repeated chart directory link")
		}
		visited[canonical] = true
		return filepath.WalkDir(canonical, func(path string, entry fs.DirEntry, walkErr error) error {
			if err := ctx.Err(); err != nil {
				return err
			}
			if walkErr != nil {
				return walkErr
			}
			if entry.Type()&os.ModeSymlink != 0 {
				target, err := resolve(path)
				if err != nil {
					return err
				}
				info, err := os.Stat(target)
				if err != nil {
					return err
				}
				if info.IsDir() {
					return walk(target)
				}
			}
			if !entry.IsDir() {
				_, err := acquire(path)
				return err
			}
			return nil
		})
	}
	if err := walk(request.Chart); err != nil {
		return request, err
	}
	for _, value := range profile.Values {
		identity, err := acquire(value)
		if err != nil {
			return request, err
		}
		request.ValuesFiles = append(request.ValuesFiles, identity)
	}
	configuration := filepath.Join(request.Root, ".argocd-ls.yaml")
	if _, err := os.Lstat(configuration); err == nil {
		if _, err := acquire(configuration); err != nil {
			return request, err
		}
	} else if !os.IsNotExist(err) {
		return request, err
	}
	for _, identity := range identities {
		request.Inputs = append(request.Inputs, identity)
	}
	sort.Slice(request.Inputs, func(i, j int) bool { return request.Inputs[i].Path < request.Inputs[j].Path })
	encoded, err := json.Marshal(request)
	if err != nil {
		return request, err
	}
	hash := sha256.Sum256(encoded)
	request.Fingerprint = hex.EncodeToString(hash[:])
	request.Options = options
	request.Options.Root, request.Options.File, request.Options.Config = request.Root, request.File, nil
	return request, ctx.Err()
}

func (request RenderRequest) Revalidate(ctx context.Context) error {
	current, err := ResolveRenderRequest(ctx, request.Options)
	if err != nil {
		return fmt.Errorf("render inputs changed: %w", err)
	}
	if current.Fingerprint != request.Fingerprint {
		return fmt.Errorf("render inputs changed; request a fresh render action")
	}
	return nil
}
