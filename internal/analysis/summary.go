package analysis

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"hash"
	"sort"
	"strconv"
	"strings"

	"github.com/lazysnail1024/k8s-crd-toolkit/index"
	"github.com/lazysnail1024/k8s-crd-toolkit/source"
)

// Summary retains the domain facts of one file, never its source text or syntax
// tree. Documents may be used for Domain and cross-file candidate lookup; a
// selected document must be acquired again for schema queries or edits.
type Summary struct {
	Documents    []Document
	Facts        index.Facts
	Repositories []RepositoryReference
	// Bytes conservatively charges strings, containers and position records.
	// It is a logical admission budget, not a measurement of process RSS.
	Bytes int
}

type compactLocation struct {
	span     source.Span
	position Range
	kind     string
}

type compactDocument struct {
	locations           map[string]compactLocation
	variants            []Document
	templateDiagnostics []Diagnostic
}

// ProjectKey is a domain identity, independent of file location. An empty
// namespace selects all namespaces for unresolved control-plane configuration.
// Length-prefixing keeps even invalid user strings from aliasing another key.
func ProjectKey(namespace, name string) index.Key {
	return index.Key("project:" + strconv.Itoa(len(namespace)) + ":" + namespace + name)
}

func (doc Document) compactLocation(parts []string) (compactLocation, bool) {
	if doc.compact == nil {
		return compactLocation{}, false
	}
	path := strings.Join(parts, "/")
	for {
		if location, ok := doc.compact.locations[path]; ok {
			return location, true
		}
		i := strings.LastIndexByte(path, '/')
		if i < 0 {
			return doc.compact.locations[""], true
		}
		path = path[:i]
	}
}

func (doc Document) rangeAt(parts []string) Range {
	if location, ok := doc.compactLocation(parts); ok {
		return location.position
	}
	return nodeRange(doc, doc.location(parts))
}

func (doc Document) compactOrigin(parts []string) (string, bool) {
	location, ok := doc.compactLocation(parts)
	return location.kind, ok
}

func (doc Document) compactVariants() ([]Document, []Diagnostic, bool) {
	if doc.compact == nil || doc.Kind != "ApplicationSet" {
		return nil, nil, false
	}
	return doc.compact.variants, doc.compact.templateDiagnostics, true
}

