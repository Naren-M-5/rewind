"""Fixed-upstream HTTP fault proxy. Faults close an actual socket, once per persisted key."""
import json
import os
import secrets
import socket
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx


def serve(host='127.0.0.1', port=8013, paypal_base='https://api-m.sandbox.paypal.com'):
    db = sqlite3.connect(os.getenv('REWIND_PROXY_DB', 'rewind-proxy.db'), check_same_thread=False)
    db.execute('CREATE TABLE IF NOT EXISTS faults(id TEXT PRIMARY KEY, mode TEXT, upstream_status INTEGER)')
    db.commit()
    lock = threading.Lock()
    targets = {'/paypal/': paypal_base.rstrip('/') + '/',
               '/buyer/': os.getenv('REWIND_INBOX_URL', 'http://127.0.0.1:8012').rstrip('/') + '/'}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Request headers may contain provider credentials.

        def reply(self, status, payload):
            self.send_response(status); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)

        def disconnect(self):
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()

        def do_GET(self):
            token = os.getenv('REWIND_SERVICE_TOKEN', '')
            if token and not secrets.compare_digest(self.headers.get('X-Rewind-Service-Token', ''), token):
                return self.reply(401, b'{"error":"service authentication failed"}')
            if not token and self.client_address[0] not in {'127.0.0.1', '::1'}:
                return self.reply(403, b'{"error":"localhost only"}')
            if self.path == '/health':
                return self.reply(200, b'{"service":"fault-proxy"}')
            if not self.path.startswith('/faults/'):
                return self.reply(404, b'{"error":"route not found"}')
            with lock:
                row = db.execute('SELECT mode,upstream_status FROM faults WHERE id=?', (self.path[len('/faults/'):],)).fetchone()
            if not row:
                return self.reply(404, b'{"error":"fault not observed"}')
            self.reply(200, json.dumps({'mode': row[0], 'upstream_status': row[1]}).encode())

        def do_POST(self):
            token = os.getenv('REWIND_SERVICE_TOKEN', '')
            if token and not secrets.compare_digest(self.headers.get('X-Rewind-Service-Token', ''), token):
                return self.reply(401, b'{"error":"service authentication failed"}')
            if not token and self.client_address[0] not in {'127.0.0.1', '::1'}:
                return self.reply(403, b'{"error":"localhost only"}')
            prefix = next((p for p in targets if self.path.startswith(p)), None)
            if not prefix or '..' in self.path or '?' in self.path:
                return self.reply(404, b'{"error":"route not allowed"}')
            suffix = self.path[len(prefix):]
            if prefix == '/paypal/' and not (suffix.startswith('v2/checkout/orders/') and suffix.endswith('/capture')):
                return self.reply(404, b'{"error":"only sandbox capture is proxied"}')
            if prefix == '/buyer/' and suffix != 'receipts':
                return self.reply(404, b'{"error":"only inbox receipt writes are proxied"}')
            size = int(self.headers.get('Content-Length', '0'))
            if size > 200_000:
                return self.reply(413, b'{"error":"body too large"}')
            body = self.rfile.read(size)
            fault, key = self.headers.get('X-Rewind-Fault', 'none'), self.headers.get('X-Rewind-Fault-Id', '')
            if fault not in {'none', 'drop_before', 'drop_after'} or (fault != 'none' and not key):
                return self.reply(400, b'{"error":"invalid fault configuration"}')
            if fault != 'none' and os.getenv('REWIND_CHAOS', 'true').lower() != 'true':
                return self.reply(403, b'{"error":"fault injection disabled"}')
            with lock:
                used = db.execute('SELECT 1 FROM faults WHERE id=?', (key,)).fetchone() if key else True
                inject = fault != 'none' and not used
                if inject and fault == 'drop_before':
                    with db:
                        db.execute('INSERT INTO faults VALUES(?,?,NULL)', (key, fault))
                    return self.disconnect()
                forwarding = {k: v for k, v in self.headers.items() if k.lower() in {
                    'authorization', 'content-type', 'paypal-request-id', 'prefer'}}
                if prefix == '/buyer/':
                    forwarding['X-Rewind-Service-Token'] = token
                try:
                    with httpx.Client(timeout=30, follow_redirects=False, trust_env=not targets[prefix].startswith('http://127.0.0.1:')) as client:
                        result = client.post(targets[prefix] + suffix, content=body, headers=forwarding)
                except httpx.HTTPError:
                    return self.reply(502, b'{"error":"upstream unavailable"}')
                if inject and fault == 'drop_after' and 200 <= result.status_code < 300:
                    with db:
                        db.execute('INSERT INTO faults VALUES(?,?,?)', (key, fault, result.status_code))
                    return self.disconnect()
                return self.reply(result.status_code, result.content)

    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv()
    serve(port=int(os.getenv('REWIND_PROXY_PORT', '8013')))
