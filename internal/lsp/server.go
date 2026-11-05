// Package lsp implements a bounded stdio transport and a serial workspace owner.
// Background workers only receive immutable snapshots and publish through epoch checks.
package lsp

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/zed-argocd/internal/analysis"
	"github.com/lazysnail1024/zed-argocd/internal/buildinfo"
	"github.com/lazysnail1024/zed-argocd/internal/workspace"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"time"
)

type message struct {
	Error  json.RawMessage `json:"error"`
	ID     json.RawMessage `json:"id"`
	Method string          `json:"method"`
	Params json.RawMessage `json:"params"`
}
type incoming struct {
	message message
	err     error
}
type buffer struct {
	snapshot    source.Snapshot
	version     int
	revision    source.Revision
	unavailable string
}
type publication struct {
	uri         string
	version     *int
	diagnostics []analysis.Diagnostic
}
type scopeResult struct {
	checked         map[source.URI]analysis.Result
	projectionStart int
	root, scope     string
	files           []workspace.File
	documents       []analysis.Document
	options         analysis.DomainOptions
}

type pendingQuery struct {
	ctx  context.Context
	stop context.CancelFunc
}

type queryResult struct {
	proof  *readProof
	trace  string
	ctx    context.Context
	id     json.RawMessage
	epoch  uint64
	answer any
	code   int
	why    string
}

type completed struct {
	stats struct {
		Scope                string   `json:"scope"`
		AcquisitionRoots     []string `json:"acquisitionRoots"`
		AcquisitionFallbacks int      `json:"acquisitionFallbacks"`
		RepositoryDecodes    int      `json:"repositoryDecodes"`
		DiskReads            int      `json:"diskReads"`
		ParsedFiles          int      `json:"parsedFiles"`
		DomainDocuments      int      `json:"domainDocuments"`
		FullScan             bool     `json:"fullScan"`
	}
	failures        map[string]error
	editGeneration  uint64
	unchangedInputs bool
	inputs          []workspace.Input
	scopes          []scopeResult
	failureURI      string
	epoch           uint64
	publications    []publication
	err             error
}

