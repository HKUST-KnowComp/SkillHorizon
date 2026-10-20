"""Fresh SocialOps portfolios; historical Reach instances remain unchanged."""

from copy import deepcopy
from pathlib import Path
import random

from integrations.internal_mcp.io import read_json, write_json
from .declaration_build import build as declaration_build
from .build import KEYS
from .portfolio_witness import REVISION as REFERENCE_POLICY

REVISION = 'socialops_portfolio_v2'


def build(source, output, *, seed, campaigns, waves, scenario, dependency_pattern,
          final_wave_campaigns=None):
    public = declaration_build(source, output, seed=seed, campaigns=campaigns,
        waves=waves, scenario=scenario, dependency_pattern=dependency_pattern,
        background=False, runtime_identity='task_local_v1')
    output = Path(output)
    world = read_json(output/'checkpoint/world.json')
    state = world['social']
    facts = random.Random(f'{REVISION}:facts:{seed}')
    labels = random.Random(f'{REVISION}:labels:{seed}')
    order = random.Random(f'{REVISION}:ordering:{seed}')
    if final_wave_campaigns is not None and final_wave_campaigns != campaigns:
        if not 1 <= final_wave_campaigns < campaigns:
            raise ValueError('Final-wave campaign count must be within portfolio width')
        final = [b for b in state['briefs'].values() if b['wave']==waves]
        retained = {b['brief_id'] for b in facts.sample(final, final_wave_campaigns)}
        removed = [b for b in final if b['brief_id'] not in retained]
        removed_posts = {pid for b in removed for pid in b['variant_post_ids']+[b['deployment_post_id']]}
        for brief in removed:
            del state['briefs'][brief['brief_id']]
        public['brief_ids'] = list(state['briefs'])
        public['scenario']['world_size']['final_wave_campaigns'] = final_wave_campaigns
        world['tables']['content_post'] = [p for p in world['tables']['content_post'] if p['post_id'] not in removed_posts]
        for pid in removed_posts:
            state['response_profiles'].pop(pid, None)
            state['cohort_response_profiles'].pop(pid, None)
        posts = world['tables']['content_post']
        world['tables']['workspace'][0]['active_post_count'] = len(posts)
        for user in world['tables']['user']:
            user['authored_post_count'] = sum(p['author_user_id']==user['user_id'] for p in posts)
        for kind, key in [('social_account','social_account_id'),('campaign','campaign_id')]:
            for entity in world['tables'][kind]:
                entity['post_count'] = sum(p[key]==entity[key] for p in posts)
        for asset in world['tables']['asset']:
            asset['attached_post_count'] = sum(asset['asset_id'] in p['asset_ids'] for p in posts)
    # Current campaign consent can expand as well as restrict the inherited
    # audience. Two fresh acquisition arms have separate local permissions;
    # the shared retained audience still depends on actual parent winners.
    audience_rng = random.Random(f'{REVISION}:consent:{seed}')
    audience_ids = [s['segment_id'] for s in world['tables']['audience_segment']]
    core = audience_rng.sample(audience_ids, 2)
    acquisition = [s for s in audience_ids if s not in core]
    for brief in state['briefs'].values():
        left, right, shared = audience_rng.sample(acquisition, 3)
        brief['allowed_audience_ids'] = core + [left, right, shared]
        brief['variant_audience_permissions'] = [core + [left, shared], core + [right, shared]]
        brief['newly_consented_audiences'] = [[left], [right]]
        brief['initial_variant_audiences'] = [core + [left], core + [right]]
        brief['learning_policy'] = (
            'Use exactly the required measured reports and their winning CTAs; select the highest CTR parent, ties by report ID. '
            'For a later variant, union the actual parent winner audiences and intersect that union with this arm\'s variant_audience_permissions, '
            'then union this arm\'s newly_consented_audiences. These are current campaign opt-ins, not inferred consent. '
            'For a first-wave variant use its initial_variant_audiences. A deployment retains its actual winner audience.')
    # Response facts are independently sampled, never derived from IDs/parity.
    # Correlated conversion order is the explicit overlap treatment.
    wave_orders = {}
    cohorts = [f'c{i:02d}' for i in range(16)]
    allocation_rng = random.Random(f'{REVISION}:conversion-partitions:{seed}')
    partition_order = allocation_rng.sample(cohorts, len(cohorts))
    campaign_ids = [r['campaign_id'] for r in world['tables']['campaign']]
    partitions = {(cid, arm): partition_order[(lane*2+arm)::(campaigns*2)]
                  for lane, cid in enumerate(campaign_ids) for arm in range(2)}
    for brief in state['briefs'].values():
        brief['product_fact'] = f"Offer price: {facts.randint(15, 180)} USD."
        brief['variant_exposure_units'] = [facts.randint(3, 7) for _ in range(2)]
        brief['variant_fatigue_sensitivity'] = [facts.randint(1, 6) for _ in range(2)]
        if brief['wave'] not in wave_orders:
            wave_orders[brief['wave']] = facts.sample([f'c{i:02d}' for i in range(16)], 16)
        for arm, pid in enumerate(brief['variant_post_ids']):
            state['response_profiles'][pid] = dict(impressions=facts.randint(1600, 2400), clicks=facts.randint(90, 190))
            cohort = state['cohort_response_profiles'][pid]
            cohort['click_weights'] = {f'c{i:02d}': facts.randint(1, 4) for i in range(16)}
            cohort['conversion_order'] = (list(wave_orders[brief['wave']])
                if scenario in {'overlapping_conversions', 'overlap_reactivation'}
                else facts.sample([f'c{i:02d}' for i in range(16)], 16))
            if scenario == 'cooldown_reactivation':
                # Exclusive attribution pools prevent same-window duplicate
                # conversion claims; eligibility still uses observed history.
                cohort['conversion_partition'] = partitions[(brief['campaign_id'], arm)]
    for policy in public['reach_policy']['segments']:
        policy['conversion_cooldown_hours'] = (waves*48+72 if scenario == 'overlapping_conversions'
            else (24 if policy['segment_id'] == core[0] else 120 if policy['segment_id'] == core[1]
                  else allocation_rng.choice([24, 36, 72, 120])))
    public['reach_policy']['attribution'] = ('exclusive stable campaign-arm conversion pools; no concurrent duplicate attribution'
        if scenario == 'cooldown_reactivation' else 'multi-touch attribution; observed conversions may overlap')
    public['reach_policy']['cooldown_scope'] = ('longer than the entire task horizon'
        if scenario == 'overlapping_conversions' else 'mixed short and long conversion cooldowns')
    # Asset eligibility stays public. Vary which campaign/edition changes;
    # avoiding an affected draft is a legal prevention strategy.
    affected_asset = facts.choice(world['tables']['asset'])['asset_id']
    state['scheduled_updates'][0].update(asset_id=affected_asset,
        after_wave=max(1, waves//2), trigger_variant_publications=1,
        reason='A campaign image license was withdrawn for future publication.')
    for asset in world['tables']['asset']:
        asset['creative_id'] = asset['campaign_id']
    identities = {}
    for kind, key in KEYS.items():
        for row in world['tables'][kind]:
            identities[row[key]] = f'{kind.replace("_", "-")}-{labels.getrandbits(40):010x}'
    for key in state['briefs']:
        identities[key] = f'brief-{labels.getrandbits(40):010x}'
    if len(set(identities.values())) != len(identities):
        raise ValueError('Instance identity collision')

    def remap(value):
        if isinstance(value, str):
            return identities.get(value, value)
        if isinstance(value, list):
            return [remap(v) for v in value]
        if isinstance(value, dict):
            return {identities.get(k, k): remap(v) for k, v in value.items()}
        return value

    world, public = remap(world), remap(public)
    state = world['social']
    for rows in world['tables'].values():
        order.shuffle(rows)
    order.shuffle(public['brief_ids'])
    for brief in state['briefs'].values():
        order.shuffle(brief['eligible_asset_ids'])
    public.update(environment_id='C06', environment_name='SocialOps',
        instance_revision=REVISION, reference_policy=REFERENCE_POLICY,
        rights_update_after_wave=max(1, waves//2),
        rights_update_trigger={'wave': max(1, waves//2), 'variant_publications': 1},
        campaign_waves=sorted({b['wave'] for b in state['briefs'].values()}))
    public['scenario']['scope'] = 'generated_evaluation_candidate_not_model_evaluated'
    public['policy']['fresh_task_history'] = 'No initial publications, observations or completed experiment reports; all posts are assigned drafts.'
    public['policy']['audience_learning'] = 'campaign_consent_renewal_v2'
    public['policy']['deployment_asset'] = 'same_creative_current_license_v2'
    public['goal'] = public['goal'].replace('post-wave-one rights update',
        'rights update due immediately after the first variant publication in the declared trigger wave')
    public['policy']['rights_updates'] = ('Poll the declared notice after rights_update_trigger is met and before any further publication. '
        'Only unpublished drafts using the withdrawn asset lose approval and reservation. Already published history remains valid. '
        'A deployment may use a currently licensed eligible copy with the same creative_id as its winner; preserve text and audience.')
    public['policy']['discovery'] = 'list_campaign_briefs(wave=...) returns the public directory; read details when that wave is actionable.'
    # Replace obsolete instructions instead of appending contradictory rules.
    from .declaration_build import DECLARATION_GUIDANCE, LOCAL_GOALS
    instructions = {
        'inspect-campaign-brief': 'Use read_task and list_campaign_briefs to find current-wave work. Read needed briefs for facts, parents, consent permissions, deadlines and asset metadata. Future-wave detail prefetch is optional, never required.',
        'adapt-campaign-content': 'Read the post and brief. Preserve the product fact, disclosure and required CTA. Use an eligible ready asset and the computed audience. A deployment copies the actual winner text and audience; it may replace a withdrawn license with an eligible ready asset having the same creative_id. Every edit invalidates approval and scheduling.',
        'propagate-campaign-learning': 'Use exactly the required parent reports and their winning CTAs. Select the highest clicks/impressions parent, ties by report ID. For variants with parents, union all actual parent winner audiences, intersect this union with the arm-specific variant_audience_permissions, then union the arm-specific newly_consented_audiences. Consent combines successful acquisition pools while respecting current local permissions; no new permission is inferred. For variants without parents use initial_variant_audiences. Deployments retain the winner audience. Follow the public rights_update_trigger; poll before the next publication. Update changed asset status, repair only affected unpublished drafts, obtain new approvals and restore reservations. Historical publications and unaffected approvals remain valid.'}
    for name, instruction in instructions.items():
        goal = LOCAL_GOALS[name].replace('audience intersection', 'current consent computation')
        path = output/f'public/skills/{name}/SKILL.md'
        path.write_text(f'# {name}\n\n{instruction}\n'+DECLARATION_GUIDANCE+'\n### Local completion goal\n\n'+goal+'\n')
    # One shared protocol contract; do not repeat the same controller guidance
    # verbatim in all eight documents that the reference reads.
    public['skill_declaration_guidance'] = DECLARATION_GUIDANCE.strip()
    for skill in public['skills']:
        path = output/'public/skills'/skill['name']/'SKILL.md'
        path.write_text(path.read_text().replace(DECLARATION_GUIDANCE,
            '\n\nThe common declaration contract is in read_task.skill_declaration_guidance.\n'))
    spec = dict(public=deepcopy(public), initial_tables=deepcopy(world['tables']),
        briefs=deepcopy(state['briefs']), response_profiles=deepcopy(state['response_profiles']),
        scheduled_updates=deepcopy(state['scheduled_updates']),
        cohort_response_profiles=deepcopy(state['cohort_response_profiles']))
    for folder in ('checkpoint', 'runtime'):
        write_json(output/folder/'world.json', world)
    write_json(output/'public/task.json', public)
    write_json(output/'private/initial_obligations.json', spec)
    return public
