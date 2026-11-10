package analysis

import (
	"context"
	"fmt"
	"strings"
	"testing"
)

func assertApplicationSetBudget(t *testing.T, value map[string]any, limited bool) {
	t.Helper()
	cost, err := applicationSetExpansion(context.Background(), value, maxProjectionNodes, maxApplicationSetExpansionBytes)
	if limited {
		failure, ok := err.(*projectionError)
		if !ok || failure.Code != "INPUT_BUDGET_EXCEEDED" {
			t.Fatalf("amplified input was admitted: cost=%+v err=%v", cost, err)
		}
		return
	}
	if err != nil || cost.nodes <= 0 || cost.nodes > maxProjectionNodes || cost.bytes > maxApplicationSetExpansionBytes {
		t.Fatalf("bounded control was not admitted: cost=%+v err=%v", cost, err)
	}
}

func TestApplicationSetPatchAliasesAreAdmittedBeforeExpansion(t *testing.T) {
	for _, test := range []struct {
		name    string
		scalar  int
		copies  int
		limited bool
	}{
		{"small-alias-control", 100, 10, false},
		{"small-input-large-expansion", 700000, 100, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			patch := "anchor: &x " + strings.Repeat("x", test.scalar) + "\nspec:\n  payloads: [" + strings.TrimSuffix(strings.Repeat("*x, ", test.copies), ", ") + "]\n"
			if len(patch) >= 1024*1024 {
				t.Fatal("the alias fixture must stay smaller than one MiB")
			}
			value := map[string]any{"spec": map[string]any{
				"template":      map[string]any{"metadata": map[string]any{"name": "bounded"}},
				"templatePatch": patch,
				"generators":    []any{map[string]any{"list": map[string]any{"elements": []any{}}}},
			}}
			assertApplicationSetBudget(t, value, test.limited)
		})
	}
}

// Every individual key remains a legal short YAML key, while the path to each
// leaf repeats a long prefix. The input has few nodes and little scalar text;
// the copied unknown/override paths are the expensive derived representation.
func longApplicationSetPaths(dynamic bool) map[string]any {
	leaves := map[string]any{}
	for i := 0; i < 2048; i++ {
		value := "literal"
		if dynamic {
			value = "{{value}}"
		}
		leaves[fmt.Sprintf("k%04d", i)] = value
	}
	for i := 0; i < 40; i++ {
		leaves = map[string]any{strings.Repeat("k", 1000): leaves}
	}
	return map[string]any{"spec": map[string]any{"source": map[string]any{"helm": map[string]any{"valuesObject": leaves}}}}
}

func TestApplicationSetDerivedPathBudget(t *testing.T) {
	for _, test := range []struct {
		name     string
		dynamic  bool
		override bool
		limited  bool
	}{
		{"static-base-control", false, false, false},
		{"unknown-path-copies", true, false, true},
		{"override-origin-paths", false, true, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			template := longApplicationSetPaths(test.dynamic)
			list := map[string]any{"elements": []any{}}
			if test.override {
				list["template"] = template
				template = map[string]any{"metadata": map[string]any{"name": "bounded"}}
			}
			value := map[string]any{"spec": map[string]any{
				"template":   template,
				"generators": []any{map[string]any{"list": list}},
			}}
			assertApplicationSetBudget(t, value, test.limited)
		})
	}
}
