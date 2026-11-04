package analysis

import (
	"context"
	"fmt"
	"github.com/lazysnail1024/zed-argocd/internal/assets"
	"io"
	"net/url"
	"path"
	"regexp"
	"strings"
)

type DomainOptions struct {
	Trace                 io.Writer
	Context               context.Context
	LocalPaths            map[string]map[string]bool
	ControlPlaneNamespace string
	ReferencePolicy       string
	Complete              bool
}

// Domain checks only values whose interpretation is static in a complete scope.
// Project policy intentionally supports exact allow entries and a whole '*'.
func Domain(documents []Document, options DomainOptions) []Diagnostic {
	diagnostics := []Diagnostic{}
	ctx := options.Context
	if ctx == nil {
		ctx = context.Background()
	}
	projects := map[string][]Document{}
	projectDocuments := []Document{}
	for _, doc := range documents {
		if ctx.Err() != nil {
			return diagnostics
		}
		if doc.Kind != "AppProject" {
			continue
		}
		projectDocuments = append(projectDocuments, doc)
		meta := object(doc.Value["metadata"])
		if options.ControlPlaneNamespace == "" || literal(meta["namespace"]) == options.ControlPlaneNamespace {
			name := literal(meta["name"])
			projects[name] = append(projects[name], doc)
		}
	}
	for _, doc := range documents {
		if ctx.Err() != nil {
			return diagnostics
		}
		if doc.Kind == "ApplicationSet" {
			variants, templateDiagnostics := appSetVariants(doc)
			diagnostics = append(diagnostics, templateDiagnostics...)
			related := append([]Document{}, variants...)
			related = append(related, projectDocuments...)
			for _, d := range Domain(related, options) {
				d.Message = d.Code + ": " + d.Message
				d.Code = "CD_APPSET_STATIC_FIELD"
				diagnostics = append(diagnostics, d)
			}
			continue
		}
		if doc.Kind != "Application" {
			continue
		}
		spec := object(doc.Value["spec"])
		emit := func(parts []string, code string, severity int, message string) {
			diagnostics = append(diagnostics, diagnostic(doc, parts, code, severity, message))
		}
		project := literal(spec["project"])
		if project != "" {
			candidates := projects[project]
			if len(candidates) > 1 || (len(candidates) > 0 && options.ControlPlaneNamespace == "") {
				emit([]string{"spec", "project"}, "CD_PROJECT_AMBIGUOUS", 2, "Project navigation and references require an unambiguous control-plane namespace. Set controlPlaneNamespace in .argocd-ls.yaml (for example, controlPlaneNamespace: argocd); use the namespace containing AppProject, not the Application namespace")
			} else if len(candidates) == 0 {
				severity := 3
				if options.ReferencePolicy == "closed" && options.Complete {
					severity = 1
				}
				emit([]string{"spec", "project"}, "CD_PROJECT_UNRESOLVED", severity, "Project is not resolved in this local control-plane scope")
			} else {
				diagnostics = append(diagnostics, projectPolicy(doc, candidates[0])...)
			}
		}
		dest := object(spec["destination"])
		if literal(dest["name"]) != "" && literal(dest["server"]) != "" {
			emit([]string{"spec", "destination"}, "CD_DESTINATION_CONFLICT", 1, "destination.name and destination.server cannot both identify the target")
		}
		sources, hasSources := spec["sources"].([]any)
		if !hasSources || len(sources) == 0 {
			if source, ok := spec["source"].(map[string]any); ok {
				sources = []any{source}
			}
		} else if _, ok := spec["source"]; ok {
			emit([]string{"spec", "source"}, "CD_SOURCE_IGNORED", 2, "source is ignored when nonempty sources is supplied")
		}
		refs := map[string]int{}
		unknownRef := false
		for p := range doc.UnknownPaths {
			if strings.HasPrefix(p, "spec/sources/") && strings.HasSuffix(p, "/ref") {
				unknownRef = true
			}
		}
		for _, value := range sources {
			ref := literal(object(value)["ref"])
			if ref != "" {
				refs[ref]++
			}
		}
		for i, value := range sources {
			if ctx.Err() != nil {
				return diagnostics
			}
			src := object(value)
			parts := []string{"spec", "source"}
			if hasSources && len(objectSlice(spec["sources"])) > 0 {
				parts = []string{"spec", "sources", fmt.Sprint(i)}
			}

			if localPath := literal(src["path"]); localPath != "" && options.Complete {
				cleaned := path.Clean(localPath)
				if cleaned != ".." && !strings.HasPrefix(cleaned, "../") && !strings.HasPrefix(cleaned, "/") {
					if known, ok := options.LocalPaths[literal(src["repoURL"])+"\x00"+literal(src["targetRevision"])]; ok && !known[cleaned] {
						emit(appendPath(parts, "path"), "CD_LOCAL_PATH_MISSING", 2, "Path is absent from the explicitly mapped local repository")
					}
				}
			}
			ref := literal(src["ref"])
			if refs[ref] > 1 && ref != "" {
				emit(appendPath(parts, "ref"), "CD_SOURCE_REF_DUPLICATE", 1, "Source ref is duplicated within this Application")
			}
			if sourceRefsActive(doc.Target, len(objectSlice(spec["sources"]))) && ref != "" && (literal(src["chart"]) != "" || !sourceRefName.MatchString(ref)) {
				emit(appendPath(parts, "ref"), "CD_VALUES_REF_INVALID", 1, "Source ref cannot contain a path or be combined with chart")
			}
			count := 0
			for _, name := range []string{"helm", "kustomize", "directory", "plugin"} {
				if v, exists := src[name]; exists && v != nil {
					count++
				}
			}
			if count > 1 {
				emit(parts, "CD_SOURCE_OPTIONS_CONFLICT", 1, "Only one source generation option may be selected")
			}
			helm := object(src["helm"])
			for _, valueFile := range helmValuePaths(helm) {
				v := valueFile.value
				file := literal(v)
				if file == "" {
					continue
				}
				field := appendPath(parts, append([]string{"helm"}, valueFile.path...)...)
				if hasSources && len(objectSlice(spec["sources"])) > 0 && strings.HasPrefix(file, "$") {
					key := strings.SplitN(file[1:], "/", 2)[0]
					if len(sources) == 1 && refs[key] > 0 && !assets.SingleSourceRefs(doc.Target) {
						emit(field, "CD_VERSION_FEATURE", 1, "This target cannot resolve a ref from a sole sources entry; support starts in Argo CD 3.5")
					} else if refs[key] == 0 && !unknownRef {
						emit(field, "CD_VALUES_REF_MISSING", 1, "Values path references an undeclared source ref")
					}
				}
			}
		}
	}
	return diagnostics
}
func projectPolicy(app, project Document) []Diagnostic {
	policy := object(project.Value["spec"])
	spec := object(app.Value["spec"])
	sources := objectSlice(spec["sources"])
	if len(sources) == 0 {
		sources = []any{spec["source"]}
	}
	out := []Diagnostic{}
	allowed := objectSlice(policy["sourceRepos"])
	for _, v := range sources {
		repo := literal(object(v)["repoURL"])
		if repo == "" {
			continue
		}
		match, known := false, true
		for _, a := range allowed {
			s := literal(a)
			if s == "*" || normalizedRepository(s) == normalizedRepository(repo) {
				match = true
			}
			if s == "" || strings.HasPrefix(s, "!") || (s != "*" && strings.ContainsAny(s, "*?[]{}\\")) {
				known = false
			}
		}
		if known && !match {
			out = append(out, diagnostic(app, []string{"spec"}, "CD_PROJECT_POLICY_CONTRADICTION", 2, "Static repository URL is absent from the local Project allow list"))
			break
		}
	}

	destination := object(spec["destination"])
	server, namespace := literal(destination["server"]), literal(destination["namespace"])
	if server != "" && namespace != "" {
		allowed, known := false, true
		for _, entry := range objectSlice(policy["destinations"]) {
			item := object(entry)
			s, n := literal(item["server"]), literal(item["namespace"])
			if literal(item["name"]) != "" || s == "" || n == "" || (s != "*" && strings.ContainsAny(s, "*?[]{}!\\")) || (n != "*" && strings.ContainsAny(n, "*?[]{}!\\")) {
				known = false
			}
			if (s == "*" || s == server) && (n == "*" || n == namespace) {
				allowed = true
			}
		}
		if known && !allowed {
			out = append(out, diagnostic(app, []string{"spec", "destination"}, "CD_PROJECT_POLICY_CONTRADICTION", 2, "Static destination is absent from the local Project allow list"))
		}
	}
	return out
}
func diagnostic(doc Document, parts []string, code string, severity int, message string) Diagnostic {
	node := doc.location(parts)
	a, _ := doc.Snapshot.Position(node.Span().Start)
	b, _ := doc.Snapshot.Position(node.Span().End)
	return Diagnostic{URI: string(doc.Snapshot.URI()), Range: Range{Position{a.Line, a.UTF16Column}, Position{b.Line, b.UTF16Column}}, Code: code, Source: "argocd-ls", Severity: severity, Message: doc.locationMessage(parts, message)}
}
func object(value any) map[string]any { m, _ := value.(map[string]any); return m }
func objectSlice(value any) []any     { s, _ := value.([]any); return s }
func literal(value any) string        { s, _ := value.(string); return s }

