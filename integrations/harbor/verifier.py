"""Controller-only Harbor verifier; agent files never establish business success."""

import asyncio
import json

from harbor.models.verifier.result import VerifierResult
from harbor.verifier.base import BaseVerifier


class SkillHorizonVerifier(BaseVerifier):
    def __init__(self, *, max_calls, require_code_origins=False, origin_manifest=None, **kwargs):
        self._origin_manifest = origin_manifest
        super().__init__(**kwargs)
        if type(max_calls) is not int or max_calls < 1:
            raise ValueError("Explicit business-call budget required")
        self.max_calls = max_calls
        self.require_code_origins = require_code_origins

    async def verify(self):
        from integrations.harbor.native_origins import scope
        with scope(self._origin_manifest, self.trial_paths.trial_dir, "verifier", objects={
                "verifier": self, "environment": self.environment}):
            return await self._verify_business()

    async def _verify_business(self):
        # Harbor mounts verifier/ in main; private state must never be collected there.
        destination = self.trial_paths.trial_dir / "business_private"
        destination.mkdir(parents=True, exist_ok=False)
        result, execution_error, collection_error = None, None, None
        try:
            result = await self.environment.service_exec(
                "python -m integrations.harbor.backend verify /task /state --max-calls " + str(self.max_calls)
                + (" --require-code-origins" if self.require_code_origins else ""),
                service="business", timeout_sec=540)
        except BaseException as error:
            execution_error = error
        try:
            await asyncio.wait_for(self.environment.service_download_dir(
                "/state", destination, service="business"), timeout=45)
        except BaseException as error:
            collection_error = error
        if execution_error is not None or collection_error is not None:
            (destination / "collection_failure.json").write_text(json.dumps({
                "execution_error": type(execution_error).__name__ if execution_error else None,
                "collection_error": type(collection_error).__name__ if collection_error else None,
                "snapshot_consistency": "unverified; not valid business evidence",
                "container_policy": "qualification jobs retain containers"}))
            if execution_error is not None:
                raise execution_error
            raise collection_error
        if result.return_code != 0:
            raise RuntimeError("Business verification failed; no valid business reward (see trusted logs)")
        if self.require_code_origins:
            origins = [json.loads((destination / ("CODE_ORIGINS_" + phase + ".json")).read_text())
                       for phase in ("SERVE", "VERIFY")]
            if (any(record.get("status") != "passed" for record in origins)
                    or origins[0]["code_manifest_sha256"] != origins[1]["code_manifest_sha256"]
                    or origins[0]["business_source_root"] != origins[1]["business_source_root"]):
                raise RuntimeError("Private runtime/evaluator source identity differs")
        report = json.loads((destination / "evaluation.json").read_text())
        business = report["business"]
        if (business.get("valid") is not True or type(business.get("task_success")) is not bool
                or report["gateway"].get("sealed") is not True
                or report["gateway"].get("execution_unknown") is not False):
            raise RuntimeError("Invalid business replay/seal; no valid business reward")
        return VerifierResult(rewards={"reward": float(business["task_success"])})
