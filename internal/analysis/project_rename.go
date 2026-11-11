package analysis

import (
	"context"
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strings"

	"github.com/lazysnail1024/k8s-crd-toolkit/edit"
	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/k8s-crd-toolkit/yamlview"
	"sigs.k8s.io/yaml"
)

// ProjectRenameTarget identifies the binding and files that must be acquired as
// current editable snapshots before RenameProject. Prepare may inspect detached
// summaries; it does not establish source-version proof for those files.
type ProjectRenameTarget struct {
	Range       Range    `json:"range"`
	Placeholder string   `json:"placeholder"`
	URIs        []string `json:"-"`
}

type DocumentPlan struct {
	Document Document
	Plan     edit.Plan
}

type projectRenameSite struct {
	document    Document
	path        []string
	name        string
	declaration bool
	span        source.Span
	exact       bool
}

func projectName(value any) (string, error) {
	name, ok := value.(string)
	if !ok || name == "" || strings.ContainsAny(name, "${}") {
		return "", fmt.Errorf("an unknown or dynamic Project binding prevents a complete rename")
	}
	return name, nil
}

func projectSite(doc Document, path []string, name string, declaration bool) projectRenameSite {
	site := projectRenameSite{document: doc, path: path, name: name, declaration: declaration}
	if compact, ok := doc.compactLocation(path); ok {
		site.span, site.exact = compact.span, compact.kind == "exact"
		return site
	}
	node := doc.location(path)
	site.span = node.Span()
	var decoded string
	site.exact = doc.exact(path) && node.Kind() == yamlview.Scalar && !node.Damaged() &&
		!strings.ContainsAny(node.Raw(), "\r\n") &&
		yaml.UnmarshalStrict([]byte(node.Raw()), &decoded) == nil && decoded == name && literal(valueAt(doc.Value, path)) == name
	return site
}

// ApplicationSet's existing projection owns generator precedence and protected
// templatePatch project semantics. An override is editable only when its source
// maps back to one independent scalar in the original generator template.
func appSetProjectSite(doc, variant Document, name string) projectRenameSite {
	path := []string{"spec", "template", "spec", "project"}
	if doc.compact != nil {
		location, _ := variant.compactLocation([]string{"spec", "project"})
		return projectRenameSite{document: doc, name: name, span: location.span, exact: location.kind == "exact"}
	}
	if override, exists := variant.OverrideOrigins["spec/project"]; exists {
		matches := [][]string{}
		for i, raw := range objectSlice(object(doc.Value["spec"])["generators"]) {
			for kind, body := range object(raw) {
				if kind == "selector" || len(object(object(body)["template"])) == 0 {
					continue
				}
				prefix := []string{"spec", "generators", fmt.Sprint(i), kind, "template"}
				if locate(doc.Root, prefix).Span() == override.Span() {
					matches = append(matches, appendPath(prefix, "spec", "project"))
				}
			}
		}
		if len(matches) != 1 {
			return projectRenameSite{document: doc, name: name}
		}
		path = matches[0]
	}
	return projectSite(doc, path, name, false)
}

func projectRenameSites(ctx context.Context, documents []Document, options DomainOptions) ([]projectRenameSite, error) {
	if !options.Complete || options.ReferencePolicy != "closed" || options.ControlPlaneNamespace == "" {
		return nil, fmt.Errorf("Project rename requires a complete closed scope and an explicit controlPlaneNamespace")
	}
	sites := []projectRenameSite{}
	for _, doc := range documents {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if doc.Kind == "AppProject" {
			meta := object(doc.Value["metadata"])
			namespace := literal(meta["namespace"])
			if strings.ContainsAny(namespace, "${}") {
				return nil, fmt.Errorf("an unknown Project namespace prevents a complete rename")
			}
			if namespace != options.ControlPlaneNamespace {
				continue
			}
			name, err := projectName(meta["name"])
			if err != nil {
				return nil, err
			}
			sites = append(sites, projectSite(doc, []string{"metadata", "name"}, name, true))
			continue
		}
		variants := []Document{doc}
		if doc.Kind == "ApplicationSet" {
			var diagnostics []Diagnostic
			variants, diagnostics = appSetVariants(doc)
			for _, diagnostic := range diagnostics {
				if diagnostic.Severity == 1 {
					return nil, fmt.Errorf("ApplicationSet projection errors prevent a complete Project rename")
				}
			}
		}
		for _, variant := range variants {
			if err := ctx.Err(); err != nil {
				return nil, err
			}
			if variant.Kind != "Application" {
				continue
			}
			spec, objectSpec := variant.Value["spec"].(map[string]any)
			if !objectSpec {
				if doc.Kind == "ApplicationSet" {
					return nil, fmt.Errorf("an unknown ApplicationSet template prevents a complete Project rename")
				}
				if _, present := variant.Value["spec"]; present {
					return nil, fmt.Errorf("an unknown Application spec prevents a complete Project rename")
				}
				continue
			}
			value, present := spec["project"]
			if !present && !variant.UnknownPaths["spec/project"] {
				continue
			}
			name, err := projectName(value)
			if err != nil {
				return nil, err
			}
			if variant.UnknownPaths["spec/project"] {
				return nil, fmt.Errorf("an unknown Project field prevents a complete rename")
			}
			site := projectSite(doc, []string{"spec", "project"}, name, false)
			if doc.Kind == "ApplicationSet" {
				site = appSetProjectSite(doc, variant, name)
			}
			sites = append(sites, site)
		}
	}
	return sites, nil
}

