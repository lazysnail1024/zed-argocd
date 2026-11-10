package analysis

import (
	"context"
	"encoding/json"
	"reflect"
	"sort"
	"strings"
	"testing"
	"unsafe"

	"github.com/lazysnail1024/k8s-crd-toolkit/index"
	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/k8s-crd-toolkit/yamlview"
)

// Project real YAML through the same bounded alias/merge interpreter as Analyze.
// These tests compare the existing domain evaluator with its detached inputs;
// schema compilation and workspace discovery are intentionally independent.
func summaryDocuments(t *testing.T, text string) []Document {
	t.Helper()
	snapshot, err := source.New("file:///workspace/resources.yaml", 7, text)
	if err != nil {
		t.Fatal(err)
	}
	view, err := yamlview.Parse(context.Background(), snapshot)
	if err != nil {
		t.Fatal(err)
	}
	if len(view.Issues()) > 0 {
		t.Fatalf("fixture parse issues: %+v", view.Issues())
	}
	var documents []Document
	for _, parsed := range view.Documents() {
		if !parsed.Complete() {
			t.Fatal("fixture has incomplete document")
		}
		raw, err := documentText(snapshot, parsed.Root().Span())
		if err != nil {
			t.Fatal(err)
		}
		projected, err := projectValues(context.Background(), raw, parsed.Root())
		if err != nil {
			t.Fatal(err)
		}
		value := object(projected.Value)
		documents = append(documents, Document{Snapshot: snapshot, Root: parsed.Root(), Origin: projected.Origin, ProjectionNodes: projected.Nodes, Value: value, Kind: literal(value["kind"]), Target: "3.5.0"})
	}
	return documents
}

func summaryDiagnosticSet(diagnostics []Diagnostic) []string {
	out := []string{}
	for _, diagnostic := range diagnostics {
		data, _ := json.Marshal(diagnostic)
		out = append(out, string(data))
	}
	sort.Strings(out)
	return out
}

func assertSummaryDomain(t *testing.T, docs []Document, options DomainOptions) Summary {
	t.Helper()
	summary := Summarize(docs, options.ControlPlaneNamespace)
	want, got := summaryDiagnosticSet(Domain(docs, options)), summaryDiagnosticSet(Domain(summary.Documents, options))
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("detached Domain differs\nwant: %v\ngot:  %v", want, got)
	}
	return summary
}

func TestSummaryPreservesPolicyKnownnessAndSourceChecks(t *testing.T) {
	const resources = `apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata: {name: platform, namespace: argocd}
spec:
  sourceRepos: [https://allowed.example/repo.git, '!https://blocked.example/*']
  destinations: [{name: named-cluster, server: https://cluster, namespace: '*'}]
---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata: {name: sample, namespace: applications}
spec:
  project: platform
  destination: {name: named, server: https://other, namespace: production}
  source: {repoURL: https://ignored.example/repo, path: ignored}
  sources:
    - repoURL: https://actual.example/repo
      targetRevision: main
      path: missing
      ref: values
      directory: {}
      helm:
        valuesObject: {large: irrelevant}
        valueFiles: ['$unknown/values.yaml']
        fileParameters: [{name: config, path: '$absent/config.yaml'}]
    - repoURL: https://actual.example/repo
      ref: values
      chart: chart
`
	options := DomainOptions{ControlPlaneNamespace: "argocd", ReferencePolicy: "closed", Complete: true, LocalPaths: map[string]map[string]bool{"https://actual.example/repo\x00main": {"missing": false}}}
	docs := summaryDocuments(t, resources)
	summary := assertSummaryDomain(t, docs, options)
	codes := map[string]int{}
	for _, d := range Domain(summary.Documents, options) {
		codes[d.Code]++
	}
	for _, code := range []string{"CD_SOURCE_IGNORED", "CD_SOURCE_REF_DUPLICATE", "CD_SOURCE_OPTIONS_CONFLICT", "CD_VALUES_REF_MISSING", "CD_VALUES_REF_INVALID", "CD_DESTINATION_CONFLICT", "CD_LOCAL_PATH_MISSING"} {
		if codes[code] == 0 {
			t.Fatalf("fixture did not exercise %s: %v", code, codes)
		}
	}
	if codes["CD_PROJECT_POLICY_CONTRADICTION"] != 0 {
		t.Fatal("complex/named Project policies must remain unknown")
	}
	strict := strings.ReplaceAll(resources, "[https://allowed.example/repo.git, '!https://blocked.example/*']", "[https://allowed.example/repo.git]")
	strict = strings.ReplaceAll(strict, "[{name: named-cluster, server: https://cluster, namespace: '*'}]", "[{server: https://cluster, namespace: development}]")
	strictSummary := assertSummaryDomain(t, summaryDocuments(t, strict), options)
	contradictions := 0
	for _, d := range Domain(strictSummary.Documents, options) {
		if d.Code == "CD_PROJECT_POLICY_CONTRADICTION" {
			contradictions++
		}
	}
	if contradictions != 2 || strictSummary.Facts.SemanticDigest == summary.Facts.SemanticDigest {
		t.Fatalf("exact policy changes must invalidate and expose both contradictions, got %d", contradictions)
	}
	for _, namespace := range []string{"", "other"} {
		for _, complete := range []bool{false, true} {
			assertSummaryDomain(t, docs, DomainOptions{ControlPlaneNamespace: namespace, ReferencePolicy: "closed", Complete: complete})
		}
	}
}

