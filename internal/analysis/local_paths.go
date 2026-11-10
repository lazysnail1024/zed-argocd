package analysis

import (
	"context"
	"strconv"
	"strings"

	"github.com/lazysnail1024/k8s-crd-toolkit/source"
)

// RepositoryReference names one statically effective Application source. These
// are dependency facts for the bounded workspace sampler, not filesystem I/O.
type RepositoryReference struct {
	RepoURL        string
	TargetRevision string
	Path           string
}

// RepositoryReferences uses the same effective ApplicationSet variants as
// Domain, including inherited generator templates and static templatePatch.
// Raw template fields are not sufficient: all three source fields can change.
func RepositoryReferences(ctx context.Context, snapshot source.Snapshot, value map[string]any) ([]RepositoryReference, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if literal(value["apiVersion"]) != "argoproj.io/v1alpha1" {
		return nil, nil
	}
	doc := Document{Snapshot: snapshot, Value: value, Kind: literal(value["kind"])}
	documents := []Document{doc}
	if doc.Kind == "ApplicationSet" {
		// Workspace repository extraction precedes Analyzer in the CLI and
		// explicit whole-scope edit proof. It must admit the same expansion.
		if _, err := applicationSetExpansion(ctx, value, maxProjectionNodes, maxApplicationSetExpansionBytes); err != nil {
			return nil, err
		}
		documents, _ = appSetVariants(doc)
	}
	refs := []RepositoryReference{}
	for _, effective := range documents {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if effective.Kind != "Application" {
			continue
		}
		spec := object(effective.Value["spec"])
		sources := objectSlice(spec["sources"])
		if len(sources) == 0 {
			sources = []any{spec["source"]}
		}
		for i, value := range sources {
			parts := []string{"spec", "source"}
			if len(objectSlice(spec["sources"])) > 0 {
				parts = []string{"spec", "sources", strconv.Itoa(i)}
			}
			if ref, known := staticRepositoryReference(effective, object(value), parts); known {
				refs = append(refs, ref)
			}
		}
	}
	return refs, ctx.Err()
}

// An omitted revision is a static empty value; a templated revision is unknown.
// Keep that distinction when selecting the repository mapping as well as path.
func staticRepositoryReference(doc Document, src map[string]any, parts []string) (RepositoryReference, bool) {
	for _, field := range []string{"repoURL", "targetRevision", "path"} {
		// Template evaluation leaves unknown values as explicit nil fields.
		// A later static patch can replace that placeholder or delete the key;
		// those effective values are known even if provenance retains the marker.
		if value, present := src[field]; !present || value != nil {
			continue
		}
		fieldPath := strings.Join(appendPath(parts, field), "/")
		for unknown := range doc.UnknownPaths {
			if fieldPath == unknown || strings.HasPrefix(fieldPath, unknown+"/") {
				return RepositoryReference{}, false
			}
		}
	}
	ref := RepositoryReference{literal(src["repoURL"]), literal(src["targetRevision"]), literal(src["path"])}
	if ref.RepoURL == "" || ref.Path == "" || strings.Contains(ref.Path, "{{") || strings.Contains(ref.RepoURL, "{{") || strings.Contains(ref.TargetRevision, "{{") {
		return RepositoryReference{}, false
	}
	return ref, true
}
