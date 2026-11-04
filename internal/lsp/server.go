// Package lsp implements a bounded stdio transport and a serial workspace owner.
// Background workers only receive immutable snapshots and publish through epoch checks.
package lsp

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"github.com/lazysnail1024/k8s-crd-toolkit/source"
	"github.com/lazysnail1024/zed-argocd/internal/analysis"
	"github.com/lazysnail1024/zed-argocd/internal/buildinfo"
	"github.com/lazysnail1024/zed-argocd/internal/workspace"
	protocol "github.com/tliron/glsp/protocol_3_16"
	"io"
	"net/url"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"time"
)

type message struct {
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
	ctx    context.Context
	id     json.RawMessage
	epoch  uint64
	answer any
	code   int
	why    string
}

type completed struct {
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
	roots := []string{}
	published := map[string][]byte{}
	epoch := uint64(0)
	editGeneration := uint64(0)
	initialized := false
	shutdown := false
	canEdit := false
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
	debounce := time.NewTimer(time.Hour)
	debounce.Stop()
	defer debounce.Stop()
	var ready <-chan time.Time
	launch := func(inputChanged bool) {
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
		latest = completed{}
		for _, request := range requests {
			request.stop()
		}
		launch(true)
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
		go func() {
			defer c()
			result := analyze(ctx, current, copyRoots, copyBuffers, selected, previous)
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
				launch(false)
			}
		case q := <-queries:
			key := string(q.id)
			request, pending := requests[key]
			if !pending || request.ctx != q.ctx {
				continue
			}
			request.stop()
			delete(requests, key)
			if q.epoch != epoch || shutdown {
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
				continue
			}
			if !result.unchangedInputs {
				editGeneration++
			}
			result.editGeneration = editGeneration
			latest = result
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
				initialized = true
				canEdit = p.Capabilities.Workspace.WorkspaceEdit.DocumentChanges
				yes := true
				var codeActionOptions any
				if canEdit {
					codeActionOptions = &protocol.CodeActionOptions{ResolveProvider: &yes}
				}
				kind := protocol.TextDocumentSyncKindIncremental
				reply(m.ID, protocol.InitializeResult{Capabilities: protocol.ServerCapabilities{TextDocumentSync: protocol.TextDocumentSyncOptions{OpenClose: &yes, Change: &kind, Save: true}, HoverProvider: true, DefinitionProvider: true, ReferencesProvider: true, DocumentSymbolProvider: true, CompletionProvider: &protocol.CompletionOptions{}, CodeActionProvider: codeActionOptions, Workspace: &protocol.ServerCapabilitiesWorkspace{WorkspaceFolders: &protocol.WorkspaceFoldersServerCapabilities{Supported: &yes}}}, ServerInfo: &protocol.InitializeResultServerInfo{Name: "argocd-ls", Version: &buildinfo.ServerVersion}})
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
				launch(true)
			case "shutdown":
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
				launch(true)
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
					launch(true)
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
					launch(existed)
				}
			case "textDocument/didSave", "workspace/didChangeWatchedFiles":
				launch(false)
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
				launch(!reflect.DeepEqual(previousRoots, roots))
			case "workspace/didChangeConfiguration":
				var p struct {
					Settings struct {
						TargetVersion string `json:"targetVersion"`
					} `json:"settings"`
				}
				if json.Unmarshal(m.Params, &p) == nil {
					changed := override != p.Settings.TargetVersion
					override = p.Settings.TargetVersion
					launch(changed)
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
				var queryRoots []string
				var copied map[string]buffer
				if m.Method == "textDocument/codeAction" || m.Method == "codeAction/resolve" {
					queryRoots = append([]string{}, roots...)
					copied = map[string]buffer{}
					for uri, b := range buffers {
						copied[uri] = b
					}
				}
				editable := canEdit
				go func() {
					var answer any
					var code int
					var why string
					if m.Method == "textDocument/codeAction" || m.Method == "codeAction/resolve" {
						answer, code, why = fixRequest(ctx, m, input, copied, current, editable, queryRoots, selected)
					} else {
						answer, code, why = readRequest(ctx, m, input, current)
					}
					if ctx.Err() != nil {
						code, why = -32800, "request cancelled"
					}
					select {
					case queries <- queryResult{ctx, m.ID, current, answer, code, why}:
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

func analyze(ctx context.Context, epoch uint64, roots []string, buffers map[string]buffer, override string, previous completed) completed {
	result := completed{epoch: epoch}
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
	analyzer := new(analysis.Analyzer)
	if previous.err == nil {
		for _, scope := range previous.scopes {
			analyzer.ReuseSchemas(scope.documents)
		}
	}
	remaining := workspace.MaxRetainedText
	openFiles := map[string]source.Snapshot{}
	for uri, b := range buffers {
		if b.unavailable != "" {
			result.err = &workspace.Error{Code: "INPUT_BUDGET_EXCEEDED", Message: b.unavailable}
			result.failureURI = uri
			return result
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
	for _, root := range roots {
		configURI := workspace.FileURI(filepath.Join(root, ".argocd-ls.yaml"))
		var config *string
		configPath, _ := uriPath(configURI)
		if snapshot, ok := openFiles[configPath]; ok {
			// Filesystem identity selects authority; replies retain the client's URI.
			configURI = string(snapshot.URI())
			t := snapshot.Text()
			config = &t
		}

		input, e := workspace.LoadBuffers(ctx, root, override, config, openFiles, roots, &remaining)
		if e != nil {
			result.err = e
			result.failureURI = configURI
			return result
		}
		result.inputs = append(result.inputs, input)
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
			incomplete := false
			firstPublication := len(result.publications)
			for _, f := range currentFiles {
				uri := string(f.Snapshot.URI())
				if ctx.Err() != nil {
					result.err = ctx.Err()
					result.failureURI = uri
					return result
				}
				checked, e := analyzer.Analyze(ctx, f.Snapshot, f.Target.Version)
				if e != nil {
					result.err = e
					result.failureURI = uri
					return result
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
			result.scopes = append(result.scopes, scopeResult{projectionStart: projectionStart, root: input.Root, scope: scope, files: currentFiles, documents: docs, options: options})
			domainOptions := options
			domainOptions.Context = ctx
			for _, d := range analysis.Domain(docs, domainOptions) {
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
	complete := previous.err == nil
	for _, scopes := range [][]scopeResult{previous.scopes, result.scopes} {
		for _, scope := range scopes {
			complete = complete && scope.options.Complete
		}
	}
	result.unchangedInputs = complete && reflect.DeepEqual(previous.inputs, result.inputs)
	return result
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
		if !ok || prior.Target != f.Target || prior.Output != f.Output || prior.Snapshot.Revision() != f.Snapshot.Revision() || prior.Snapshot.Text() != f.Snapshot.Text() {
			return false
		}
	}
	return true
}

func uriPath(uri string) (string, error) {
	u, e := url.Parse(uri)
	if e != nil || u.Scheme != "file" || (u.Host != "" && u.Host != "localhost") {
		return "", fmt.Errorf("only local file URIs are supported")
	}
	// A Windows drive URI has a leading slash that is not part of the drive path.
	if filepath.Separator == '\\' && len(u.Path) >= 4 && u.Path[0] == '/' && u.Path[2:4] == ":/" && ((u.Path[1] >= 'A' && u.Path[1] <= 'Z') || (u.Path[1] >= 'a' && u.Path[1] <= 'z')) {
		u.Path = u.Path[1:]
	}
	p := filepath.FromSlash(u.Path)
	if !filepath.IsAbs(p) {
		return "", fmt.Errorf("absolute file URI required")
	}
	if real, err := filepath.EvalSymlinks(p); err == nil {
		return real, nil
	}
	return filepath.Clean(p), nil
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
				for _, fix := range analysis.FixesInRange(ctx, doc, scope.documents, scope.options, source.Span{Start: rangeStart, End: rangeEnd}) {
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
				for _, fix := range analysis.FixesInRange(ctx, doc, scope.documents, scope.options, source.Span{Start: data.Start, End: data.End}) {
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
