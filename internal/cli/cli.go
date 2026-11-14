// Package cli exposes local analysis and explicit rendering through public reports.
package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"github.com/lazysnail1024/zed-argocd/internal/analysis"
	"github.com/lazysnail1024/zed-argocd/internal/lsp"
	"github.com/lazysnail1024/zed-argocd/internal/workspace"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/lazysnail1024/zed-argocd/internal/assets"
	"github.com/lazysnail1024/zed-argocd/internal/buildinfo"
)

type ConfigError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

type Report struct {
	Renderer            *RenderInfo           `json:"renderer,omitempty"`
	ResourcesAnalyzed   int                   `json:"resourcesAnalyzed"`
	ReportSchemaVersion int                   `json:"reportSchemaVersion"`
	Product             string                `json:"product"`
	ToolVersion         string                `json:"toolVersion"`
	CatalogHash         *string               `json:"catalogHash"`
	InputSource         string                `json:"inputSource"`
	ValidationScope     string                `json:"validationScope"`
	AnalysisComplete    bool                  `json:"analysisComplete"`
	NotEvaluated        []string              `json:"notEvaluated"`
	FilesAnalyzed       int                   `json:"filesAnalyzed"`
	Targets             []workspace.Target    `json:"targets"`
	Diagnostics         []analysis.Diagnostic `json:"diagnostics"`
	ConfigurationErrors []ConfigError         `json:"configurationErrors"`
	ElapsedMilliseconds int64                 `json:"elapsedMilliseconds"`
}

func Run(args []string, out, errOut io.Writer) int {
	if len(args) == 0 || args[0] == "help" || args[0] == "--help" || args[0] == "-h" {
		if _, err := fmt.Fprintln(out, "argocd-ls (development)\nCommands:\n  version [--json]\n  check [--root DIR] [--scope NAME] [--file PATH] [--target-version VERSION] [--format json|human]\n  serve --stdio\n\nOffline local schema and domain analysis; no cluster connection or official Argo CD lint."); err != nil {
			fmt.Fprintln(errOut, err)
			return 2
		}
		return 0
	}
	switch args[0] {
	case "version":
		fs := flag.NewFlagSet("version", flag.ContinueOnError)
		fs.SetOutput(errOut)
		asJSON := fs.Bool("json", false, "emit build identity")
		if err := fs.Parse(args[1:]); err != nil || fs.NArg() != 0 {
			return 2
		}
		if *asJSON {
			if err := json.NewEncoder(out).Encode(buildinfo.Current()); err != nil {
				fmt.Fprintln(errOut, err)
				return 2
			}
		} else {
			if _, err := fmt.Fprintf(out, "%s %s (local editing contract; development build)\n", buildinfo.Server, buildinfo.ServerVersion); err != nil {
				fmt.Fprintln(errOut, err)
				return 2
			}
		}
		return 0
	case "explain-config":
		return explainConfig(args[1:], out, errOut)
	case "render-check":
		return renderCheck(args[1:], out, errOut)
	case "check":
		return check(args[1:], out, errOut)
	case "serve":
		fs := flag.NewFlagSet("serve", flag.ContinueOnError)
		fs.SetOutput(errOut)
		stdio := fs.Bool("stdio", false, "stdio transport")
		if err := fs.Parse(args[1:]); err != nil || fs.NArg() != 0 {
			return 2
		}
		if !*stdio {
			fmt.Fprintln(errOut, "serve requires --stdio")
			return 2
		}
		return lsp.Run(os.Stdin, out, errOut, func(ctx context.Context, root, file, target string) (lsp.RenderResult, error) {
			var output, errors bytes.Buffer
			args := []string{"--root", root, "--file", file, "--format", "json"}
			if target != "" {
				args = append(args, "--target-version", target)
			}
			exit := renderCheckContext(ctx, args, &output, &errors)
			var report Report
			if err := json.Unmarshal(output.Bytes(), &report); err != nil || exit >= 2 || report.Renderer == nil {
				message := strings.TrimSpace(errors.String())
				if message == "" {
					message = fmt.Sprintf("render-check failed with exit %d: %s", exit, output.String())
				}
				return lsp.RenderResult{}, fmt.Errorf("%s", message)
			}
			return lsp.RenderResult{Output: report.Renderer.Output, Report: filepath.Join(filepath.Dir(report.Renderer.Output), "report.json"), Message: fmt.Sprintf("Argo CD render-check: analysisComplete=%v, %d diagnostics. Generated YAML: %s; report: %s", report.AnalysisComplete, len(report.Diagnostics), report.Renderer.Output, filepath.Join(filepath.Dir(report.Renderer.Output), "report.json"))}, nil
		})
	default:
		fmt.Fprintf(errOut, "Unknown or unimplemented command %q. Run argocd-ls help.\n", args[0])
		return 2
	}
}

