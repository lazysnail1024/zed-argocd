package cli

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"github.com/lazysnail1024/zed-argocd/internal/analysis"
	"github.com/lazysnail1024/zed-argocd/internal/assets"
	"github.com/lazysnail1024/zed-argocd/internal/buildinfo"
	"github.com/lazysnail1024/zed-argocd/internal/rendercache"
	"github.com/lazysnail1024/zed-argocd/internal/workspace"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

type RenderInfo struct {
	Output       string                         `json:"output"`
	Directory    string                         `json:"directory"`
	Profile      string                         `json:"profile"`
	ValuesFiles  []workspace.RenderFileIdentity `json:"valuesFiles"`
	Input        *workspace.RenderRequest       `json:"input"`
	Binary       string                         `json:"binary"`
	BinarySHA256 string                         `json:"binarySHA256"`
	Version      string                         `json:"version"`
	Command      []string                       `json:"command"`
	ReleaseName  string                         `json:"releaseName"`
	Namespace    string                         `json:"namespace"`
	KubeVersion  string                         `json:"kubeVersion"`
	APIVersions  []string                       `json:"apiVersions"`
	Chart        string                         `json:"chart"`
	Values       string                         `json:"values"`
	Stdout       string                         `json:"stdout"`
	Stderr       string                         `json:"stderr"`
	Exit         int                            `json:"exit"`
}
type stringFlags []string

func (v *stringFlags) String() string     { return strings.Join(*v, ",") }
func (v *stringFlags) Set(s string) error { *v = append(*v, s); return nil }

type outputBudget struct {
	mu        sync.Mutex
	remaining int
	exceeded  bool
	cancel    context.CancelFunc
}
type boundedOutput struct {
	buffer bytes.Buffer
	budget *outputBudget
}

func (b *boundedOutput) Bytes() []byte { return b.buffer.Bytes() }
func (b *boundedOutput) Write(p []byte) (int, error) {
	n := len(p)
	b.budget.mu.Lock()
	defer b.budget.mu.Unlock()
	if len(p) > b.budget.remaining {
		b.budget.exceeded = true
		p = p[:b.budget.remaining]
		b.budget.cancel()
	}
	b.budget.remaining -= len(p)
	_, _ = b.buffer.Write(p)
	return n, nil
}

func renderCheck(args []string, out, errOut io.Writer) int {
	return renderCheckContext(context.Background(), args, out, errOut)
}

