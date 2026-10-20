"""Shared adaptive request slots; independent of task budgets and retries."""
import time

REVISION = 'transport_wave_backoff_v2'
WAVE_SECONDS = 180.0
QUIET_SECONDS = 60.0
RECOVERY_SECONDS = 30.0
TRANSPORT_FLOOR = 4


def state_for(state, policy, now):
    meta = state.get('adaptive_v2')
    if meta is None:
        limit = state.setdefault('concurrency_limit', policy['initial_concurrency'])
        # A legacy reduction below four might have been caused by real 429s.
        # Keep that protection until observed healthy recovery, not migration.
        if not state.get('rate_limits', 0):
            limit = max(min(TRANSPORT_FLOOR, policy['max_concurrency']), limit)
        state['concurrency_limit'] = min(limit, policy['max_concurrency'])
        recent = state.get('recent_responses', [])
        last_bad = max((e['time'] for e in recent
                        if e.get('transport_failure') or e['status'] == 429), default=0)
        meta = dict(revision=REVISION, healthy=0, last_bad=last_bad,
            last_adjusted=now, last_transport_reduction=state.get('adjusted_at', 0) if last_bad else 0,
            applied_events=0, cursor=now, activated_at=now)
        state['adaptive_v2'] = meta
    return meta


def apply_event(state, policy, event):
    """Apply one completion exactly once, under the caller's traffic lock."""
    now = event['time']; meta = state_for(state, policy, now)
    limit = state['concurrency_limit']; maximum = policy['max_concurrency']
    floor = min(TRANSPORT_FLOOR, maximum)
    meta['applied_events'] += 1
    meta['cursor'] = max(meta.get('cursor', 0), now)
    if event.get('transport_failure'):
        meta.update(healthy=0, last_bad=max(meta['last_bad'], now))
        recent = [e for e in state.get('recent_responses', []) if 0 <= now-e['time'] <= 60]
        failures = sum(e.get('transport_failure', False) for e in recent)
        if (failures >= 4 and failures/len(recent) >= .3
                and now-meta['last_transport_reduction'] >= WAVE_SECONDS):
            # Do not lift a lower concurrency imposed by real rate limiting.
            state['concurrency_limit'] = min(limit, max(floor, limit//2))
            meta.update(last_adjusted=now, last_transport_reduction=now,
                        last_action='transport_wave_reduction')
    elif event['status'] == 429:
        # observe() owns immediate rate-limit reduction and Retry-After.
        meta.update(healthy=0, last_bad=max(meta['last_bad'], now), last_adjusted=max(meta['last_adjusted'], now),
                    last_action='rate_limit_protection')
    elif event['status'] == 200:
        meta['healthy'] += 1
        if (meta['healthy'] >= 4 and now-meta['last_adjusted'] >= RECOVERY_SECONDS
                and now-meta['last_bad'] >= QUIET_SECONDS
                and time.monotonic() >= state.get('cooldown_until', 0)):
            state['concurrency_limit'] = min(maximum, max(floor, limit*2))
            meta.update(healthy=0, last_adjusted=now, last_action='healthy_doubling')
    return meta


def completed(traffic, state, status, transport_failure, now):
    """Called by updated workers; a live bridge owns adaptation during migration."""
    state_for(state, traffic.policy, now)
    bridge = state.get('adaptive_bridge') or {}
    if now-bridge.get('heartbeat', 0) > 10:
        apply_event(state, traffic.policy,
                    dict(time=now, status=status, transport_failure=transport_failure))
        state['adjusted_at'] = state['adaptive_v2']['last_adjusted']
    recent = state.get('recent_responses', [])
    failures = sum(e.get('transport_failure', False) for e in recent)
    # Preserve the existing severe transport failure circuit; never clear STOP.
    if transport_failure and len(recent) >= 16 and failures/len(recent) >= .75:
        traffic.stop('transport_failure_circuit', responses=len(recent), failures=failures)


def bridge_tick(state, policy, *, now=None, pid=None):
    """Feed legacy/new worker events through v2 without restarting active calls.

    Legacy workers retain their loaded completed() method. Its last-adjustment
    timestamp is held briefly ahead while this bridge owns adaptation; immediate
    429 handling and cooldown remain untouched. If the bridge dies, suppression
    expires, and updated workers automatically take over after ten seconds.
    """
    now = time.time() if now is None else now
    meta = state_for(state, policy, now)
    # A legacy observe(429) happens at response-head time, before completed().
    # Preserve its lower limit; don't allow a simultaneous success to raise it.
    if state.get('limited_at', 0) > meta.get('seen_limited_at', 0):
        meta.update(seen_limited_at=state['limited_at'], healthy=0,
                    last_bad=max(meta['last_bad'], state['limited_at']),
                    last_adjusted=max(meta['last_adjusted'], state['limited_at']))
    cursor = meta.get('cursor', now)
    for event in sorted(state.get('recent_responses', []), key=lambda e:e['time']):
        if event['time'] > cursor:
            apply_event(state, policy, event)
    state['adaptive_bridge'] = dict(pid=pid, heartbeat=now, revision=REVISION)
    state['legacy_adaptation_suppressed_until'] = now+30
    state['adjusted_at'] = now+30
