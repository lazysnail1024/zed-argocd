package cli

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
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
	fs := flag.NewFlagSet("render-check", flag.ContinueOnError)
	fs.SetOutput(errOut)
	root := fs.String("root", ".", "source workspace")
	scope := fs.String("scope", "", "source scope")
	target := fs.String("target-version", "", "exact target")
	chart := fs.String("chart", "", "local chart")
	values := fs.String("values", "", "local values file")
	helm := fs.String("helm-binary", "", "explicit Helm 3 binary")
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
		report := Report{ReportSchemaVersion: 1, Product: buildinfo.Product, ToolVersion: buildinfo.ServerVersion, CatalogHash: &catalogHash, InputSource: inputSource, ValidationScope: "local-editing-contract", NotEvaluated: []string{"rendered-analysis"}, Targets: []workspace.Target{}, Diagnostics: []any{}, ConfigurationErrors: []ConfigError{{Code: reason, Message: e.Error()}}}
		if *format == "json" {
			if writeErr := json.NewEncoder(out).Encode(report); writeErr != nil {
				fmt.Fprintln(errOut, writeErr)
			}
		} else {
			fmt.Fprintln(errOut, e)
		}
		return 3
	}
	if *chart == "" || *helm == "" || *output == "" {
		return fail(fmt.Errorf("render-check requires --chart, --helm-binary and --output"))
	}
	if *format != "json" && *format != "human" {
		return fail(fmt.Errorf("format must be json or human"))
	}
	if !filepath.IsAbs(*helm) {
		return fail(fmt.Errorf("helm binary must be an explicit absolute path"))
	}
	signalCtx, stopSignal := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stopSignal()
	var cancel context.CancelFunc
	ctx, cancel = context.WithTimeout(signalCtx, 30*time.Second)
	defer cancel()
	input, e := workspace.Load(ctx, *root, *scope, "", *target)
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
	var toolPrepared bool
	run := func(arguments []string) ([]byte, []byte, int, error) {
		if !toolPrepared {
			var err error
			binaryHash, err = rendererHash(ctx, *helm)
			if err != nil {
				return nil, nil, -1, err
			}
			toolPrepared = true
		}
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
		report := Report{ReportSchemaVersion: 1, Product: buildinfo.Product, ToolVersion: buildinfo.ServerVersion, CatalogHash: &catalogHash, InputSource: "helm-generated", ValidationScope: "local-editing-contract", NotEvaluated: []string{"rendered-analysis"}, Targets: []workspace.Target{}, Diagnostics: []any{}, ConfigurationErrors: []ConfigError{{Code: reason, Message: commandErr.Error()}}, Renderer: &RenderInfo{Binary: *helm, BinarySHA256: binaryHash, Command: append([]string{*helm}, arguments...), Stdout: string(stdout), Stderr: string(stderr), Exit: code}}
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
	versionBytes, versionStderr, versionCode, e := run(versionArgs)
	if e != nil {
		return renderFailure(versionArgs, versionBytes, versionStderr, versionCode, e)
	}
	version := strings.TrimSpace(string(versionBytes))
	if !strings.HasPrefix(version, "v3.") {
		return fail(fmt.Errorf("Helm 3 is required; found %s", version))
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
	var checked bytes.Buffer
	checkArgs := []string{"--root", outputPath, "--file", generated, "--target-version", input.Targets[0].Version, "--format", "json"}
	if *warningsAsErrors {
		checkArgs = append(checkArgs, "--warnings-as-errors")
	}
	exit := check(checkArgs, &checked, errOut)
	var report Report
	if e = json.Unmarshal(checked.Bytes(), &report); e != nil {
		return fail(e)
	}
	report.Renderer = &RenderInfo{Binary: *helm, BinarySHA256: binaryHash, Version: version, Command: append([]string{*helm}, command...), ReleaseName: *release, Namespace: *namespace, KubeVersion: *kube, APIVersions: apis, Chart: chartPath, Values: valuesPath, Stdout: string(rendered), Stderr: string(stderr), Exit: code}
	report.InputSource = "helm-generated"
	if *format == "json" {
		if e = json.NewEncoder(out).Encode(report); e != nil {
			fmt.Fprintln(errOut, e)
			if exit == 3 {
				return 3
			}
			return 2
		}
	} else {
		if _, e = fmt.Fprintf(out, "Helm %s generated %s; analysisComplete=%v\n", version, generated, report.AnalysisComplete); e != nil {
			fmt.Fprintln(errOut, e)
			if exit == 3 {
				return 3
			}
			return 2
		}
		for _, d := range report.Diagnostics {
			if _, e = fmt.Fprintln(out, d); e != nil {
				fmt.Fprintln(errOut, e)
				if exit == 3 {
					return 3
				}
				return 2
			}
		}
	}
	return exit
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
