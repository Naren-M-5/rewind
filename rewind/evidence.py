"""Separate missing delivery from incompatible fulfillment claims."""


def assess(task, events, diagnosis=None):
    committed = [e for e in events if e['kind'] == 'ARTIFACT_COMMITTED']
    failed = [e for e in events if e['kind'] == 'MERCHANT_EXECUTION_FAILED']
    hashes = {e['detail'].get('sha256') for e in committed if e['detail'].get('sha256')}
    trusted_conflict = bool((committed and failed) or len(hashes) > 1 or task.get('merchant_conflict'))
    ai_conflict = bool(diagnosis and diagnosis.get('live_ai') and (
        diagnosis.get('conflict_type') == 'fulfillment_conflict' or (
            diagnosis.get('conflict_type') is None and diagnosis.get('contradictions'))))
    gap = bool(committed and task.get('delivery') in {'lost', 'unknown', 'pending'})
    return {'contradiction': trusted_conflict or ai_conflict,
            'delivery_gap': gap,
            'conflict_kind': 'fulfillment_conflict' if trusted_conflict or ai_conflict else 'delivery_gap' if gap else 'none'}