func TestSummaryKeepsDerivedDuplicateAndReferenceOnlyFacts(t *testing.T) {
	docs := summaryDocuments(t, `apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata: {name: shared, namespace: argocd}
spec: {sourceRepos: ['*'], destinations: []}
---
apiVersion: argoproj.io/v1alpha1
kind: AppProject
projectMetadata: &meta {name: shared, namespace: argocd}
metadata: *meta
spec: {sourceRepos: ['*'], destinations: []}
---
apiVersion: argoproj.io/v1alpha1
kind: Application
projectName: &project shared
spec: {project: *project}
`)
	summary := assertSummaryDomain(t, docs, DomainOptions{ControlPlaneNamespace: "argocd", Complete: true})
	if docs[1].exact([]string{"metadata", "name"}) || summary.Documents[1].exact([]string{"metadata", "name"}) {
		t.Fatal("alias declaration gained an exact navigation location")
	}
	if len(summary.Facts.References) != 0 || !reflect.DeepEqual(summary.Facts.Dependencies, []index.Key{ProjectKey("argocd", "shared")}) {
		t.Fatalf("derived use must retain dependency without a fabricated location: %+v", summary.Facts)
	}
	store, err := index.New(index.Limits{Files: 10, Symbols: 20, References: 20, Bytes: 10000})
	if err != nil {
		t.Fatal(err)
	}
	ticket, err := store.Begin(docs[0].Snapshot.URI(), "scope", 7)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.Commit(ticket, summary.Facts); err != nil {
		t.Fatal(err)
	}
	for _, namespace := range []string{"argocd", ""} {
		definitions, _ := store.Definitions("scope", ProjectKey(namespace, "shared"))
		if len(definitions) != 2 {
			t.Fatalf("namespace %q lost a semantic duplicate: %+v", namespace, definitions)
		}
	}
	if ProjectKey("a", "bc") == ProjectKey("ab", "c") || ProjectKey("a\x00b", "c") == ProjectKey("a", "b\x00c") {
		t.Fatal("project key aliases different identities")
	}
}

func TestSummaryPreservesEffectiveApplicationSetVariants(t *testing.T) {
	docs := summaryDocuments(t, `apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata: {name: generated, namespace: argocd}
spec:
  goTemplate: true
  generators:
    - list: {elements: [{name: a}]}
    - list:
        elements: [{name: b}]
        template:
          spec:
            project: override
            source: {repoURL: https://override.example/repo, path: '{{.path}}'}
  template:
    metadata: {name: '{{.name}}'}
    spec:
      project: base
      source: {repoURL: https://base.example/repo, path: original, targetRevision: '{{.revision}}'}
  templatePatch: |
    spec:
      project: ignored-protected-project
      source: {path: services, targetRevision: main}
`)
	summary := assertSummaryDomain(t, docs, DomainOptions{ControlPlaneNamespace: "argocd", ReferencePolicy: "closed", Complete: true})
	variants, templateDiagnostics, ok := summary.Documents[0].compactVariants()
	if !ok || len(variants) != 2 || len(templateDiagnostics) == 0 {
		t.Fatalf("effective variants or dynamic diagnostics lost: %d variants, %d diagnostics", len(variants), len(templateDiagnostics))
	}
	keys := map[index.Key]bool{}
	for _, use := range summary.Facts.References {
		keys[use.Target] = true
	}
	for _, key := range summary.Facts.Dependencies {
		keys[key] = true
	}
	if len(keys) != 2 || !keys[ProjectKey("argocd", "base")] || !keys[ProjectKey("argocd", "override")] {
		t.Fatalf("project dependencies ignored generator override/protected project: %v", keys)
	}
	wantRepositories := []RepositoryReference{{"https://base.example/repo", "main", "services"}, {"https://override.example/repo", "main", "services"}}
	if !reflect.DeepEqual(summary.Repositories, wantRepositories) {
		t.Fatalf("effective repository dependencies differ: %+v", summary.Repositories)
	}
	for i := 0; i < 30; i++ {
		if summary.Facts.SemanticDigest != Summarize(docs, "argocd").Facts.SemanticDigest {
			t.Fatal("map iteration order changed the ApplicationSet semantic digest")
		}
	}
	for _, variant := range variants {
		if variant.Snapshot.Text() != "" || variant.Origin != nil || variant.Root.Kind() != yamlview.Empty || len(variant.OverrideOrigins) != 0 {
			t.Fatal("effective variant retained source/projection graph")
		}
	}
	dynamic := summaryDocuments(t, `apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata: {name: dynamic}
spec:
  goTemplate: true
  template:
    metadata: {name: '{{.name}}'}
    spec:
      project: '{{.project}}'
      sources:
        - repoURL: https://example/repo
          ref: '{{.ref}}'
          helm: {valueFiles: ['$unknown/values.yaml']}
`)
	dynamicSummary := assertSummaryDomain(t, dynamic, DomainOptions{ControlPlaneNamespace: "argocd", Complete: true})
	if len(dynamicSummary.Facts.References)+len(dynamicSummary.Facts.Dependencies) != 0 {
		t.Fatal("dynamic project created a fabricated static dependency")
	}
	for _, diagnostic := range Domain(dynamicSummary.Documents, DomainOptions{ControlPlaneNamespace: "argocd", Complete: true}) {
		if strings.Contains(diagnostic.Message, "CD_VALUES_REF_MISSING") || strings.Contains(diagnostic.Message, "CD_PROJECT_UNRESOLVED") {
			t.Fatalf("unknown template value became a proven missing target: %+v", diagnostic)
		}
	}
}