// Summarize preserves duplicate semantic candidates, missing-target dependencies
// and non-exact origins. Neither a summary nor its index facts proves that the
// enclosing discovery scope is complete.
func Summarize(documents []Document, controlPlaneNamespace string) Summary {
	out := Summary{Bytes: 128}
	digest := sha256.New()
	writeSummaryValue(digest, []any{controlPlaneNamespace, len(documents)})
	dependencies := map[index.Key]bool{}
	uses := map[index.Reference]bool{}
	repositories := map[RepositoryReference]bool{}
	for i, original := range documents {
		doc := detachDocument(original)
		out.Documents = append(out.Documents, doc)
		writeDocumentMeaning(digest, doc)
		out.Bytes += documentSummaryBytes(doc)
		meta := object(doc.Value["metadata"])
		name, namespace := literal(meta["name"]), literal(meta["namespace"])
		if name != "" {
			selection, _ := doc.compactLocation([]string{"metadata", "name"})
			root, _ := doc.compactLocation(nil)
			key := index.Key("resource:" + strconv.Itoa(len(doc.Kind)) + ":" + doc.Kind + string(ProjectKey(namespace, name)))
			if doc.Kind == "AppProject" {
				key = ProjectKey(namespace, name)
			}
			symbol := index.Symbol{ID: index.SymbolID("document:" + strconv.Itoa(i)), Key: key, Name: name, Kind: doc.Kind, Span: root.span, Selection: selection.span}
			out.Facts.Symbols = append(out.Facts.Symbols, symbol)
			if doc.Kind == "AppProject" && namespace != "" {
				// This is a second lookup key for the same semantic candidate,
				// not a second user-facing document symbol.
				symbol.ID += ":any-namespace"
				symbol.Key = ProjectKey("", name)
				out.Facts.Symbols = append(out.Facts.Symbols, symbol)
			}
		}
		effective := []Document{doc}
		if doc.Kind == "ApplicationSet" {
			effective, _, _ = doc.compactVariants()
		}
		for _, app := range effective {
			if app.Kind != "Application" {
				continue
			}
			spec := object(app.Value["spec"])
			if project := literal(spec["project"]); project != "" {
				key := ProjectKey(controlPlaneNamespace, project)
				if app.exact([]string{"spec", "project"}) {
					location, _ := app.compactLocation([]string{"spec", "project"})
					uses[index.Reference{Target: key, Span: location.span}] = true
				} else {
					dependencies[key] = true
				}
			}
			for j, value := range effectiveSources(spec) {
				parts := effectiveSourcePath(spec, j)
				if ref, known := staticRepositoryReference(app, object(value), parts); known {
					repositories[ref] = true
				}
			}
		}
	}
	for use := range uses {
		out.Facts.References = append(out.Facts.References, use)
		// A reference itself establishes the dependency; avoid charging it twice.
		delete(dependencies, use.Target)
	}
	sort.Slice(out.Facts.References, func(i, j int) bool {
		a, b := out.Facts.References[i], out.Facts.References[j]
		if a.Target != b.Target {
			return a.Target < b.Target
		}
		if a.Span.Start != b.Span.Start {
			return a.Span.Start < b.Span.Start
		}
		return a.Span.End < b.Span.End
	})
	for key := range dependencies {
		out.Facts.Dependencies = append(out.Facts.Dependencies, key)
	}
	sort.Slice(out.Facts.Dependencies, func(i, j int) bool { return out.Facts.Dependencies[i] < out.Facts.Dependencies[j] })
	for ref := range repositories {
		out.Repositories = append(out.Repositories, ref)
	}
	sort.Slice(out.Repositories, func(i, j int) bool {
		a, b := out.Repositories[i], out.Repositories[j]
		if a.RepoURL != b.RepoURL {
			return a.RepoURL < b.RepoURL
		}
		if a.TargetRevision != b.TargetRevision {
			return a.TargetRevision < b.TargetRevision
		}
		return a.Path < b.Path
	})
	copy(out.Facts.SemanticDigest[:], digest.Sum(nil))
	for _, symbol := range out.Facts.Symbols {
		out.Bytes += 192 + len(symbol.ID) + len(symbol.Key) + len(symbol.Name) + len(symbol.Kind)
	}
	for _, use := range out.Facts.References {
		out.Bytes += 64 + len(use.Target)
	}
	for _, key := range out.Facts.Dependencies {
		out.Bytes += 32 + len(key)
	}
	for _, ref := range out.Repositories {
		out.Bytes += 96 + len(ref.RepoURL) + len(ref.TargetRevision) + len(ref.Path)
	}
	return out
}

