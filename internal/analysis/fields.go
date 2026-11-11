package analysis

import (
	"context"
	"fmt"
	"github.com/lazysnail1024/k8s-crd-toolkit/edit"
	"github.com/lazysnail1024/k8s-crd-toolkit/schemaquery"
	"github.com/lazysnail1024/k8s-crd-toolkit/yamlview"
	schema "github.com/santhosh-tekuri/jsonschema/v6"
	"net/url"
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

// Unknown-field warnings are an editor policy: covered field names stay known
// across all selected releases, even when their combination is incompatible.
// Every name and child path comes from the same compiled graph as other queries.
func UnknownFields(ctx context.Context, doc Document) ([]Field, error) {
	fields := []Field{}
	var visit func(any, []string) error
	visit = func(value any, path []string) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		if items, ok := value.([]any); ok {
			for i, x := range items {
				if err := visit(x, appendPath(path, fmt.Sprint(i))); err != nil {
					return err
				}
			}
			return nil
		}
		objectValue, ok := value.(map[string]any)
		if !ok {
			return nil
		}
		query, err := schemaquery.Lookup(ctx, doc.CompiledSchema, path, doc.Value, schemaquery.Limits{})
		if err != nil {
			return err
		}
		properties := map[string]bool{}
		additional, open := false, false
		for _, node := range query.Nodes {
			if preservesUnknown(doc.SchemaData, node.Location) {
				return nil
			}
			for name := range node.Properties {
				properties[name] = true
			}
			switch v := node.AdditionalProperties.(type) {
			case bool:
				open = open || v
			case *schema.Schema:
				additional = true
			}
			if node.Types != nil && len(node.Properties) == 0 && node.Ref == nil && len(node.AllOf)+len(node.AnyOf)+len(node.OneOf) == 0 && node.AdditionalProperties == nil {
				for _, name := range node.Types.ToStrings() {
					if name == "object" {
						open = true
					}
				}
			}
		}
		for key, x := range objectValue {
			childPath := appendPath(path, key)
			if properties[key] || additional {
				if err := visit(x, childPath); err != nil {
					return err
				}
				continue
			}
			if open || len(properties) == 0 {
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
					if _, exists := objectValue[name]; !exists && (distance(key, name) <= 2 || len(key) >= 3 && strings.HasPrefix(name, key)) {
						candidates = append(candidates, name)
					}
				}
			}
			sort.Strings(candidates)
			fields = append(fields, Field{childPath, keyNode, candidates})
		}
		return nil
	}
	if err := visit(doc.Value, nil); err != nil {
		return nil, err
	}
	sort.SliceStable(fields, func(i, j int) bool { return strings.Join(fields[i].Path, "/") < strings.Join(fields[j].Path, "/") })
	return fields, nil
}

// The validator omits this annotation from compiled metadata. Use its already
// resolved schema location only to read that annotation; do not interpret refs,
// compositions, properties or items a second time.
func preservesUnknown(data map[string]any, location string) bool {
	_, fragment, _ := strings.Cut(location, "#")
	pointer, err := url.PathUnescape(fragment)
	if err != nil {
		return false
	}
	var value any = data
	if pointer != "" {
		if !strings.HasPrefix(pointer, "/") {
			return false
		}
		for _, part := range strings.Split(pointer[1:], "/") {
			part = strings.ReplaceAll(strings.ReplaceAll(part, "~1", "/"), "~0", "~")
			value = valueAt(value, []string{part})
		}
	}
	preserve, _ := object(value)["x-kubernetes-preserve-unknown-fields"].(bool)
	return preserve
}

func fieldCandidates(ctx context.Context, doc Document) []Fix {
	fixes := []Fix{}
	fields, err := UnknownFields(ctx, doc)
	if err != nil {
		return nil
	}
	for _, field := range fields {
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
