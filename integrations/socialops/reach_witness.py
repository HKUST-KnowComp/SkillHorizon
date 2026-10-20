"""Public conversion joins layered on the existing operational witness."""

from datetime import timedelta

from .witness import run as campaign_run, date, _protocol_caller, _validate_public


async def run(call, *, skill_protocol=None, max_calls=None, **options):
    call, counter = _protocol_caller(call, skill_protocol, max_calls)
    policies = {}
    clock = None

    async def wrapped(name, **args):
        nonlocal clock
        if name == "publish_post":
            post = (await call("get_content_post_detail", post_id=args["post_id"]))["post"]
            if post["status"] != "published":
                if counter is None:
                    run_id = (await call("begin_skill", skill_id="reconcile-converted-audience", post_ids=[post["post_id"]]))["run_id"]
                remaining = {s: set(policies[s]["cohort_ids"]) for s in post["audience_segment_ids"]}
                required, offset = [], 0
                while True:
                    history = await call("list_audience_conversions", offset=offset, limit=100)
                    for observation in history["records"]:
                        contributes = False
                        for segment, converted in observation["converted_cohorts"].items():
                            if segment in remaining and date(clock) < date(observation["observed_until"]) + timedelta(hours=policies[segment]["conversion_cooldown_hours"]):
                                remaining[segment].difference_update(converted)
                                contributes |= bool(converted)
                        if contributes:
                            required.append(observation["snapshot_id"])
                    offset += len(history["records"])
                    if offset >= history["total"]:
                        break
                await call("bind_audience_reach", post_id=post["post_id"], conversion_snapshot_ids=required,
                           eligible_cohorts={s: sorted(group) for s, group in remaining.items()})
                publication = await call(name, **args)
                if counter is None:
                    await call("finish_skill", run_id=run_id, output_ids=[publication["publication_id"]])
                return publication
        result = await call(name, **args)
        if name == "read_task":
            if counter is not None:
                _validate_public(result)
            policies.update({p["segment_id"]: p for p in result["reach_policy"]["segments"]})
        if name in {"advance_campaign_clock", "read_campaign_brief"}:
            clock = result["clock"]
        return result

    # The campaign layer forwards through this bridge, but counts only at call.
    if counter is not None:
        wrapped._declaration_counter = counter
    try:
        return await campaign_run(wrapped, skill_protocol=skill_protocol, max_calls=max_calls, **options)
    except BaseException as error:
        if counter is not None:
            error.witness_calls = counter.calls
        raise
