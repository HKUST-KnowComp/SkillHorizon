"""Reach V2 business runtime without observational skill-run bookkeeping."""

from pathlib import Path

from integrations.internal_mcp.io import read_json
from .kernel import require, schema
from .reach_build import FORMAT
from .reach_runtime import ReachSocial


PROTOCOL = "skill_declaration_v1"
RETIRED_TOOLS = frozenset({
    "begin_skill", "select_skill_run", "finish_skill", "read_skill_run",
})


class DeclarationReachSocial(ReachSocial):
    def __init__(self, task, runtime):
        public = read_json(Path(task) / "public/task.json")
        world = read_json(Path(runtime) / "world.json")
        require(public.get("format") == world.get("format") == FORMAT,
                "DECLARATION_REACH_FORMAT")
        require(public.get("policy", {}).get("skill_protocol") == PROTOCOL,
                "DECLARATION_PROTOCOL_REQUIRED")
        require(public.get("runtime_identity") == "task_local_v1",
                "DECLARATION_RUNTIME_IDENTITY")
        require(isinstance(world.get("social"), dict)
                and not {"skill_runs", "active_skill_run"}.intersection(world["social"]),
                "DECLARATION_LEGACY_STATE")
        # Progressed worlds are reloadable; freshness is a construction constraint.
        super().__init__(task, runtime)
        for name in RETIRED_TOOLS:
            self.metadata.pop(name, None)
        if 'rights_update_trigger' in public:
            self.metadata['poll_campaign_updates']['description'] = (
                'Poll after rights_update_trigger variant publication progress, before any further release. '
                'Returns withdrawn asset, affected drafts, invalidated approvals and released reservations.')
            self.metadata['bind_campaign_learning']['description'] = (
                'Validate exact measured parents and current arm permissions plus new consent. '
                'Use the brief learning_policy; no corrected answer is returned on rejection.')
            self.metadata['list_campaign_briefs'] = {
                'description': 'Public lightweight brief directory, optionally one wave. No metrics or future event outcomes.',
                'input_schema': schema({'wave': {'type':'integer','minimum':1},
                    'offset': {'type':'integer','minimum':0}, 'limit': {'type':'integer','minimum':1,'maximum':100}}, [])}
        elif 'rights_update_after_wave' in public:
            self.metadata['poll_campaign_updates']['description'] = (
                'Poll the declared rights update after every delivery in wave '
                f"{public['rights_update_after_wave']} is complete; published history is retained.")

    def dispatch(self, name, args):
        require(name not in RETIRED_TOOLS, "TOOL_NOT_EXPOSED")
        if name == 'list_campaign_briefs' and self.public.get('campaign_waves'):
            rows = [dict(brief_id=b['brief_id'], wave=b['wave'], parent_brief_ids=b['parent_brief_ids'])
                    for b in self.state['briefs'].values() if 'wave' not in args or b['wave']==args['wave']]
            rows.sort(key=lambda b:(b['wave'], b['brief_id']))
            offset, limit = args.get('offset',0), args.get('limit',100)
            return dict(briefs=rows[offset:offset+limit], total=len(rows))
        return super().dispatch(name, args)

    def record_call(self, name, arguments, result):
        # Declaration attribution belongs to the controller, not business state.
        pass
