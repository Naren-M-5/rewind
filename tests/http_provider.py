"""Loopback payment server used ONLY by integration tests; never PayPal evidence."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

orders = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def reply(self,body,status=200):
        data=json.dumps(body).encode();self.send_response(status)
        self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)))
        self.end_headers();self.wfile.write(data)
    def do_GET(self):
        if self.path=='/health':return self.reply({'stub':True})
        if self.path.startswith('/v2/checkout/orders/'):
            return self.reply(orders[self.path.split('/')[-1]])
        if self.path.startswith('/v2/payments/captures/'):
            cid=self.path.split('/')[-1]
            cap=next(c for o in orders.values() for u in o['purchase_units'] for c in u.get('payments',{}).get('captures',[]) if c['id']==cid)
            return self.reply(cap)
        self.reply({},404)
    def do_POST(self):
        raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
        if self.path=='/v1/oauth2/token':return self.reply({'access_token':'TEST-TOKEN'})
        body=json.loads(raw or b'{}')
        if self.path=='/v2/checkout/orders':
            oid='STUB-ORDER-'+uuid4().hex[:12]
            order={'id':oid,'status':'APPROVED','purchase_units':body['purchase_units']}
            orders[oid]=order;return self.reply(order,201)
        if self.path.endswith('/capture'):
            oid=self.path.split('/')[-2];order=orders[oid];u=order['purchase_units'][0]
            if not u.get('payments'):
                u['payments']={'captures':[{'id':'STUB-CAP-'+uuid4().hex[:12],'status':'COMPLETED','amount':u['amount']}]}
                order['status']='COMPLETED'
            return self.reply(order,201)
        if self.path.endswith('/refund'):
            cid=self.path.split('/')[-2]
            cap=next(c for o in orders.values() for u in o['purchase_units'] for c in u.get('payments',{}).get('captures',[]) if c['id']==cid)
            cap['status']='REFUNDED';cap.setdefault('refund_id','STUB-REF-'+uuid4().hex[:12])
            return self.reply({'id':cap['refund_id'],'status':'COMPLETED'},201)
        self.reply({},404)


if __name__=='__main__':
    ThreadingHTTPServer(('127.0.0.1',int(os.environ['TEST_PROVIDER_PORT'])),Handler).serve_forever()
