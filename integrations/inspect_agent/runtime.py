"""Inspect model/tool execution with an explicit text-protocol transport boundary."""
import asyncio
from copy import deepcopy
from importlib.metadata import version
import json

from inspect_ai.model import (
    ChatMessageSystem, ChatMessageUser, ChatMessageAssistant, GenerateConfig,
    Model, ModelAPI, ModelOutput, ModelUsage, execute_tools, modelapi,
)
from inspect_ai.tool import ToolCall, ToolDef, ToolParams
from integrations.harbor.controlled_agent import dispatch
from integrations.harbor.declarations import strict_json
from integrations.harbor.output_recovery import OutputLimitFeedback

PIN = '0.3.269'


async def settled_thread(function, *args, **kwargs):
    # Cancellation cannot leave a background tool mutating during scoring or
    # after the run lock is released. Provider's existing bounded deadlines apply.
    task=asyncio.create_task(asyncio.to_thread(function,*args,**kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancellation:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        try:
            task.result()
        except BaseException as error:
            raise cancellation from error
        raise cancellation



def chat_messages(messages):
    classes = {'system': ChatMessageSystem, 'developer': ChatMessageSystem,
               'user': ChatMessageUser, 'assistant': ChatMessageAssistant}
    return [classes[m['role']](content=m['content']) for m in messages]


@modelapi(name='skillhorizon_guarded')
class GuardedModelAPI(ModelAPI):
    """One Inspect generation maps to one bounded SkillHorizon logical request.

    No SDK provider, credential discovery, retry, fallback or native tool schema.
    The original wire envelope is retained, including developer role and output
    field. Inspect's developer->system projection is only its internal view.
    """
    def __init__(self, proxy, checkpoint):
        super().__init__(proxy.model, api_key_vars=[])
        self.proxy, self.checkpoint = proxy, checkpoint
        self.request = self.request_id = self.raw = self.rejection = None

    def should_retry(self, ex):
        return False

    async def generate(self, input, tools, tool_choice, config):
        if tools or any(getattr(m, 'tool_calls', None) for m in input):
            raise ValueError('Native tool calling is forbidden')
        expected = chat_messages(self.request['messages'])
        if [(m.role, m.text) for m in input] != [(m.role, m.text) for m in expected]:
            raise ValueError('Inspect changed the public input')
        saved = self.checkpoint.replay(self.request)
        if saved is not None:
            self.raw = json.dumps(saved['response'], ensure_ascii=False).encode()
            rejected = saved.get('rejected')
            if rejected:
                self.rejection = OutputLimitFeedback(self.raw, rejected['usage'], rejected['maximum'])
        else:
            self.checkpoint.request_pending(self.request, self.request_id)
            try:
                self.raw = await settled_thread(self.proxy.forward, deepcopy(self.request),
                                                   request_id=self.request_id)
            except OutputLimitFeedback as error:
                self.raw, self.rejection = error.raw, error
            self.checkpoint.response_accepted(self.raw, self.rejection)
        raw = strict_json(self.raw)
        choice = raw['choices'][0]
        content = choice['message'].get('content')
        # Preserve invalid/native payloads in raw; the policy classifies/rejects
        # them. Never translate native tool calls into executable JSON actions.
        output = ModelOutput.from_message(ChatMessageAssistant(
            content=content if isinstance(content, str) else '', model=self.proxy.model),
            stop_reason={'stop':'stop', 'length':'max_tokens', 'content_filter':'content_filter'}
                .get(choice.get('finish_reason'), 'unknown'))
        usage = raw.get('usage')
        if usage and all(type(usage.get(k)) is int for k in ('prompt_tokens','completion_tokens','total_tokens')):
            cached = (usage.get('prompt_tokens_details') or {}).get('cached_tokens')
            if type(cached) is not int or not 0 <= cached <= usage['prompt_tokens']:
                cached = None
            output.usage = ModelUsage(input_tokens=usage['prompt_tokens']-(cached or 0),
                input_tokens_cache_read=cached, output_tokens=usage['completion_tokens'],
                total_tokens=usage['total_tokens'])
        output.metadata = {'request_id':self.request_id, 'accounting_authority':'SkillHorizon provider ledger'}
        return output


class InspectRuntime:
    def __init__(self, proxy, state, checkpoint):
        self.version = version('inspect-ai')
        if self.version != PIN:
            raise RuntimeError(f'This adapter requires inspect-ai=={PIN}')
        self.state = state
        self.api = GuardedModelAPI(proxy, checkpoint)
        self.model = Model(self.api, GenerateConfig(max_retries=0, max_tokens=8192,
            max_connections=1, cache=False))

    async def generate(self, request, request_id):
        self.api.request, self.api.request_id = deepcopy(request), request_id
        self.api.raw = self.api.rejection = None
        self.state.messages = chat_messages(request['messages'])
        self.state.output = await self.model.generate(self.state.messages, tools=[], cache=False)
        if self.api.rejection is not None:
            raise self.api.rejection
        return self.api.raw

    async def dispatch(self, client, call, tools, **kwargs):
        """Parse text JSON first; execute exactly one explicit call via Inspect.

        Internal bridge is not advertised to the model. Validation stays inside
        the existing dispatcher so rejected calls still consume B and return the
        same public errors. No default parameter injection or output truncation.
        """
        box = {}
        async def public_call(action: dict) -> str:
            if action != call:
                raise ValueError('Inspect changed explicit tool arguments')
            try:
                box['result'] = await settled_thread(dispatch, client, action, tools, **kwargs)
            except BaseException as error:
                box['error'] = error
                return 'Unsettled tool execution; controller stops.'
            return json.dumps(box['result'], ensure_ascii=False, sort_keys=True)
        tdef = ToolDef(public_call, name='skillhorizon_public_call',
            description='Controller-only dispatch of one already parsed public action.',
            parameters=ToolParams.model_validate({'type':'object', 'properties':{
                'action':{'type':'object', 'additionalProperties':True}}, 'required':['action']}),
            parallel=False, max_output=0)
        message = ChatMessageAssistant(content='', tool_calls=[ToolCall(
            id='public-call', function=tdef.name, arguments={'action':deepcopy(call)})])
        messages, _ = await execute_tools([message], [tdef], max_output=0)
        if 'error' in box:
            raise box['error']
        if 'result' not in box or len(messages) != 1 or messages[0].error:
            raise RuntimeError('Inspect tool execution did not return a settled result')
        if messages[0].text != json.dumps(box['result'], ensure_ascii=False, sort_keys=True):
            raise RuntimeError('Inspect changed the public observation')
        return box['result']