func Run(in io.Reader, out, errOut io.Writer) int {
	messages := make(chan incoming)
	done := make(chan struct{})
	defer close(done)
	go func() {
		reader := bufio.NewReader(in)
		for {
			m, e := readMessage(reader)
			select {
			case messages <- incoming{m, e}:
			case <-done:
				return
			}
			if e != nil {
				return
			}
		}
	}()
	results := make(chan completed, 1)
	queries := make(chan queryResult, 8)
	requests := map[string]pendingQuery{}
	defer func() {
		for _, request := range requests {
			request.stop()
		}
	}()
	buffers := map[string]buffer{}
	diskGenerations := map[string]uint64{}
	roots := []string{}
	published := map[string][]byte{}
	epoch := uint64(0)
	editGeneration := uint64(0)
	initialized := false
	shutdown := false
	canEdit := false
	canWatch := false
	override := ""
	var cancel context.CancelFunc
	var latest completed
	defer func() {
		if cancel != nil {
			cancel()
		}
	}()
	send := func(value any) error {
		b, e := json.Marshal(value)
		if e != nil {
			return e
		}
		_, e = fmt.Fprintf(out, "Content-Length: %d\r\n\r\n%s", len(b), b)
		return e
	}
	// Keep only the current payload actually sent for each URI. Equal payloads
	// use the new bytes so this state does not retain older result data.
	publish := func(params map[string]any) {
		uri := params["uri"].(string)
		payload, err := json.Marshal(params)
		if err != nil {
			return
		}
		if bytes.Equal(published[uri], payload) {
			published[uri] = payload
			return
		}
		if send(map[string]any{"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": json.RawMessage(payload)}) == nil {
			published[uri] = payload
		}
	}
	reply := func(id json.RawMessage, value any) {
		_ = send(map[string]any{"jsonrpc": "2.0", "id": id, "result": value})
	}
	reject := func(id json.RawMessage, code int, why string) {
		if len(id) > 0 {
			_ = send(map[string]any{"jsonrpc": "2.0", "id": id, "error": map[string]any{"code": code, "message": why}})
		}
	}
	var active, pending bool
	scanPending := map[string]bool{}
	debounce := time.NewTimer(time.Hour)
	debounce.Stop()
	defer debounce.Stop()
	var ready <-chan time.Time
	launch := func(inputChanged bool, rescanRoots ...string) {
		for _, root := range rescanRoots {
			if root != "" {
				scanPending[root] = true
			}
		}
		if inputChanged {
			editGeneration++
		}
		epoch++
		if cancel != nil {
			cancel()
		}
		pending = true
		debounce.Reset(120 * time.Millisecond)
		ready = debounce.C
	}
	rootForURI := func(uri string) string {
		path, _ := uriPath(uri)
		return workspace.Owner(roots, path)
	}
	fitsBuffer := func(uri string, size int) bool {
		if size < 0 || size > workspace.MaxFileText {
			return false
		}
		total := size
		for other, b := range buffers {
			if other != uri {
				total += len(b.snapshot.Text())
				if total > workspace.MaxRetainedText {
					return false
				}
			}
		}
		return total <= workspace.MaxRetainedText
	}
	refuseBuffer := func(uri string, version int, revision source.Revision, reason string) {
		buffers[uri] = buffer{version: version, revision: revision, unavailable: reason}
		// Release current owner facts immediately. Already-running consumers are
		// cancelled and still must pass the independent epoch delivery guard.
		for _, request := range requests {
			request.stop()
		}
		launch(true, rootForURI(uri))
		publish(map[string]any{"uri": uri, "version": version, "diagnostics": []analysis.Diagnostic{{URI: uri, Code: "INPUT_BUDGET_EXCEEDED", Source: "argocd-ls", Severity: 1, Message: reason}}})
	}
	startAnalysis := func() {
		if active || !pending || shutdown {
			return
		}
		pending = false
		active = true
		ctx, c := context.WithTimeout(context.Background(), 5*time.Second)
		cancel = c
		copyBuffers := map[string]buffer{}
		for uri, b := range buffers {
			copyBuffers[uri] = b
		}
		copyRoots := append([]string{}, roots...)
		current, selected := epoch, override
		previous := latest
		rescanRoots := copyRootSet(scanPending)
		// Acquisition remains owed until a current pass commits it. Starting a
		// scan must not consume a watcher/save event that cancellation can lose.
		go func() {
			defer c()
			result := analyzeRoots(ctx, current, copyRoots, copyBuffers, selected, previous, rescanRoots)
			select {
			case results <- result:
			case <-done:
			}
		}()
	}
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ready:
			ready = nil
			startAnalysis()
		case <-ticker.C:
			if initialized && !shutdown {
				for _, root := range roots {
					diskGenerations[root]++
				}
				launch(false, roots...)
			}
		case q := <-queries:
			if q.trace != "" {
				fmt.Fprint(errOut, q.trace)
			}
			key := string(q.id)
			request, pending := requests[key]
			if !pending || request.ctx != q.ctx {
				continue
			}
			request.stop()
			delete(requests, key)
			if shutdown || (q.epoch != epoch && (q.proof == nil || !q.proof.valid(buffers, roots, override, diskGenerations))) {
				reject(q.id, -32801, "analysis changed")
				continue
			}
			if q.code != 0 {
				reject(q.id, q.code, q.why)
			} else {
				reply(q.id, q.answer)
			}
		case result := <-results:
			active = false
			if pending && ready == nil {
				startAnalysis()
			}
			if result.epoch != epoch || shutdown {
				if os.Getenv("ARGOCD_LS_TRACE") == "1" {
					trace, _ := json.Marshal(result.stats)
					fmt.Fprintln(errOut, "argocd-analysis-discarded", string(trace))
				}
				continue
			}
			if !result.unchangedInputs {
				editGeneration++
			}
			result.editGeneration = editGeneration
			latest = result
			if result.err == nil && result.stats.FullScan {
				clear(scanPending)
			}
			if os.Getenv("ARGOCD_LS_TRACE") == "1" {
				trace, _ := json.Marshal(result.stats)
				fmt.Fprintln(errOut, "argocd-analysis", string(trace))
			}
			if result.err != nil {
				fmt.Fprintln(errOut, "analysis incomplete:", result.err)
				code := "ANALYSIS_INCOMPLETE"
				if e, ok := result.err.(*workspace.Error); ok {
					code = e.Code
				}
				var version *int
				if b, ok := buffers[result.failureURI]; ok {
					v := b.version
					version = &v
				}
				publish(map[string]any{"uri": result.failureURI, "version": version, "diagnostics": []analysis.Diagnostic{{URI: result.failureURI, Code: code, Source: "argocd-ls", Severity: 1, Message: result.err.Error()}}})
				for uri := range published {
					if uri == result.failureURI {
						continue
					}
					var v *int
					if b, ok := buffers[uri]; ok {
						n := b.version
						v = &n
					}
					publish(map[string]any{"uri": uri, "version": v, "diagnostics": []any{}})
					delete(published, uri)
				}
				_ = send(map[string]any{"jsonrpc": "2.0", "method": "window/logMessage", "params": map[string]any{"type": 2, "message": "Argo CD analysis incomplete: " + result.err.Error()}})
				continue
			}
			current := map[string]bool{}
			for _, p := range result.publications {
				current[p.uri] = true
				publish(map[string]any{"uri": p.uri, "version": p.version, "diagnostics": p.diagnostics})
			}
			for uri := range published {
				if !current[uri] {
					publish(map[string]any{"uri": uri, "diagnostics": []any{}})
					delete(published, uri)
				}
			}
		case event := <-messages:
			if event.err != nil {
				if event.err != io.EOF {
					fmt.Fprintln(errOut, event.err)
				}
				if shutdown {
					return 0
				}
				fmt.Fprintln(errOut, "LSP ended before shutdown")
				return 2
			}
			m := event.message
			if m.Method == "" && string(m.ID) == `"argocd-watch-unregister"` {
				continue
			}
			if m.Method == "" && string(m.ID) == `"argocd-watch-registration"` {
				if len(m.Error) > 0 && string(m.Error) != "null" {
					fmt.Fprintln(errOut, "file watcher registration failed; using 30-second reconciliation")
				}
				continue
			}
			if m.Method == "exit" {
				if shutdown {
					return 0
				}
				return 2
			}
			if m.Method == "initialize" {
				if initialized {
					reject(m.ID, -32600, "already initialized")
					continue
				}
				var p struct {
					Capabilities struct {
						Workspace struct {
							DidChangeWatchedFiles struct {
								DynamicRegistration bool `json:"dynamicRegistration"`
							} `json:"didChangeWatchedFiles"`
							WorkspaceEdit struct {
								DocumentChanges bool `json:"documentChanges"`
							} `json:"workspaceEdit"`
						} `json:"workspace"`
					} `json:"capabilities"`
					RootURI          string `json:"rootUri"`
					WorkspaceFolders []struct {
						URI string `json:"uri"`
					} `json:"workspaceFolders"`
					InitializationOptions struct {
						TargetVersion string `json:"targetVersion"`
					} `json:"initializationOptions"`
				}
				if json.Unmarshal(m.Params, &p) != nil {
					reject(m.ID, -32602, "invalid initialize params")
					continue
				}
				roots = nil
				if len(p.WorkspaceFolders) > 0 {
					for _, f := range p.WorkspaceFolders {
						if path, e := uriPath(f.URI); e == nil {
							roots = append(roots, path)
						}
					}
				} else if path, e := uriPath(p.RootURI); e == nil {
					roots = append(roots, path)
				}
				if len(roots) == 0 {
					reject(m.ID, -32602, "a local workspace root is required")
					continue
				}
				override = p.InitializationOptions.TargetVersion
				for _, root := range roots {
					scanPending[root] = true
				}
				initialized = true
				canEdit = p.Capabilities.Workspace.WorkspaceEdit.DocumentChanges
				canWatch = p.Capabilities.Workspace.DidChangeWatchedFiles.DynamicRegistration
				capabilities := map[string]any{
					"textDocumentSync":       map[string]any{"openClose": true, "change": 2, "save": true},
					"hoverProvider":          true,
					"definitionProvider":     true,
					"referencesProvider":     true,
					"documentSymbolProvider": true,
					"completionProvider":     map[string]any{},
					"workspace": map[string]any{
						"workspaceFolders": map[string]any{"supported": true, "changeNotifications": true},
					},
				}
				if canEdit {
					capabilities["codeActionProvider"] = map[string]any{"resolveProvider": true}
				}
				reply(m.ID, map[string]any{
					"capabilities": capabilities,
					"serverInfo":   map[string]any{"name": "argocd-ls", "version": buildinfo.ServerVersion},
				})
				continue
			}
			if !initialized {
				reject(m.ID, -32002, "server not initialized")
				continue
			}
			if shutdown {
				reject(m.ID, -32600, "server is shut down")
				continue
			}
			switch m.Method {
			case "initialized":
				if canWatch {
					_ = send(map[string]any{"jsonrpc": "2.0", "id": "argocd-watch-registration", "method": "client/registerCapability", "params": map[string]any{"registrations": []any{map[string]any{"id": "argocd-workspace-files", "method": "workspace/didChangeWatchedFiles", "registerOptions": map[string]any{"watchers": []any{map[string]any{"globPattern": "**/*", "kind": 7}}}}}}})
				}
				launch(true, roots...)
			case "shutdown":
				if canWatch {
					_ = send(map[string]any{"jsonrpc": "2.0", "id": "argocd-watch-unregister", "method": "client/unregisterCapability", "params": map[string]any{"unregisterations": []any{map[string]any{"id": "argocd-workspace-files", "method": "workspace/didChangeWatchedFiles"}}}})
				}
				shutdown = true
				for key, request := range requests {
					request.stop()
					reject(json.RawMessage(key), -32800, "server shutting down")
					delete(requests, key)
				}
				epoch++
				if cancel != nil {
					cancel()
				}
				reply(m.ID, nil)
			case "textDocument/didOpen":
				var p struct {
					TextDocument struct {
						URI     string `json:"uri"`
						Version int    `json:"version"`
						Text    string `json:"text"`
					} `json:"textDocument"`
				}
				if json.Unmarshal(m.Params, &p) != nil {
					continue
				}
				if _, e := uriPath(p.TextDocument.URI); e != nil {
					continue
				}
				if old, ok := buffers[p.TextDocument.URI]; ok && p.TextDocument.Version <= old.version {
					continue
				}
				revision := source.Revision(1)
				if old, ok := buffers[p.TextDocument.URI]; ok {
					revision = old.revision + 1
				}
				if !fitsBuffer(p.TextDocument.URI, len(p.TextDocument.Text)) {
					refuseBuffer(p.TextDocument.URI, p.TextDocument.Version, revision, "open buffer text exceeds the 5 MiB file or 128 MiB retained-text budget")
					continue
				}
				s, e := source.New(source.URI(p.TextDocument.URI), revision, p.TextDocument.Text)
				if e != nil {
					fmt.Fprintln(errOut, e)
					continue
				}
				buffers[p.TextDocument.URI] = buffer{snapshot: s, version: p.TextDocument.Version, revision: s.Revision()}
				launch(true, rootForURI(p.TextDocument.URI))
			case "textDocument/didChange":
				var p struct {
					TextDocument struct {
						URI     string `json:"uri"`
						Version int    `json:"version"`
					} `json:"textDocument"`
					ContentChanges []struct {
						Range *analysis.Range `json:"range"`
						Text  string          `json:"text"`
					} `json:"contentChanges"`
				}
				if json.Unmarshal(m.Params, &p) != nil {
					continue
				}
				old, ok := buffers[p.TextDocument.URI]
				if !ok || p.TextDocument.Version <= old.version {
					continue
				}
				snapshot := old.snapshot
				valid := true
				unavailable := old.unavailable != ""
				budgetFailure := ""
				nextRevision := old.revision + 1
				for _, change := range p.ContentChanges {
					if unavailable {
						if change.Range != nil {
							budgetFailure = "buffer unavailable after budget refusal; send a full replacement or close/reopen"
							valid = false
							break
						}
						if !fitsBuffer(p.TextDocument.URI, len(change.Text)) {
							budgetFailure = "replacement exceeds retained text budget"
							valid = false
							break
						}
						next, e := source.New(source.URI(p.TextDocument.URI), nextRevision, change.Text)
						if e != nil {
							valid = false
							break
						}
						snapshot = next
						nextRevision++
						unavailable = false
						continue
					}
					span := source.Span{Start: 0, End: len(snapshot.Text())}
					if change.Range != nil {
						a, e1 := snapshot.Offset(source.Position{Line: change.Range.Start.Line, UTF16Column: change.Range.Start.Character})
						b, e2 := snapshot.Offset(source.Position{Line: change.Range.End.Line, UTF16Column: change.Range.End.Character})
						if e1 != nil || e2 != nil {
							valid = false
							break
						}
						span = source.Span{Start: a, End: b}
					}
					if !fitsBuffer(p.TextDocument.URI, len(snapshot.Text())-(span.End-span.Start)+len(change.Text)) {
						budgetFailure = "replacement exceeds the 5 MiB file or 128 MiB retained-text budget"
						valid = false
						break
					}
					next, e := snapshot.Apply(nextRevision, []source.Change{{Range: span, Text: change.Text}})
					if e != nil {
						valid = false
						break
					}
					snapshot = next
					nextRevision++
				}
				if unavailable && budgetFailure == "" {
					budgetFailure = "buffer unavailable after budget refusal; send a full replacement or close/reopen"
				}
				if budgetFailure != "" {
					refuseBuffer(p.TextDocument.URI, p.TextDocument.Version, nextRevision, budgetFailure)
					continue
				}
				if valid {
					buffers[p.TextDocument.URI] = buffer{snapshot: snapshot, version: p.TextDocument.Version, revision: snapshot.Revision()}
					path, _ := uriPath(p.TextDocument.URI)
					var rescanRoots []string
					if filepath.Base(path) == ".argocd-ls.yaml" {
						rescanRoots = append(rescanRoots, workspace.Owner(roots, path))
					}
					launch(true, rescanRoots...)
				} else {
					fmt.Fprintln(errOut, "rejected invalid incremental edit")
				}
			case "textDocument/didClose":
				var p struct {
					TextDocument struct {
						URI string `json:"uri"`
					} `json:"textDocument"`
				}
				if json.Unmarshal(m.Params, &p) == nil {
					_, existed := buffers[p.TextDocument.URI]
					delete(buffers, p.TextDocument.URI)
					launch(existed, rootForURI(p.TextDocument.URI))
				}
			case "textDocument/didSave", "workspace/didChangeWatchedFiles":
				var event struct {
					TextDocument struct {
						URI string `json:"uri"`
					} `json:"textDocument"`
					Changes []struct {
						URI string `json:"uri"`
					} `json:"changes"`
				}
				_ = json.Unmarshal(m.Params, &event)
				changedRoots := map[string]bool{}
				if event.TextDocument.URI != "" {
					p, _ := uriPath(event.TextDocument.URI)
					changedRoots[workspace.Owner(roots, p)] = true
				}
				for _, change := range event.Changes {
					p, _ := uriPath(change.URI)
					changedRoots[workspace.Owner(roots, p)] = true
				}
				if len(changedRoots) == 0 {
					for _, root := range roots {
						changedRoots[root] = true
					}
				}
				var rescanRoots []string
				for root := range changedRoots {
					diskGenerations[root]++
					rescanRoots = append(rescanRoots, root)
				}
				launch(false, rescanRoots...)
			case "workspace/didChangeWorkspaceFolders":
				previousRoots := append([]string(nil), roots...)
				var p struct {
					Event struct {
						Added []struct {
							URI string `json:"uri"`
						} `json:"added"`
						Removed []struct {
							URI string `json:"uri"`
						} `json:"removed"`
					} `json:"event"`
				}
				if json.Unmarshal(m.Params, &p) != nil {
					continue
				}
				for _, f := range p.Event.Removed {
					path, e := uriPath(f.URI)
					if e != nil {
						continue
					}
					remaining := roots[:0]
					for _, r := range roots {
						if r != path {
							remaining = append(remaining, r)
						}
					}
					roots = remaining
				}
				for _, f := range p.Event.Added {
					path, e := uriPath(f.URI)
					if e != nil {
						continue
					}
					exists := false
					for _, r := range roots {
						if r == path {
							exists = true
						}
					}
					if !exists {
						roots = append(roots, path)
					}
				}
				sort.Slice(roots, func(i, j int) bool {
					if len(roots[i]) != len(roots[j]) {
						return len(roots[i]) > len(roots[j])
					}
					return roots[i] < roots[j]
				})
				launch(!reflect.DeepEqual(previousRoots, roots), roots...)
			case "workspace/didChangeConfiguration":
				var p struct {
					Settings struct {
						TargetVersion string `json:"targetVersion"`
					} `json:"settings"`
				}
				if json.Unmarshal(m.Params, &p) == nil {
					changed := override != p.Settings.TargetVersion
					override = p.Settings.TargetVersion
					launch(changed, roots...)
				}

			case "textDocument/codeAction", "codeAction/resolve", "textDocument/definition", "textDocument/references", "textDocument/documentSymbol", "textDocument/hover", "textDocument/completion":
				key := string(m.ID)
				if _, exists := requests[key]; exists {
					reject(m.ID, -32600, "duplicate request id")
					continue
				}
				ctx, stop := context.WithTimeout(context.Background(), 5*time.Second)
				requests[key] = pendingQuery{ctx, stop}
				input, current := latest, epoch
				selected := override
				queryScan := copyRootSet(scanPending)
				var queryRoots []string
				var copied map[string]buffer
				queryDisk := make(map[string]uint64, len(diskGenerations))
				{
					queryRoots = append([]string{}, roots...)
					copied = map[string]buffer{}
					for uri, b := range buffers {
						copied[uri] = b
					}
					for root, generation := range diskGenerations {
						queryDisk[root] = generation
					}
				}
				editable := canEdit
				go func() {
					var proof *readProof
					var trace bytes.Buffer
					if os.Getenv("ARGOCD_LS_TRACE") == "1" && (m.Method == "textDocument/codeAction" || m.Method == "codeAction/resolve") {
						input.scopes = append([]scopeResult(nil), input.scopes...)
						for i := range input.scopes {
							input.scopes[i].options.Trace = &trace
						}
					}
					var answer any
					var code int
					var why string
					if m.Method == "textDocument/codeAction" || m.Method == "codeAction/resolve" {
						answer, code, why = fixRequest(ctx, m, input, copied, current, editable, queryRoots, selected)
					} else {
						fresh := queryInput(ctx, m, input, copied, queryRoots, selected, current, queryScan)
						answer, code, why = readRequest(ctx, m, fresh, current)
						if code == 0 {
							// The proof must describe the very scopes used by this
							// answer, including configuration changes refreshed above.
							proof = newReadProof(m, fresh, copied, queryRoots, selected, queryDisk)
						}
						traceReadRequest(&trace, m, fresh, proof)
					}
					if ctx.Err() != nil {
						code, why = -32800, "request cancelled"
					}
					select {
					case queries <- queryResult{proof, trace.String(), ctx, m.ID, current, answer, code, why}:
					case <-done:
					}
				}()
			case "$/cancelRequest":
				var p struct {
					ID json.RawMessage `json:"id"`
				}
				if json.Unmarshal(m.Params, &p) == nil {
					key := string(p.ID)
					if request, ok := requests[key]; ok {
						request.stop()
						delete(requests, key)
						reject(p.ID, -32800, "request cancelled")
					}
				}
			case "$/setTrace":
			default:
				reject(m.ID, -32601, "method is not implemented")
			}
		}
	}
}

