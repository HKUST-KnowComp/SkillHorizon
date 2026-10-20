"""Pure public-input arithmetic. No environment, filesystem or network access."""

from datetime import datetime, timezone
import json
import sys


class InputError(ValueError):
    def __init__(self, path):
        self.path = path


def require(condition, path):
    if not condition:
        raise InputError(path)


def text(value, path):
    require(isinstance(value, str) and bool(value), path)
    return value


def identities(value, path):
    require(isinstance(value, list), path)
    for item in value:
        text(item, path)
    require(len(value) == len(set(value)), path)
    return value


def instant(value, path):
    text(value, path)
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(result.tzinfo is not None, path)
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise InputError(path) from None


def calculate(inputs):
    require(isinstance(inputs, dict), 'inputs')
    post_id = text(inputs.get('post_id'), 'post_id')
    now = instant(inputs.get('now'), 'now')
    audience = identities(inputs.get('audience_segment_ids'), 'audience_segment_ids')
    segments = inputs.get('segments')
    require(isinstance(segments, list), 'segments')
    windows = {}
    for index, row in enumerate(segments):
        path = f'segments[{index}]'
        require(isinstance(row, dict), path)
        sid = text(row.get('segment_id'), path + '.segment_id')
        hours = row.get('memory_hours')
        require(type(hours) is int and hours > 0, path + '.memory_hours')
        require(sid not in windows, path + '.segment_id')
        windows[sid] = hours * 3600
    require(all(s in windows for s in audience), 'segments')
    records = inputs.get('publications')
    require(isinstance(records, list), 'publications')
    loads, origins, seen = dict.fromkeys(audience, 0), [], set()
    for index, row in enumerate(records):
        path = f'publications[{index}]'
        require(isinstance(row, dict), path)
        rid = text(row.get('publication_id'), path + '.publication_id')
        require(rid not in seen, path + '.publication_id')
        seen.add(rid)
        published = instant(row.get('published_at'), path + '.published_at')
        require(published <= now, path + '.published_at')
        prior_audience = identities(row.get('audience_segment_ids'), path + '.audience_segment_ids')
        units = row.get('allocated_units')
        require(type(units) is int and units >= 0, path + '.allocated_units')
        age = int((now - published).total_seconds())
        positive = False
        for sid in set(audience).intersection(prior_audience):
            contribution = units * max(0, windows[sid] - age) // windows[sid]
            loads[sid] += contribution
            positive |= contribution > 0
        if positive:
            origins.append(rid)
    return dict(post_id=post_id, as_of=inputs['now'], prior_publication_ids=sorted(origins),
                audience_loads=loads, state_verified=False)


if __name__ == '__main__':
    try:
        print(json.dumps(calculate(json.load(sys.stdin)), allow_nan=False))
    except (InputError, json.JSONDecodeError) as error:
        print(json.dumps({'script_input_error': {
            'code': 'INVALID_EXPOSURE_INPUT', 'paths': [getattr(error, 'path', 'inputs')],
            'missing_source_keys': []}}))
        sys.exit(2)
