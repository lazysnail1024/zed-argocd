"""Thin stdio session; domain correctness remains in the calling consumers."""
import json, os, queue, signal, subprocess, threading, time
from native_ci import start_command

class Session:
    def __init__(self, out, roots, binary=None, command=None, initialize_timeout=12, transcript_limit=128*1024*1024, server_request=None):
        self.closed=False
        self.out = out
        self.start = time.monotonic()
        self.proc, self.close_group = start_command(command or [str(binary), 'serve', '--stdio'], dict(os.environ), stdin=subprocess.PIPE)
        self.write_lock=threading.Lock()
        self.transcript_limit=transcript_limit
        self.received_bytes=self.sent_bytes=self.stderr_bytes=0
        self.server_request=server_request
        self.pending = []
        self.lifecycle = {'shutdownResponse': False, 'exitSent': False, 'reaped': False}
        self.q, self.messages, self.sent, self.raw = queue.Queue(), [], [], []
        self.stderr_chunks = []
        self.seq = 10
        def read():
            try:
                while True:
                    header = b''
                    while not header.endswith(b'\r\n\r\n'):
                        c = self.proc.stdout.read(1)
                        if not c:
                            raise EOFError("Peer exited before next frame")
                        header += c
                        if len(header)>8192:raise ValueError("LSP header exceeds 8192 bytes")
                    lengths=[line.split(b':',1)[1] for line in header.split(b'\r\n') if line.lower().startswith(b'content-length:')]
                    if len(lengths)!=1:raise ValueError('Expected exactly one Content-Length')
                    length=int(lengths[0])
                    if not 0 <= length <= 32*1024*1024:raise ValueError('Frame exceeds transport bound')
                    body = self.proc.stdout.read(length)
                    self.received_bytes+=len(header)+len(body)
                    if self.received_bytes>self.transcript_limit:raise ValueError('stdout transcript quota exceeded')
                    self.raw.append(header + body)
                    if len(body)!=length:raise EOFError("Truncated LSP body")
                    msg = json.loads(body)
                    self.messages.append({'elapsed': time.monotonic()-self.start, 'message': msg})
                    if not isinstance(msg,dict):raise ValueError('JSON-RPC message is not an object')
                    if 'method' in msg and 'id' in msg and self.server_request is not None:
                        try:
                            response={'jsonrpc':'2.0','id':msg['id'],'result':self.server_request(msg)}
                        except Exception as error:response={'jsonrpc':'2.0','id':msg['id'],'error':{'code':-32603,'message':str(error)}}
                        self.write_message(response)
                    else:self.q.put(msg)
            except Exception as exc:
                self.q.put({'readerError': repr(exc)})
        self.thread = threading.Thread(target=read, daemon=True)
        self.thread.start()
        # Trace-enabled consumers can exceed a pipe's capacity before close().
        # Drain stderr concurrently so test instrumentation cannot stall LSP I/O.
        def read_stderr():
            while True:
                chunk = self.proc.stderr.read(65536)
                if not chunk:
                    return
                self.stderr_bytes+=len(chunk)
                if self.stderr_bytes<=self.transcript_limit:self.stderr_chunks.append(chunk)
                elif self.stderr_bytes-len(chunk)<=self.transcript_limit:self.q.put({'readerError':'stderr transcript quota exceeded'})
        self.stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        self.stderr_thread.start()
        try:
            self.send('initialize', {'processId': None, 'workspaceFolders':[{'uri':r.as_uri(),'name':r.name} for r in roots], 'capabilities':{'workspace':{'workspaceEdit':{'documentChanges':True}}}}, 1)
            self.wait(lambda m: m.get('id') == 1, initialize_timeout)
            self.send('initialized', {})
        except BaseException:
            self.close()
            raise

    def send(self, method, params=None, ident=None):
        # A new workspace/document event invalidates older diagnostic publications.
        # Keep unrelated responses available to their own waiters.
        if method in ('textDocument/didOpen', 'textDocument/didChange', 'textDocument/didClose',
                      'workspace/didChangeConfiguration', 'workspace/didChangeWatchedFiles',
                      'workspace/didChangeWorkspaceFolders'):
            while True:
                try:self.pending.append(self.q.get_nowait())
                except queue.Empty:break
            uri=(params or {}).get('textDocument',{}).get('uri')
            self.pending=[m for m in self.pending if m.get('method')!='textDocument/publishDiagnostics'
                          or (uri is not None and m.get('params',{}).get('uri')!=uri)]
        msg = {'jsonrpc':'2.0', 'method':method}
        if params is not None:
            msg['params'] = params
        if ident is not None:
            msg['id'] = ident
        self.write_message(msg)

    def write_message(self,msg):
        body=json.dumps(msg,ensure_ascii=False).encode()
        frame=f'Content-Length: {len(body)}\r\n\r\n'.encode()+body
        with self.write_lock:
            self.sent_bytes+=len(frame)
            if self.sent_bytes>self.transcript_limit:raise ValueError('stdin transcript quota exceeded')
            self.sent.append(frame)
            self.proc.stdin.write(frame)
            self.proc.stdin.flush()

    def wait(self, predicate, timeout=12):
        for index, msg in enumerate(self.pending):
            if "readerError" in msg:raise RuntimeError(msg["readerError"])
            if predicate(msg):return self.pending.pop(index)
        end = time.monotonic()+timeout
        while True:
            remaining = end-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('LSP response deadline')
            try:
                msg = self.q.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError('LSP response deadline')
            if "readerError" in msg:raise RuntimeError(msg["readerError"])
            if predicate(msg):
                return msg
            self.pending.append(msg) # Total retained frames remain bounded by transcript_limit.

    def request(self, method, params, timeout=12):
        self.seq += 1
        ident = self.seq
        self.send(method, params, ident)
        return self.wait(lambda m: m.get('id') == ident, timeout)

    def open(self, path, text, version=1, language_id='yaml'):
        self.send('textDocument/didOpen', {'textDocument':{'uri':path.as_uri(),'languageId':language_id,'version':version,'text':text}})

    def change(self, path, text, version):
        self.send('textDocument/didChange', {'textDocument':{'uri':path.as_uri(),'version':version},'contentChanges':[{'text':text}]})

    def diag(self, path, version, scope='workspace'):
        # A document-only publication is not proof that cross-file analysis finished.
        # Unmarked publications from released servers retain the workspace contract.
        return self.wait(lambda m: m.get('method') == 'textDocument/publishDiagnostics' and m.get('params',{}).get('uri') == path.as_uri() and m['params'].get('version') == version and m['params'].get('x-argocd-analysis-scope', 'workspace') == scope)['params']['diagnostics']

    def close(self):
        if self.closed:return
        self.closed=True
        if self.proc.poll() is None:
            try:
                response=self.request('shutdown', {}, timeout=5)
                self.lifecycle['shutdownResponse']='error' not in response
                self.send('exit')
                self.lifecycle['exitSent']=True
                self.proc.wait(timeout=5)
            except Exception:
                if os.name!='nt':
                    try:os.killpg(self.proc.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                else:self.proc.kill()
                self.proc.wait(timeout=5)
        self.close_group() # Also kill descendants when the direct peer already exited.
        self.lifecycle['transcriptQuotaBytes']=self.transcript_limit
        self.lifecycle['transcriptExceeded']=any(n>self.transcript_limit for n in (self.sent_bytes,self.received_bytes,self.stderr_bytes))
        self.lifecycle['reaped']=self.proc.poll() is not None
        self.lifecycle['exitCode']=self.proc.returncode
        (self.out/'lifecycle.json').write_text(json.dumps(self.lifecycle,indent=2)+'\n')
        self.thread.join(timeout=2)
        self.stderr_thread.join(timeout=2)
        (self.out/'stdin').write_bytes(b''.join(self.sent))
        (self.out/'stdout').write_bytes(b''.join(self.raw))
        (self.out/'stderr').write_bytes(b''.join(self.stderr_chunks))
        (self.out/'messages.json').write_text(json.dumps(self.messages, indent=2)+'\n')