func renderCheckContext(parent context.Context, args []string, out, errOut io.Writer) int {
	fs := flag.NewFlagSet("render-check", flag.ContinueOnError)
	fs.SetOutput(errOut)
	root := fs.String("root", ".", "source workspace")
	scope := fs.String("scope", "", "source scope")
	target := fs.String("target-version", "", "exact target")
	chart := fs.String("chart", "", "local chart (default: nearest Chart.yaml)")
	file := fs.String("file", "", "current source file for chart discovery")
	profile := fs.String("profile", "", "named render profile from schemaVersion 2 configuration")
	helm := fs.String("helm-binary", "", "Helm 3 or 4 binary (default: helm on PATH)")
	output := fs.String("output", "", "generated directory (default: owned user cache)")
	format := fs.String("format", "human", "json or human")
	warningsAsErrors := fs.Bool("warnings-as-errors", false, "treat warnings as errors")
	release := fs.String("release-name", "argo-ls", "release name")
	namespace := fs.String("namespace", "default", "namespace")
	kube := fs.String("kube-version", "1.31.0", "Kubernetes version")
	apis, values := stringFlags{}, stringFlags{}
	fs.Var(&apis, "api-version", "available API version; repeat to preserve order")
	fs.Var(&values, "values", "local values file; repeat in Helm precedence order")
	if !parseFlags(fs, args, errOut) {
		return 2
	}
	if *format != "json" && *format != "human" {
		fmt.Fprintln(errOut, "format must be json or human")
		return 2
	}
	explicitRoot, conflicting := false, ""
	fs.Visit(func(f *flag.Flag) {
		if f.Name == "root" {
			explicitRoot = true
		}
		switch f.Name {
		case "chart", "values", "release-name", "namespace", "kube-version", "api-version":
			conflicting = f.Name
		}
	})
	if *profile != "" && conflicting != "" {
		fmt.Fprintf(errOut, "--profile cannot be combined with render parameter override --%s\n", conflicting)
		return 2
	}
	signalCtx, stopSignal := signal.NotifyContext(parent, os.Interrupt)
	defer stopSignal()
	ctx, cancel := context.WithTimeout(signalCtx, 30*time.Second)
	defer cancel()
	fail := func(err error) int {
		if ctx.Err() == nil {
			fmt.Fprintln(errOut, err)
			return 2
		}
		report, exit := renderFailureReport(ctx, err, nil, "filesystem")
		return writeReport(report, *format, out, errOut, exit)
	}
	// Retain the existing no-root CLI discovery contract for the default action.
	if *profile == "" && *chart == "" {
		if !explicitRoot {
			start := *file
			if start == "" {
				start = "."
			}
			start, err := filepath.Abs(start)
			if err != nil {
				return fail(err)
			}
			found, err := workspace.FindChart(filepath.VolumeName(start)+string(filepath.Separator), start)
			if err != nil {
				return fail(err)
			}
			*root = found
			if *file != "" {
				*file = start
			}
		}
		start := *file
		if start == "" {
			start = "."
		}
		found, err := workspace.FindChart(*root, start)
		if err != nil {
			return fail(err)
		}
		*chart = found
	}
	request, err := workspace.ResolveRenderRequest(ctx, workspace.RenderOptions{Root: *root, Scope: *scope, File: *file, Chart: *chart, Profile: *profile, Target: *target, ReleaseName: *release, Namespace: *namespace, KubeVersion: *kube, Values: values, APIVersions: apis})
	if err != nil {
		return fail(err)
	}
	report, exit, err := executeRender(ctx, request, *helm, *output, *warningsAsErrors)
	if err != nil {
		return fail(err)
	}
	if *format == "human" && report.Renderer != nil && report.Renderer.Output != "" {
		if _, err := fmt.Fprintf(out, "Helm %s generated %s; analysisComplete=%v\n", report.Renderer.Version, report.Renderer.Output, report.AnalysisComplete); err != nil {
			fmt.Fprintln(errOut, err)
			if exit == 3 {
				return 3
			}
			return 2
		}
	}
	return writeReport(report, *format, out, errOut, exit)
}

func renderFailureReport(ctx context.Context, commandErr error, renderer *RenderInfo, inputSource string) (Report, int) {
	exit, reason := 2, "RENDER_FAILED"
	if ctx.Err() != nil {
		exit, reason = 3, "RENDER_CANCELLED"
		if ctx.Err() == context.DeadlineExceeded {
			reason = "RENDER_DEADLINE_EXCEEDED"
		}
	}
	catalogHash := assets.CatalogHash()
	return Report{ReportSchemaVersion: 1, Product: buildinfo.Product, ToolVersion: buildinfo.ServerVersion, CatalogHash: &catalogHash, InputSource: inputSource, ValidationScope: "local-editing-contract", NotEvaluated: []string{"rendered-analysis"}, Targets: []workspace.Target{}, Diagnostics: []analysis.Diagnostic{}, ConfigurationErrors: []ConfigError{{Code: reason, Message: commandErr.Error()}}, Renderer: renderer}, exit
}

