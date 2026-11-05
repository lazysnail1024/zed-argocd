package analysis

import (
	"fmt"
	"github.com/lazysnail1024/k8s-crd-toolkit/yamlview"
	"regexp"
	"sigs.k8s.io/yaml"
	"strings"
	"text/template/parse"
)

// ApplicationSet templates are inspected, never executed. Dynamic strings are
// unknown only in the embedded Application projection; boolean types stay fixed.
func appSet(doc Document) (Document, []Diagnostic) {
	spec := object(doc.Value["spec"])
	template := object(spec["template"])
	diagnostics := []Diagnostic{}
	unknown := map[string]bool{}
	goTemplate, _ := spec["goTemplate"].(bool)
	var walk func(any, []string) any
	walk = func(value any, path []string) any {
		switch v := value.(type) {
		case string:
			if (!goTemplate && !fastTemplateMarker.MatchString(v)) || (goTemplate && !strings.Contains(v, "{{")) {
				return v
			}
			if len(path) > 2 && path[0] == "spec" && path[1] == "template" {
				unknown[strings.Join(path[2:], "/")] = true
			}
			diagnostics = append(diagnostics, diagnostic(doc, path, "CD_APPSET_DYNAMIC_FIELD", 3, "Template value is dynamic; dependent value checks are not evaluated"))
			var err error
			if goTemplate {
				tree := parse.New("field")
				tree.Mode = parse.SkipFuncCheck
				_, err = tree.Parse(v, "{{", "}}", map[string]*parse.Tree{})
			} else {
				remaining := v
				for {
					start := strings.Index(remaining, "{{")
					if start < 0 {
						break
					}
					end := strings.Index(remaining[start+2:], "}}")
					if end < 0 {
						err = fmt.Errorf("unclosed template tag")
						break
					}
					end += start + 2
					remaining = remaining[end+2:]
				}
			}
			if err != nil {
				diagnostics = append(diagnostics, diagnostic(doc, path, "CD_APPSET_TEMPLATE_SYNTAX", 1, "Invalid template syntax: "+err.Error()))
			}
			return nil
		case map[string]any:
			copy := map[string]any{}
			for k, x := range v {
				copy[k] = walk(x, appendPath(path, k))
			}
			return copy
		case []any:
			copy := make([]any, len(v))
			for i, x := range v {
				copy[i] = walk(x, appendPath(path, fmt.Sprint(i)))
			}
			return copy
		default:
			return v
		}
	}
	transformed := object(walk(template, []string{"spec", "template"}))
	origins := map[string]yamlview.Node{}
	if patch, ok := spec["templatePatch"].(string); ok {
		patchValue := walk(patch, []string{"spec", "templatePatch"})
		if _, static := patchValue.(string); static {
			var value any
			err := yaml.UnmarshalStrict([]byte(patch), &value)
			_, mapping := value.(map[string]any)
			if err != nil || !mapping {
				diagnostics = append(diagnostics, diagnostic(doc, []string{"spec", "templatePatch"}, "CD_APPSET_TEMPLATE_SYNTAX", 1, "Static templatePatch must be a valid YAML object"))
			} else {
				diagnostics = append(diagnostics, applyStaticTemplatePatch(doc, transformed, object(value), origins, unknown)...)
			}
		} else {
			// A dynamic patch can replace any spec field except protected project.
			project := object(transformed["spec"])["project"]
			transformed["spec"] = map[string]any{"project": project}
			unknown["spec"] = true
		}
	}
	return Document{OverrideOrigins: origins, Origin: doc.originBelow([]string{"spec", "template"}), UnknownPaths: unknown, Snapshot: doc.Snapshot, Root: locate(doc.Root, []string{"spec", "template"}), Value: transformed, Kind: "Application", Target: doc.Target}, diagnostics
}

var fastTemplateMarker = regexp.MustCompile(`.*{{.*}}.*`)

// A merge-patch deletion produces an empty typed spec before the controller
// restores project. A scalar/list replacement cannot be decoded as that spec.
// Keep those cases separate: neither may write through a nil map or retain
// unrelated fields from the unpatched template as if the patch had succeeded.
func applyStaticTemplatePatch(doc Document, transformed, patch map[string]any, origins map[string]yamlview.Node, unknown map[string]bool) []Diagnostic {
	parts := []string{"spec", "templatePatch"}
	project := object(transformed["spec"])["project"]
	patchSpec, exists := patch["spec"]
	if exists && patchSpec != nil && object(patchSpec) == nil {
		transformed["spec"] = map[string]any{"project": project}
		unknown["spec"] = true
		return []Diagnostic{diagnostic(doc, parts, "CD_APPSET_TEMPLATE_SYNTAX", 1, "Static templatePatch spec must be a YAML object or null")}
	}
	if patchUnsupported(patch) {
		transformed["spec"] = map[string]any{"project": project}
		unknown["spec"] = true
		return []Diagnostic{diagnostic(doc, parts, "CD_APPSET_DYNAMIC_FIELD", 3, "Patch directives or merged list effects are not evaluated")}
	}
	mergeTemplate(transformed, patch, nil, origins, locate(doc.Root, parts), true)
	effectiveSpec := object(transformed["spec"])
	if effectiveSpec == nil {
		effectiveSpec = map[string]any{}
	}
	effectiveSpec["project"] = project
	transformed["spec"] = effectiveSpec
	delete(origins, "spec/project")
	clearPatchedUnknowns(transformed, origins, unknown)

	// The original template's schema check cannot see fields removed by a
	// patch. Use the already prepared target schema's required fields, without
	// revalidating dynamic values as if they were statically known nulls.
	specSchema := doc.CompiledSchema
	for _, name := range []string{"spec", "template", "spec"} {
		if specSchema == nil {
			return nil
		}
		specSchema = specSchema.Properties[name]
	}
	if specSchema == nil {
		return nil
	}
	diagnostics := []Diagnostic{}
	for _, name := range specSchema.Required {
		if _, present := effectiveSpec[name]; !present {
			diagnostics = append(diagnostics, diagnostic(doc, parts, "CD_APPSET_STATIC_FIELD", 1, "SCHEMA_REQUIRED: Static templatePatch removes required Application spec field "+name))
		}
	}
	return diagnostics
}

