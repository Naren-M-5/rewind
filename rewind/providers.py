import json
import os
from uuid import uuid4

import httpx


class ProviderError(Exception):
    pass


class Emulator:
    """Persistent fake provider. Every identifier visibly starts with SIM-."""
    name = 'emulator'

    def __init__(self, store):
        self.store = store

    def _read(self, oid):
        row = self.store.db.execute('SELECT body FROM provider_orders WHERE id=?', (oid,)).fetchone()
        if not row:
            raise ProviderError('Order missing')
        return json.loads(row[0])

    def _write(self, body):
        with self.store.db:
            self.store.db.execute('INSERT INTO provider_orders VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                                  (body['id'], json.dumps(body)))

    async def create(self, task, key):
        oid = 'SIM-ORDER-' + task['id']
        try:
            return self._read(oid)
        except ProviderError:
            body = {'id': oid, 'status': 'APPROVED', 'purchase_units': [
                {'custom_id': task['id'], 'amount': {'value': task['amount'], 'currency_code': 'USD'}}]}
            self._write(body)
            return body

    async def capture(self, oid, key):
        body = self._read(oid)
        unit = body['purchase_units'][0]
        if not unit.get('payments'):
            unit['payments'] = {'captures': [{'id': 'SIM-CAP-' + uuid4().hex[:12],
                'status': 'COMPLETED', 'amount': unit['amount']} ]}
            body['status'] = 'COMPLETED'
            self._write(body)
        return body

    async def order(self, oid):
        return self._read(oid)

    async def capture_details(self, cid):
        for row in self.store.db.execute('SELECT body FROM provider_orders'):
            body = json.loads(row[0])
            caps = body['purchase_units'][0].get('payments', {}).get('captures', [])
            if caps and caps[0]['id'] == cid:
                return caps[0]
        raise ProviderError('Capture missing')

    async def refund(self, cid, amount, key):
        for row in self.store.db.execute('SELECT body FROM provider_orders'):
            body = json.loads(row[0])
            caps = body['purchase_units'][0].get('payments', {}).get('captures', [])
            if caps and caps[0]['id'] == cid:
                cap = caps[0]
                if cap['status'] != 'REFUNDED':
                    cap['status'] = 'REFUNDED'
                    cap['refund_id'] = 'SIM-REF-' + uuid4().hex[:12]
                    self._write(body)
                return {'id': cap['refund_id'], 'status': 'COMPLETED'}
        raise ProviderError('Capture missing')


class PayPalSandbox:
    name = 'sandbox'
    base = 'https://api-m.sandbox.paypal.com'

    async def request(self, method, path, body=None, key=None, fault=False):
        client_id, secret = os.getenv('PAYPAL_CLIENT_ID'), os.getenv('PAYPAL_CLIENT_SECRET')
        if not client_id or not secret:
            raise ProviderError('Set PayPal sandbox credentials in your local .env')
        async with httpx.AsyncClient(timeout=25, trust_env=not self.base.startswith('http://127.0.0.1:')) as client:
            auth = await client.post(self.base + '/v1/oauth2/token', auth=(client_id, secret),
                                     data={'grant_type': 'client_credentials'})
            if auth.status_code != 200:
                raise ProviderError('Sandbox authentication failed; check credentials')
            headers = {'Authorization': 'Bearer ' + auth.json()['access_token'],
                       'Content-Type': 'application/json', 'Prefer': 'return=representation'}
            if key:
                headers['PayPal-Request-Id'] = key
            target = self.base + path
            if fault:
                proxy = os.getenv('REWIND_FAULT_PROXY_URL', 'http://127.0.0.1:8013').rstrip('/')
                target = proxy + '/paypal' + path
                headers.update({'X-Rewind-Service-Token': os.getenv('REWIND_SERVICE_TOKEN', ''),
                                'X-Rewind-Fault': 'drop_after', 'X-Rewind-Fault-Id': 'capture-' + key})
            if fault:
                async with httpx.AsyncClient(timeout=25, trust_env=False) as proxy_client:
                    r = await proxy_client.request(method, target, headers=headers, json=body)
            else:
                r = await client.request(method, target, headers=headers, json=body)
            if r.status_code >= 400:
                payload = r.json()
                # No raw payloads, credentials, or buyer PII exposed to browser/logs.
                raise ProviderError(f"PayPal {r.status_code}: {payload.get('name','API_ERROR')} (debug {payload.get('debug_id','n/a')})")
            return r.json() if r.content else {}

    async def create(self, task, key):
        url = os.getenv('REWIND_PUBLIC_URL', 'http://127.0.0.1:8000').rstrip('/')
        return await self.request('POST', '/v2/checkout/orders', {
            'intent': 'CAPTURE', 'purchase_units': [{'custom_id': task['id'],
                'description': 'REWIND sandbox report task',
                'amount': {'currency_code': 'USD', 'value': task['amount']}}],
            'payment_source': {'paypal': {'experience_context': {
                'return_url': url + '/?approved=' + task['id'],
                'cancel_url': url + '/?cancelled=' + task['id'], 'user_action': 'PAY_NOW'}}}}, key)

    async def order(self, oid):
        return await self.request('GET', '/v2/checkout/orders/' + oid)

    async def capture(self, oid, key):
        return await self.request('POST', '/v2/checkout/orders/' + oid + '/capture', {}, key)

    async def capture_fault(self, oid, key):
        return await self.request('POST', '/v2/checkout/orders/' + oid + '/capture', {}, key, fault=True)

    async def capture_details(self, cid):
        return await self.request('GET', '/v2/payments/captures/' + cid)

    async def refund(self, cid, amount, key):
        return await self.request('POST', '/v2/payments/captures/' + cid + '/refund',
                                  {'amount': {'value': amount, 'currency_code': 'USD'}}, key)

    async def verify_webhook(self, headers, event):
        webhook_id = os.getenv('PAYPAL_WEBHOOK_ID')
        if not webhook_id:
            raise ProviderError('Webhook ID not configured')
        values = {'auth_algo': headers.get('paypal-auth-algo'),
                  'cert_url': headers.get('paypal-cert-url'),
                  'transmission_id': headers.get('paypal-transmission-id'),
                  'transmission_sig': headers.get('paypal-transmission-sig'),
                  'transmission_time': headers.get('paypal-transmission-time')}
        if not all(values.values()):
            return False
        result = await self.request('POST', '/v1/notifications/verify-webhook-signature',
                                    {**values, 'webhook_id': webhook_id, 'webhook_event': event})
        return result.get('verification_status') == 'SUCCESS'

