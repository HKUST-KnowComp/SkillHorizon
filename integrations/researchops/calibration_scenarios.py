"""Source-grounded exclusion and a later withdrawal of a previously pooled study."""

from copy import deepcopy
import json


def configure(world, public, family, seed):
    batches = world["library"]["batches"]
    if family in ("identity_compatibility", "ambiguous_withdrawal"):
        for batch in batches:
            changed = 0
            for work in batch["works"].values():
                version = work["versions"]["1"]
                if version["metadata"]["study_type"] != "commentary":
                    continue
                version["metadata"].update(study_type="empirical", year=2024 + batch["round"])
                abstract = next(p for p in version["passages"] if p["passage_id"] == "abstract")
                abstract["text"] = abstract["text"].replace("a historical commentary", "an empirical study")
                version["metadata"]["abstract"] = abstract["text"]
                passage = next(p for p in version["passages"] if p["passage_id"] == "results")
                value = json.loads(passage["text"])
                if (changed + seed) % 2:
                    value.update(metric="allocation_score", population="synthetic_adults", unit="points")
                else:
                    value.update(metric="latency", population="synthetic_items", unit="milliseconds")
                passage["text"] = json.dumps(value, sort_keys=True)
                changed += 1
            if changed == 0:
                raise ValueError("No eligible-but-incompatible source created")
            for record in batch["records"]:
                if record["source_key"] in batch["works"]:
                    metadata = batch["works"][record["source_key"]]["versions"]["1"]["metadata"]
                    for field in ("study_type", "year", "abstract"):
                        record[field] = metadata[field]
        public["goal"] += " Near-title empirical records can use a different measurement or population despite satisfying section metadata filters. Canonicalize aliases before pooling."
    if family in ("study_retraction", "ambiguous_withdrawal"):
        key, origin = next((k, w["versions"]["1"]) for k, w in batches[0]["works"].items()
            if json.loads(next(p["text"] for p in w["versions"]["1"]["passages"] if p["passage_id"] == "results"))["role"] == "study")
        revised = deepcopy(origin)
        revised["revision"] = 2
        revised["metadata"]["title"] += " (withdrawal notice)"
        passage = next(p for p in revised["passages"] if p["passage_id"] == "results")
        value = json.loads(passage["text"])
        value.update(evidence_status="retracted", withdrawal_reason="Synthetic duplicate cohort discovered after publication")
        passage["text"] = json.dumps(value, sort_keys=True)
        batches[2]["correction"] = {"source_key": key, "version": revised}
        public["batches"][2]["has_publisher_correction"] = True
        public["synthesis_policy"]["withdrawal"] = "Retain current evidence coverage for retracted studies, but exclude them from section and global sample totals with reason retracted_study. Committed earlier reviews retain their historical state."
        public["goal"] += " Account for later study withdrawal independently of calibration correction; preserve prior review versions and reuse unaffected annotations."
