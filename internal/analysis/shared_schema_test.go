package analysis

import (
	"context"
	"encoding/json"
	"reflect"
	"testing"

	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/k8s-crd-toolkit/yamlview"
	"github.com/lazysnail1024/zed-argocd/internal/assets"
)

func TestCompiledFieldQueriesKeepPreservedUnknownRegions(t *testing.T) {
	// The annotation is not retained by jsonschema/v6. The compiled reference
	// resolves an escaped JSON pointer; only that node's annotation is read.
	var data map[string]any
	if err := json.Unmarshal([]byte(`{
      "type":"object","properties":{"payload":{"$ref":"#/$defs/preserve~1node"},"known":{"type":"string"}},
      "$defs":{"preserve/node":{"type":"object","x-kubernetes-preserve-unknown-fields":true,
        "properties":{"enabled":{"type":"boolean"}}}}
    }`), &data); err != nil {
		t.Fatal(err)
	}
	compiled, err := assets.CompileSchema(data)
	if err != nil {
		t.Fatal(err)
	}
	snapshot, err := source.New("file:///preserved.yaml", 1, "payload:\n  enabled: true\n  vendor:\n    future: anything\noutside: typo\n")
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	view, err := yamlview.Parse(ctx, snapshot)
	if err != nil {
		t.Fatal(err)
	}
	parsed := view.Documents()[0]
	projected, err := projectValues(ctx, snapshot, parsed)
	if err != nil {
		t.Fatal(err)
	}
	doc := Document{Snapshot: snapshot, Root: parsed.Root(), Value: object(projected.Value), Origin: projected.Origin, SchemaData: data, CompiledSchema: compiled}
	fields, err := UnknownFields(ctx, doc)
	if err != nil {
		t.Fatal(err)
	}
	if len(fields) != 1 || !reflect.DeepEqual(fields[0].Path, []string{"outside"}) {
		t.Fatalf("preserved subtree must not become unknown fields: %#v", fields)
	}
	doc.Value["payload"].(map[string]any)["enabled"] = "invalid"
	if compiled.Validate(doc.Value) == nil {
		t.Fatal("known-field constraints inside preserved region were lost")
	}
}