func analyze(ctx context.Context, epoch uint64, roots []string, buffers map[string]buffer, override string, previous completed, fullScan bool) completed {
	rescanRoots := map[string]bool{}
	if fullScan {
		for _, root := range roots {
			rescanRoots[root] = true
		}
	}
	return analyzeRoots(ctx, epoch, roots, buffers, override, previous, rescanRoots)
}

func analyzeRoots(ctx context.Context, epoch uint64, roots []string, buffers map[string]buffer, override string, previous completed, rescanRoots map[string]bool) completed {
	result := completed{epoch: epoch, failures: map[string]error{}}
	result.stats.FullScan = len(rescanRoots) > 0
	result.stats.Scope = "diagnostic-pass"
	if ctx.Err() != nil {
		result.err = ctx.Err()
		return result
	}
	oldPublications := map[string]publication{}
	if previous.err == nil {
		for _, p := range previous.publications {
			oldPublications[p.uri] = p
		}
	}
	seen := map[string]bool{}
	analyzer := retainedAnalyzer(previous, nil)
	remaining := workspace.MaxRetainedText
	openFiles := map[string]source.Snapshot{}
	for uri, b := range buffers {
		if b.unavailable != "" {
			file, _ := uriPath(uri)
			root := workspace.Owner(roots, file)
			result.failures[root] = &workspace.Error{Code: "INPUT_BUDGET_EXCEEDED", Message: b.unavailable}
			v := b.version
			result.publications = append(result.publications, publication{uri, &v, []analysis.Diagnostic{{URI: uri, Code: "INPUT_BUDGET_EXCEEDED", Source: "argocd-ls", Severity: 1, Message: b.unavailable}}})
			continue
		}
		remaining -= len(b.snapshot.Text())
		if remaining < 0 {
			result.err = &workspace.Error{Code: "INPUT_BUDGET_EXCEEDED", Message: "open buffer text exceeds 128 MiB"}
			result.failureURI = uri
			return result
		}
		if file, e := uriPath(uri); e == nil {
			openFiles[file] = b.snapshot
		}
	}
	roots = append([]string(nil), roots...)
	sort.Slice(roots, func(i, j int) bool {
		if len(roots[i]) != len(roots[j]) {
			return len(roots[i]) > len(roots[j])
		}
		return roots[i] < roots[j]
	})
rootLoop:
	for _, root := range roots {
		if result.failures[root] != nil {
			continue
		}
		checkpoint := rootCheckpoint{len(result.inputs), len(result.scopes), len(result.publications)}
		configURI := workspace.FileURI(filepath.Join(root, ".argocd-ls.yaml"))
		var config *string
		configPath, _ := uriPath(configURI)
		if snapshot, ok := openFiles[configPath]; ok {
			// Filesystem identity selects authority; replies retain the client's URI.
			configURI = string(snapshot.URI())
			t := snapshot.Text()
			config = &t
		}

		var input workspace.Input
		var e error
		reusedInput := false
		beforeRemaining := remaining
		if !rescanRoots[root] {
			for _, prior := range previous.inputs {
				if prior.Root == root {
					input, reusedInput = workspace.RefreshBuffers(ctx, prior, openFiles, roots, &remaining)
					break
				}
			}
		}
		if !reusedInput {
			remaining = beforeRemaining
			result.stats.AcquisitionRoots = append(result.stats.AcquisitionRoots, root)
			if !rescanRoots[root] {
				result.stats.AcquisitionFallbacks++
			}
			input, e = workspace.LoadBuffers(ctx, root, override, config, openFiles, roots, &remaining)
			result.stats.DiskReads += input.AcquisitionReads
			input.AcquisitionReads = 0
		}
		if e != nil {
			remaining = beforeRemaining
			if ctx.Err() != nil {
				result.err = ctx.Err()
				result.failureURI = configURI
				return result
			}
			result.failRoot(checkpoint, root, configURI, buffers, e)
			continue
		}
		result.stats.RepositoryDecodes += input.RepositoryDecodes
		input.RepositoryDecodes = 0
		result.inputs = append(result.inputs, input)
		for _, snapshot := range input.Skipped {
			uri := string(snapshot.URI())
			if b, ok := buffers[uri]; ok {
				v := b.version
				result.publications = append(result.publications, publication{uri, &v, []analysis.Diagnostic{{URI: uri, Code: "HELM_UNRENDERED", Source: "argocd-ls", Severity: 3, Message: "Helm template is not rendered; static Argo CD checks are incomplete. Run the explicit render-check task and inspect its generated YAML diagnostics."}}})
			}
		}

		files := map[string]workspace.File{}
		fileIndices := map[source.URI]int{}
		for i, f := range input.Files {
			fileIndices[f.Snapshot.URI()] = i
			files[string(f.Snapshot.URI())] = f
		}

		scopeFiles := map[string][]workspace.File{}
		for _, target := range input.Targets {
			scopeFiles[target.Scope] = nil
		}
		for uri, f := range files {
			if !seen[uri] {
				seen[uri] = true
				scopeFiles[f.Target.Scope] = append(scopeFiles[f.Target.Scope], f)
			}
		}
		reusableRoot := false
		if previous.err == nil {
			for _, oldInput := range previous.inputs {
				if oldInput.Root == input.Root && oldInput.Complete == input.Complete && reflect.DeepEqual(oldInput.Config, input.Config) && reflect.DeepEqual(oldInput.LocalPaths, input.LocalPaths) {
					reusableRoot = true
					break
				}
			}
		}
		scopeNames := make([]string, 0, len(scopeFiles))
		for scope := range scopeFiles {
			scopeNames = append(scopeNames, scope)
		}
		sort.Strings(scopeNames)
		for _, scope := range scopeNames {
			currentFiles := scopeFiles[scope]
			sort.Slice(currentFiles, func(i, j int) bool { return currentFiles[i].Snapshot.URI() < currentFiles[j].Snapshot.URI() })
			projectionStart := analyzer.ProjectionNodes()
			if ctx.Err() != nil {
				result.err = ctx.Err()
				result.failureURI = configURI
				return result
			}
			reused := false
			if reusableRoot {
				for _, old := range previous.scopes {
					if old.root == input.Root && old.scope == scope && old.projectionStart == projectionStart && sameScopeFiles(old.files, currentFiles) && analyzer.ReuseProjections(old.documents) {
						// This is the single latest-result cache. Fresh acquisition
						// proved identical authority; no disk metadata shortcut.
						// old is a value copy: keep earlier readers immutable while
						// sharing this acquisition's equal whole-root path facts.
						old.options.LocalPaths = input.LocalPaths
						result.scopes = append(result.scopes, old)
						// Keep the completed input and reused documents on the same
						// immutable snapshots after exact fresh-text comparison.
						for _, f := range old.files {
							input.Files[fileIndices[f.Snapshot.URI()]].Snapshot = f.Snapshot
						}
						for _, f := range currentFiles {
							uri := string(f.Snapshot.URI())
							p := oldPublications[uri]
							p.version = nil
							if b, ok := buffers[uri]; ok {
								v := b.version
								p.version = &v
							}
							result.publications = append(result.publications, p)
						}
						reused = true
						break
					}
				}
			}
			if reused {
				continue
			}
			docs := []analysis.Document{}
			checks := map[source.URI]analysis.Result{}
			var priorScope *scopeResult
			if reusableRoot {
				for i := range previous.scopes {
					old := &previous.scopes[i]
					if old.root == root && old.scope == scope {
						priorScope = old
						break
					}
				}
			}
			priorFiles := map[source.URI]workspace.File{}
			if priorScope != nil {
				for _, f := range priorScope.files {
					priorFiles[f.Snapshot.URI()] = f
				}
			}
			changed := map[string]bool{}
			projectChanged := false
			if priorScope != nil {
				currentMembers := map[source.URI]bool{}
				for _, f := range currentFiles {
					currentMembers[f.Snapshot.URI()] = true
				}
				for _, f := range priorScope.files {
					if !currentMembers[f.Snapshot.URI()] {
						projectChanged = true
					}
				}
			}
			incomplete := false
			firstPublication := len(result.publications)
			for _, f := range currentFiles {
				uri := string(f.Snapshot.URI())
				if ctx.Err() != nil {
					result.err = ctx.Err()
					result.failureURI = uri
					return result
				}
				var checked analysis.Result
				var e error
				reusedFile := false
				if priorScope != nil {
					if oldFile, ok := priorFiles[f.Snapshot.URI()]; ok && sameScopeFiles([]workspace.File{oldFile}, []workspace.File{f}) {
						if old, ok := priorScope.checked[f.Snapshot.URI()]; ok && analyzer.ReuseProjections(old.Documents) {
							checked = old
							reusedFile = true
						}
					}
				}

				if !reusedFile {
					checked, e = analyzer.Analyze(ctx, f.Snapshot, f.Target.Version)
					result.stats.ParsedFiles++
					changed[uri] = true
				}
				checks[f.Snapshot.URI()] = checked
				if changed[uri] {
					for _, doc := range checked.Documents {
						if doc.Kind == "AppProject" {
							projectChanged = true
						}
					}
					if priorScope != nil {
						for _, doc := range priorScope.documents {
							if string(doc.Snapshot.URI()) == uri && doc.Kind == "AppProject" {
								projectChanged = true
							}
						}
					}
				}
				if e != nil {
					if ctx.Err() != nil {
						result.err = ctx.Err()
						result.failureURI = uri
						return result
					}
					// A parser/input failure invalidates this root's authority,
					// not the other roots. Discard all partial facts and charges
					// from this root before continuing with independent inputs.
					result.failRoot(checkpoint, root, uri, buffers, e)
					remaining = beforeRemaining
					for uri := range files {
						delete(seen, uri)
					}
					analyzer = retainedAnalyzer(previous, result.scopes)
					continue rootLoop
				}
				incomplete = incomplete || checked.Incomplete
				docs = append(docs, checked.Documents...)
				var version *int
				if b, ok := buffers[uri]; ok {
					v := b.version
					version = &v
				}
				result.publications = append(result.publications, publication{uri, version, checked.Diagnostics})
			}
			options := analysis.DomainOptions{LocalPaths: input.LocalPaths, ControlPlaneNamespace: input.Config.ControlPlaneNamespace, ReferencePolicy: input.Config.ReferencePolicy, Complete: input.Complete && !incomplete && !strings.HasPrefix(scope, "isolated:")}
			result.scopes = append(result.scopes, scopeResult{checked: checks, projectionStart: projectionStart, root: input.Root, scope: scope, files: currentFiles, documents: docs, options: options})
			domainOptions := options
			domainOptions.Context = ctx
			domainDocs := docs
			if priorScope != nil && priorScope.options.Complete && options.Complete && !projectChanged && len(priorScope.files) == len(currentFiles) {
				domainDocs = nil
				for _, doc := range docs {
					if changed[string(doc.Snapshot.URI())] || doc.Kind == "AppProject" {
						domainDocs = append(domainDocs, doc)
					}
				}
				for i := firstPublication; i < len(result.publications); i++ {
					uri := result.publications[i].uri
					if !changed[uri] {
						result.publications[i].diagnostics = oldPublications[uri].diagnostics
					}
				}
			}
			result.stats.DomainDocuments += len(domainDocs)
			for _, d := range analysis.Domain(domainDocs, domainOptions) {
				for i := firstPublication; i < len(result.publications); i++ {
					if result.publications[i].uri == d.URI {
						result.publications[i].diagnostics = append(result.publications[i].diagnostics, d)
						break
					}
				}
			}
			if ctx.Err() != nil {
				result.err = ctx.Err()
				result.failureURI = configURI
				return result
			}
		}
	}
	// Open skipped documents still receive an explicit empty replacement.
	for uri, b := range buffers {
		path, err := uriPath(uri)
		if err != nil || workspace.Owner(roots, path) == "" {
			continue
		}
		found := false
		for _, p := range result.publications {
			if p.uri == uri {
				found = true
				break
			}
		}
		if !found {
			version := b.version
			result.publications = append(result.publications, publication{uri, &version, []analysis.Diagnostic{}})
		}
	}

	// Reconciliation may replace the worker epoch without changing edit inputs.
	// Compare existing immutable acquisition facts in the worker, not the owner.
	if err := ctx.Err(); err != nil {
		result.err = err
		return result
	}
	complete := previous.err == nil && len(previous.failures) == 0 && len(result.failures) == 0
	for _, scopes := range [][]scopeResult{previous.scopes, result.scopes} {
		for _, scope := range scopes {
			complete = complete && scope.options.Complete
		}
	}
	result.unchangedInputs = complete && reflect.DeepEqual(previous.inputs, result.inputs)
	return result
}

