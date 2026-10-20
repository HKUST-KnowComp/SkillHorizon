"""Seven existing worlds, with no model loop or alternate scoring semantics."""
from .common import read


class FieldGateway:
    def __init__(self, task, directory, *, max_calls):
        from integrations.harbor.backend import BusinessGateway
        self.gateway = BusinessGateway(task, directory, max_calls=max_calls)
        self.client = self

    def tools(self):
        return {"tools": list(self.gateway.tools.values())}

    def call(self, name, arguments, **metadata):
        return self.gateway.call(name, arguments, **metadata)["result"]

    def seal_and_score(self):
        return self.gateway.seal_and_score()

    def close(self):
        pass


def create(environment, task, state, B, public):
    from integrations.inspect_agent.environments import environment_options
    options = environment_options(environment, task, B=B, public_task=read(public),
                                  declarations_consume_B=False)
    factory = options["gateway_factory"]
    if factory is None:
        if environment != "FieldOps":
            raise ValueError("Only native FinanceOps V6 bundles are supported")
        factory = FieldGateway
    return factory(task, state, max_calls=B), options["tool_profile"]


def accepted(score):
    from integrations.inspect_agent.scoring import business_accepted
    return business_accepted(score)
