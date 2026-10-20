"""Single-parent PTC bridge between guest frames and an owned business client.

The caller supplies a validated, consumed grant and the worker's checked public
catalog. This layer owns neither a business world nor the worker process: on an
unknown effect it closes IPC, and the native process owner must still quiesce it.
"""

from copy import deepcopy

from jsonschema import Draft202012Validator

from .argument_feedback import schema_rejection
from .ptc_budget import ParentBudget, PTCBudgetError


class PTCBridgeError(RuntimeError):
    pass


class PTCBridge:
    def __init__(self, *, budget, tools, client):
        if not isinstance(budget, ParentBudget):
            raise TypeError("A controller-owned parent budget is required")
        if not client.ready or client.tools != tools:
            raise PTCBridgeError("PTC_WORKER_CATALOG_MISMATCH")
        self.budget, self.client = budget, client
        self.validators = {}
        for tool in deepcopy(tools):
            name = tool["name"]
            if (name in self.validators or name in {"python_execute", "ptc_execute"}
                    or name.startswith("workspace_")):
                raise PTCBridgeError("PTC_CATALOG_INVALID")
            Draft202012Validator.check_schema(tool["parameters"])
            self.validators[name] = Draft202012Validator(tool["parameters"])
        self.worker_seq = None
        self.failed = False

    def _open(self):
        if self.failed or self.budget.stopped:
            raise PTCBridgeError("PTC_BRIDGE_CLOSED")

    def abort(self, code):
        # Closing IPC does not mean the business writer has stopped. The native
        # owner retains that responsibility, including when this method raises.
        self.failed = True
        try:
            if not self.budget.stopped:
                if self.budget.active == "dispatched":
                    self.budget.unknown(code)
                elif self.budget.active == "admitted":
                    self.budget.cancel_before_dispatch(code)
                else:
                    self.budget.stop(code)
        finally:
            self.client.close()

    def accept(self, frame):
        """Return a recorded rejection frame or None while business is pending."""
        self._open()
        if (type(frame) is not dict or set(frame) != {"type", "seq", "payload"}
                or frame["type"] != "child_request" or type(frame["seq"]) is not int
                or frame["seq"] != self.budget.admitted + 1 or self.worker_seq is not None):
            self.abort("REQUEST_ORDER_INVALID")
            raise PTCBridgeError("PTC_REQUEST_ORDER_INVALID")
        request = frame["payload"]
        try:
            seq = self.budget.admit(request)
            if (type(request) is not dict or set(request) != {"name", "arguments"}
                    or type(request["name"]) is not str):
                output = self.budget.reject("ARGUMENT_SCHEMA")
                return {"type": "child_response", "seq": seq, "payload": output}
            name, arguments = request["name"], request["arguments"]
            if name not in self.validators:
                output = self.budget.reject("TOOL_NOT_EXPORTED")
                return {"type": "child_response", "seq": seq, "payload": output}
            rejected = schema_rejection(self.validators[name], arguments)
            if rejected is not None:
                output = self.budget.reject("ARGUMENT_SCHEMA", rejected)
                return {"type": "child_response", "seq": seq, "payload": output}
            self.budget.dispatch_intent()
            self.worker_seq = self.client.submit(name, arguments)
            return None
        except (PTCBudgetError, ValueError):
            if self.budget.stopped and self.budget.active is None and not self.budget.failed:
                # A known program quota/representation stop closes only this
                # bridge. The episode's idle business worker can still be used.
                return {"type": "terminate", "seq": frame["seq"],
                        "payload": {"code": "PTC_PROGRAM_LIMIT"}}
            self.abort("CHILD_SUBMISSION_FAILED")
            raise
        except Exception:
            self.abort("CHILD_SUBMISSION_FAILED")
            raise

    def poll(self, deadline):
        """Nonblocking when deadline is now; usable in the Docker I/O selector."""
        self._open()
        if self.worker_seq is None:
            return None
        try:
            reply = self.client.poll(deadline)
            if reply is None:
                return None
            if reply.seq != self.worker_seq or reply.dispatched is not True:
                raise PTCBridgeError("PTC_WORKER_DISPATCH_MISMATCH")
            output = self.budget.complete(reply.output)
            self.worker_seq = None
            return {"type": "child_response", "seq": self.budget.admitted, "payload": output}
        except Exception:
            self.abort("CHILD_RESPONSE_UNKNOWN")
            raise

    def finish(self, reason="PROGRAM_COMPLETE"):
        if self.budget.stopped and self.budget.active is None and not self.failed:
            return
        self._open()
        if self.worker_seq is not None or self.budget.active is not None:
            self.abort("PROGRAM_ENDED_WITH_ACTIVE_CHILD")
            raise PTCBridgeError("PTC_ACTIVE_CHILD_AT_PROGRAM_END")
        self.budget.stop(reason)