type rootCheckpoint struct{ inputs, scopes, publications int }

// failRoot drops the failed root's partially accumulated result, so it cannot
// supply stale diagnostics, reference authority, or edit candidates.
func (result *completed) failRoot(checkpoint rootCheckpoint, root, uri string, buffers map[string]buffer, err error) {
	clear(result.inputs[checkpoint.inputs:])
	clear(result.scopes[checkpoint.scopes:])
	clear(result.publications[checkpoint.publications:])
	result.inputs = result.inputs[:checkpoint.inputs]
	result.scopes = result.scopes[:checkpoint.scopes]
	result.publications = result.publications[:checkpoint.publications]
	result.failures[root] = err
	code := "ANALYSIS_INCOMPLETE"
	if failure, ok := err.(*workspace.Error); ok {
		code = failure.Code
	}
	var version *int
	if b, ok := buffers[uri]; ok {
		v := b.version
		version = &v
	}
	result.publications = append(result.publications, publication{uri, version, []analysis.Diagnostic{{URI: uri, Code: code, Source: "argocd-ls", Severity: 1, Message: err.Error()}}})
}

// Rebuild budget accounting only after a root is rolled back. Schema preparation
// remains reusable, while projections belonging to discarded roots are released.
func retainedAnalyzer(previous completed, retained []scopeResult) *analysis.Analyzer {
	analyzer := new(analysis.Analyzer)
	if previous.err == nil {
		for _, scope := range previous.scopes {
			analyzer.ReuseSchemas(scope.documents)
		}
	}
	for _, scope := range retained {
		analyzer.ReuseSchemas(scope.documents)
		analyzer.ReuseProjections(scope.documents)
	}
	return analyzer
}

