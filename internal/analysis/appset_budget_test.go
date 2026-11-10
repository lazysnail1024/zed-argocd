package analysis

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	yamlv3 "gopkg.in/yaml.v3"
)

func expansionFixture(generators, keys int, project string) string {
	var text strings.Builder
	text.WriteString("apiVersion: argoproj.io/v1alpha1\nkind: ApplicationSet\nmetadata: {name: bounded}\nspec:\n  generators:\n")
	for i := 0; i < generators; i++ {
		text.WriteString("    - list: {elements: []}\n")
	}
	fmt.Fprintf(&text, "  template:\n    metadata: {name: example}\n    spec:\n      project: %s\n      destination: {server: here, namespace: default}\n      source:\n        repoURL: https://example.org/repo\n        path: .\n        helm:\n          valuesObject:\n", project)
	for i := 0; i < keys; i++ {
		fmt.Fprintf(&text, "            k%05d: value\n", i)
	}
	return text.String()
}

func TestApplicationSetExpansionAdmission(t *testing.T) {
	for _, test := range []struct {
		name       string
		generators int
		keys       int
		project    string
		limited    bool
	}{
		{"ordinary-variants", 16, 2000, "default", false},
		{"generator-times-template", 512, 8000, "default", true},
		{"repeated-large-scalar", 1000, 1, strings.Repeat("x", 70000), true},
	} {
		t.Run(test.name, func(t *testing.T) {
			text := expansionFixture(test.generators, test.keys, test.project)
			if len(text) >= 1024*1024 {
				t.Fatal("fixture should demonstrate amplification from a small input")
			}
			snapshot, err := source.New("file:///appset-budget.yaml", 1, text)
			if err != nil {
				t.Fatal(err)
			}
			result, err := (&Analyzer{}).Analyze(context.Background(), snapshot, "3.0.0")
			if err != nil {
				t.Fatal(err)
			}
			budget := false
			for _, diagnostic := range result.Diagnostics {
				budget = budget || diagnostic.Code == "INPUT_BUDGET_EXCEEDED"
			}
			if result.Incomplete != test.limited || budget != test.limited {
				t.Fatalf("incomplete=%v budget=%v diagnostics=%v", result.Incomplete, budget, result.Diagnostics)
			}
			if test.limited && len(result.Documents) != 0 {
				t.Fatal("an over-budget ApplicationSet must not reach variant construction or complete-scope summaries")
			}
			if !test.limited {
				if len(result.Documents) != 1 {
					t.Fatalf("ordinary ApplicationSet lost: %d documents", len(result.Documents))
				}
				summary := Summarize(result.Documents, "argocd")
				variants, _, ok := summary.Documents[0].compactVariants()
				if !ok || len(variants) != test.generators {
					t.Fatalf("ordinary variants changed: %d", len(variants))
				}
			}
		})
	}
}

func TestApplicationSetBudgetPreservesOtherDocuments(t *testing.T) {
	text := expansionFixture(512, 8000, "default") + "---\napiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata: {name: healthy}\nspec:\n  project: default\n  source: {repoURL: https://example.org/repo, path: .}\n  destination: {server: here, namespace: default}\n"
	snapshot, err := source.New("file:///mixed-budget.yaml", 1, text)
	if err != nil {
		t.Fatal(err)
	}
	result, err := (&Analyzer{}).Analyze(context.Background(), snapshot, "3.0.0")
	if err != nil {
		t.Fatal(err)
	}
	if !result.Incomplete || len(result.Documents) != 1 || result.Documents[0].Kind != "Application" {
		t.Fatalf("partial input discarded the independent document: incomplete=%v documents=%v", result.Incomplete, result.Documents)
	}
}

func TestApplicationSetBudgetAccumulatesPlainDocuments(t *testing.T) {
	for _, test := range []struct {
		name string
		text string
	}{
		{"nodes", expansionFixture(16, 2000, "default")},
		{"strings", expansionFixture(50, 1, strings.Repeat("x", 700000))},
	} {
		t.Run(test.name, func(t *testing.T) {
			snapshot, err := source.New("file:///aggregate-appsets.yaml", 1, test.text+"---\n"+test.text)
			if err != nil {
				t.Fatal(err)
			}
			analyzer := new(Analyzer)
			result, err := analyzer.Analyze(context.Background(), snapshot, "3.0.0")
			if err != nil {
				t.Fatal(err)
			}
			budget := false
			for _, diagnostic := range result.Diagnostics {
				budget = budget || diagnostic.Code == "INPUT_BUDGET_EXCEEDED"
			}
			if !result.Incomplete || !budget || len(result.Documents) != 1 {
				t.Fatalf("individually affordable documents escaped aggregate admission: incomplete=%v budget=%v documents=%d", result.Incomplete, budget, len(result.Documents))
			}
			document := result.Documents[0]
			if document.Origin != nil || !document.exact([]string{"metadata", "name"}) {
				t.Fatal("ordinary exact provenance changed to implement budget accounting")
			}
			if analyzer.ProjectionNodes() != document.ProjectionNodes || analyzer.appSetBytes != document.appSetExpansion.bytes || len(analyzer.appSetProjections) != 1 {
				t.Fatal("the admitted plain document was not charged exactly once")
			}
			if test.name == "strings" && 2*document.ProjectionNodes >= maxProjectionNodes {
				t.Fatal("the string counterexample must fit the aggregate node budget")
			}
		})
	}
}

