package analysis

import (
	"fmt"
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
	if patch, ok := spec["templatePatch"].(string); ok {
		patchValue := walk(patch, []string{"spec", "templatePatch"})
		if _, static := patchValue.(string); static {
			var value any
			err := yaml.UnmarshalStrict([]byte(patch), &value)
			_, mapping := value.(map[string]any)
			if err != nil || !mapping {
				diagnostics = append(diagnostics, diagnostic(doc, []string{"spec", "templatePatch"}, "CD_APPSET_TEMPLATE_SYNTAX", 1, "Static templatePatch must be a valid YAML object"))
			}
		}
	}
	return Document{Origin: doc.originBelow([]string{"spec", "template"}), UnknownPaths: unknown, Snapshot: doc.Snapshot, Root: locate(doc.Root, []string{"spec", "template"}), Value: transformed, Kind: "Application", Target: doc.Target}, diagnostics
}

var fastTemplateMarker = regexp.MustCompile(`.*{{.*}}.*`)