func sameScopeFiles(before, after []workspace.File) bool {
	if len(before) != len(after) {
		return false
	}
	old := make(map[source.URI]workspace.File, len(before))
	for _, f := range before {
		old[f.Snapshot.URI()] = f
	}
	for _, f := range after {
		prior, ok := old[f.Snapshot.URI()]
		if !ok || prior.Path != f.Path || prior.Target != f.Target || prior.Output != f.Output || prior.Snapshot.Revision() != f.Snapshot.Revision() || prior.Snapshot.Text() != f.Snapshot.Text() {
			return false
		}
	}
	return true
}

func uriPath(uri string) (string, error) {
	return workspace.PathFromURI(uri)
}
func readMessage(reader *bufio.Reader) (message, error) {
	var m message
	length := -1
	headers := 0
	for {
		line, e := reader.ReadSlice('\n')
		if e != nil {
			return m, e
		}
		headers += len(line)
		if headers > 8192 {
			return m, fmt.Errorf("LSP headers too large")
		}
		text := strings.TrimSpace(string(line))
		if text == "" {
			break
		}
		parts := strings.SplitN(text, ":", 2)
		if len(parts) != 2 {
			return m, fmt.Errorf("invalid LSP header")
		}
		if strings.EqualFold(parts[0], "Content-Length") {
			if length >= 0 {
				return m, fmt.Errorf("duplicate Content-Length")
			}
			length, e = strconv.Atoi(strings.TrimSpace(parts[1]))
			if e != nil {
				return m, e
			}
		}
	}
	if length < 0 || length > 10*1024*1024 {
		return m, fmt.Errorf("invalid LSP message length")
	}
	body := make([]byte, length)
	if _, e := io.ReadFull(reader, body); e != nil {
		return m, e
	}
	if e := json.Unmarshal(body, &m); e != nil {
		return m, e
	}
	return m, nil
}

