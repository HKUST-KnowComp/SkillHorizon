"""Fixed suffix qualification after the independently accepted native-v3 prefix."""

import hashlib
from pathlib import Path

from .business_view import inventory
from .native_origins import load, sha, tree_hash


PREFIX_ROOT = Path("/workspace/runs/skillhorizon-harbor-mainline-20260917/harbor-core7-native-v3")
PREFIX = ("C25", "DataOps", "FieldOps", "C06")
REMAINING = ("C09", "C11", "C50")
REPAIRS = {
    "C09": ("integrations/logisticsops/claims.py", "9c8f7b7151d7ab91d375bcee2017426ca17bfe14a6639856895b2adf43c90903"),
    "C11": ("integrations/researchops/claims.py", "84b700dd2bdd19cb0e8a5fd056e2403990dd2b14f88be8520b3f587014514c8e"),
}
PREIMAGES = {"C09": "fc126d7424857a67ee104507aa3612c76f4da5696102f808d18a096a625dea41",
            "C11": "2c60c393f6bef99ff5fb86471b3f0b0c0a8541554a8a1340320bfc2c07509b89"}


def validate_prefix(path):
    binding = load(path)
    root = Path(binding["root"])
    if root != PREFIX_ROOT or root != root.resolve() or binding["accepted_domains"] != list(PREFIX):
        raise ValueError("Only the accepted native-v3 prefix is allowed")
    required = {"NATIVE_RESULT.json"} | {
        "probes/" + domain + "/" + name
        for domain in (*PREFIX, "C09") for name in ("QUALIFICATION.json", "RUN_RECEIPT.json")}
    if not required <= set(binding["files"]):
        raise ValueError("Incomplete accepted prefix bindings")
    for name, digest in binding["files"].items():
        target = root / name
        if (Path(name).is_absolute() or ".." in Path(name).parts or target.is_symlink()
                or not target.resolve().is_relative_to(root) or sha(target) != digest):
            raise ValueError("Accepted native prefix changed")
    batch = load(root / "NATIVE_RESULT.json")
    if batch["status"] != "failed" or [c["domain"] for c in batch["cells"]] != list(PREFIX):
        raise ValueError("Original first-failure batch differs")
    for domain in PREFIX:
        result = load(root / "probes" / domain / "QUALIFICATION.json")
        receipt = load(root / "probes" / domain / "RUN_RECEIPT.json")
        if result["status"] != "passed" or receipt["all_trials_valid"] is not True:
            raise ValueError("Prefix contains an unaccepted cell")
    if load(root / "probes/C09/QUALIFICATION.json")["status"] != "failed":
        raise ValueError("Original C09 failure was not preserved")
    if any((root / "probes" / domain / "STARTED.json").exists() for domain in ("C11", "C50")):
        raise ValueError("Previously unstarted suffix was started")
    return binding


def repaired_bytes(original):
    changed = original
    for field in ("scope", "outputs"):
        before = f'result.get("{field}") != claim.get("{field}", [])'.encode()
        after = f'result.get("{field}", []) != claim.get("{field}", [])'.encode()
        if changed.count(before) != 1:
            raise ValueError("Frozen claims patch preimage differs")
        changed = changed.replace(before, after)
    return changed


def apply_repair(domain, source, payload):
    source, payload = Path(source), Path(payload)
    relative, expected = REPAIRS[domain]
    target = source / relative
    patch = payload / "repairs" / domain / "claims.py"
    if target.is_symlink() or patch.is_symlink():
        raise ValueError("Claims patch must use ordinary files")
    original, replacement = target.read_bytes(), patch.read_bytes()
    if sha(target) != PREIMAGES[domain] or sha(patch) != expected or replacement != repaired_bytes(original):
        raise ValueError("Unapproved claims patch")
    before = tree_hash(source)
    target.write_bytes(replacement)
    return {"file": relative, "before": before, "after": tree_hash(source),
            "old_sha256": hashlib.sha256(original).hexdigest(), "new_sha256": expected}


def validate_sources(root):
    root = Path(root)
    accepted = load(root / "payload/ACCEPTED_INPUTS.json")
    if set(accepted) != set(REMAINING):
        raise ValueError("Exactly three accepted source bindings required")
    for domain in REMAINING:
        binding = accepted[domain]
        original, source = Path(binding["source"]), root / "business" / domain
        if tree_hash(original) != binding["source_identity"]:
            raise ValueError("Original accepted source changed")
        expected = inventory(original)["files"]
        actual = inventory(source)
        if actual["exclusions"]:
            raise ValueError("Unexpected source exclusions")
        if domain in REPAIRS:
            relative, digest = REPAIRS[domain]
            new_bytes = (source / relative).read_bytes()
            if (sha(original / relative) != PREIMAGES[domain]
                    or new_bytes != repaired_bytes((original / relative).read_bytes())
                    or sha(source / relative) != digest):
                raise ValueError("Repaired consumer differs from the domain handoff")
            expected[relative] = {"sha256": digest, "bytes": len(new_bytes)}
        if actual["files"] != expected:
            raise ValueError("Unexpected business source delta")


def validate_suffix(root, execution):
    root = Path(root)
    prefix = root / "payload/ACCEPTED_PREFIX.json"
    if execution.get("profile") != "remaining3" or execution.get("accepted_prefix_sha256") != sha(prefix):
        raise ValueError("Missing fixed remaining-profile identity")
    validate_prefix(prefix)
    validate_sources(root)