// executeRender is the single CLI/editor rendering path. Collection and resolve
// only construct RenderRequest; Helm is selected and invoked here, explicitly.
func executeRender(ctx context.Context, request workspace.RenderRequest, helm, output string, warningsAsErrors bool) (Report, int, error) {
	// Source acquisition keeps the existing cancellation and scope contract. A
	// partial or failed workspace read must not launch an external process.
	if _, err := workspace.Load(ctx, request.Root, request.Target.Scope, request.File, request.Options.Target); err != nil {
		return Report{}, 2, err
	}
	if err := request.Revalidate(ctx); err != nil {
		return Report{}, 2, err
	}
	if output == "" {
		cache, err := os.UserCacheDir()
		if err != nil {
			return Report{}, 2, err
		}
		if _, err := renderOutputPath(filepath.Join(cache, "argocd-ls", "render"), request.Chart); err != nil {
			return Report{}, 2, fmt.Errorf("default render cache placement: %w", err)
		}
	}
	if helm == "" {
		found, err := exec.LookPath("helm")
		if err != nil {
			return Report{}, 2, fmt.Errorf("Helm is unavailable on PATH: %w", err)
		}
		helm = found
	}
	if !filepath.IsAbs(helm) {
		return Report{}, 2, fmt.Errorf("helm binary must be an explicit absolute path")
	}
	if output != "" {
		var err error
		output, err = renderOutputPath(output, request.Chart)
		if err != nil {
			return Report{}, 2, err
		}
		if err := renderOutputInputs(output, request); err != nil {
			return Report{}, 2, err
		}
	}
	temp, err := os.MkdirTemp("", "argocd-helm-environment-")
	if err != nil {
		return Report{}, 2, err
	}
	defer os.RemoveAll(temp)
	env := append(os.Environ(), "HELM_CACHE_HOME="+filepath.Join(temp, "cache"), "HELM_CONFIG_HOME="+filepath.Join(temp, "config"), "HELM_DATA_HOME="+filepath.Join(temp, "data"))
	run := func(arguments []string) ([]byte, []byte, int, error) {
		commandCtx, stop := context.WithCancel(ctx)
		defer stop()
		cmd := exec.CommandContext(commandCtx, helm, arguments...)
		cmd.Env = env
		cmd.WaitDelay = 500 * time.Millisecond
		resume, cleanup, err := prepareCommand(cmd)
		if err != nil {
			return nil, nil, -1, err
		}
		var once sync.Once
		terminate := func() error { var err error; once.Do(func() { err = cleanup() }); return err }
		defer terminate()
		cmd.Cancel = terminate
		budget := &outputBudget{remaining: 10 * 1024 * 1024, cancel: stop}
		stdout, stderr := boundedOutput{budget: budget}, boundedOutput{budget: budget}
		cmd.Stdout, cmd.Stderr = &stdout, &stderr
		err = cmd.Start()
		if err == nil {
			if err = resume(); err != nil {
				_ = cmd.Process.Kill()
				_ = cmd.Wait()
			} else {
				err = cmd.Wait()
			}
		}
		code := -1
		if cmd.ProcessState != nil {
			code = cmd.ProcessState.ExitCode()
		}
		if budget.exceeded {
			err = fmt.Errorf("Helm output exceeds 10 MiB")
		}
		return stdout.Bytes(), stderr.Bytes(), code, err
	}
	info := &RenderInfo{Binary: helm, ReleaseName: request.ReleaseName, Namespace: request.Namespace, KubeVersion: request.KubeVersion, APIVersions: request.APIVersions, Chart: request.Chart, Profile: request.Profile, ValuesFiles: request.ValuesFiles, Input: &request, Exit: -1}
	if len(request.ValuesFiles) == 1 {
		info.Values = request.ValuesFiles[0].Path
	}
	failure := func(command []string, stdout, stderr []byte, code int, err error) (Report, int) {
		info.Command, info.Stdout, info.Stderr, info.Exit = append([]string{helm}, command...), string(stdout), string(stderr), code
		return renderFailureReport(ctx, err, info, "helm-generated")
	}
	versionArgs := []string{"version", "--short"}
	info.BinarySHA256, err = rendererHash(ctx, helm)
	if err != nil {
		report, exit := failure(versionArgs, nil, nil, -1, err)
		return report, exit, nil
	}
	versionBytes, versionStderr, versionCode, err := run(versionArgs)
	if err != nil {
		report, exit := failure(versionArgs, versionBytes, versionStderr, versionCode, err)
		return report, exit, nil
	}
	info.Version = strings.TrimSpace(string(versionBytes))
	if !strings.HasPrefix(info.Version, "v3.") && !strings.HasPrefix(info.Version, "v4.") {
		return Report{}, 2, fmt.Errorf("Helm 3 or 4 is required; found %s", info.Version)
	}
	if err := request.Revalidate(ctx); err != nil {
		return Report{}, 2, err
	}
	currentTool, err := rendererHash(ctx, helm)
	if err != nil || currentTool != info.BinarySHA256 {
		return Report{}, 2, fmt.Errorf("Helm executable changed before rendering")
	}
	command := []string{"template", request.ReleaseName, request.Chart, "--namespace", request.Namespace, "--kube-version", request.KubeVersion, "--include-crds"}
	for _, value := range request.ValuesFiles {
		command = append(command, "--values", value.Path)
	}
	for _, api := range request.APIVersions {
		command = append(command, "--api-versions", api)
	}
	var store *rendercache.Store
	var custom *customRenderOutput
	if output == "" {
		store, err = rendercache.Create()
		if err != nil {
			return Report{}, 2, err
		}
		defer store.Close()
		output = store.Directory
		if _, err := renderOutputPath(output, request.Chart); err != nil {
			return Report{}, 2, fmt.Errorf("default render cache placement changed: %w", err)
		}
	} else {
		custom, err = openCustomRenderOutput(output, request.Chart)
		if err != nil {
			return Report{}, 2, err
		}
		defer custom.root.Close()
	}
	info.Directory = output
	write := func(name string, data []byte) error {
		if store != nil {
			return store.Write(name, data)
		}
		return custom.write(name, data)
	}
	persist := func(report Report) error {
		for _, artifact := range []struct {
			name string
			data []byte
		}{{"helm.stdout", []byte(info.Stdout)}, {"helm.stderr", []byte(info.Stderr)}} {
			if err := write(artifact.name, artifact.data); err != nil {
				return err
			}
		}
		data, err := json.MarshalIndent(report, "", "  ")
		if err != nil {
			return err
		}
		if err := write("report.json", append(data, '\n')); err != nil {
			return err
		}
		return nil
	}
	seal := func() error {
		if store != nil {
			return store.Seal()
		}
		return custom.check()
	}
	artifactFailure := func(err error) (Report, int, error) {
		report, exit := renderFailureReport(ctx, fmt.Errorf("retain render evidence: %w", err), info, "helm-generated")
		return report, exit, nil
	}
	rendered, stderr, code, renderErr := run(command)
	info.Command, info.Stdout, info.Stderr, info.Exit = append([]string{helm}, command...), string(rendered), string(stderr), code
	if renderErr == nil {
		renderErr = ctx.Err()
	}
	if renderErr == nil {
		renderErr = request.Revalidate(ctx)
	}
	if renderErr == nil {
		currentTool, err = rendererHash(ctx, helm)
		if err != nil || currentTool != info.BinarySHA256 {
			renderErr = fmt.Errorf("Helm executable changed while rendering")
		}
	}
	if renderErr != nil {
		report, exit := renderFailureReport(ctx, renderErr, info, "helm-generated")
		if err := persist(report); err != nil {
			return artifactFailure(err)
		}
		if err := seal(); err != nil {
			return artifactFailure(err)
		}
		return report, exit, nil
	}
	info.Output = filepath.Join(output, "generated.yaml")
	if err := write("generated.yaml", rendered); err != nil {
		// The held output may have moved. Preserve raw evidence in the CLI
		// report without redirecting any artifact writes to the new pathname.
		info.Output = ""
		report, exit := renderFailureReport(ctx, err, info, "helm-generated")
		return report, exit, nil
	}
	report, exit := checkReportContext(ctx, output, "", info.Output, request.Target.Version, warningsAsErrors)
	report.Renderer, report.InputSource = info, "helm-generated"
	if err := ctx.Err(); err != nil {
		report, exit = renderFailureReport(ctx, err, info, "helm-generated")
	}
	if err := persist(report); err != nil {
		return artifactFailure(err)
	}
	// Generated analysis and evidence writes are part of the same operation.
	// Recheck the selected inputs before sealing or reporting success.
	finalErr := request.Revalidate(ctx)
	if finalErr == nil {
		finalTool, err := rendererHash(ctx, helm)
		if err != nil || finalTool != info.BinarySHA256 {
			finalErr = fmt.Errorf("Helm executable changed before render publication")
		}
	}
	if finalErr != nil {
		report, exit = renderFailureReport(ctx, finalErr, info, "helm-generated")
		if err := persist(report); err != nil {
			return artifactFailure(err)
		}
	}
	if err := seal(); err != nil {
		return artifactFailure(err)
	}
	return report, exit, nil
}