type fixData struct {
	URI      string `json:"uri"`
	Epoch    uint64 `json:"epoch"`
	Revision uint64 `json:"revision"`
	Target   string `json:"target"`
	FixCode  string `json:"fixCode"`
	Start    int    `json:"start"`
	End      int    `json:"end"`
	NewText  string `json:"newText"`
}

func replacementEdit(snapshot source.Snapshot, span source.Span, text string) analysis.TextEdit {
	a, _ := snapshot.Position(span.Start)
	b, _ := snapshot.Position(span.End)
	return analysis.TextEdit{Range: analysis.Range{Start: analysis.Position{Line: a.Line, Character: a.UTF16Column}, End: analysis.Position{Line: b.Line, Character: b.UTF16Column}}, NewText: text}
}

func fixRequest(ctx context.Context, m message, latest completed, buffers map[string]buffer, epoch uint64, canEdit bool, roots []string, override string) (answer any, code int, why string) {
	if result, status, message, handled := pinRequest(ctx, m, latest, buffers, epoch, canEdit, roots, override); handled {
		return result, status, message
	}
	reply := func(_ json.RawMessage, value any) { answer = value }
	reject := func(_ json.RawMessage, c int, text string) { code, why = c, text }
	switch m.Method {
	case "textDocument/codeAction":
		if !canEdit {
			reply(m.ID, []any{})
			return
		}
		if latest.epoch != epoch || latest.err != nil {
			reject(m.ID, -32801, "analysis changed")
			return
		}
		var p struct {
			TextDocument struct {
				URI string `json:"uri"`
			} `json:"textDocument"`
			Range analysis.Range `json:"range"`
		}
		if json.Unmarshal(m.Params, &p) != nil {
			reject(m.ID, -32602, "invalid codeAction params")
			return
		}
		b, ok := buffers[p.TextDocument.URI]
		if !ok {
			reply(m.ID, []any{})
			return
		}
		actions := []any{}
		for _, scope := range latest.scopes {
			for _, doc := range scope.documents {
				if string(doc.Snapshot.URI()) != p.TextDocument.URI {
					continue
				}
				rangeStart, e1 := doc.Snapshot.Offset(source.Position{Line: p.Range.Start.Line, UTF16Column: p.Range.Start.Character})
				rangeEnd, e2 := doc.Snapshot.Offset(source.Position{Line: p.Range.End.Line, UTF16Column: p.Range.End.Character})
				if e1 != nil || e2 != nil {
					continue
				}
				for _, fix := range analysis.FixCandidatesInRange(ctx, doc, scope.documents, scope.options, source.Span{Start: rangeStart, End: rangeEnd}) {
					replacement := fix.Plan.Replacements[0]
					rangeStart, e1 := doc.Snapshot.Offset(source.Position{Line: p.Range.Start.Line, UTF16Column: p.Range.Start.Character})
					rangeEnd, e2 := doc.Snapshot.Offset(source.Position{Line: p.Range.End.Line, UTF16Column: p.Range.End.Character})
					if e1 != nil || e2 != nil || replacement.Span.Start > rangeEnd || replacement.Span.End < rangeStart {
						continue
					}
					data := fixData{URI: p.TextDocument.URI, Epoch: latest.editGeneration, Revision: uint64(b.snapshot.Revision()), Target: doc.Target, FixCode: fix.Code, Start: replacement.Span.Start, End: replacement.Span.End, NewText: replacement.NewText}
					actions = append(actions, map[string]any{"title": fix.Title, "kind": "quickfix", "data": data})
				}
			}
		}
		reply(m.ID, actions)
	case "codeAction/resolve":
		var action map[string]any
		var envelope struct {
			Data fixData `json:"data"`
		}
		if json.Unmarshal(m.Params, &action) != nil || json.Unmarshal(m.Params, &envelope) != nil {
			reject(m.ID, -32602, "invalid action")
			return
		}
		data := envelope.Data
		b, ok := buffers[data.URI]
		if !canEdit || !ok || data.Epoch != latest.editGeneration || latest.epoch != epoch || latest.err != nil || data.Revision != uint64(b.snapshot.Revision()) {
			reject(m.ID, -32801, "code action is stale")
			return
		}
		matched := false
		for _, scope := range latest.scopes {
			for _, doc := range scope.documents {
				if string(doc.Snapshot.URI()) != data.URI || doc.Target != data.Target {
					continue
				}
				for _, fix := range analysis.ResolveFix(ctx, doc, scope.documents, scope.options, source.Span{Start: data.Start, End: data.End}, data.FixCode, data.NewText) {
					r := fix.Plan.Replacements[0]
					if fix.Code == data.FixCode && r.Span.Start == data.Start && r.Span.End == data.End && r.NewText == data.NewText {
						action["edit"] = map[string]any{"documentChanges": []any{map[string]any{"textDocument": map[string]any{"uri": data.URI, "version": b.version}, "edits": []analysis.TextEdit{replacementEdit(doc.Snapshot, r.Span, r.NewText)}}}}
						matched = true
					}
				}
			}
		}
		if !matched {
			reject(m.ID, -32801, "action is no longer safe")
			return
		}
		reply(m.ID, action)
	}
	return
}

