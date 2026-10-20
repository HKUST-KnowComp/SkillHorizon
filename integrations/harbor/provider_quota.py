"""Distinguish an account outage from ordinary request rate limiting."""


def account_outage(record):
    message = str((record.get('provider_error') or {}).get('message', '')).lower()
    status = record.get('status')
    if status in {401, 403} and any(text in message for text in
            ('token quota exhausted', 'token quota is not enough', 'insufficient_quota')):
        return 'quota_exhausted'
    if status == 429 and 'used invalid tokens multiple times' in message:
        return 'credential_cooldown'
    return None


def settled_quota_request(state):
    if (state.get('locked') not in {'fatal', 'retry_exhausted'}
            or state.get('pending') is not None
            or any(r.get('state') == 'active' for r in state['requests'].values())):
        raise ValueError('Unsettled provider boundary')
    last = state['records'][-1]
    rid = last['request_id']
    records = [r for r in state['records'] if r['request_id'] == rid]
    info = state['requests'][rid]
    if (not account_outage(last) or info.get('state') != 'failed'
            or len(records) != info['sends'] or not records
            or any(r.get('task_accepted') is not False or r.get('send_state') != 'attempted'
                   for r in records)):
        raise ValueError('Not a settled unaccepted account-outage request')
    return rid, last
