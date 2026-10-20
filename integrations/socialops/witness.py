"""Public-tool-only multi-campaign witness, including actual review recovery."""

from datetime import datetime, timedelta
from fractions import Fraction


PROTOCOL = "skill_declaration_v1"


class WitnessCallLimit(BaseException):
    def __init__(self, calls, max_calls):
        self.calls = self.witness_calls = calls
        self.max_calls = max_calls
        super().__init__(f"C06 witness call limit reached: {calls}/{max_calls}")


class _CountedCaller:
    def __init__(self, invoke, max_calls):
        self.invoke, self.max_calls, self.calls = invoke, max_calls, 0
        self._declaration_counter = self

    async def __call__(self, name, **arguments):
        if self.calls >= self.max_calls:
            raise WitnessCallLimit(self.calls, self.max_calls)
        self.calls += 1
        try:
            return await self.invoke(name, **arguments)
        except BaseException as error:
            error.witness_calls = self.calls
            raise


def _protocol_caller(call, skill_protocol, max_calls):
    if skill_protocol is None:
        if max_calls is not None:
            raise ValueError("max_calls requires an explicit declaration protocol")
        return call, None
    if skill_protocol != PROTOCOL:
        raise ValueError("Unknown C06 witness skill protocol")
    if type(max_calls) is not int or max_calls <= 0:
        raise ValueError("Declaration witness requires a finite positive integer max_calls")
    counter = getattr(call, "_declaration_counter", None)
    if counter is not None:
        if not isinstance(counter, _CountedCaller) or counter.max_calls != max_calls:
            raise ValueError("C06 witness layers must share the same caller budget")
        return call, counter
    counter = _CountedCaller(call, max_calls)
    return counter, counter


def _validate_public(public):
    if (public.get("format") != "skillhorizon.c06.reach_network.v2"
            or public.get("runtime_identity") != "task_local_v1"
            or public.get("policy", {}).get("skill_protocol") != PROTOCOL):
        raise ValueError("Declaration witness requires public Reach V2/task_local_v1 identity")