func projectRenameSelection(ctx context.Context, documents []Document, options DomainOptions, uri string, pos Position, requireFull bool) (*ProjectRenameTarget, []projectRenameSite, error) {
	sites, err := projectRenameSites(ctx, documents, options)
	if err != nil {
		return nil, nil, err
	}
	var selected *projectRenameSite
	for i := range sites {
		site := &sites[i]
		if string(site.document.Snapshot.URI()) != uri || site.document.compact != nil || !site.exact {
			continue
		}
		offset, err := site.document.Snapshot.Offset(source.Position{Line: pos.Line, UTF16Column: pos.Character})
		if err != nil {
			return nil, nil, err
		}
		if offset >= site.span.Start && offset <= site.span.End {
			if selected != nil && selected.name != site.name {
				return nil, nil, fmt.Errorf("Project selection is ambiguous")
			}
			selected = site
		}
	}
	if selected == nil {
		return nil, nil, fmt.Errorf("position is not an independently editable Project binding")
	}
	if selected.name == "default" {
		return nil, nil, fmt.Errorf("the built-in default Project has implicit bindings and cannot be renamed")
	}
	declarations := 0
	uris := map[string]bool{}
	for _, site := range sites {
		if site.name != selected.name {
			continue
		}
		if site.declaration {
			declarations++
		}
		if site.document.compact != nil {
			if requireFull {
				return nil, nil, fmt.Errorf("every affected Project document must be reacquired before rename")
			}
		} else if !site.exact {
			return nil, nil, fmt.Errorf("a Project binding has a shared, derived or unmapped source")
		}
		uris[string(site.document.Snapshot.URI())] = true
	}
	if declarations != 1 {
		return nil, nil, fmt.Errorf("Project declaration is missing or ambiguous in the control-plane namespace")
	}
	span := selected.span
	raw, _ := selected.document.Snapshot.Slice(span)
	if len(raw) >= 2 && (raw[0] == '\'' || raw[0] == '"') && raw[len(raw)-1] == raw[0] && raw[1:len(raw)-1] == selected.name {
		span.Start++
		span.End--
	}
	a, err := selected.document.Snapshot.Position(span.Start)
	if err != nil {
		return nil, nil, err
	}
	b, err := selected.document.Snapshot.Position(span.End)
	if err != nil {
		return nil, nil, err
	}
	target := &ProjectRenameTarget{Range: Range{Position{a.Line, a.UTF16Column}, Position{b.Line, b.UTF16Column}}, Placeholder: selected.name}
	for uri := range uris {
		target.URIs = append(target.URIs, uri)
	}
	sort.Strings(target.URIs)
	return target, sites, nil
}

func PrepareProjectRename(ctx context.Context, documents []Document, options DomainOptions, uri string, pos Position) (*ProjectRenameTarget, error) {
	target, _, err := projectRenameSelection(ctx, documents, options, uri, pos, false)
	return target, err
}

