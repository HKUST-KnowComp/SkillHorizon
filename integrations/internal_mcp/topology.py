"""Deterministic construction primitives for layered business dependencies."""

import random

PATTERNS = ("staggered", "distributed", "alternating_fork_join", "protected_branch")


def parent_positions(stage, lane, width, seed, pattern="staggered"):
    if pattern not in PATTERNS or width < 1 or stage < 0 or not 0 <= lane < width:
        raise ValueError("Invalid dependency construction parameters")
    if pattern == "protected_branch" and width < 3:
        raise ValueError("Protected branch needs at least three lanes")
    if stage == 0:
        return []
    positions = [(stage - 1, lane)]
    if pattern == "staggered":
        positions.append((max(0, stage - 2), (lane + 1) % width))
        if stage >= 3:
            positions.append((stage - 3, (lane + 2) % width))
    elif pattern == "distributed":
        rng = random.Random(f"skillhorizon-distributed-v1:{seed}:{stage}:{lane}:{width}")
        others = [i for i in range(width) if i != lane]
        # Keep the longitudinal path and independently vary cross-case old origins.
        if others:
            positions.append((rng.randrange(max(1, stage - 1)), rng.choice(others)))
        if stage >= 3:
            candidates = [(s, i) for s in range(stage - 1) for i in range(width)
                          if (s, i) not in positions]
            if candidates:
                positions.append(rng.choice(candidates))
    elif pattern == "alternating_fork_join":
        hub = seed % width
        if stage % 2 and lane == hub:
            positions = [(stage - 1, i) for i in range(width)]
        elif not stage % 2:
            positions.append((stage - 1, hub))
            if stage >= 4:
                positions.append((stage - 3, (lane + 1) % width))
    elif pattern == "protected_branch":
        # The last lane remains outside the changing component's descendants.
        if lane < width - 1 and stage >= 2:
            positions.append((stage - 1, (lane + 1) % (width - 1)))
            if stage >= 3:
                positions.append((0, (lane + 1) % (width - 1)))
    return list(dict.fromkeys(positions))


def disclose_pattern(public, pattern, seed):
    if pattern != "staggered":
        public["dependency_construction"] = {"pattern": pattern, "revision": 1, "seed": seed,
            "scope": "construction_development_not_heldout", "minimum_horizon_proven": False}


def disclose_runtime_identity(public, mode):
    if mode not in {"legacy", "task_local_v1"}:
        raise ValueError("Unknown runtime identity mode")
    if mode != "legacy":
        public["runtime_identity"] = mode


def runtime_identifier(public, kind, value, historical):
    if public.get("runtime_identity") == "task_local_v1":
        return f"new-{kind}-{value}"
    return historical