// Resolve existing ancestors before creating a custom output directory, so an
// alias cannot redirect generated files back into the source chart.
func renderOutputPath(output, chart string) (string, error) {
	absolute, err := filepath.Abs(output)
	if err != nil {
		return "", err
	}
	ancestor, suffix := absolute, []string{}
	for {
		if _, err := os.Lstat(ancestor); err == nil {
			break
		} else if !os.IsNotExist(err) {
			return "", err
		}
		parent := filepath.Dir(ancestor)
		if parent == ancestor {
			return "", fmt.Errorf("output has no existing parent")
		}
		suffix = append(suffix, filepath.Base(ancestor))
		ancestor = parent
	}
	resolved, err := filepath.EvalSymlinks(ancestor)
	if err != nil {
		return "", err
	}
	for i := len(suffix) - 1; i >= 0; i-- {
		resolved = filepath.Join(resolved, suffix[i])
	}
	rel, err := filepath.Rel(chart, resolved)
	if err != nil || rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("render output must be outside the source chart")
	}
	return resolved, nil
}

// Existing regular output leaves may be replaced, but never a render input or
// a hardlink to one. This also covers values selected outside the chart.
func renderOutputInputs(output string, request workspace.RenderRequest) error {
	for _, name := range []string{"generated.yaml", "helm.stdout", "helm.stderr", "report.json"} {
		path := filepath.Join(output, name)
		info, err := os.Lstat(path)
		if err != nil && !os.IsNotExist(err) {
			return err
		}
		if info != nil && (!info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0) {
			return fmt.Errorf("render output must be a regular file: %s", name)
		}
		for _, input := range request.Inputs {
			if path == input.Path {
				return fmt.Errorf("render output would overwrite a selected input: %s", name)
			}
			if info != nil {
				other, _ := os.Stat(input.Path)
				if other != nil && os.SameFile(info, other) {
					return fmt.Errorf("render output aliases a selected input: %s", name)
				}
			}
		}
	}
	return nil
}

