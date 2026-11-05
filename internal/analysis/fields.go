package analysis

import (
	"fmt"
	"github.com/lazysnail1024/k8s-crd-toolkit/edit"
	"github.com/lazysnail1024/k8s-crd-toolkit/yamlview"
	schema "github.com/santhosh-tekuri/jsonschema/v6"
	"sigs.k8s.io/yaml"
	"sort"
	"strings"
)

type Field struct {
	Path       []string
	Key        yamlview.Node
	Candidates []string
}
type Fix struct {
	Code  string
	Title string
	Plan  edit.Plan
}

func UnknownFields(doc Document) []Field {
	fields := []Field{}
	var visit func(any, map[string]any, []string)
	visit = func(value any, schema map[string]any, path []string) {
		if doc.Target == "auto" {
			schema = autoFieldSchema(doc, path)
		}
		if preserve, _ := schema["x-kubernetes-preserve-unknown-fields"].(bool); preserve {
			return
		}
		properties := object(schema["properties"])
		for key, x := range object(value) {
			childPath := appendPath(path, key)
			if child, ok := properties[key]; ok {
				visit(x, object(child), childPath)
				continue
			}
			if additional, ok := schema["additionalProperties"].(map[string]any); ok {
				visit(x, additional, childPath)
				continue
			}
			if allowed, _ := schema["additionalProperties"].(bool); allowed || len(properties) == 0 {
				continue
			}
			origin := doc.origin(childPath)
			keyNode := origin.Key
			if origin.Kind == "derived" {
				keyNode = origin.Node
			} else if doc.Origin == nil {
				for _, member := range locate(doc.Root, path).Members() {
					var name string
					if yaml.UnmarshalStrict([]byte(member.Key.Raw()), &name) == nil && name == key {
						keyNode = member.Key
						break
					}
				}
			}
			candidates := []string{}
			if origin.Kind == "exact" && keyNode.Kind() == yamlview.Scalar {
				for name := range properties {
					if _, exists := object(value)[name]; !exists && (distance(key, name) <= 2 || (len(key) >= 3 && strings.HasPrefix(name, key))) {
						candidates = append(candidates, name)
					}
				}
			}
			sort.Strings(candidates)
			fields = append(fields, Field{childPath, keyNode, candidates})
		}
		for i, x := range objectSlice(value) {
			visit(x, object(schema["items"]), appendPath(path, fmt.Sprint(i)))
		}
	}
	visit(doc.Value, doc.SchemaData, nil)
	sort.SliceStable(fields, func(i, j int) bool { return strings.Join(fields[i].Path, "/") < strings.Join(fields[j].Path, "/") })
	return fields
}

// Auto queries use all covered schema branches. The map here contains names
// only; constraints and references are interpreted by the compiled validator.
func autoFieldSchema(doc Document, path []string) map[string]any {
	properties := map[string]any{}
	result := map[string]any{"properties": properties}
	for _, facet := range queryAt(doc, path) {
		node := facet.schema
		for name := range node.Properties {
			properties[name] = map[string]any{}
		}
		switch additional := node.AdditionalProperties.(type) {
		case bool:
			if additional {
				result["additionalProperties"] = true
			}
		case *schema.Schema:
			if result["additionalProperties"] != true {
				result["additionalProperties"] = map[string]any{}
			}
		}
		// A genuine unconstrained object is open; reference/composition wrappers
		// do not make an otherwise structured object open.
		if node.Types != nil && len(node.Properties) == 0 && node.Ref == nil && len(node.AllOf)+len(node.AnyOf)+len(node.OneOf) == 0 && node.AdditionalProperties == nil {
			for _, name := range node.Types.ToStrings() {
				if name == "object" {
					result["additionalProperties"] = true
				}
			}
		}
	}
	return result
}

func fieldCandidates(doc Document) []Fix {
	fixes := []Fix{}
	for _, field := range UnknownFields(doc) {
		for _, candidate := range field.Candidates {
			plan := edit.Plan{BaseRevision: doc.Snapshot.Revision(), Replacements: []edit.Replacement{{Span: field.Key.Span(), ExpectedText: field.Key.Raw(), NewText: candidate}}}
			fixes = append(fixes, Fix{Code: "FIX_FIELD_NAME", Title: "Rename field to " + candidate, Plan: plan})
		}
	}
	return fixes
}
func distance(a, b string) int {
	left, right := []rune(a), []rune(b)
	row := make([]int, len(right)+1)
	for j := range row {
		row[j] = j
	}
	for i, x := range left {
		last := row[0]
		row[0] = i + 1
		for j, y := range right {
			old := row[j+1]
			cost := 0
			if x != y {
				cost = 1
			}
			row[j+1] = min(row[j]+1, row[j+1]+1, last+cost)
			last = old
		}
	}
	return row[len(right)]
}