func appendPath(path []string, parts ...string) []string {
	return append(append([]string{}, path...), parts...)
}

// These literal paths participate in upstream source-ref resolution. Environment
// substitution outside a leading multi-source ref is not evaluated locally.
type helmValuePath struct {
	value any
	path  []string
}

func helmValuePaths(helm map[string]any) []helmValuePath {
	out := []helmValuePath{}
	for i, value := range objectSlice(helm["valueFiles"]) {
		out = append(out, helmValuePath{value, []string{"valueFiles", fmt.Sprint(i)}})
	}
	for i, value := range objectSlice(helm["fileParameters"]) {
		out = append(out, helmValuePath{object(value)["path"], []string{"fileParameters", fmt.Sprint(i), "path"}})
	}
	return out
}

var sourceRefName = regexp.MustCompile(`^[a-zA-Z0-9_-]+$`)
var sshRepository = regexp.MustCompile(`^(ssh://)?([^/:]*?)@[^@]+$`)

// Exact policy entries use the same URL normalization as all pinned upstream
// versions. Complex policy patterns remain unknown and are never normalized as
// evidence that a repository is denied.
func normalizedRepository(repo string) string {
	repo = strings.ToLower(strings.TrimSpace(repo))
	if sshRepository.MatchString(repo) {
		if !strings.HasPrefix(repo, "ssh://") {
			repo = "ssh://" + strings.Replace(repo, ":", "/", 1)
		}
	}
	repo = strings.TrimSuffix(repo, ".git")
	parsed, err := url.Parse(repo)
	if err != nil {
		return ""
	}
	return strings.TrimPrefix(parsed.String(), "ssh://")
}

// Ref declarations are interpreted only by the active sources representation.
// Upstream GetRefSources ignored a sole entry until the pinned 3.5 boundary.
func sourceRefsActive(target string, count int) bool {
	return count > 1 || (count == 1 && assets.SingleSourceRefs(target))
}
