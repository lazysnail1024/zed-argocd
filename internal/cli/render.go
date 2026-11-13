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
	Output       string   `json:"output"`
	Binary       string   `json:"binary"`
	BinarySHA256 string   `json:"binarySHA256"`
	Version      string   `json:"version"`
	Command      []string `json:"command"`
	ReleaseName  string   `json:"releaseName"`
	Namespace    string   `json:"namespace"`
	KubeVersion  string   `json:"kubeVersion"`
	APIVersions  []string `json:"apiVersions"`
	Chart        string   `json:"chart"`
	Values       string   `json:"values"`
	Stdout       string   `json:"stdout"`
	Stderr       string   `json:"stderr"`
	Exit         int      `json:"exit"`
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
	values := fs.String("values", "", "local values file")
	helm := fs.String("helm-binary", "", "Helm 3 or 4 binary (default: helm on PATH)")
	output := fs.String("output", "", "generated directory")
	format := fs.String("format", "human", "json or human")
	warningsAsErrors := fs.Bool("warnings-as-errors", false, "treat warnings as errors")
	release := fs.String("release-name", "argo-ls", "release name")
	namespace := fs.String("namespace", "default", "namespace")
	kube := fs.String("kube-version", "1.31.0", "Kubernetes version")
	apis := stringFlags{}
	fs.Var(&apis, "api-version", "available API version")
	if fs.Parse(args) != nil || fs.NArg() != 0 {
		return 2
	}
	var ctx context.Context
	inputSource := "filesystem"
	fail := func(e error) int {
		if ctx == nil || ctx.Err() == nil {
			fmt.Fprintln(errOut, e)
			return 2
		}
		reason := "RENDER_CANCELLED"
		if ctx.Err() == context.DeadlineExceeded {
			reason = "RENDER_DEADLINE_EXCEEDED"
		}
		catalogHash := assets.CatalogHash()
		report := Report{ReportSchemaVersion: 1, Product: buildinfo.Product, ToolVersion: buildinfo.ServerVersion, CatalogHash: &catalogHash, InputSource: inputSource, ValidationScope: "local-editing-contract", NotEvaluated: []string{"rendered-analysis"}, Targets: []workspace.Target{}, Diagnostics: []analysis.Diagnostic{}, ConfigurationErrors: []ConfigError{{Code: reason, Message: e.Error()}}}
		if *format == "json" {
			if writeErr := json.NewEncoder(out).Encode(report); writeErr != nil {
				fmt.Fprintln(errOut, writeErr)
			}
		} else {
			fmt.Fprintln(errOut, e)
		}
		return 3
	}
	if *helm == "" {
		found, e := exec.LookPath("helm")
		if e != nil {
			return fail(fmt.Errorf("Helm is unavailable on PATH: %w", e))
		}
		*helm = found
	}
	if *chart == "" {
		explicitRoot := false
		fs.Visit(func(f *flag.Flag) {
			if f.Name == "root" {
				explicitRoot = true
			}
		})
		if !explicitRoot {
			start := *file
			if start == "" {
				start = "."
			}
			start, e := filepath.Abs(start)
			if e != nil {
				return fail(e)
			}
			boundary := filepath.VolumeName(start) + string(filepath.Separator)
			found, e := workspace.FindChart(boundary, start)
			if e != nil {
				return fail(e)
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
		found, e := workspace.FindChart(*root, start)
		if e != nil {
			return fail(e)
		}
		*chart = found
	}
	if *format != "json" && *format != "human" {
		return fail(fmt.Errorf("format must be json or human"))
	}
	if !filepath.IsAbs(*helm) {
		return fail(fmt.Errorf("helm binary must be an explicit absolute path"))
	}
	signalCtx, stopSignal := signal.NotifyContext(parent, os.Interrupt)
	defer stopSignal()
	var cancel context.CancelFunc
	ctx, cancel = context.WithTimeout(signalCtx, 30*time.Second)
	defer cancel()
	input, e := workspace.Load(ctx, *root, *scope, *file, *target)
	if e != nil {
		return fail(e)
	}
	if len(input.Targets) != 1 {
		return fail(fmt.Errorf("select exactly one source scope"))
	}
	resolve := func(p string) (string, error) {
		if err := ctx.Err(); err != nil {
			return "", err
		}
		if !filepath.IsAbs(p) {
			p = filepath.Join(input.Root, p)
		}
		p, e := filepath.EvalSymlinks(p)
		if e != nil {
			return "", e
		}
		if err := ctx.Err(); err != nil {
			return "", err
		}
		rel, e := filepath.Rel(input.Root, p)
		if e != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
			return "", fmt.Errorf("render inputs must stay inside source root")
		}
		return p, nil
	}
	chartPath, e := resolve(*chart)
	if e != nil {
		return fail(e)
	}
	valuesPath := ""
	if *values != "" {
		valuesPath, e = resolve(*values)
		if e != nil {
			return fail(e)
		}
	}
	e = filepath.WalkDir(chartPath, func(p string, d os.DirEntry, e error) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		if e != nil {
			return e
		}
		if d.Type()&os.ModeSymlink != 0 {
			_, e = resolve(p)
			return e
		}
		return nil
	})
	if e != nil {
		return fail(e)
	}
	if e := ctx.Err(); e != nil {
		return fail(e)
	}
	temp, e := os.MkdirTemp("", "argocd-helm-environment-")
	if e != nil {
		return fail(e)
	}
	defer os.RemoveAll(temp)
	if e := ctx.Err(); e != nil {
		return fail(e)
	}
	env := append(os.Environ(), "HELM_CACHE_HOME="+filepath.Join(temp, "cache"), "HELM_CONFIG_HOME="+filepath.Join(temp, "config"), "HELM_DATA_HOME="+filepath.Join(temp, "data"))
	var binaryHash string
	run := func(arguments []string) ([]byte, []byte, int, error) {
		commandCtx, stop := context.WithCancel(ctx)
		defer stop()
		cmd := exec.CommandContext(commandCtx, *helm, arguments...)
		cmd.Env = env
		cmd.WaitDelay = 500 * time.Millisecond
		resume, cleanup, e := prepareCommand(cmd)
		if e != nil {
			return nil, nil, -1, e
		}
		var once sync.Once
		terminate := func() error { var err error; once.Do(func() { err = cleanup() }); return err }
		defer terminate()
		cmd.Cancel = terminate
		budget := &outputBudget{remaining: 10 * 1024 * 1024, cancel: stop}
		stdout, stderr := boundedOutput{budget: budget}, boundedOutput{budget: budget}
		cmd.Stdout = &stdout
		cmd.Stderr = &stderr
		e = cmd.Start()
		if e == nil {
			if e = resume(); e != nil {
				_ = cmd.Process.Kill()
				_ = cmd.Wait()
			} else {
				e = cmd.Wait()
			}
		}
		code := -1
		if cmd.ProcessState != nil {
			code = cmd.ProcessState.ExitCode()
		}
		if budget.exceeded {
			e = fmt.Errorf("Helm output exceeds 10 MiB")
		}
		return stdout.Bytes(), stderr.Bytes(), code, e
	}
	renderFailure := func(arguments []string, stdout, stderr []byte, code int, commandErr error) int {
		exit := 2
		reason := "RENDER_FAILED"
		if ctx.Err() != nil {
			exit = 3
			reason = "RENDER_CANCELLED"
			if ctx.Err() == context.DeadlineExceeded {
				reason = "RENDER_DEADLINE_EXCEEDED"
			}
		}
		catalogHash := assets.CatalogHash()
		report := Report{ReportSchemaVersion: 1, Product: buildinfo.Product, ToolVersion: buildinfo.ServerVersion, CatalogHash: &catalogHash, InputSource: "helm-generated", ValidationScope: "local-editing-contract", NotEvaluated: []string{"rendered-analysis"}, Targets: []workspace.Target{}, Diagnostics: []analysis.Diagnostic{}, ConfigurationErrors: []ConfigError{{Code: reason, Message: commandErr.Error()}}, Renderer: &RenderInfo{Binary: *helm, BinarySHA256: binaryHash, Command: append([]string{*helm}, arguments...), Stdout: string(stdout), Stderr: string(stderr), Exit: code}}
		if *format == "json" {
			if writeErr := json.NewEncoder(out).Encode(report); writeErr != nil {
				fmt.Fprintln(errOut, writeErr)
			}
		} else {
			fmt.Fprintln(errOut, commandErr)
			fmt.Fprint(errOut, string(stderr))
		}
		return exit
	}
	versionArgs := []string{"version", "--short"}
	binaryHash, e = rendererHash(ctx, *helm)
	if e != nil {
		return renderFailure(versionArgs, nil, nil, -1, e)
	}
	versionBytes, versionStderr, versionCode, e := run(versionArgs)
	if e != nil {
		return renderFailure(versionArgs, versionBytes, versionStderr, versionCode, e)
	}
	version := strings.TrimSpace(string(versionBytes))
	if !strings.HasPrefix(version, "v3.") && !strings.HasPrefix(version, "v4.") {
		return fail(fmt.Errorf("Helm 3 or 4 is required; found %s", version))
	}
	command := []string{"template", *release, chartPath, "--namespace", *namespace, "--kube-version", *kube, "--include-crds"}
	if valuesPath != "" {
		command = append(command, "--values", valuesPath)
	}
	for _, api := range apis {
		command = append(command, "--api-versions", api)
	}
	rendered, stderr, code, e := run(command)
	inputSource = "helm-generated"
	if e != nil {
		return renderFailure(command, rendered, stderr, code, e)
	}
	if e := ctx.Err(); e != nil {
		return fail(e)
	}
	if *output == "" {
		*output, e = os.MkdirTemp("", "argocd-render-")
		if e != nil {
			return fail(e)
		}
	}
	outputPath, e := filepath.Abs(*output)
	if e != nil {
		return fail(e)
	}
	if e = os.MkdirAll(outputPath, 0755); e != nil {
		return fail(e)
	}
	generated := filepath.Join(outputPath, "generated.yaml")
	if info, e := os.Lstat(generated); e == nil && info.Mode()&os.ModeSymlink != 0 {
		return fail(fmt.Errorf("generated output must not be a symlink"))
	}
	if e := ctx.Err(); e != nil {
		return fail(e)
	}
	if e = os.WriteFile(generated, rendered, 0644); e != nil {
		return fail(e)
	}
	report, exit := checkReport(outputPath, "", generated, input.Targets[0].Version, *warningsAsErrors)
	report.Renderer = &RenderInfo{Output: generated, Binary: *helm, BinarySHA256: binaryHash, Version: version, Command: append([]string{*helm}, command...), ReleaseName: *release, Namespace: *namespace, KubeVersion: *kube, APIVersions: apis, Chart: chartPath, Values: valuesPath, Stdout: string(rendered), Stderr: string(stderr), Exit: code}
	report.InputSource = "helm-generated"
	// Keep an inspectable report beside the generated YAML for editor actions.
	reportBytes, reportErr := json.MarshalIndent(report, "", "  ")
	if reportErr != nil {
		return fail(reportErr)
	}
	reportPath := filepath.Join(outputPath, "report.json")
	if info, e := os.Lstat(reportPath); e == nil && (!info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0) {
		return fail(fmt.Errorf("report output must be a regular file"))
	}
	if e := os.WriteFile(reportPath, append(reportBytes, '\n'), 0644); e != nil {
		return fail(e)
	}
	if *format == "human" {
		if _, e = fmt.Fprintf(out, "Helm %s generated %s; analysisComplete=%v\n", version, generated, report.AnalysisComplete); e != nil {
			fmt.Fprintln(errOut, e)
			if exit == 3 {
				return 3
			}
			return 2
		}
	}
	return writeReport(report, *format, out, errOut, exit)
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
