package assets

import (
	"encoding/json"
	"fmt"
	"path"
	"reflect"
	"slices"
	"testing"

	schema "github.com/santhosh-tekuri/jsonschema/v6"
)

// Expectations come from the immutable input bytes, independently of SchemaData
// and CompileSchema. Walking only the compiled tree would silently skip any
// constraint or property that either production step accidentally discarded.
func inspectDeclaredSchema(t *testing.T, declared map[string]any, node *schema.Schema, location string) int {
	t.Helper()
	if node == nil {
		t.Fatalf("declared schema node missing at %s", location)
	}
	if kind, ok := declared["type"].(string); ok {
		if node.Types == nil || !slices.Equal(node.Types.ToStrings(), []string{kind}) {
			t.Fatalf("declared type constraint discarded at %s: want %s", location, kind)
		}
		for _, probe := range []struct {
			value any
			kinds []string
		}{
			{nil, []string{"null"}}, {true, []string{"boolean"}}, {"text", []string{"string"}},
			{float64(3), []string{"integer", "number"}}, {float64(3.5), []string{"number"}},
			{[]any{}, []string{"array"}}, {map[string]any{}, []string{"object"}},
		} {
			if !slices.Contains(probe.kinds, kind) && node.Validate(probe.value) == nil {
				t.Fatalf("declared type constraint not enforced at %s", location)
			}
		}
	} else if _, exists := declared["type"]; exists {
		t.Fatalf("unhandled declared type shape at %s", location)
	}
	if required, ok := declared["required"].([]any); ok {
		want := make([]string, len(required))
		for i, name := range required {
			want[i] = name.(string)
		}
		got := slices.Clone(node.Required)
		slices.Sort(want)
		slices.Sort(got)
		if !slices.Equal(want, got) || len(want) > 0 && node.Validate(map[string]any{}) == nil {
			t.Fatalf("declared required constraint discarded at %s", location)
		}
	}
	if values, ok := declared["enum"].([]any); ok {
		if node.Enum == nil || !reflect.DeepEqual(values, node.Enum.Values) || node.Validate("__not_a_declared_enum_value__") == nil {
			t.Fatalf("declared enum constraint discarded at %s", location)
		}
	}
	count := 1
	if properties, ok := declared["properties"].(map[string]any); ok {
		for name, child := range properties {
			count += inspectDeclaredSchema(t, child.(map[string]any), node.Properties[name], location+"/properties/"+name)
		}
	}
	if items, ok := declared["items"].(map[string]any); ok {
		count += inspectDeclaredSchema(t, items, node.Items2020, location+"/items")
	}
	if additional, ok := declared["additionalProperties"].(map[string]any); ok {
		child, _ := node.AdditionalProperties.(*schema.Schema)
		count += inspectDeclaredSchema(t, additional, child, location+"/additionalProperties")
	}
	for _, group := range []struct {
		name  string
		nodes []*schema.Schema
	}{{"anyOf", node.AnyOf}, {"allOf", node.AllOf}, {"oneOf", node.OneOf}} {
		if children, ok := declared[group.name].([]any); ok {
			if len(children) != len(group.nodes) {
				t.Fatalf("declared %s branches discarded at %s", group.name, location)
			}
			for i, child := range children {
				count += inspectDeclaredSchema(t, child.(map[string]any), group.nodes[i], fmt.Sprintf("%s/%s/%d", location, group.name, i))
			}
		}
	}
	// Exact source schemas are inline today; do not silently omit a new reference
	// representation if the catalog format changes. Auto's shared definitions are
	// exercised by the independent whole-version and CLI/LSP consumers.
	if _, exists := declared["$ref"]; exists {
		t.Fatalf("exact source schema acquired an uninspected reference at %s", location)
	}
	return count
}

func TestEveryReleaseSchemaConstraints(t *testing.T) {
	catalog, err := catalogData()
	if err != nil {
		t.Fatal(err)
	}
	compiled := map[string]*schema.Schema{}
	count := 0
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
				raw, err := schemas.ReadFile("schemas/" + path.Base(entry.Path))
				if err != nil {
					t.Fatal(err)
				}
				var declared map[string]any
				if err := json.Unmarshal(raw, &declared); err != nil {
					t.Fatal(err)
				}
				data, err := SchemaData(release.Version, entry.APIVersion, entry.Kind)
				if err != nil {
					t.Fatalf("%s %s: %v", release.Version, entry.Kind, err)
				}
				current, err = CompileSchema(data)
				if err != nil {
					t.Fatalf("%s %s: %v", release.Version, entry.Kind, err)
				}
				compiled[entry.Hash] = current
				count += inspectDeclaredSchema(t, declared, current, release.Version+"/"+entry.Kind)
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
	if len(catalog.Versions) == 0 || len(compiled) == 0 || count == 0 {
		t.Fatal("empty release schema coverage")
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