func readRequest(ctx context.Context, m message, latest completed, epoch uint64) (answer any, code int, why string) {
	if latest.epoch != epoch || latest.err != nil {
		return nil, -32801, "analysis input changed; retry after current diagnostics"
	}
	var p struct {
		TextDocument struct {
			URI string `json:"uri"`
		} `json:"textDocument"`
		Position analysis.Position `json:"position"`
		Context  struct {
			IncludeDeclaration bool `json:"includeDeclaration"`
		} `json:"context"`
	}
	if json.Unmarshal(m.Params, &p) != nil {
		return nil, -32602, "invalid query params"
	}
	answer = []any{}
	if m.Method == "textDocument/hover" {
		answer = nil
	}
	for _, scope := range latest.scopes {
		found := false
		for _, doc := range scope.documents {
			if string(doc.Snapshot.URI()) == p.TextDocument.URI {
				found = true
				break
			}
		}
		if m.Method == "textDocument/completion" {
			for _, f := range scope.files {
				if string(f.Snapshot.URI()) == p.TextDocument.URI {
					found = true
				}
			}
		}
		if m.Method == "textDocument/definition" {
			for _, f := range scope.files {
				if string(f.Snapshot.URI()) == p.TextDocument.URI {
					if recovered := analysis.RecoverProjectUse(ctx, f.Snapshot, f.Target.Version, p.Position); recovered != nil {
						scope.documents = append(append([]analysis.Document(nil), scope.documents...), *recovered)
						found = true
					}
				}
			}
		}
		if !found {
			continue
		}
		switch m.Method {
		case "textDocument/definition":
			answer = analysis.Definition(scope.documents, scope.options, p.TextDocument.URI, p.Position)
		case "textDocument/references":
			answer = analysis.References(scope.documents, scope.options, p.TextDocument.URI, p.Position, p.Context.IncludeDeclaration)
		case "textDocument/documentSymbol":
			answer = analysis.Symbols(scope.documents, p.TextDocument.URI)
		case "textDocument/hover":
			for _, doc := range scope.documents {
				if string(doc.Snapshot.URI()) == p.TextDocument.URI {
					if hover := analysis.SchemaHover(doc, p.Position); hover != nil {
						answer = hover
						break
					}
				}
			}
		case "textDocument/completion":
			for _, f := range scope.files {
				if string(f.Snapshot.URI()) == p.TextDocument.URI {
					if items := analysis.ReferenceCandidates(ctx, f.Snapshot, f.Target.Version, scope.documents, scope.options, p.Position); len(items) > 0 {
						answer = items
						break
					}
				}
			}
			if items, ok := answer.([]analysis.Completion); ok && len(items) > 0 {
				break
			}
			for _, f := range scope.files {
				if string(f.Snapshot.URI()) == p.TextDocument.URI {
					if items := analysis.FieldCompletions(ctx, f.Snapshot, f.Target.Version, scope.documents, p.Position); len(items) > 0 {
						answer = items
					}
					break
				}
			}
		}
		break
	}
	return
}