func renderCache(args []string, out, errOut io.Writer) int {
	flags := flag.NewFlagSet("render-cache", flag.ContinueOnError)
	flags.SetOutput(errOut)
	remove := flags.String("remove", "", "explicit verified default-cache directory to remove")
	if !parseFlags(flags, args, errOut) {
		return 2
	}
	if *remove != "" {
		if err := rendercache.Remove(*remove); err != nil {
			fmt.Fprintln(errOut, err)
			return 2
		}
		if err := json.NewEncoder(out).Encode(map[string]any{"removed": *remove}); err != nil {
			fmt.Fprintln(errOut, err)
			return 2
		}
		return 0
	}
	entries, err := rendercache.List()
	if err != nil {
		fmt.Fprintln(errOut, err)
		return 2
	}
	if err := json.NewEncoder(out).Encode(map[string]any{"entries": entries}); err != nil {
		fmt.Fprintln(errOut, err)
		return 2
	}
	return 0
}

// Renderer provenance is acquired once from a regular file before execution.
// Failure reporting must never reopen the selected path (which may be invalid).
func rendererHash(ctx context.Context, path string) (string, error) {
	if err := ctx.Err(); err != nil {
		return "", err
	}
	info, err := os.Stat(path)
	if err != nil {
		return "", err
	}
	if !info.Mode().IsRegular() {
		return "", fmt.Errorf("renderer must be a regular file: %s", path)
	}
	if err := ctx.Err(); err != nil {
		return "", err
	}
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	info, err = f.Stat()
	if err != nil {
		return "", err
	}
	if !info.Mode().IsRegular() {
		return "", fmt.Errorf("renderer must be a regular file: %s", path)
	}
	hash := sha256.New()
	buffer := make([]byte, 32*1024)
	for {
		if err := ctx.Err(); err != nil {
			return "", err
		}
		n, err := f.Read(buffer)
		if e := ctx.Err(); e != nil {
			return "", e
		}
		_, _ = hash.Write(buffer[:n])
		if err == io.EOF {
			return hex.EncodeToString(hash.Sum(nil)), nil
		}
		if err != nil {
			return "", err
		}
	}
}