func TestSummarySeparatesMeaningFromPosition(t *testing.T) {
	const text = `apiVersion: argoproj.io/v1alpha1
kind: Application
metadata: {name: sample}
spec:
  project: platform
  source: {repoURL: https://example/repo, path: services}
`
	before := Summarize(summaryDocuments(t, text), "argocd")
	after := Summarize(summaryDocuments(t, "# 🚀 comment-only edit\n"+text), "argocd")
	if before.Facts.SemanticDigest != after.Facts.SemanticDigest {
		t.Fatal("comment/location edit changed domain meaning")
	}
	if after.Documents[0].rangeAt([]string{"spec", "project"}).Start.Line != before.Documents[0].rangeAt([]string{"spec", "project"}).Start.Line+1 || before.Facts.References[0].Span == after.Facts.References[0].Span {
		t.Fatal("unchanged digest retained stale source locations")
	}
	changed := Summarize(summaryDocuments(t, strings.ReplaceAll(text, "project: platform", "project: other")), "argocd")
	if changed.Facts.SemanticDigest == before.Facts.SemanticDigest {
		t.Fatal("changed dependency did not change domain meaning")
	}
	if before.Facts.SemanticDigest != Summarize(before.Documents, "argocd").Facts.SemanticDigest {
		t.Fatal("summarizing an existing detached view changed its meaning")
	}
}

func TestSummaryDoesNotRetainIgnoredPayloadOrBorrowedStrings(t *testing.T) {
	text := "sample-name https://example/repository services main " + strings.Repeat("x", 1<<20)
	snapshot, err := source.New("file:///large.yaml", 1, text)
	if err != nil {
		t.Fatal(err)
	}
	parts := strings.SplitN(text, " ", 5)
	doc := Document{Snapshot: snapshot, Kind: "Application", Target: "3.5.0", SchemaData: map[string]any{"retain": text}, Value: map[string]any{
		"metadata": map[string]any{"name": parts[0], "annotations": map[string]any{"payload": text}},
		"spec":     map[string]any{"project": parts[0], "source": map[string]any{"repoURL": parts[1], "path": parts[2], "targetRevision": parts[3], "helm": map[string]any{"valuesObject": map[string]any{"payload": text}}}},
	}}
	summary := Summarize([]Document{doc}, "argocd")
	detached := summary.Documents[0]
	if summary.Bytes > 32*1024 || detached.Snapshot.Text() != "" || detached.Root.Kind() != yamlview.Empty || detached.Origin != nil || detached.SchemaData != nil || detached.CompiledSchema != nil || len(detached.OverrideOrigins) != 0 {
		t.Fatalf("detached summary retained full-document state: logical bytes=%d", summary.Bytes)
	}
	start := uintptr(unsafe.Pointer(unsafe.StringData(text)))
	end := start + uintptr(len(text))
	var check func(any)
	check = func(value any) {
		switch v := value.(type) {
		case string:
			pointer := uintptr(unsafe.Pointer(unsafe.StringData(v)))
			if len(v) > 0 && pointer >= start && pointer < end {
				t.Fatalf("retained string %q still borrows the large input", v)
			}
		case map[string]any:
			for key, child := range v {
				check(key)
				check(child)
			}
		case []any:
			for _, child := range v {
				check(child)
			}
		}
	}
	check(detached.Value)
	for _, ref := range summary.Repositories {
		check(ref.RepoURL)
		check(ref.Path)
		check(ref.TargetRevision)
	}
	object(doc.Value["metadata"])["name"] = "mutated"
	object(object(doc.Value["spec"])["source"])["repoURL"] = "mutated"
	if literal(object(detached.Value["metadata"])["name"]) != "sample-name" || summary.Repositories[0].RepoURL != "https://example/repository" {
		t.Fatal("summary borrowed mutable input containers")
	}
}
