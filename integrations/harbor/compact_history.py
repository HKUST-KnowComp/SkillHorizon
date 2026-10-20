"""Bound compaction inputs without dropping an oversized public tool result."""
from copy import deepcopy


def bounded_history_prefix(initial, history, measure, *, limit):
    if not history:
        return [], []
    if measure(initial + history) <= limit:
        return history, []
    best = 0
    cuts = [i for i in range(1, len(history) + 1) if history[i - 1]['role'] == 'user']
    lo, hi = 0, len(cuts) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if measure(initial + history[:cuts[mid]]) <= limit:
            best, lo = cuts[mid], mid + 1
        else:
            hi = mid - 1
    prefix, tail = history[:best], history[best:]
    # A single large user observation cannot be made smaller by choosing fewer
    # turns. Split its text exactly; neither fragment is executed as an action.
    user = next((i for i, m in enumerate(tail) if m['role'] == 'user'), None)
    if user is not None and isinstance(tail[user].get('content'), str):
        original = tail[user]
        before = prefix + tail[:user]
        if measure(initial + before + [original]) > limit:
            lo, hi, chars = 1, len(original['content']) - 1, 0
            while lo <= hi:
                mid = (lo + hi) // 2
                head = dict(original, content=original['content'][:mid])
                if measure(initial + before + [head]) <= limit:
                    chars, lo = mid, mid + 1
                else:
                    hi = mid - 1
            if chars:
                head = dict(original, content=original['content'][:chars])
                rest = dict(original, content=original['content'][chars:])
                return deepcopy(before + [head]), deepcopy([rest] + tail[user + 1:])
    if not prefix:
        raise ValueError('No public history prefix fits the compaction input limit')
    return prefix, tail