// queryInput refreshes changed buffers within the requested root without waiting
// for diagnostic debounce. It never promotes this read-only view to edit state.
func queryInput(ctx context.Context, m message, latest completed, buffers map[string]buffer, roots []string, override string, epoch uint64, rescanRoots map[string]bool) completed {

	var p struct {
		TextDocument struct {
			URI string `json:"uri"`
		} `json:"textDocument"`
	}
	if json.Unmarshal(m.Params, &p) != nil {
		return latest
	}
	file, err := uriPath(p.TextDocument.URI)
	if err != nil {
		return latest
	}
	root := workspace.Owner(roots, file)
	if root == "" {
		return completed{epoch: epoch}
	}
	if failure := latest.failures[root]; failure != nil && !rescanRoots[root] {
		latest.err = failure
		return latest
	}
	if rescanRoots[root] || queryMembershipChanged(latest, buffers, root) {
		result := analyzeRoots(ctx, epoch, roots, buffers, override, latest, rescanRoots)
		result.stats.Scope = "query-acquisition"
		if failure := result.failures[root]; failure != nil {
			result.err = failure
		}
		return result
	}
	for _, input := range latest.inputs {
		if input.Root != root {
			continue
		}
		if override != "" {
			for _, t := range input.Targets {
				if t.Version != override {
					return latest
				}
			}
		}
		configURI := workspace.FileURI(filepath.Join(root, ".argocd-ls.yaml"))
		if b, ok := buffers[configURI]; ok { // Raw config bytes are part of the target identity.
			hash := sha256.Sum256([]byte(b.snapshot.Text()))
			for _, t := range input.Targets {
				if t.ConfigHash != hex.EncodeToString(hash[:]) {
					return latest
				}
			}
		}
	}
	fresh := latest
	fresh.epoch = epoch
	fresh.scopes = append([]scopeResult(nil), latest.scopes...)
	analyzer := new(analysis.Analyzer)
	for _, scope := range latest.scopes {
		analyzer.ReuseSchemas(scope.documents)
	}
	for i, scope := range fresh.scopes {
		if scope.root != root {
			continue
		}
		scope.files = append([]workspace.File(nil), scope.files...)
		scope.documents = append([]analysis.Document(nil), scope.documents...)
		for j, f := range scope.files {
			b, ok := buffers[string(f.Snapshot.URI())]
			if !ok {
				continue
			}
			if b.unavailable != "" {
				return latest
			}
			if b.snapshot.Revision() == f.Snapshot.Revision() && b.snapshot.Text() == f.Snapshot.Text() {
				continue
			}
			checked, e := analyzer.Analyze(ctx, b.snapshot, f.Target.Version)
			if e != nil {
				return latest
			}
			kept := []analysis.Document{}
			for _, doc := range scope.documents {
				if doc.Snapshot.URI() != f.Snapshot.URI() {
					kept = append(kept, doc)
				}
			}
			scope.documents = append(kept, checked.Documents...)
			scope.files[j].Snapshot = b.snapshot
		}
		fresh.scopes[i] = scope
	}
	return fresh
}

// The query fast path may replace text only while acquisition membership is
// unchanged. Apply the same URI and Helm policies as diagnostic acquisition.
func queryMembershipChanged(latest completed, buffers map[string]buffer, root string) bool {
	for _, input := range latest.inputs {
		for _, f := range input.Files {
			b, ok := buffers[string(f.Snapshot.URI())]
			if !ok || b.unavailable != "" {
				continue
			}
			path, err := uriPath(string(b.snapshot.URI()))
			if err != nil || path != f.Path {
				return true
			}
			if input.Root == root && workspace.IsHelmTemplate(root, path, b.snapshot) {
				return true
			}
		}
		if input.Root != root {
			continue
		}
		for _, snapshot := range input.Skipped {
			b, ok := buffers[string(snapshot.URI())]
			if !ok || b.unavailable != "" {
				continue
			}
			path, err := uriPath(string(b.snapshot.URI()))
			if err != nil || !workspace.IsHelmTemplate(root, path, b.snapshot) {
				return true
			}
		}
	}
	return false
}

func copyRootSet(roots map[string]bool) map[string]bool {
	copied := make(map[string]bool, len(roots))
	for root := range roots {
		copied[root] = true
	}
	return copied
}

// A read proof only spans the queried scope, while edits retain the global epoch.
// It permits unrelated input churn without accepting stale text or dependencies.
type readProof struct {
	root, override    string
	diskGeneration    uint64
	files, knownFiles map[string]bool
	buffers           map[string]buffer
}

func traceReadRequest(out io.Writer, m message, input completed, proof *readProof) {
	if os.Getenv("ARGOCD_LS_TRACE") != "1" {
		return
	}
	if input.stats.Scope == "query-acquisition" {
		payload, _ := json.Marshal(input.stats)
		fmt.Fprintln(out, "argocd-query-acquisition", string(payload))
	}
	if proof == nil {
		return
	}
	// Counts and identities explain which immutable dependency set was used
	// without logging document contents or unbounded lists of files.
	payload, _ := json.Marshal(map[string]any{"method": m.Method, "epoch": input.epoch, "root": proof.root, "dependencyFiles": len(proof.files), "knownFiles": len(proof.knownFiles), "diskGeneration": proof.diskGeneration})
	fmt.Fprintln(out, "argocd-read-proof", string(payload))
}

func newReadProof(m message, input completed, buffers map[string]buffer, roots []string, override string, disk map[string]uint64) *readProof {
	var p struct {
		TextDocument struct {
			URI string `json:"uri"`
		} `json:"textDocument"`
	}
	if json.Unmarshal(m.Params, &p) != nil {
		return nil
	}
	path, err := uriPath(p.TextDocument.URI)
	if err != nil {
		return nil
	}
	root := workspace.Owner(roots, path)
	if root == "" {
		return nil
	}
	proof := &readProof{root: root, override: override, diskGeneration: disk[root], files: map[string]bool{}, knownFiles: map[string]bool{}, buffers: buffers}
	found := false
	for _, scope := range input.scopes {
		if scope.root != root {
			continue
		}
		selected := false
		for _, f := range scope.files {
			uri := string(f.Snapshot.URI())
			proof.knownFiles[uri] = true
			if uri == p.TextDocument.URI {
				selected = true
			}
		}
		if selected {
			found = true
			for _, f := range scope.files {
				proof.files[string(f.Snapshot.URI())] = true
			}
		}
	}
	if !found {
		return nil
	}
	return proof
}
func (proof *readProof) valid(current map[string]buffer, roots []string, override string, disk map[string]uint64) bool {
	if proof.override != override || proof.diskGeneration != disk[proof.root] {
		return false
	}
	relevant := func(uri string) bool {
		p, err := uriPath(uri)
		return err == nil && workspace.Owner(roots, p) == proof.root && (proof.files[uri] || filepath.Base(p) == ".argocd-ls.yaml")
	}
	same := func(a, b buffer) bool {
		return a.revision == b.revision && a.version == b.version && a.unavailable == b.unavailable && a.snapshot.Text() == b.snapshot.Text()
	}
	for uri := range proof.files {
		p, err := uriPath(uri)
		if err != nil || workspace.Owner(roots, p) != proof.root {
			return false
		}
	}
	for uri, b := range proof.buffers {
		if relevant(uri) {
			now, ok := current[uri]
			if !ok || !same(b, now) {
				return false
			}
		}
	}
	for uri, b := range current {
		p, err := uriPath(uri)
		if err != nil || workspace.Owner(roots, p) != proof.root {
			continue
		}
		old, wasOpen := proof.buffers[uri]
		if relevant(uri) {
			if !wasOpen || !same(old, b) {
				return false
			}
		} else if !proof.knownFiles[uri] && (!wasOpen || !same(old, b)) {
			return false
		}
	}
	return true
}