func check(args []string, out, errOut io.Writer) int {
	fs := flag.NewFlagSet("check", flag.ContinueOnError)
	fs.SetOutput(errOut)
	format := fs.String("format", "human", "json or human")
	root := fs.String("root", ".", "workspace root")
	scope := fs.String("scope", "", "scope selection")
	file := fs.String("file", "", "file selection")
	target := fs.String("target-version", "", "target override")
	warningsAsErrors := fs.Bool("warnings-as-errors", false, "treat warnings as errors")
	if err := fs.Parse(args); err != nil || fs.NArg() != 0 {
		return 2
	}
	if *format != "json" && *format != "human" {
		fmt.Fprintln(errOut, "format must be json or human")
		return 2
	}
	report, exit := checkReport(*root, *scope, *file, *target, *warningsAsErrors)
	return writeReport(report, *format, out, errOut, exit)
}

func checkReport(root, scope, file, target string, warningsAsErrors bool) (Report, int) {
	start := time.Now()
	catalogHash := assets.CatalogHash()
	r := Report{ReportSchemaVersion: 1, Product: buildinfo.Product, ToolVersion: buildinfo.ServerVersion, CatalogHash: &catalogHash, InputSource: "filesystem", ValidationScope: "local-editing-contract", NotEvaluated: []string{"controller-admission-and-runtime", "source-hydration", "helm-value-file-expansion", "template-function-availability", "templatePatch-effects", "controller-generators", "complex-project-policy", "CEL"}, Targets: []workspace.Target{}, Diagnostics: []analysis.Diagnostic{}, ConfigurationErrors: []ConfigError{}}
	signalCtx, stopSignal := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stopSignal()
	ctx, cancel := context.WithTimeout(signalCtx, 5*time.Second)
	defer cancel()
	input, loadErr := workspace.Load(ctx, root, scope, file, target)
	if loadErr != nil {
		code := "INPUT_INCOMPLETE"
		if e, ok := loadErr.(*workspace.Error); ok {
			code = e.Code
		}
		r.ConfigurationErrors = append(r.ConfigurationErrors, ConfigError{Code: code, Message: loadErr.Error()})
	} else {
		documents := map[string][]analysis.Document{}
		scopeIncomplete := map[string]bool{}
		scopeCounts := map[string][2]int{}
		outputs := map[string]bool{}
		analyzer := new(analysis.Analyzer)
		if len(input.Skipped) > 0 {
			r.NotEvaluated = append(r.NotEvaluated, "unrendered-helm-templates")
		}
		sort.SliceStable(input.Files, func(i, j int) bool {
			if input.Files[i].Target.Scope != input.Files[j].Target.Scope {
				return input.Files[i].Target.Scope < input.Files[j].Target.Scope
			}
			return input.Files[i].Snapshot.URI() < input.Files[j].Snapshot.URI()
		})
		parsedFiles, stopParsing := parseFiles(ctx, input.Files)
		defer stopParsing()
		for _, f := range input.Files {
			if ctx.Err() != nil {
				break
			}
			outputs[string(f.Snapshot.URI())] = f.Output
			parsed, ok := <-parsedFiles
			if !ok {
				break
			}
			result, e := analysis.Result{}, parsed.err
			if e == nil {
				result, e = analyzer.AnalyzeParsed(ctx, parsed.file, f.Target.Version)
			}
			if e != nil {
				scopeIncomplete[f.Target.Scope] = true
				r.ConfigurationErrors = append(r.ConfigurationErrors, ConfigError{Code: "ANALYSIS_INCOMPLETE", Message: e.Error()})
				continue
			}
			if result.Incomplete {
				scopeIncomplete[f.Target.Scope] = true
			}
			documents[f.Target.Scope] = append(documents[f.Target.Scope], result.Documents...)
			counts := scopeCounts[f.Target.Scope]
			if !result.Incomplete {
				counts[0]++
			}
			counts[1] += len(result.Documents)
			scopeCounts[f.Target.Scope] = counts
			if f.Output {
				for _, d := range result.Diagnostics {
					d.TargetID = f.Target.ID
					r.Diagnostics = append(r.Diagnostics, d)
				}
			}
		}
		for _, target := range input.Targets {
			scope := target.Scope
			docs := documents[scope]
			for _, d := range analysis.Domain(docs, analysis.DomainOptions{Context: ctx, LocalPaths: input.LocalPaths, ControlPlaneNamespace: input.Config.ControlPlaneNamespace, ReferencePolicy: input.Config.ReferencePolicy, Complete: input.Complete && !scopeIncomplete[scope]}) {
				if outputs[d.URI] {
					d.TargetID = target.ID
					r.Diagnostics = append(r.Diagnostics, d)
				}
			}
			target.Complete = ctx.Err() == nil && input.Complete && !scopeIncomplete[scope] && assets.Verified(target.Version)
			r.Targets = append(r.Targets, target)
			if ctx.Err() == nil {
				counts := scopeCounts[scope]
				r.FilesAnalyzed += counts[0]
				r.ResourcesAnalyzed += counts[1]
			}
			if !assets.Verified(target.Version) {
				r.ConfigurationErrors = append(r.ConfigurationErrors, ConfigError{Code: "TARGET_NOT_VERIFIED", Message: "selected target local editing contract is not verified"})
			}
		}
	}

	r.AnalysisComplete = loadErr == nil && len(r.ConfigurationErrors) == 0
	for _, target := range r.Targets {
		r.AnalysisComplete = r.AnalysisComplete && target.Complete
	}
	exit := 2
	if r.AnalysisComplete {
		exit = 0
		for _, d := range r.Diagnostics {
			if d.Severity == 1 || (warningsAsErrors && d.Severity == 2) {
				exit = 1
			}
		}
	}
	if e := ctx.Err(); e != nil {
		r.AnalysisComplete = false
		exit = 3
		code := "ANALYSIS_CANCELLED"
		if errors.Is(e, context.DeadlineExceeded) {
			code = "ANALYSIS_DEADLINE_EXCEEDED"
		}
		filtered := r.ConfigurationErrors[:0]
		for _, item := range r.ConfigurationErrors {
			if item.Code != "TARGET_NOT_VERIFIED" && item.Code != "ANALYSIS_INCOMPLETE" {
				filtered = append(filtered, item)
			}
		}
		r.ConfigurationErrors = append(filtered, ConfigError{Code: code, Message: e.Error()})
	}
	r.ElapsedMilliseconds = time.Since(start).Milliseconds()
	return r, exit
}

