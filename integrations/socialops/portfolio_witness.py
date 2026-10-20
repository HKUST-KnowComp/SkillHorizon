"""Public, demand-driven reference. Its only environment access is call()."""

from datetime import timedelta
from fractions import Fraction

from .witness import date, stamp

REVISION = 'public_campaign_cache_exposure_script_v3'


def rate(row):
    return Fraction(row['clicks'], row['impressions']) if row['impressions'] else Fraction(0)


async def run(call):
    public = await call('read_task')
    if not public['policy'].get('fresh_task_history'):
        raise ValueError('Fresh-history contract required')
    if public['policy'].get('exposure_calculation') != 'explicit_input_skill_script_v1':
        raise ValueError('This reference requires the explicit-input exposure skill script')
    for skill in public['skills']:
        await call('read_skill_file', skill_id=skill['name'])
    briefs, assets, posts, reports = {}, {}, {}, {}
    clock = public['clock_origin']
    publications, conversions, occupied = [], [], []
    rights_processed = False
    exposure_windows = {r['segment_id']: timedelta(hours=r['memory_hours'])
                        for r in public['exposure_policy']['segments']}
    reach_policy = {r['segment_id']: r for r in public['reach_policy']['segments']}

    async def advance(target):
        nonlocal clock
        if date(target) > date(clock):
            clock = (await call('advance_campaign_clock', to=target))['clock']

    async def prepare(pid, brief):
        if pid not in posts:
            posts[pid] = (await call('get_content_post_detail', post_id=pid))['post']
        # The rights notice changes license status and invalidates review and
        # reservation, not identity, text or measured-learning content.
        post = posts[pid]
        parents = [reports[bid] for bid in ([brief['brief_id']] if post['role']=='deployment' else brief['parent_brief_ids'])]
        selected = sorted(parents, key=lambda r: (-rate(r['winner_observation']), r['report_id']))[0]['report_id'] if parents else ''
        audience = set(brief['allowed_audience_ids']) if parents else set(brief['initial_variant_audiences'][post['variant_index']])
        for parent in parents:
            audience &= set(parent['winner_content']['audience_segment_ids'])
        if parents and post['role']=='variant':
            audience = set.union(*(set(p['winner_content']['audience_segment_ids']) for p in parents))
            audience &= set(brief['variant_audience_permissions'][post['variant_index']])
            audience |= set(brief['newly_consented_audiences'][post['variant_index']])
        learning = dict(report_ids=sorted(p['report_id'] for p in parents),
            selected_parent_report_id=selected, audience_segment_ids=sorted(audience))
        if post.get('learning') != learning:
            post['learning'] = (await call('bind_campaign_learning', post_id=pid, **learning))['learning']
        if post['role']=='deployment':
            body, chosen = parents[0]['winner_content']['body'], parents[0]['winner_content']['asset_ids']
            chosen = [aid if assets[aid]['status']=='ready' else next(
                candidate for candidate in brief['eligible_asset_ids']
                if assets[candidate]['status']=='ready' and assets[candidate]['creative_id']==assets[aid]['creative_id'])
                for aid in chosen]
        else:
            chosen = [next(aid for aid in brief['eligible_asset_ids'] if assets[aid]['status']=='ready')]
            body = brief['product_fact']+' '+brief['ctas'][post['variant_index']]+'. '+brief['disclosure']
            if parents:
                body += ' Prior measured campaigns: '+'; '.join(p['winning_cta'] for p in parents)+'.'
        content = dict(body=body, asset_ids=chosen, audience_segment_ids=sorted(audience))
        changes = {k:v for k,v in content.items() if post.get(k)!=v}
        if changes:
            await call('update_content_post', post_id=pid, **changes)
        # Locally remember only supplied fields; never fabricate a full object.
        post.update(content)
        request = await call('request_post_approval', post_id=pid,
            requested_by_user_id=public['editor_user_id'], assigned_reviewer_user_id=public['reviewer_user_id'], stage='final_approval')
        decision = await call('run_content_review', approval_request_id=request['approval_request_id'])
        if decision['status'] != 'approved':
            raise ValueError(f'Unexpected public review rejection: {decision}')

    async def release(selected, role):
        nonlocal rights_processed
        requests = []
        for brief in selected:
            for pid in (brief['variant_post_ids'] if role=='variant' else [brief['deployment_post_id']]):
                window = brief['variant_window'] if role=='variant' else brief['deployment_window']
                requests.append(dict(post_id=pid, account_id=posts[pid]['social_account_id'], earliest=window['start'], latest=window['end']))
        lower = min(max(date(clock), date(r['earliest'])) for r in requests)-timedelta(hours=1)
        relevant_slots = [r for r in occupied if date(r['at'])>lower]
        schedule = (await call('run_skill_script', skill_id='schedule-cross-platform-release',
            inputs=dict(requests=requests, occupied=relevant_slots, now=clock)))['schedule']
        for row in schedule:
            await call('schedule_post', **row)
        for row in sorted(schedule, key=lambda r: (date(r['scheduled_for']), r['post_id'])):
            await advance(row['scheduled_for'])
            pid = row['post_id']
            audience = posts[pid]['audience_segment_ids']
            # Public timestamps/audiences prove these omitted records contribute
            # zero; do not resend permanently expired or unrelated history.
            relevant_publications = [p for p in publications if any(
                date(clock) - date(p['published_at']) < exposure_windows[s]
                for s in set(audience) & set(p['content']['audience_segment_ids']))]
            exposure = await call('run_skill_script', skill_id='reconcile-audience-exposure',
                inputs=dict(post_id=pid, now=clock, audience_segment_ids=audience,
                    segments=public['exposure_policy']['segments'], publications=[dict(
                        publication_id=p['publication_id'], published_at=p['published_at'],
                        audience_segment_ids=p['content']['audience_segment_ids'],
                        allocated_units=p['exposure']['allocated_units']) for p in relevant_publications]))
            await call('bind_audience_exposure', post_id=pid,
                prior_publication_ids=exposure['prior_publication_ids'], audience_loads=exposure['audience_loads'])
            remaining, sources = {s:set(reach_policy[s]['cohort_ids']) for s in audience}, []
            for prior in conversions:
                positive = False
                for segment, converted in prior['converted_cohorts'].items():
                    if segment in remaining and date(clock)<date(prior['observed_until'])+timedelta(hours=reach_policy[segment]['conversion_cooldown_hours']):
                        remaining[segment].difference_update(converted)
                        positive |= bool(converted)
                if positive:
                    sources.append(prior['snapshot_id'])
            await call('bind_audience_reach', post_id=pid, conversion_snapshot_ids=sources,
                eligible_cohorts={s:sorted(c) for s,c in remaining.items()})
            published = await call('publish_post', post_id=pid)
            publications.append(published)
            posts[pid]['published_at'] = published['published_at']
            occupied.append(dict(account_id=posts[pid]['social_account_id'],at=published['published_at']))
            trigger = public['rights_update_trigger']
            progressed = sum(1 for p in publications if posts[p['post_id']]['role']=='variant'
                and briefs[posts[p['post_id']]['brief_id']]['wave']==trigger['wave'])
            if not rights_processed and progressed >= trigger['variant_publications']:
                notice = await call('poll_campaign_updates')
                if not notice['events']:
                    raise ValueError('Declared release-progress notice missing')
                rights_processed = True
                for event in notice['events']:
                    assets[event['asset_id']]['status'] = 'withdrawn'
                    for impacted in event['released_reservation_post_ids']:
                        await prepare(impacted, briefs[posts[impacted]['brief_id']])
                        reservation = next(s for s in schedule if s['post_id']==impacted)
                        await call('schedule_post', **reservation)

    for wave in public['campaign_waves']:
        directory = await call('list_campaign_briefs', wave=wave)
        if len(directory['briefs']) != directory['total']:
            raise ValueError('Reference requires a complete current-wave directory page')
        selected = []
        for row in directory['briefs']:
            current = await call('read_campaign_brief', brief_id=row['brief_id'])
            brief = current['brief']
            briefs[brief['brief_id']] = brief
            selected.append(brief)
            assets.update({a['asset_id']:a for a in current['assets']})
            clock = current['clock']
        for brief in selected:
            for pid in brief['variant_post_ids']:
                await prepare(pid, brief)
        await release(selected, 'variant')
        end = max(date(posts[pid]['published_at'])+timedelta(hours=b['observation_hours']) for b in selected for pid in b['variant_post_ids'])
        await advance(stamp(end))
        for brief in selected:
            test = await call('launch_ab_test', brief_id=brief['brief_id'], variant_post_ids=brief['variant_post_ids'])
            observations = [await call('observe_post', post_id=pid) for pid in brief['variant_post_ids']]
            conversions.extend(r['conversion'] for r in observations)
            # The optional rate script is unnecessary for this exact calculation.
            winner = sorted(observations, key=lambda r:(-rate(r),r['post_id']))[0]['post_id']
            report = await call('complete_campaign_experiment', ab_test_id=test['ab_test_id'],
                snapshot_ids=[r['snapshot_id'] for r in observations], winning_post_id=winner)
            reports[brief['brief_id']] = report
            await prepare(brief['deployment_post_id'], brief)
        await release(selected, 'deployment')
    return dict(status='reference_complete', briefs=len(briefs), publications=len(publications))
