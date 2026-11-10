import asyncio
import hashlib
import os
from datetime import datetime, timezone
from uuid import uuid4

import httpx

from . import ai
from .providers import ProviderError
from .store import now
from .evidence import assess
from .merchants import HTTPMerchant, InlineMerchant


def policy(task):
    """Only validated payment and merchant state select executable actions."""
    if task.get('contradiction') or task['payment'] in ['unknown', 'pending', 'partial_refund']:
        return 'review', 'Payment or fulfillment evidence is unresolved. No money action allowed.'
    if task['payment'] == 'refunded':
        return 'none', 'Payment already refunded.'
    if task['payment'] != 'captured':
        return 'none', 'No completed capture to recover.'
    if task['fulfillment'] == 'completed' and task.get('artifact'):
        if task['delivery'] != 'delivered':
            return 'redeliver', 'Completed artifact exists. Deliver it instead of refunding completed work.'
        return 'none', 'Payment, fulfillment, and delivery are complete.'
    if task['fulfillment'] == 'failed' and not task.get('artifact') and task.get('merchant_terminal'):
        return 'refund', 'Merchant execution terminated without an artifact. Refund the verified capture.'
    return 'review', 'A timeout alone does not prove non-fulfillment. Wait for authoritative merchant evidence.'


class Engine:
    def __init__(self, store, provider, merchant=None):
        self.store, self.provider = store, provider
        self.merchant_client = merchant or (InlineMerchant() if os.getenv("REWIND_MERCHANT", "http") == "inline" else HTTPMerchant())
        self.locks = {}

    def lock(self, tid):
        # One uvicorn worker only. Durable request journal handles process restarts.
        return self.locks.setdefault(tid, asyncio.Lock())

    def task(self, tid):
        task = self.store.get(tid)
        if task['provider'] != self.provider.name:
            raise ProviderError('Task belongs to another provider; use a separate database per mode')
        return task

    def create(self, scenario):
        tid = uuid4().hex
        task = {'id': tid, 'scenario': scenario, 'created': now(), 'amount': '50.00',
            'provider': self.provider.name, 'intent': 'Generate one competitive research report for $50',
            'payment': 'none', 'fulfillment': 'pending', 'delivery': 'pending',
            'order_id': None, 'capture_id': None, 'refund_id': None,
            'artifact': None, 'merchant_terminal': False, 'contradiction': False,
            'diagnosis': None, 'decision': None, 'last_verified': None,
            'merchant_mode': self.merchant_client.name, 'merchant_conflict': False,
            'receipt_id': None, 'needs_diagnosis': False, 'delivery_gap': False, 'conflict_kind': 'none'}
        self.store.save(task)
        self.store.event(tid, 'TASK_CREATED', 'user', {'intent': task['intent'], 'scenario': scenario})
        return task

    def event(self, task, kind, source, detail):
        self.store.event(task['id'], kind, source, detail)

    def key(self, tid, action):
        # Stable, <=38 chars; scoped per business task and operation, never regenerated on retry.
        return hashlib.sha256(f'{tid}:{action}'.encode()).hexdigest()[:32]

    async def mutation(self, task, action, call):
        key = self.key(task['id'], action)
        op = self.store.operation(key, task['id'], action)
        if op['status'] == 'done':
            return op['result']
        # PayPal retention is endpoint-specific. Use a conservative local 1h retry window;
        # old unresolved operations require investigation, never a fresh idempotency key.
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(op['created'])).total_seconds()
        if age > 3600:
            raise ProviderError('Unresolved operation exceeds local retry window; manual review required')
        result = await call(key)
        self.store.finish(key, result)
        return result

    async def order(self, tid):
        async with self.lock(tid):
            task = self.task(tid)
            if task['order_id']:
                return task
            result = await self.mutation(task, 'create', lambda key: self.provider.create(task, key))
            task['order_id'] = result['id']
            task['payment'] = 'created'
            task['approval_url'] = next((l['href'] for l in result.get('links', [])
                                         if l['rel'] in ['approve', 'payer-action']), None)
            self.store.save(task)
            self.event(task, 'ORDER_CREATED', self.provider.name, {'order_id': result['id']})
            return task

    async def _reconcile(self, task):
        if not task['order_id']:
            return task
        body = await self.provider.order(task['order_id'])
        units = body.get('purchase_units', [])
        if len(units) != 1 or units[0].get('custom_id') != task['id']:
            raise ProviderError('Order task binding mismatch; recovery stopped')
        caps = units[0].get('payments', {}).get('captures', [])
        task['capture_count'] = len(caps)
        if len(caps) > 1:
            raise ProviderError('Multiple captures require manual review')
        if caps:
            cap = await self.provider.capture_details(caps[0]['id'])
            if cap.get('amount') != {'currency_code': 'USD', 'value': task['amount']}:
                raise ProviderError('Capture amount/currency mismatch; recovery stopped')
            state = cap.get('status')
            task['payment'] = {'COMPLETED': 'captured', 'REFUNDED': 'refunded',
                               'PARTIALLY_REFUNDED': 'partial_refund', 'PENDING': 'pending'}.get(state, 'unknown')
            task['capture_id'] = cap['id']
        elif task['payment'] not in ['captured', 'refunded']:
            task['payment'] = 'created' if body.get('status') in ['CREATED', 'APPROVED', 'PAYER_ACTION_REQUIRED', 'SAVED'] else 'unknown'
        else:
            # Absence in one read cannot undo prior positive financial evidence.
            task['payment'] = 'unknown'
        task['last_verified'] = now()
        self.store.save(task)
        self.event(task, 'STATE_RECONCILED', self.provider.name, {
            'order_id': task['order_id'], 'capture_id': task['capture_id'], 'payment': task['payment']})
        return task

    async def reconcile(self, tid):
        async with self.lock(tid):
            return await self._reconcile(self.task(tid))

    async def capture(self, tid, drop=False):
        async with self.lock(tid):
            task = self.task(tid)
            if not task['order_id']:
                raise ValueError('Create and approve the order first')
            # Includes task-level duplicate prevention even if an agent proposes a NEW API key/order.
            await self._reconcile(task)
            if task['payment'] in ['captured', 'refunded', 'partial_refund', 'pending']:
                self.event(task, 'DUPLICATE_INTENT_BLOCKED', 'policy', {
                    'reason': 'Task already has a payment; no new order or capture permitted'})
                return task
            if task['payment'] != 'created':
                raise ProviderError('Unresolved payment state; reconcile or review before capture')
            self.event(task, 'CAPTURE_REQUESTED', 'agent', {'request_id': self.key(tid, 'capture')})
            try:
                network_fault = drop and hasattr(self.provider, 'capture_fault')
                result = await self.mutation(task, 'capture', lambda key: self.provider.capture_fault(task['order_id'], key)
                    if network_fault else self.provider.capture(task['order_id'], key))
                if drop and not network_fault:
                    # Controlled fault adapter: provider completed real capture, downstream response discarded.
                    task['payment'] = 'unknown'
                    self.store.save(task)
                    self.event(task, 'RESPONSE_DROPPED', 'chaos', {
                        'simulated': True, 'fault_mode': 'inline_fixture', 'boundary': 'provider capture → agent acknowledgement'})
                    return task
                self.event(task, 'CAPTURE_ACKNOWLEDGED', self.provider.name, {'order_id': result['id']})
                return await self._reconcile(task)
            except (httpx.TransportError, ProviderError):
                task['payment'] = 'unknown'
                self.store.save(task)
                self.event(task, 'CAPTURE_UNCERTAIN', 'transport', {'next': 'Reconcile provider state; do not create another order',
                    'fault_requested': drop, 'fault_mode': 'socket_proxy' if drop and hasattr(self.provider, 'capture_fault') else 'transport'})
                if drop and hasattr(self.provider, 'capture_fault'):
                    return task
                raise

    def assess_evidence(self, task):
        task.update(assess(task, self.store.events(task['id']), task.get('diagnosis')))
        return self.store.save(task)

    def sync_merchant(self, task, job):
        if not job:
            return task
        if job.get('task_id') != task['id']:
            raise ProviderError('Merchant task binding mismatch')
        artifact = job.get('artifact')
        if artifact and hashlib.sha256(artifact.encode()).hexdigest() != job.get('sha256'):
            raise ProviderError('Merchant artifact digest mismatch')
        if (job['status'] == 'failed' and artifact) or (
            task.get('merchant_terminal') and task['fulfillment'] != job['status']):
            task['merchant_conflict'] = True
        task.update(fulfillment=job['status'], merchant_terminal=bool(job.get('terminal')),
            artifact=artifact, artifact_hash=job.get('sha256'),
            delivery=job.get('delivery', 'unknown'), receipt_id=job.get('receipt_id'))
        if task['delivery'] == 'delivered' and not task['receipt_id']:
            task['delivery'] = 'unknown'
        source = 'merchant_http' if self.merchant_client.name == 'http' else 'inline_fixture'
        if artifact:
            self.store.event(task['id'], 'ARTIFACT_COMMITTED', source, {
                'sha256': job['sha256'], 'message': 'Merchant persisted the result'},
                dedupe='artifact:' + task['id'] + ':' + job['sha256'])
        if job['status'] == 'failed' and job.get('terminal'):
            self.store.event(task['id'], 'MERCHANT_EXECUTION_FAILED', source, {
                'terminal': True, 'message': 'Merchant reports terminal execution failure'},
                dedupe='merchant_failed:' + task['id'])
        return self.assess_evidence(task)

    async def merchant(self, tid):
        async with self.lock(tid):
            task = self.task(tid)
            await self._reconcile(task)
            if task['payment'] != 'captured':
                raise ValueError('Reconcile a completed capture before fulfillment')
            if task['merchant_terminal']:
                job = await self.merchant_client.status(task)
                return self.sync_merchant(task, job)
            job = await self.merchant_client.execute(task)
            self.sync_merchant(task, job)
            if task['fulfillment'] == 'completed':
                job = await self.merchant_client.deliver(task,
                    fault='drop_before' if task['scenario'] == 'delivery_loss' else 'none')
                self.sync_merchant(task, job)
                self.event(task, 'DELIVERY_ACKNOWLEDGED' if task['delivery'] == 'delivered' else 'DELIVERY_LOST',
                    'buyer_inbox' if self.merchant_client.name == 'http' else 'inline_fixture', {
                        'message': 'Matching buyer receipt received' if task['delivery'] == 'delivered' else 'Buyer receipt missing',
                        'receipt_id': task['receipt_id'], 'http_transport': self.merchant_client.name == 'http'})
            return self.assess_evidence(task)

    async def add_evidence(self, tid, message):
        async with self.lock(tid):
            task = self.task(tid)
            self.event(task, 'MERCHANT_MESSAGE', 'untrusted_message', {'message': message})
            task['needs_diagnosis'] = True
            return self.assess_evidence(task)

    async def diagnose(self, tid):
        async with self.lock(tid):
            task = self.task(tid)
            try:
                task['diagnosis'] = await ai.diagnose(task, self.store.events(tid))
                self.event(task, 'AI_DIAGNOSIS', 'ai', task['diagnosis'])
                task['needs_diagnosis'] = bool(task.get('needs_diagnosis') and not task['diagnosis'].get('live_ai', False))
            except (ValueError, httpx.HTTPError, KeyError, IndexError, TypeError) as exc:
                task['diagnosis'] = {'provider': 'unavailable', 'live_ai': False,
                    'recommendation': 'review', 'explanation': str(exc) if isinstance(exc, ai.AIError) else 'AI unavailable or invalid output. No inference was substituted.'}
                self.event(task, 'AI_REJECTED', 'validator', {'reason': task['diagnosis']['explanation']})
            return self.assess_evidence(task)

    async def recover(self, tid):
        async with self.lock(tid):
            task = self.task(tid)
            # Revalidate immediately before any recovery. Webhook order is never financial truth.
            await self._reconcile(task)
            if task.get('merchant_terminal'):
                self.sync_merchant(task, await self.merchant_client.status(task))
            self.assess_evidence(task)
            action, reason = policy(task)
            if action == 'refund' and task.get('diagnosis') and task['diagnosis'].get('provider') == 'unavailable':
                action, reason = 'review', 'A valid live evidence diagnosis is required before refund.'
            if action == 'refund' and task.get('needs_diagnosis'):
                action, reason = 'review', 'New untrusted evidence requires a valid live diagnosis before refund.'
            diagnosis = task.get('diagnosis')
            if diagnosis and diagnosis.get('live_ai') and (
                diagnosis['recommendation'] == 'review' or diagnosis['confidence'] < .65 or (diagnosis['contradictions'] and diagnosis.get('conflict_type') != 'delivery_gap')):
                # Ambiguous live AI adds a stop; it can never enable an unsafe money operation.
                if action == 'refund':
                    action, reason = 'review', 'Ambiguous AI evidence requires human investigation before refund.'
            override = bool(diagnosis and diagnosis['recommendation'] != action)
            task['decision'] = {'action': action, 'reason': reason, 'ai_overridden': override}
            self.store.save(task)
            self.event(task, 'POLICY_OVERRIDE' if override else 'POLICY_DECISION', 'policy', task['decision'])
            if action == 'redeliver':
                key = self.key(tid, 'redeliver')
                self.store.operation(key, tid, 'redeliver')
                # Delivery is an idempotent HTTP write to a separate process.
                # Lost acknowledgement remains unknown until a matching inbox receipt is read.
                job = await self.merchant_client.deliver(task)
                self.sync_merchant(task, job)
                if task['delivery'] == 'delivered':
                    self.store.finish(key, {'sha256': task['artifact_hash'], 'receipt_id': task['receipt_id']})
                    self.event(task, 'RESULT_REDELIVERED', 'buyer_inbox' if self.merchant_client.name == 'http' else 'inline_fixture',
                        {'sha256': task['artifact_hash'], 'receipt_id': task['receipt_id'],
                         'http_transport': self.merchant_client.name == 'http'})
                else:
                    self.event(task, 'REDELIVERY_UNCERTAIN', 'transport', {'next': 'Read buyer receipt before claiming delivery'})
            elif action == 'refund':
                try:
                    result = await self.mutation(task, 'refund', lambda key: self.provider.refund(
                        task['capture_id'], task['amount'], key))
                    task['refund_id'] = result['id']
                    # Submission of a refund is not proof it completed.
                    task['payment'] = 'refunded' if result.get('status') == 'COMPLETED' else 'unknown'
                    self.store.save(task)
                    self.event(task, 'REFUND_COMPLETED' if task['payment'] == 'refunded' else 'REFUND_PENDING',
                               self.provider.name, {'refund_id': result['id'], 'status': result.get('status')})
                except (httpx.TransportError, ProviderError):
                    task['payment'] = 'unknown'
                    self.store.save(task)
                    self.event(task, 'REFUND_UNCERTAIN', 'transport', {'next': 'Reconcile capture before any retry'})
                    raise
            return task