func writeReport(r Report, format string, out, errOut io.Writer, exit int) int {
	var writeErr error
	if format == "json" {
		writeErr = json.NewEncoder(out).Encode(r)
	} else {
		state := "incomplete"
		if r.AnalysisComplete {
			state = "complete"
		}
		_, writeErr = fmt.Fprintf(out, "Files analyzed: %d; resources analyzed: %d; analysis %s.\n", r.FilesAnalyzed, r.ResourcesAnalyzed, state)
		for _, e := range r.ConfigurationErrors {
			if writeErr == nil {
				_, writeErr = fmt.Fprintf(out, "%s: %s\n", e.Code, e.Message)
			}
		}
		for _, d := range r.Diagnostics {
			if writeErr == nil {
				_, writeErr = fmt.Fprintln(out, d)
			}
		}
	}
	if writeErr != nil {
		fmt.Fprintln(errOut, writeErr)
		if exit != 3 {
			return 2
		}
	}
	return exit
}

func explainConfig(args []string, out, errOut io.Writer) int {
	fs := flag.NewFlagSet("explain-config", flag.ContinueOnError)
	fs.SetOutput(errOut)
	root := fs.String("root", ".", "workspace root")
	scope := fs.String("scope", "", "scope selection")
	target := fs.String("target-version", "", "exact target override")
	if fs.Parse(args) != nil || fs.NArg() != 0 {
		return 2
	}
	signalCtx, stopSignal := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stopSignal()
	ctx, cancel := context.WithTimeout(signalCtx, 5*time.Second)
	defer cancel()
	input, e := workspace.Load(ctx, *root, *scope, "", *target)
	if e != nil {
		fmt.Fprintln(errOut, e)
		if ctx.Err() != nil {
			return 3
		}
		return 2
	}
	if e = json.NewEncoder(out).Encode(map[string]any{"product": buildinfo.Product, "schemaVersion": 1, "root": input.Root, "config": input.Config, "targets": input.Targets, "analysisReady": buildinfo.Current().AnalysisReady}); e != nil {
		fmt.Fprintln(errOut, e)
		return 2
	}
	return 0
}
