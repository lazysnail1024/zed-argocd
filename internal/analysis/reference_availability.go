package analysis

import (
	"context"
	"strings"
)

func sourceReferenceLimit(ctx context.Context, doc Document) *Refusal {
	for path, unknown := range doc.UnknownPaths {
		parts := strings.Split(path, "/")
		if unknown && (path == "" || path == "spec" || len(parts) >= 2 && parts[0] == "spec" && parts[1] == "sources" &&
			(len(parts) <= 3 || parts[3] == "ref" || parts[3] == "helm" && (len(parts) == 4 || parts[4] == "valueFiles" || parts[4] == "fileParameters"))) {
			return Refuse("DYNAMIC_REFERENCE", "dynamic or replaced source references prevent a complete location count")
		}
	}
	if ctx == nil {
		ctx = context.Background()
	}
	items, err := sourceOccurrences(ctx, doc)
	if err != nil {
		return ExplainRefusal(err)
	}
	for _, item := range items {
		if !item.precise {
			return Refuse("REFERENCE_COVERAGE_UNKNOWN", "shared or derived source references prevent a complete location count")
		}
	}
	return nil
}

func projectReferenceLimit(ctx context.Context, documents []Document) *Refusal {
	for _, doc := range documents {
		if ctx != nil && ctx.Err() != nil {
			return ExplainRefusal(ctx.Err())
		}
		if doc.Kind == "AppProject" {
			if namespace := literal(object(doc.Value["metadata"])["namespace"]); strings.ContainsAny(namespace, "${}") {
				return Refuse("TARGET_UNKNOWN", "an unknown Project namespace prevents proving reference uniqueness")
			}
			continue
		}
		if doc.Kind != "Application" {
			continue
		}
		spec := object(doc.Value["spec"])
		value, present := spec["project"]
		unknown := doc.UnknownPaths[""] || doc.UnknownPaths["spec"] || doc.UnknownPaths["spec/project"]
		if !present && !unknown {
			continue
		}
		project, literalValue := value.(string)
		if !literalValue || unknown || strings.ContainsAny(project, "${}") {
			return Refuse("DYNAMIC_REFERENCE", "unknown or dynamic Project uses prevent a complete reference count")
		}
		if !doc.exact([]string{"spec", "project"}) {
			return Refuse("REFERENCE_COVERAGE_UNKNOWN", "a Project use has no independent source location")
		}
	}
	return nil
}

func projectionReferenceLimit(documents []Document, uri string) *Refusal {
	for _, doc := range documents {
		if doc.Kind != "ApplicationSet" || uri != "" && string(doc.Snapshot.URI()) != uri {
			continue
		}
		_, diagnostics := appSetVariants(doc)
		for _, diagnostic := range diagnostics {
			if diagnostic.Severity == 1 {
				return Refuse("REFERENCE_COVERAGE_UNKNOWN", "ApplicationSet projection errors prevent a complete reference count")
			}
		}
	}
	return nil
}
