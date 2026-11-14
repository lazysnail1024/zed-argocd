#!/usr/bin/env python3
"""Run real faulty peer processes against the thin session transport."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

from support.session import Session

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False);rows=[]
    peers={'malformed':b'Content-Length: bad\r\n\r\n', 'duplicate':b'Content-Length: 2\r\nContent-Length: 3\r\n\r\n{}',
           'truncated':b'Content-Length: 100\r\n\r\n{}','oversized':b'Content-Length: 999999999\r\n\r\n', 'early-exit':b'', 'never-responds':None, 'stderr-quota':None}
    for name,frame in peers.items():
        folder=args.output/name;folder.mkdir();peer=folder/'peer.py'
        peer.write_text('import sys,time\n'+('time.sleep(30)\n' if frame is None else 'sys.stdout.buffer.write('+repr(frame)+');sys.stdout.buffer.flush()\n'))
        if name=='stderr-quota':peer.write_text("import sys,time\nsys.stderr.buffer.write(b'x'*65536);sys.stderr.flush();time.sleep(30)\n")
        client=Session.__new__(Session)
        try:
            client.__init__(folder,[],command=[sys.executable,str(peer.resolve())],initialize_timeout=.3, transcript_limit=4096)
            rows.append({'id':name,'passed':False,'error':'Unexpected initialize success'})
        except (RuntimeError,TimeoutError,BrokenPipeError) as error:
            rows.append({'id':name,'passed':True,'observed':str(error)})
        finally:
            if hasattr(client,'proc'):client.close()
        rows[-1]['cleaned']=client.proc.poll() is not None
        rows[-1]['passed'] &= rows[-1]['cleaned'] and (folder/'lifecycle.json').is_file()
    folder=args.output/'server-request';folder.mkdir();peer=folder/'peer.py'
    peer.write_text("""import sys,json
def read():
 header=b''
 while not header.endswith(b'\\r\\n\\r\\n'):header+=sys.stdin.buffer.read(1)
 return json.loads(sys.stdin.buffer.read(int(header.split(b':',1)[1].strip())))
def send(value):
 body=json.dumps(value).encode();sys.stdout.buffer.write(('Content-Length: '+str(len(body))+'\\r\\n\\r\\n').encode()+body);sys.stdout.buffer.flush()
read()
send({'jsonrpc':'2.0','id':99,'method':'workspace/configuration','params':{'items':[]}})
reply=read()
assert reply['id']==99 and reply['result']==[]
send({'jsonrpc':'2.0','id':1,'result':{'capabilities':{}}})
read()
request=read()
send({'jsonrpc':'2.0','id':request['id'],'result':None})
assert read()['method']=='exit'
""")
    client=None
    try:
        client=Session(folder,[],command=[sys.executable,str(peer.resolve())],server_request=lambda request:[])
        client.close()
        rows.append({'id':'server-request-dispatch','passed':client.proc.returncode==0 and client.lifecycle['shutdownResponse'] and client.lifecycle['exitSent']})
    except Exception as error:rows.append({'id':'server-request-dispatch','passed':False,'error':repr(error)})
    finally:
        if client:client.close()
    folder=args.output/'interleaved';folder.mkdir();peer=folder/'peer.py'
    peer.write_text((args.output/'server-request'/'peer.py').read_text().split('read()\nsend(')[0]+"""read()
send({'jsonrpc':'2.0','id':1,'result':{'capabilities':{}}})
read()
request=read()
send({'jsonrpc':'2.0','method':'textDocument/publishDiagnostics','params':{'uri':'file:///workspace/test.yaml','version':1,'diagnostics':[{'message':'retained'}]}})
send({'jsonrpc':'2.0','id':900,'result':'other-response'})
send({'jsonrpc':'2.0','id':request['id'],'result':'requested'})
request=read()
send({'jsonrpc':'2.0','id':request['id'],'result':None})
assert read()['method']=='exit'
""")
    client=None
    try:
        client=Session(folder,[],command=[sys.executable,str(peer.resolve())])
        answer=client.request('test',{})
        diagnostic=client.wait(lambda m:m.get('method')=='textDocument/publishDiagnostics',.3)
        other=client.wait(lambda m:m.get('id')==900,.3)
        checks={'requested':answer.get('result')=='requested','diagnostic':diagnostic['params']['diagnostics']==[{'message':'retained'}],'otherResponse':other.get('result')=='other-response'}
        client.close();checks['cleanShutdown']=client.proc.returncode==0 and client.lifecycle['shutdownResponse']
        rows.append({'id':'interleaved-messages','checks':checks,'passed':all(checks.values())})
    except Exception as error:rows.append({'id':'interleaved-messages','passed':False,'error':repr(error)})
    finally:
        if client:client.close()
    report={'rows':rows,'passed':all(x['passed'] for x in rows),'runnerSHA256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report));return 0 if report['passed'] else 1
if __name__=='__main__':sys.exit(main())
