package cli

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"time"

	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/zed-argocd/internal/analysis"
	"github.com/lazysnail1024/zed-argocd/internal/workspace"
)

func explainCompatibility(args []string, out, errOut io.Writer) int {
	fs := flag.NewFlagSet("explain-compatibility", flag.ContinueOnError)
	fs.SetOutput(errOut)
	root := fs.String("root", ".", "workspace root")
	file := fs.String("file", "", "one YAML file to explain")
	target := fs.String("target-version", "", "selected target override")
	if !parseFlags(fs, args, errOut) {
		return 2
	}
	if *file == "" {
		fmt.Fprintln(errOut, "explain-compatibility requires --file PATH")
		return 2
	}
	signalCtx, stopSignal := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stopSignal()
	ctx, cancel := context.WithTimeout(signalCtx, 5*time.Second)
	defer cancel()
	fail := func(err error) int {
		fmt.Fprintln(errOut, err)
		if ctx.Err() != nil {
			return 3
		}
		return 2
	}
	catalog, err := workspace.Prepare(ctx, *root, *target, nil, nil)
	if err != nil {
		return fail(err)
	}
	selected := *file
	if !filepath.IsAbs(selected) {
		selected = filepath.Join(catalog.Input.Root, selected)
	}
	selected, err = filepath.EvalSymlinks(selected)
	if err != nil {
		return fail(err)
	}
	selected, err = filepath.Abs(selected)
	if err != nil {
		return fail(err)
	}
	// Explicit explanation may inspect an excluded file as an isolated input,
	// but cannot acquire a file outside its configured owning root.
	selection, included, err := catalog.Select(selected, true)
	if err != nil {
		return fail(err)
	}
	if !included {
		return fail(fmt.Errorf("selected file is outside the workspace root"))
	}
	text, err := workspace.ReadFile(ctx, selected)
	if err != nil {
		return fail(err)
	}
	snapshot, err := source.New(source.URI(workspace.FileURI(selected)), 1, text)
	if err != nil {
		return fail(err)
	}
	analyzer := new(analysis.CompatibilityAnalyzer)
	checked := analysis.Result{}
	skipped := workspace.IsHelmTemplate(catalog.Input.Root, selected, snapshot)
	if skipped {
		checked.Incomplete = true
	} else {
		checked, err = analyzer.Analyze(ctx, snapshot)
		if err != nil {
			return fail(err)
		}
	}
	var trace io.Writer
	if os.Getenv("ARGOCD_LS_TRACE") == "1" {
		trace = errOut
	}
	report, err := analyzer.Explain(ctx, checked, selection.Version, trace)
	if err != nil {
		return fail(err)
	}
	if skipped {
		report.Unknowns = append(report.Unknowns, analysis.CompatibilityUnknown{Code: "UNRENDERED_HELM_TEMPLATE", Message: "Render this Helm template explicitly before explaining the generated resources"})
	}
	hash := sha256.Sum256([]byte(text))
	report.Root, report.Scope = catalog.Input.Root, selection.Scope
	report.InputIdentity = analysis.CompatibilityInput{Source: "disk", URI: string(snapshot.URI()), SHA256: hex.EncodeToString(hash[:]), ConfigHash: selection.ConfigHash, ConfigSource: "disk-or-defaults", TargetVersion: selection.Version, TargetSource: selection.Source}
	if err = json.NewEncoder(out).Encode(report); err != nil {
		return fail(err)
	}
	if !report.Complete {
		return 2
	}
	return 0
}
