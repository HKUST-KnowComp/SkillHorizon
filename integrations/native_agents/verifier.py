"""Harbor host verifier: score the live sealed sidecar, never agent-written files."""
import asyncio

from harbor.models.verifier.result import VerifierResult
from harbor.verifier.base import BaseVerifier

from .common import decode, read
from .gateways import accepted


def validate_report(report):
    if not isinstance(report, dict) or "error" in report:
        raise ValueError("No valid private evaluation")
    state = report.get("state", {})
    if (state.get("sealed") is not True or state.get("uncertain") is not False
            or state.get("inflight") is not None or "inflight" not in state
            or state.get("phase") != "sealed"):
        raise ValueError("Evaluation was not sealed and settled")
    value = accepted(report.get("business"))
    if type(report.get("accepted")) is not bool or report["accepted"] != value:
        raise ValueError("Evaluation normalization mismatch")
    return float(value)


class NativeVerifier(BaseVerifier):
    async def verify(self):
        result = await self.environment.service_exec(
            "python -m integrations.native_agents.service seal", service="business", timeout_sec=600)
        if result.return_code != 0:
            raise RuntimeError("Private native service did not produce a valid sealed evaluation")
        validate_report(decode(result.stdout))
        destination = self.trial_paths.trial_dir / "business_private"
        destination.mkdir(exist_ok=False)
        await asyncio.wait_for(self.environment.service_download_dir(
            "/state", destination, service="business"), timeout=45)
        report = read(destination / "EVALUATION.json")
        state = read(destination / "STATE.json")
        if (state.get("phase") != "scored" or state.get("sealed") is not True
                or state.get("uncertain") is not False or state.get("inflight") is not None
                or state.get("attempts") != report["state"].get("attempts")):
            raise RuntimeError("Private score and collected state disagree")
        return VerifierResult(rewards={"reward": validate_report(report)})
