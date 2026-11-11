package analysis

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/lazysnail1024/k8s-crd-toolkit/schemaquery"
	"os"
	"sort"
	"strings"
	"testing"

	"github.com/lazysnail1024/zed-argocd/internal/assets"
	schema "github.com/santhosh-tekuri/jsonschema/v6"
)

// Independently enumerate every declared property path in every exact schema.
// The auto query view must expose them all, including fields deleted later and
// deeply nested array/map content. This also exercises the traversal budget.
func TestAutoQueriesCoverEveryDeclaredField(t *testing.T) {
	type step struct {
		name  string
		array bool
	}
	type expectation struct {
		kind   string
		path   []step
		fields map[string]bool
	}
	paths := map[string]*expectation{}
	seen := map[string]bool{}
	compiled := map[string]*schema.Schema{}
	for _, kind := range []string{"Application", "ApplicationSet", "AppProject"} {
		data, err := assets.SchemaData("auto", "argoproj.io/v1alpha1", kind)
		if err != nil {
			t.Fatal(err)
		}
		compiled[kind], err = assets.CompileSchema(data)
		if err != nil {
			t.Fatal(err)
		}
	}
	// SupportedVersions is an identity summary. The embedded catalog supplies
	// the exact tag inventory so this check automatically grows with the window.
	var catalog struct {
		Versions []struct {
			Version string `json:"version"`
			Schemas []struct {
				Kind string `json:"kind"`
				Hash string `json:"sha256"`
			} `json:"schemas"`
		} `json:"versions"`
	}
	data, err := os.ReadFile("../assets/catalog.json")
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(data, &catalog); err != nil {
		t.Fatal(err)
	}
	for _, release := range catalog.Versions {
		for _, entry := range release.Schemas {
			if seen[entry.Hash] {
				continue
			}
			seen[entry.Hash] = true
			data, err := assets.SchemaData(release.Version, "argoproj.io/v1alpha1", entry.Kind)
			if err != nil {
				t.Fatal(err)
			}
			root := data
			var walk func(map[string]any, []step)
			walk = func(node map[string]any, path []step) {
				properties := object(node["properties"])
				if len(properties) > 0 {
					key := entry.Kind + fmt.Sprint(path)
					if paths[key] == nil {
						paths[key] = &expectation{entry.Kind, append([]step{}, path...), map[string]bool{}}
					}
					for name, child := range properties {
						paths[key].fields[name] = true
						walk(object(child), append(append([]step{}, path...), step{name: name}))
					}
				}
				if child, ok := node["items"].(map[string]any); ok {
					walk(child, append(append([]step{}, path...), step{name: "0", array: true}))
				}
				if child, ok := node["additionalProperties"].(map[string]any); ok {
					walk(child, append(append([]step{}, path...), step{name: "arbitrary-entry"}))
				}
				for _, keyword := range []string{"anyOf", "allOf", "oneOf"} {
					for _, child := range objectSlice(node[keyword]) {
						walk(object(child), path)
					}
				}
			}
			walk(root, nil)
		}
	}
	keys := []string{}
	for key := range paths {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	fields := 0
	for _, key := range keys {
		expect := paths[key]
		parts := []string{}
		var value any = map[string]any{}
		for _, part := range expect.path {
			parts = append(parts, part.name)
		}
		for i := len(expect.path) - 1; i >= 0; i-- {
			part := expect.path[i]
			if part.array {
				value = []any{value}
			} else {
				value = map[string]any{part.name: value}
			}
		}
		doc, err := automaticQueryDocument(context.Background(), Document{Target: "auto", Value: object(value), CompiledSchema: compiled[expect.kind]}, parts)
		if err != nil {
			t.Fatal(err)
		}
		query, err := schemaquery.Lookup(context.Background(), doc.CompiledSchema, parts, doc.Value, schemaquery.Limits{})
		if err != nil {
			t.Fatal(err)
		}
		observed := map[string]bool{}
		for _, facet := range query.Nodes {
			for name := range facet.Properties {
				observed[name] = true
			}
		}
		for name := range expect.fields {
			fields++
			if !observed[name] {
				t.Fatalf("auto query hides %s /%s/%s", expect.kind, strings.Join(parts, "/"), name)
			}
		}
	}
	t.Logf("covered %d field declarations at %d distinct paths across %d source schemas", fields, len(paths), len(seen))
}