func detachDocument(original Document) Document {
	if original.compact != nil {
		return original // Already detached and immutable.
	}
	snapshot, _ := source.New(source.URI(strings.Clone(string(original.Snapshot.URI()))), original.Snapshot.Revision(), "")
	doc := Document{Snapshot: snapshot, Kind: strings.Clone(original.Kind), Target: strings.Clone(original.Target), Value: map[string]any{}, compact: &compactDocument{locations: map[string]compactLocation{}}}
	copySummaryFields(doc.Value, original.Value, "apiVersion", "kind")
	if metadata, ok := original.Value["metadata"].(map[string]any); ok {
		meta := map[string]any{}
		copySummaryFields(meta, metadata, "name", "namespace")
		doc.Value["metadata"] = meta
	}
	for path, unknown := range original.UnknownPaths {
		if doc.UnknownPaths == nil {
			doc.UnknownPaths = map[string]bool{}
		}
		doc.UnknownPaths[strings.Clone(path)] = unknown
	}
	remember := func(parts ...string) {
		origin := original.origin(parts)
		doc.compact.locations[strings.Join(parts, "/")] = compactLocation{span: origin.Node.Span(), position: original.rangeAt(parts), kind: strings.Clone(origin.Kind)}
	}
	remember()
	remember("metadata", "name")
	remember("metadata", "namespace")
	spec := object(original.Value["spec"])
	switch original.Kind {
	case "AppProject":
		policy := map[string]any{}
		if entries, ok := spec["sourceRepos"].([]any); ok {
			xs := make([]any, len(entries))
			for i, entry := range entries {
				xs[i] = summaryScalar(entry)
			}
			policy["sourceRepos"] = xs
		}
		if entries, ok := spec["destinations"].([]any); ok {
			xs := make([]any, len(entries))
			for i, entry := range entries {
				if item := object(entry); item != nil {
					xs[i] = summaryFields(item, "name", "server", "namespace")
				}
			}
			policy["destinations"] = xs
		}
		doc.Value["spec"] = policy
	case "Application":
		value := summaryFields(spec, "project")
		if destination := object(spec["destination"]); destination != nil {
			value["destination"] = summaryFields(destination, "name", "server", "namespace")
		}
		if raw, present := spec["source"]; present {
			value["source"] = compactSource(raw)
		}
		if sources, ok := spec["sources"].([]any); ok {
			xs := make([]any, len(sources))
			for i, src := range sources {
				xs[i] = compactSource(src)
			}
			value["sources"] = xs
		}
		doc.Value["spec"] = value
		remember("spec")
		remember("spec", "project")
		remember("spec", "destination")
		remember("spec", "source")
		for i, raw := range effectiveSources(value) {
			parts := effectiveSourcePath(value, i)
			remember(parts...)
			remember(appendPath(parts, "ref")...)
			remember(appendPath(parts, "path")...)
			for _, field := range helmValuePaths(object(object(raw)["helm"])) {
				remember(appendPath(parts, append([]string{"helm"}, field.path...)...)...)
			}
		}
	case "ApplicationSet":
		variants, diagnostics := appSetVariants(original)
		for _, variant := range variants {
			doc.compact.variants = append(doc.compact.variants, detachDocument(variant))
		}
		for _, diagnostic := range diagnostics {
			diagnostic.URI = strings.Clone(diagnostic.URI)
			diagnostic.TargetID = strings.Clone(diagnostic.TargetID)
			diagnostic.Code = strings.Clone(diagnostic.Code)
			diagnostic.Source = strings.Clone(diagnostic.Source)
			diagnostic.Message = strings.Clone(diagnostic.Message)
			doc.compact.templateDiagnostics = append(doc.compact.templateDiagnostics, diagnostic)
		}
	}
	return doc
}

func effectiveSources(spec map[string]any) []any {
	if sources := objectSlice(spec["sources"]); len(sources) > 0 {
		return sources
	}
	if source := object(spec["source"]); source != nil {
		return []any{source}
	}
	return nil
}

func effectiveSourcePath(spec map[string]any, i int) []string {
	if len(objectSlice(spec["sources"])) > 0 {
		return []string{"spec", "sources", strconv.Itoa(i)}
	}
	return []string{"spec", "source"}
}

// Only these source fields are read by Domain or cross-file reference lookup.
// Generator-option presence is semantic, but arbitrary helm values and other
// generation payloads are not: keeping them would retain almost the full file.
func compactSource(value any) any {
	src := object(value)
	if src == nil {
		return summaryScalar(value)
	}
	out := summaryFields(src, "repoURL", "targetRevision", "path", "chart", "ref")
	for _, name := range []string{"helm", "kustomize", "ksonnet", "directory", "plugin"} {
		if value, present := src[name]; present {
			out[name] = summaryScalar(value)
		}
	}
	if helm := object(src["helm"]); helm != nil {
		value := map[string]any{}
		if paths, ok := helm["valueFiles"].([]any); ok {
			xs := make([]any, len(paths))
			for i, path := range paths {
				xs[i] = summaryScalar(path)
			}
			value["valueFiles"] = xs
		}
		if files, ok := helm["fileParameters"].([]any); ok {
			xs := make([]any, len(files))
			for i, file := range files {
				xs[i] = summaryFields(object(file), "path")
			}
			value["fileParameters"] = xs
		}
		out["helm"] = value
	}
	return out
}

