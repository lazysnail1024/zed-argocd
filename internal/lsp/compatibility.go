package lsp

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"path/filepath"

	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/zed-argocd/internal/analysis"
	"github.com/lazysnail1024/zed-argocd/internal/workspace"
)

const compatibilityMethod = "argocd/compatibility"
const compatibilityCommand = "argocd.explainCompatibility"
const compatibilityActionKind = "source.argocd.explainCompatibility"

// Configuration and the accepted buffer are the complete input to this local,
// read-only explanation. It deliberately has no workspace index dependency.
func explainCompatibility(ctx context.Context, analyzer *analysis.CompatibilityAnalyzer, m message, previous completed, buffers map[string]buffer, roots []string, override string, trace io.Writer) (analysis.CompatibilityReport, completed, error) {
	report, input := analysis.CompatibilityReport{}, completed{}
	uri := queryURI(m)
	b, opened := buffers[uri]
	if !opened || b.unavailable != "" {
		return report, input, analysis.Refuse("INPUT_UNAVAILABLE", "compatibility explanation requires an accepted open buffer")
	}
	path, err := workspace.PathFromURI(uri)
	if err != nil {
		return report, input, err
	}
	root := workspace.Owner(roots, path)
	if root == "" {
		return report, input, analysis.Refuse("OUTSIDE_WORKSPACE", "document is outside the roots supplied by the client")
	}
	var config *string
	for spelling, buffer := range buffers {
		configPath, _ := workspace.PathFromURI(spelling)
		if configPath == filepath.Join(root, ".argocd-ls.yaml") {
			if buffer.unavailable != "" {
				return report, input, fmt.Errorf("current configuration buffer is unavailable")
			}
			text := buffer.snapshot.Text()
			config = &text
		}
	}
	catalog, err := workspace.Prepare(ctx, root, override, config, roots)
	if err != nil {
		return report, input, err
	}
	file, included, skipped, err := catalog.File(b.snapshot, path, true)
	if err != nil {
		return report, input, err
	}
	if !included {
		return report, input, analysis.Refuse("OUTSIDE_WORKSPACE", "document has no owning workspace target")
	}
	checked, reused := analysis.Result{}, false
	for _, scope := range previous.scopes {
		for _, prior := range scope.files {
			if prior.Snapshot.URI() == b.snapshot.URI() && prior.Snapshot.Revision() == b.snapshot.Revision() && prior.Snapshot.Text() == b.snapshot.Text() && prior.Target == file.Target {
				checked, reused = scope.checked[b.snapshot.URI()]
			}
		}
	}
	if skipped {
		checked = analysis.Result{Incomplete: true}
	} else if !reused {
		checked, err = analyzer.Analyze(ctx, b.snapshot)
		if err != nil {
			return report, input, err
		}
	}
	report, err = analyzer.Explain(ctx, checked, file.Target.Version, trace)
	if err != nil {
		return report, input, err
	}
	if skipped {
		report.Unknowns = append(report.Unknowns, analysis.CompatibilityUnknown{Code: "UNRENDERED_HELM_TEMPLATE", Message: "Render this Helm template explicitly before explaining the generated resources"})
	}
	hash := sha256.Sum256([]byte(b.snapshot.Text()))
	configSource := "disk-or-defaults"
	if config != nil {
		configSource = "buffer"
	}
	report.Root, report.Scope = root, file.Target.Scope
	report.InputIdentity = analysis.CompatibilityInput{Source: "buffer", URI: uri, Version: &b.version, Revision: uint64(b.snapshot.Revision()), SHA256: hex.EncodeToString(hash[:]), ConfigHash: file.Target.ConfigHash, ConfigSource: configSource, TargetVersion: file.Target.Version, TargetSource: file.Target.Source}
	// The existing delivery proof will fence the buffer, configuration, root and
	// disk generation used here. No cross-resource absence assertion is made.
	input.scopes = []scopeResult{{root: root, scope: file.Target.Scope, files: []workspace.File{file}, documents: checked.Documents, checked: map[source.URI]analysis.Result{b.snapshot.URI(): checked}}}
	return report, input, nil
}