var projectResourceName = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$`)

func RenameProject(ctx context.Context, documents []Document, options DomainOptions, uri string, pos Position, newName string) ([]DocumentPlan, error) {
	target, sites, err := projectRenameSelection(ctx, documents, options, uri, pos, true)
	if err != nil {
		return nil, err
	}
	if newName == "default" {
		return nil, fmt.Errorf("the built-in default Project name has implicit bindings")
	}
	if len(newName) > 253 || !projectResourceName.MatchString(newName) {
		return nil, fmt.Errorf("Project name must be a DNS subdomain of at most 253 characters")
	}
	if newName == target.Placeholder {
		return []DocumentPlan{}, nil
	}
	for _, site := range sites {
		if site.name == newName {
			return nil, fmt.Errorf("new Project name is already declared or used by another binding")
		}
	}
	encoded, _ := json.Marshal(newName)
	byURI := map[source.URI]*DocumentPlan{}
	seen := map[string]bool{}
	for _, site := range sites {
		if site.name != target.Placeholder {
			continue
		}
		snapshot := site.document.Snapshot
		key := fmt.Sprintf("%s\x00%d:%d", snapshot.URI(), site.span.Start, site.span.End)
		if seen[key] {
			continue
		}
		seen[key] = true
		plan := byURI[snapshot.URI()]
		if plan == nil {
			plan = &DocumentPlan{Document: site.document, Plan: edit.Plan{BaseRevision: snapshot.Revision()}}
			byURI[snapshot.URI()] = plan
		}
		if plan.Document.Target != site.document.Target || plan.Document.Snapshot.Revision() != snapshot.Revision() || plan.Document.Snapshot.Text() != snapshot.Text() {
			return nil, fmt.Errorf("affected document has inconsistent snapshot or target identity")
		}
		expected, err := snapshot.Slice(site.span)
		if err != nil {
			return nil, err
		}
		plan.Plan.Replacements = append(plan.Plan.Replacements, edit.Replacement{Span: site.span, ExpectedText: expected, NewText: string(encoded)})
	}
	plans := []DocumentPlan{}
	for _, uri := range target.URIs {
		plan := byURI[source.URI(uri)]
		if plan == nil {
			return nil, fmt.Errorf("affected Project document has no proved replacement")
		}
		sort.Slice(plan.Plan.Replacements, func(i, j int) bool {
			return plan.Plan.Replacements[i].Span.Start < plan.Plan.Replacements[j].Span.Start
		})
		plans = append(plans, *plan)
	}
	if err := safeProjectRename(ctx, documents, options, plans); err != nil {
		return nil, err
	}
	return plans, nil
}

// All replacements are applied before any after-state domain check. Unchanged
// detached documents remain sufficient domain inputs; only changed files need
// fresh schema analysis. Intermediate broken bindings are never evaluated.
func safeProjectRename(ctx context.Context, documents []Document, options DomainOptions, plans []DocumentPlan) error {
	next := map[source.URI]source.Snapshot{}
	for _, plan := range plans {
		snapshot, err := edit.Apply(plan.Document.Snapshot, plan.Document.Snapshot.Revision()+1, plan.Plan)
		if err != nil {
			return err
		}
		next[snapshot.URI()] = snapshot
	}
	beforeAnalyzer, afterAnalyzer := new(Analyzer), new(Analyzer)
	beforeAnalyzer.ReuseSchemas(documents)
	afterAnalyzer.ReuseSchemas(documents)
	beforeDiagnostics, afterDiagnostics := []Diagnostic{}, []Diagnostic{}
	afterDocuments := []Document{}
	for _, doc := range documents {
		if _, changed := next[doc.Snapshot.URI()]; !changed {
			afterDocuments = append(afterDocuments, doc)
		}
	}
	for _, plan := range plans {
		before, err := beforeAnalyzer.Analyze(ctx, plan.Document.Snapshot, plan.Document.Target)
		if err != nil {
			return err
		}
		if before.Incomplete {
			return fmt.Errorf("Project rename input analysis is incomplete")
		}
		after, err := afterAnalyzer.Analyze(ctx, next[plan.Document.Snapshot.URI()], plan.Document.Target)
		if err != nil {
			return err
		}
		if after.Incomplete {
			return fmt.Errorf("Project rename result analysis is incomplete")
		}
		beforeDiagnostics = append(beforeDiagnostics, before.Diagnostics...)
		afterDiagnostics = append(afterDiagnostics, after.Diagnostics...)
		afterDocuments = append(afterDocuments, after.Documents...)
	}
	options.Context = ctx
	beforeDiagnostics = append(beforeDiagnostics, Domain(documents, options)...)
	afterDiagnostics = append(afterDiagnostics, Domain(afterDocuments, options)...)
	counts := map[string]int{}
	for _, d := range beforeDiagnostics {
		if d.Severity == 1 {
			counts[d.URI+"\n"+d.Code+"\n"+d.Message]++
		}
	}
	for _, d := range afterDiagnostics {
		if d.Severity != 1 {
			continue
		}
		key := d.URI + "\n" + d.Code + "\n" + d.Message
		counts[key]--
		if counts[key] < 0 {
			return fmt.Errorf("Project rename introduces a schema or scope error")
		}
	}
	return ctx.Err()
}