func summaryFields(value map[string]any, names ...string) map[string]any {
	out := map[string]any{}
	copySummaryFields(out, value, names...)
	return out
}

func copySummaryFields(dst, src map[string]any, names ...string) {
	for _, name := range names {
		if value, present := src[name]; present {
			dst[name] = summaryScalar(value)
		}
	}
}

// Domain observes scalar strings, nil/unknown placeholders and non-nil option
// presence. A wrong-type collection is represented by a non-nil non-string;
// retaining its otherwise irrelevant descendants would not add information.
func summaryScalar(value any) any {
	if value == nil {
		return nil
	}
	if text, ok := value.(string); ok {
		return strings.Clone(text)
	}
	return true
}

func writeSummaryValue(digest hash.Hash, value any) {
	// Compact values contain only JSON-compatible maps, arrays, strings, bools
	// and nil. The encoder sorts string map keys and cannot reject these values.
	_ = json.NewEncoder(digest).Encode(value)
}

func writeDocumentMeaning(digest hash.Hash, doc Document) {
	writeSummaryValue(digest, []any{doc.Kind, doc.Target, doc.Value, doc.UnknownPaths})
	writeSummaryValue(digest, []int{len(doc.compact.locations), len(doc.compact.variants), len(doc.compact.templateDiagnostics)})
	paths := make([]string, 0, len(doc.compact.locations))
	for path := range doc.compact.locations {
		paths = append(paths, path)
	}
	sort.Strings(paths)
	for _, path := range paths {
		writeSummaryValue(digest, []string{path, doc.compact.locations[path].kind})
	}
	// The existing ApplicationSet interpreter visits Go maps. Variant and
	// template-diagnostic enumeration order is not domain meaning.
	variants := make([][32]byte, 0, len(doc.compact.variants))
	for _, variant := range doc.compact.variants {
		child := sha256.New()
		writeDocumentMeaning(child, variant)
		var sum [32]byte
		copy(sum[:], child.Sum(nil))
		variants = append(variants, sum)
	}
	sort.Slice(variants, func(i, j int) bool { return bytes.Compare(variants[i][:], variants[j][:]) < 0 })
	writeSummaryValue(digest, variants)
	diagnostics := make([]string, 0, len(doc.compact.templateDiagnostics))
	for _, diagnostic := range doc.compact.templateDiagnostics {
		encoded, _ := json.Marshal([]any{diagnostic.TargetID, diagnostic.Code, diagnostic.Severity, diagnostic.Message})
		diagnostics = append(diagnostics, string(encoded))
	}
	sort.Strings(diagnostics)
	writeSummaryValue(digest, diagnostics)
}

func documentSummaryBytes(doc Document) int {
	// Includes the Document value, compact wrapper, empty Snapshot line table,
	// slice/map headers and conservative allocator slack on 64-bit platforms.
	bytes := 768 + len(doc.Snapshot.URI()) + len(doc.Kind) + len(doc.Target) + summaryValueBytes(doc.Value)
	for path := range doc.UnknownPaths {
		bytes += 64 + len(path)
	}
	for path, location := range doc.compact.locations {
		bytes += 128 + len(path) + len(location.kind)
	}
	for _, variant := range doc.compact.variants {
		bytes += documentSummaryBytes(variant)
	}
	for _, diagnostic := range doc.compact.templateDiagnostics {
		bytes += 192 + len(diagnostic.URI) + len(diagnostic.TargetID) + len(diagnostic.Code) + len(diagnostic.Source) + len(diagnostic.Message)
	}
	return bytes
}

func summaryValueBytes(value any) int {
	switch v := value.(type) {
	case map[string]any:
		bytes := 64
		for key, item := range v {
			bytes += 96 + len(key) + summaryValueBytes(item)
		}
		return bytes
	case []any:
		bytes := 32 + len(v)*24
		for _, item := range v {
			bytes += summaryValueBytes(item)
		}
		return bytes
	case string:
		return 16 + len(v)
	default:
		return 16
	}
}
