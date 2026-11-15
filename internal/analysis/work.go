package analysis

import (
	"context"

	"github.com/lazysnail1024/k8s-crd-toolkit/schemaquery"
	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/k8s-crd-toolkit/yamlview"
	"github.com/lazysnail1024/zed-argocd/internal/worktrace"
	schema "github.com/santhosh-tekuri/jsonschema/v6"
)

// All product CST parse entry points use this boundary, including transient
// recovery and Helm projections which are outside diagnostic ParsedFiles.
func parseView(ctx context.Context, snapshot source.Snapshot, purpose string) (yamlview.View, error) {
	if worktrace.From(ctx) == nil {
		return yamlview.Parse(ctx, snapshot)
	}
	defer worktrace.Measure(ctx, "parseCalls", "parseNs")()
	worktrace.Add(ctx, "parseBytes", int64(len(snapshot.Text())))
	worktrace.Add(ctx, purpose+"ParseCalls", 1)
	return yamlview.Parse(ctx, snapshot)
}

func measureQuery(ctx context.Context, operation string) func() {
	if worktrace.From(ctx) == nil {
		return func() {}
	}
	worktrace.Add(ctx, "schemaQuery"+operation, 1)
	return worktrace.Measure(ctx, "schemaQueries", "schemaQueryNs")
}

// These count actual calls to the shared schema query API, not its internal
// recursive graph visits. Limits and interpretation remain with that API.
func schemaLookup(ctx context.Context, root *schema.Schema, path []string, value any, limits schemaquery.Limits) (schemaquery.Result, error) {
	defer measureQuery(ctx, "Lookup")()
	return schemaquery.Lookup(ctx, root, path, value, limits)
}

func schemaValues(ctx context.Context, root *schema.Schema, path []string, value any, limits schemaquery.Limits) ([]any, error) {
	defer measureQuery(ctx, "Values")()
	return schemaquery.Values(ctx, root, path, value, limits)
}

func schemaAllowsKey(ctx context.Context, root *schema.Schema, path []string, value any, limits schemaquery.Limits) (bool, error) {
	defer measureQuery(ctx, "AllowsKey")()
	return schemaquery.AllowsKey(ctx, root, path, value, limits)
}

func schemaType(ctx context.Context, root *schema.Schema, path []string, value any, limits schemaquery.Limits) (string, error) {
	defer measureQuery(ctx, "Type")()
	return schemaquery.Type(ctx, root, path, value, limits)
}

func schemaSelectShapes(ctx context.Context, branches []*schema.Schema, value any, active []string, limits schemaquery.Limits) ([]*schema.Schema, error) {
	defer measureQuery(ctx, "SelectShapes")()
	return schemaquery.SelectShapes(ctx, branches, value, active, limits)
}

func validateSchema(ctx context.Context, compiled *schema.Schema, value any) error {
	defer worktrace.Measure(ctx, "schemaValidations", "schemaValidationNs")()
	return compiled.Validate(value)
}
