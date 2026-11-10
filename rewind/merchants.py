import hashlib
import os
import httpx
from .providers import ProviderError


class HTTPMerchant:
    name = 'http'

    async def request(self, method, path, body=None):
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            r = await client.request(method, os.getenv('REWIND_MERCHANT_URL', 'http://127.0.0.1:8011').rstrip('/') + path,
                headers={'X-Rewind-Service-Token': os.getenv('REWIND_SERVICE_TOKEN', '')}, json=body)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise ProviderError('Merchant verification failed (HTTP ' + str(r.status_code) + ')')
        job = r.json()
        if job.get('status') not in {'failed', 'completed', 'pending'}:
            raise ProviderError('Invalid merchant state')
        if job.get('artifact') and hashlib.sha256(job['artifact'].encode()).hexdigest() != job.get('sha256'):
            raise ProviderError('Merchant artifact digest mismatch')
        return job

    async def execute(self, task):
        return await self.request('POST', '/jobs', {'task_id': task['id'], 'scenario': task['scenario']})

    async def status(self, task):
        return await self.request('GET', '/jobs/' + task['id'])

    async def deliver(self, task, fault='none'):
        return await self.request('POST', '/jobs/' + task['id'] + '/deliver', {'fault': fault})


class InlineMerchant:
    """Explicit unit-test / offline rehearsal adapter, never the default service topology."""
    name = 'inline'

    async def execute(self, task):
        failed = task['scenario'] == 'true_failure'
        artifact = None if failed else 'REWIND inline fixture for task ' + task['id']
        return {'task_id': task['id'], 'status': 'failed' if failed else 'completed',
                'terminal': True, 'artifact': artifact,
                'sha256': hashlib.sha256(artifact.encode()).hexdigest() if artifact else None,
                'delivery': 'pending', 'receipt_id': None}

    async def status(self, task):
        if not task.get('merchant_terminal'):
            return None
        return {'task_id': task['id'], 'status': task['fulfillment'], 'terminal': True,
                'artifact': task['artifact'], 'sha256': task.get('artifact_hash'),
                'delivery': task['delivery'], 'receipt_id': task.get('receipt_id')}

    async def deliver(self, task, fault='none'):
        state = await self.status(task)
        state.update(delivery='lost' if fault != 'none' else 'delivered',
                     receipt_id=None if fault != 'none' else 'INLINE-RCPT-' + task['id'])
        return state
