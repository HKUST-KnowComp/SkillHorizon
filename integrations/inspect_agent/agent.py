"""Explicit Inspect Agent entry; no eval scorer or automatic attempts."""
import asyncio
from contextlib import contextmanager
import fcntl
from pathlib import Path
from inspect_ai.agent import AgentState, agent, run as inspect_run
from .controller import execute_policy


@contextmanager
def run_locks(root, continuation):
    paths=[Path(root)]
    if continuation:
        paths.insert(0,Path(continuation['inspect_checkpoint']).resolve().parent)
    handles=[]
    try:
        for path in paths:
            path.mkdir(parents=True,exist_ok=True)
            handle=(path/'INSPECT_RUN.lock').open('a')
            handles.append(handle)
            try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError('Inspect run/source is still active') from None
        yield
    finally:
        for handle in handles:handle.close()


@agent
def skillhorizon_agent(export, root, proxy, **options):
    async def execute(state: AgentState) -> AgentState:
        state.skillhorizon_receipt = await execute_policy(export, root, proxy,
                                                         inspect_state=state, **options)
        return state
    return execute


async def run_async(export, root, proxy, **options):
    # The registered Agent is run by Inspect, not called as a legacy loop.
    with run_locks(root, options.get('continuation')):
        state = await inspect_run(skillhorizon_agent(export, root, proxy, **options), [])
        return state.skillhorizon_receipt


def run(export, root, proxy, **options):
    return asyncio.run(run_async(export, root, proxy, **options))