def date(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def rate(row):
    return Fraction(row["clicks"], row["impressions"]) if row["impressions"] else Fraction(0)


async def run(call, *, use_scripts=True, alternative=False, exercise_recovery=False,
              stop_after_waves=None, skill_protocol=None, max_calls=None):
    call, counter = _protocol_caller(call, skill_protocol, max_calls)
    try:
        result = await _run_campaign(call, use_scripts=use_scripts, alternative=alternative,
                                     exercise_recovery=exercise_recovery,
                                     stop_after_waves=stop_after_waves, declaration=counter is not None)
        return {**result, "calls": counter.calls} if counter is not None else result
    except BaseException as error:
        if counter is not None:
            error.witness_calls = counter.calls
        raise


async def _run_campaign(call, *, use_scripts, alternative, exercise_recovery, stop_after_waves, declaration):
    public = await call("read_task")
    if declaration:
        _validate_public(public)
    for skill in public["skills"]:
        await call("read_skill_file", skill_id=skill["name"])
    briefs = [(await call("read_campaign_brief", brief_id=i))["brief"] for i in public["brief_ids"]]
    reports = {}
    for brief in briefs:
        existing = await call("list_campaign_reports", brief_id=brief["brief_id"])
        if existing["reports"]:
            reports[brief["brief_id"]] = await call("read_campaign_report", report_id=existing["reports"][-1]["report_id"])
    clock = (await call("read_campaign_brief", brief_id=briefs[0]["brief_id"]))["clock"]
    recovery_used = False

    async def begin(skill, post_ids):
        if declaration:
            return None
        return (await call("begin_skill", skill_id=skill, post_ids=post_ids))["run_id"]

    async def finish(run_id, output_ids=()):
        if not declaration:
            await call("finish_skill", run_id=run_id, output_ids=list(output_ids))

    async def prepare(post_id, brief):
        nonlocal recovery_used
        post = (await call("get_content_post_detail", post_id=post_id))["post"]
        if post["status"] in {"published", "approved", "scheduled"}:
            return
        parent_briefs = [brief["brief_id"]] if post["role"] == "deployment" else brief["parent_brief_ids"]
        parents = [reports[i] for i in parent_briefs]
        run_id = await begin("inspect-campaign-brief", [post_id])
        current = await call("read_campaign_brief", brief_id=brief["brief_id"])
        for parent in parents:
            await call("read_campaign_report", report_id=parent["report_id"])
        await finish(run_id, [p["report_id"] for p in parents])
        run_id = await begin("adapt-campaign-content", [post_id])
        selected = sorted(parents, key=lambda r: (-rate(r["winner_observation"]), r["report_id"]))[0]["report_id"] if parents else ""
        audience = set(brief["allowed_audience_ids"]) if parents else set(brief["initial_variant_audiences"][post["variant_index"]])
        for parent in parents:
            audience &= set(parent["winner_content"]["audience_segment_ids"])
        learning = (await call("bind_campaign_learning", post_id=post_id, report_ids=[p["report_id"] for p in parents], selected_parent_report_id=selected,
            audience_segment_ids=sorted(audience)))["learning"]
        if post["role"] == "deployment":
            body, asset_ids = parents[0]["winner_content"]["body"], parents[0]["winner_content"]["asset_ids"]
        else:
            ready = [a["asset_id"] for a in current["assets"] if a["status"] == "ready"]
            asset_ids = [ready[-1] if alternative else ready[0]]
            body = brief["product_fact"] + " " + brief["ctas"][post["variant_index"]] + ". " + brief["disclosure"]
            if parents:
                body += " Prior measured campaigns: " + "; ".join(p["winning_cta"] for p in parents) + "."
            if alternative:
                body = "Campaign update. " + body
        recovering = exercise_recovery and not recovery_used
        draft = body.replace(brief["disclosure"], "") if recovering else body
        recovery_used |= recovering
        await call("update_content_post", post_id=post_id, body=draft, asset_ids=asset_ids, audience_segment_ids=learning["audience_segment_ids"])
        await finish(run_id, [post_id])
        run_id = await begin("coordinate-content-approval", [post_id])

        async def review():
            requested = await call("request_post_approval", post_id=post_id, requested_by_user_id=public["editor_user_id"],
                assigned_reviewer_user_id=public["reviewer_user_id"], stage="final_approval")
            await call("run_content_review", approval_request_id=requested["approval_request_id"])
            return await call("read_approval", approval_request_id=requested["approval_request_id"])

        result = await review()
        if recovering:
            if result["status"] != "rejected":
                raise AssertionError("Missing disclosure did not fail")
            await call("update_content_post", post_id=post_id, body=body)
            result = await review()
        if result["status"] != "approved":
            raise AssertionError(result)
        await finish(run_id, [result["approval_request_id"]])

    async def release(wave_briefs, role):
        nonlocal clock
        requests, occupied = [], []
        for brief in wave_briefs:
            ids = brief["variant_post_ids"] if role == "variant" else [brief["deployment_post_id"]]
            for post_id in ids:
                post = (await call("get_content_post_detail", post_id=post_id))["post"]
                if post["status"] == "published":
                    continue
                window = brief["variant_window"] if role == "variant" else brief["deployment_window"]
                requests.append({"post_id": post_id, "account_id": post["social_account_id"], "earliest": window["start"], "latest": window["end"]})
        if not requests:
            return
        pending_ids = {r["post_id"] for r in requests}
        for status in ("published", "scheduled"):
            offset = 0
            while True:
                page = await call("search_content_posts", workspace_id=public["workspace_id"], status_filter=status, limit=100, offset=offset)
                for post in page["posts"]:
                    if post["post_id"] not in pending_ids:
                        occupied.append({"account_id": post["social_account_id"], "at": post["published_at"] or post["scheduled_for"]})
                offset += len(page["posts"])
                if offset >= page["total_count"]:
                    break
        run_id = await begin("schedule-cross-platform-release", [r["post_id"] for r in requests])
        if use_scripts:
            schedule = (await call("run_skill_script", skill_id="schedule-cross-platform-release", inputs={"requests": requests, "occupied": occupied, "now": clock}))["schedule"]
        else:
            schedule = []
            for row in sorted(requests, key=lambda r: r["post_id"], reverse=alternative):
                target = max(date(clock), date(row["earliest"]))
                while any(r["account_id"] == row["account_id"] and abs((date(r["at"]) - target).total_seconds()) < 3600 for r in occupied):
                    target += timedelta(hours=1)
                if target > date(row["latest"]):
                    raise AssertionError("Manual schedule missed release window")
                schedule.append({"post_id": row["post_id"], "scheduled_for": stamp(target)})
                occupied.append({"account_id": row["account_id"], "at": stamp(target)})
        for row in schedule:
            await call("schedule_post", **row)
        exposure_mode = "exposure_policy" in public
        if exposure_mode:
            await finish(run_id, [r["post_id"] for r in schedule])
        publications = []
        for row in sorted(schedule, key=lambda r: (date(r["scheduled_for"]), r["post_id"])):
            clock = (await call("advance_campaign_clock", to=row["scheduled_for"]))["clock"]
            if exposure_mode:
                exposure_run = await begin("reconcile-audience-exposure", [row["post_id"]])
                post = (await call("get_content_post_detail", post_id=row["post_id"]))["post"]
                loads = {segment: 0 for segment in post["audience_segment_ids"]}
                windows = {r["segment_id"]: r["memory_hours"] * 3600 for r in public["exposure_policy"]["segments"]}
                prior, offset = [], 0
                while True:
                    history = await call("list_audience_exposures", offset=offset, limit=100)
                    for exposure in history["records"]:
                        age = int((date(clock) - date(exposure["published_at"])).total_seconds())
                        positive = False
                        for segment in set(exposure["audience_segment_ids"]) & set(loads):
                            units = exposure["allocated_units"] * max(0, windows[segment] - age) // windows[segment]
                            loads[segment] += units
                            positive |= units > 0
                        if positive:
                            prior.append(exposure["publication_id"])
                    offset += len(history["records"])
                    if offset >= history["total"]:
                        break
                await call("bind_audience_exposure", post_id=row["post_id"], prior_publication_ids=prior, audience_loads=loads)
            publication = await call("publish_post", post_id=row["post_id"])
            publications.append(publication["publication_id"])
            if exposure_mode:
                await finish(exposure_run, [publication["publication_id"]])
        if not exposure_mode:
            await finish(run_id, publications)

    for wave in sorted({b["wave"] for b in briefs}):
        selected = [b for b in briefs if b["wave"] == wave]
        if alternative:
            selected.reverse()
        complete = all([(await call("get_content_post_detail", post_id=b["deployment_post_id"]))["post"]["status"] == "published" for b in selected])
        if complete:
            if wave == 1:
                await call("poll_campaign_updates")
            continue
        for brief in selected:
            for post_id in brief["variant_post_ids"]:
                await prepare(post_id, brief)
        await release(selected, "variant")
        observation_end = max([date((await call("get_content_post_detail", post_id=i))["post"]["published_at"]) + timedelta(hours=b["observation_hours"])
            for b in selected for i in b["variant_post_ids"]])
        clock = (await call("advance_campaign_clock", to=stamp(max(date(clock), observation_end))))["clock"]
        for brief in selected:
            if brief["brief_id"] in reports:
                await prepare(brief["deployment_post_id"], brief)
                continue
            run_id = await begin("analyze-campaign-experiment", brief["variant_post_ids"])
            test = await call("launch_ab_test", brief_id=brief["brief_id"], variant_post_ids=brief["variant_post_ids"])
            observations = [await call("observe_post", post_id=i) for i in brief["variant_post_ids"]]
            if use_scripts:
                winner = (await call("run_skill_script", skill_id="analyze-campaign-experiment", inputs={"items": [
                    {"id": r["post_id"], "clicks": r["clicks"], "impressions": r["impressions"]} for r in observations]}))["winning_id"]
            else:
                winner = sorted(observations, key=lambda r: (-rate(r), r["post_id"]))[0]["post_id"]
            report = await call("complete_campaign_experiment", ab_test_id=test["ab_test_id"], snapshot_ids=[r["snapshot_id"] for r in observations], winning_post_id=winner)
            reports[brief["brief_id"]] = report
            await finish(run_id, [report["report_id"]])
            run_id = await begin("propagate-campaign-learning", [brief["deployment_post_id"]])
            await call("read_campaign_report", report_id=report["report_id"])
            await finish(run_id, [report["report_id"]])
            await prepare(brief["deployment_post_id"], brief)
        await release(selected, "deployment")
        if wave == 1:
            await call("poll_campaign_updates")
        if stop_after_waves is not None and wave >= stop_after_waves:
            return {"stopped_after_wave": wave}
    return {"status": "reference_complete"}