func clearPatchedUnknowns(value map[string]any, origins map[string]yamlview.Node, unknown map[string]bool) {
	for field := range unknown {
		if field == "spec/project" {
			continue
		}
		// Unknown template values are nil placeholders. A concrete effective
		// value proves a static replacement, including an object replacing a
		// dynamic scalar. A deletion is proved by the merge's origin facts.
		if valueAt(value, strings.Split(field, "/")) != nil {
			delete(unknown, field)
			continue
		}
		for ancestor := field; ancestor != ""; {
			if _, changed := origins[ancestor]; changed {
				delete(unknown, field)
				break
			}
			separator := strings.LastIndexByte(ancestor, '/')
			if separator < 0 {
				break
			}
			ancestor = ancestor[:separator]
		}
	}
}

// mergeTemplate handles the statically decidable object/scalar subset. Lists
// replace as units; controller-specific list merge directives remain unknown.
func mergeTemplate(dst, patch map[string]any, prefix []string, origins map[string]yamlview.Node, site yamlview.Node, deletion bool) {
	for key, value := range patch {
		parts := appendPath(prefix, key)
		if items, ok := value.([]any); !deletion && ok && len(items) == 0 {
			continue
		}
		if !deletion && (value == nil || value == "" || value == false) {
			continue
		}
		if strings.HasPrefix(key, "$") {
			continue
		}
		if value == nil && deletion {
			delete(dst, key)
			origins[strings.Join(parts, "/")] = site
			continue
		}
		if nested, ok := value.(map[string]any); ok {
			prior, ok := dst[key].(map[string]any)
			if !ok {
				prior = map[string]any{}
				dst[key] = prior
			}
			mergeTemplate(prior, nested, parts, origins, site, deletion)
		} else {
			dst[key] = value
			origins[strings.Join(parts, "/")] = site
		}
	}
}

func appSetVariants(doc Document) ([]Document, []Diagnostic) {
	spec := object(doc.Value["spec"])
	variants := []Document{}
	diagnostics := []Diagnostic{}
	for i, generator := range objectSlice(spec["generators"]) {
		for kind, body := range object(generator) {
			override := object(object(body)["template"])
			if kind == "selector" {
				continue
			}
			if len(override) == 0 {
				embedded, ds := appSet(doc)
				variants = append(variants, embedded)
				diagnostics = append(diagnostics, ds...)
				continue
			}
			// Clone values before merging; shared completed documents are immutable.
			clone := cloneObject(doc.Value)
			candidateSpec := object(clone["spec"])
			template := object(candidateSpec["template"])
			if template == nil {
				// Invalid/missing base templates still have schema diagnostics.
				// A generator override must not write into their nil projection.
				template = map[string]any{}
				candidateSpec["template"] = template
			}
			site := locate(doc.Root, []string{"spec", "generators", fmt.Sprint(i), kind, "template"})
			origins := map[string]yamlview.Node{}
			mergeTemplate(template, override, nil, origins, site, false)
			candidate := doc
			candidate.Value = clone
			embedded, ds := appSet(candidate)
			for path, node := range origins {
				if _, replaced := embedded.OverrideOrigins[path]; !replaced {
					embedded.OverrideOrigins[path] = node
				}
			}
			variants = append(variants, embedded)
			diagnostics = append(diagnostics, ds...)
		}
	}
	if len(variants) == 0 {
		embedded, ds := appSet(doc)
		return []Document{embedded}, ds
	}
	return variants, diagnostics
}
func cloneObject(value map[string]any) map[string]any {
	out := map[string]any{}
	for key, v := range value {
		switch x := v.(type) {
		case map[string]any:
			out[key] = cloneObject(x)
		default:
			out[key] = v
		}
	}
	return out
}

func patchUnsupported(value any) bool {
	switch x := value.(type) {
	case map[string]any:
		for key, v := range x {
			if strings.HasPrefix(key, "$") || patchUnsupported(v) {
				return true
			}
		}
	case []any:
		// Kubernetes strategic list merge keys are controller type metadata.
		for _, v := range x {
			if _, object := v.(map[string]any); object {
				return true
			}
		}
	}
	return false
}