func TestApplicationSetBudgetReuseIsAtomic(t *testing.T) {
	for _, test := range []struct {
		name string
		text string
	}{
		{"nodes", expansionFixture(16, 2000, "default")},
		{"strings", expansionFixture(50, 1, strings.Repeat("x", 700000))},
	} {
		t.Run(test.name, func(t *testing.T) {
			var documents []Document
			// Identical spans and URI but different revisions must not collapse.
			for revision := source.Revision(1); revision <= 2; revision++ {
				snapshot, err := source.New("file:///reused-appset.yaml", revision, test.text)
				if err != nil {
					t.Fatal(err)
				}
				result, err := (&Analyzer{}).Analyze(context.Background(), snapshot, "3.0.0")
				if err != nil || result.Incomplete || len(result.Documents) != 1 {
					t.Fatalf("single document control failed: err=%v incomplete=%v documents=%d", err, result.Incomplete, len(result.Documents))
				}
				documents = append(documents, result.Documents[0])
			}
			analyzer := new(Analyzer)
			if analyzer.ReuseProjections(documents) || analyzer.projectionNodes != 0 || analyzer.appSetBytes != 0 || len(analyzer.projections) != 0 || len(analyzer.appSetProjections) != 0 {
				t.Fatal("refused aggregate reuse changed the budget ledger")
			}
			first := documents[0]
			if !analyzer.ReuseProjections([]Document{first, first}) || !analyzer.ReuseProjections([]Document{first}) {
				t.Fatal("reusing the same accepted identity twice must stay affordable")
			}
			if analyzer.ProjectionNodes() != first.ProjectionNodes || analyzer.appSetBytes != first.appSetExpansion.bytes || len(analyzer.appSetProjections) != 1 {
				t.Fatal("repeated identity was charged more than once")
			}
			if analyzer.ReuseProjections(documents[1:]) || analyzer.ProjectionNodes() != first.ProjectionNodes || analyzer.appSetBytes != first.appSetExpansion.bytes || len(analyzer.appSetProjections) != 1 {
				t.Fatal("refusal must leave an existing accepted identity unchanged")
			}
			analyzer.BeginFile()
			if analyzer.projectionNodes != 0 || analyzer.appSetBytes != 0 || len(analyzer.appSetProjections) != 0 || !analyzer.ReuseProjections(documents[1:]) {
				t.Fatal("an independent file job did not receive a fresh budget")
			}
		})
	}
}

func TestApplicationSetBudgetReuseKeepsGraphAccounting(t *testing.T) {
	text := expansionFixture(2, 10, "default")
	text = strings.Replace(text, "metadata: {name: bounded}", "metadata: {name: &name bounded}", 1)
	text = strings.Replace(text, "metadata: {name: example}", "metadata: {name: *name}", 1)
	snapshot, err := source.New("file:///graph-appset.yaml", 1, text)
	if err != nil {
		t.Fatal(err)
	}
	result, err := (&Analyzer{}).Analyze(context.Background(), snapshot, "3.0.0")
	if err != nil || result.Incomplete || len(result.Documents) != 1 {
		t.Fatalf("alias control failed: err=%v incomplete=%v documents=%d", err, result.Incomplete, len(result.Documents))
	}
	document := result.Documents[0]
	if document.Origin == nil || document.ProjectionNodes <= document.appSetExpansion.nodes {
		t.Fatal("fixture must contain both a provenance graph and ApplicationSet expansion")
	}
	analyzer := new(Analyzer)
	if !analyzer.ReuseProjections([]Document{document, document}) || !analyzer.ReuseProjections([]Document{document}) || analyzer.ProjectionNodes() != document.ProjectionNodes || analyzer.appSetBytes != document.appSetExpansion.bytes {
		t.Fatal("graph or expansion was counted twice")
	}
}

func TestRepositoryReferencesAdmitsApplicationSetExpansion(t *testing.T) {
	for _, test := range []struct {
		name       string
		generators int
		keys       int
		limited    bool
	}{
		{"ordinary", 16, 10, false},
		{"over-budget", 32, 2000, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			text := expansionFixture(test.generators, test.keys, "default")
			snapshot, err := source.New("file:///repository-appset.yaml", 1, text)
			if err != nil {
				t.Fatal(err)
			}
			var value map[string]any
			if err := yamlv3.Unmarshal([]byte(text), &value); err != nil {
				t.Fatal(err)
			}
			references, err := RepositoryReferences(context.Background(), snapshot, value)
			if test.limited {
				failure, ok := err.(*projectionError)
				if !ok || failure.Code != "INPUT_BUDGET_EXCEEDED" || references != nil {
					t.Fatalf("repository pre-analysis path hid or bypassed refusal: refs=%d err=%v", len(references), err)
				}
			} else if err != nil || len(references) != test.generators {
				t.Fatalf("ordinary references changed: refs=%d err=%v", len(references), err)
			}
			ctx, stop := context.WithCancel(context.Background())
			stop()
			if _, err := RepositoryReferences(ctx, snapshot, value); !errors.Is(err, context.Canceled) {
				t.Fatalf("cancelled extraction continued: %v", err)
			}
		})
	}
}
