package assets

import (
	"encoding/json"
	schema "github.com/santhosh-tekuri/jsonschema/v6"
	"testing"
)

// Every collected schema node must still enforce its declared type, required
// fields and enum. This catches accidental permissive conversion independently
// of the small end-to-end examples, including patches with changed CRDs.
func TestEveryReleaseSchemaConstraints(t *testing.T) {
	catalog, err := catalogData()
	if err != nil {
		t.Fatal(err)
	}
	compiled := map[string]*schema.Schema{}
	visited := map[*schema.Schema]bool{}
	count := 0
	var inspect func(*schema.Schema)
	inspect = func(node *schema.Schema) {
		if node == nil || visited[node] {
			return
		}
		visited[node] = true
		count++
		if node.Types != nil {
			for _, invalid := range []any{nil, true, "text", float64(3), []any{}, map[string]any{}} {
				hasType := false
				for _, typ := range node.Types.ToStrings() {
					switch invalid.(type) {
					case nil:
						hasType = hasType || typ == "null"
					case bool:
						hasType = hasType || typ == "boolean"
					case string:
						hasType = hasType || typ == "string"
					case float64:
						hasType = hasType || typ == "integer" || typ == "number"
					case []any:
						hasType = hasType || typ == "array"
					case map[string]any:
						hasType = hasType || typ == "object"
					}
				}
				if !hasType && node.Validate(invalid) == nil {
					t.Fatalf("type constraint discarded at %s", node.Location)
				}
			}
		}
		if len(node.Required) > 0 && node.Validate(map[string]any{}) == nil {
			t.Fatalf("required constraint discarded at %s", node.Location)
		}
		if node.Enum != nil && node.Validate("__not_a_declared_enum_value__") == nil {
			t.Fatalf("enum constraint discarded at %s", node.Location)
		}
		inspect(node.Ref)
		inspect(node.Items2020)
		if child, ok := node.AdditionalProperties.(*schema.Schema); ok {
			inspect(child)
		}
		for _, child := range node.Properties {
			inspect(child)
		}
		for _, group := range [][]*schema.Schema{node.AllOf, node.AnyOf, node.OneOf} {
			for _, child := range group {
				inspect(child)
			}
		}
	}
	auto := map[string]*schema.Schema{}
	for _, entry := range catalog.AutoSchemas {
		data, err := SchemaData("auto", entry.APIVersion, entry.Kind)
		if err != nil {
			t.Fatal(err)
		}
		auto[entry.Kind], err = CompileSchema(data)
		if err != nil {
			t.Fatal(err)
		}
	}
	for _, release := range catalog.Versions {
		for _, entry := range release.Schemas {
			current := compiled[entry.Hash]
			if current == nil {
				data, err := SchemaData(release.Version, entry.APIVersion, entry.Kind)
				if err != nil {
					t.Fatalf("%s %s: %v", release.Version, entry.Kind, err)
				}
				current, err = CompileSchema(data)
				if err != nil {
					t.Fatalf("%s %s: %v", release.Version, entry.Kind, err)
				}
				compiled[entry.Hash] = current
				inspect(current)
			}
			document := sample(entry.Kind)
			if err := current.Validate(document); err != nil {
				t.Fatalf("%s %s sample: %v", release.Version, entry.Kind, err)
			}
			if err := auto[entry.Kind].Validate(document); err != nil {
				t.Fatalf("auto rejects %s %s sample: %v", release.Version, entry.Kind, err)
			}
		}
	}
	t.Logf("validated %d releases, %d distinct source schemas, %d constrained nodes", len(catalog.Versions), len(compiled), count)
}

func sample(kind string) map[string]any {
	spec := map[string]any{"project": "default", "source": map[string]any{"repoURL": "https://example.com/repo"}, "destination": map[string]any{"server": "https://kubernetes.default.svc", "namespace": "default"}}
	switch kind {
	case "AppProject":
		spec = map[string]any{"sourceRepos": []any{"*"}, "destinations": []any{map[string]any{"server": "*", "namespace": "*"}}}
	case "ApplicationSet":
		spec = map[string]any{"generators": []any{map[string]any{"list": map[string]any{"elements": []any{}}}}, "template": map[string]any{"metadata": map[string]any{"name": "child"}, "spec": spec}}
	}
	return map[string]any{"apiVersion": "argoproj.io/v1alpha1", "kind": kind, "metadata": map[string]any{"name": "sample"}, "spec": spec}
}

func TestAutoWholeVersion(t *testing.T) {
	checks := []struct {
		name   string
		fields string
		valid  bool
	}{
		{"legacy ksonnet", `{"source":{"repoURL":"https://example.com/repo","ksonnet":{"environment":"dev"}}}`, true},
		{"current enabled", `{"syncPolicy":{"automated":{"enabled":true}}}`, true},
		{"new field wrong type", `{"syncPolicy":{"automated":{"enabled":"yes"}}}`, false},
		{"legacy field wrong type", `{"source":{"repoURL":"https://example.com/repo","ksonnet":{"environment":42}}}`, false},
		{"legacy required parameter value", `{"source":{"repoURL":"https://example.com/repo","ksonnet":{"parameters":[{"name":"missing-value"}]}}}`, false},
		{"impossible version combination", `{"source":{"repoURL":"https://example.com/repo","ksonnet":{"environment":"dev"}},"syncPolicy":{"automated":{"enabled":true}}}`, false},
	}
	for _, kind := range []string{"Application", "ApplicationSet"} {
		data, err := SchemaData("auto", "argoproj.io/v1alpha1", kind)
		if err != nil {
			t.Fatal(err)
		}
		compiled, err := CompileSchema(data)
		if err != nil {
			t.Fatal(err)
		}
		for _, check := range checks {
			t.Run(kind+"/"+check.name, func(t *testing.T) {
				value := sample(kind)
				applicationSpec := value["spec"].(map[string]any)
				if kind == "ApplicationSet" {
					applicationSpec = applicationSpec["template"].(map[string]any)["spec"].(map[string]any)
				}
				fields := map[string]any{}
				if err := json.Unmarshal([]byte(check.fields), &fields); err != nil {
					t.Fatal(err)
				}
				for key, field := range fields {
					applicationSpec[key] = field
				}
				if got := compiled.Validate(value) == nil; got != check.valid {
					t.Fatalf("accepted=%v want=%v", got, check.valid)
				}
			})
		}
	}
}

func TestHistoricalApplicationSetSource(t *testing.T) {
	for version, commit := range map[string]string{
		"2.3.0":  "eab46ad3d633317279ddf3a56855823b0057a13e",
		"2.3.17": "a5712ba564afaff0b23697135d6281d413e60345",
	} {
		want := "https://raw.githubusercontent.com/argoproj/applicationset/" + commit + "/manifests/install.yaml"
		if got := SchemaSource(version, "ApplicationSet"); got != want {
			t.Fatalf("%s source=%q want=%q", version, got, want)
		}
	}
}
