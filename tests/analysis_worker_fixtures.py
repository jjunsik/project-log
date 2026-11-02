"""Spawn-importable synthetic models. No credentials, provider client or network."""

import json
import os
import time
from pathlib import Path

import httpx
from test_experience_gemini import ModelConnection
from test_experiences import OfflineModel

from project_log.experience_gemini import GeminiExperienceModel, TransientProviderError
from project_log.experiences import Citation, Claim, Discovery, Reconstruction
from project_log.gemini import GeminiConfig
from project_log.gemini_free_tier import FreeTierQuota, MinuteProviderError
from project_log.git import CollectionError


class ControlledOffline(OfflineModel):
    def __init__(self, profile):
        super().__init__()
        self.directory = Path(profile["test_directory"])
        self.mode = profile.get("test_mode", "success")
        if profile.get("test_control_file"):
            self.mode = Path(profile["test_control_file"]).read_text().strip()
        self.attempts = 0

    def complete(self, *args):
        self.attempts += 1
        (self.directory / f"attempt-{self.attempts}").write_text("synthetic model entry")
        if self.mode == "blocked":
            while not (self.directory / "release").exists():
                time.sleep(0.02)
        if self.mode == "crash":
            os._exit(19)
        if self.mode == "network":
            raise CollectionError("provider_network_error", "Synthetic unknown transmission")
        if self.mode == "network_stage_" + args[0]:
            raise CollectionError("provider_network_error", "Synthetic stage interruption")
        if self.mode == "empty":
            return Discovery(seeds=[])
        if self.mode in {"discovery_failure_after_one", "discovery_empty"}:
            if self.mode == "discovery_failure_after_one" and self.attempts == 2:
                raise CollectionError("provider_network_error", "Synthetic interrupted discovery")
            return Discovery(seeds=[])
        if self.mode == "blocked_audit" and args[0] == "audit":
            while not (self.directory / "release").exists():
                time.sleep(0.02)
        if self.mode == "503always" or (self.mode == "503once" and self.attempts == 1):
            raise TransientProviderError()
        if (
            self.mode in {"durable_quota_after_candidate", "durable_block_after_candidate"}
            and args[0] == "reconstruct"
        ):
            self.reconstruction_calls = getattr(self, "reconstruction_calls", 0) + 1
            if self.reconstruction_calls == 2:
                if self.mode == "durable_quota_after_candidate":
                    raise CollectionError(
                        "gemini_free_daily_limit", "Synthetic quota after valid candidate"
                    )
                while not (self.directory / "release").exists():
                    time.sleep(0.02)
        if self.mode == "planning_short_claims" and args[0] == "reconstruct":
            return Reconstruction(
                claims=[
                    Claim(
                        section="implementation",
                        statement=c.text[:100],
                        status="EVIDENCED",
                        citations=[Citation(evidence_id=c.id, quote=c.text[:100], locator=None)],
                    )
                    for c in args[2]
                    if c.text
                ]
            )
        result = super().complete(*args)
        if self.mode == "planning_many_claims" and args[0] == "reconstruct":
            result.claims = [
                result.claims[0].model_copy(update={"statement": f"{i}:" + "한글" * 500})
                for i in range(50)
            ]
        if self.mode == "durable_recover_audit" and args[0] == "audit":
            self.audit_calls = getattr(self, "audit_calls", 0) + 1
            if self.audit_calls == 1:
                result.findings = []
        if (
            self.mode
            in {
                "durable_quota_after_candidate",
                "durable_block_after_candidate",
                "durable_recover_audit",
            }
            and args[0] == "reconcile"
        ):
            context = json.loads(args[3])
            result.assignments = {s["seed_id"]: i for i, s in enumerate(context["seeds"])}
            ground = result.grounds[0].model_copy(
                update={"label": context["seeds"][0]["initial_group"]}
            )
            result.grounds = []
            result.splits = [ground]
        if args[0] == "reconcile" and self.mode.startswith("grounding_"):
            if self.mode == "grounding_503_first" and self.attempts == 3:
                raise TransientProviderError()
            feedback = json.loads(args[3]).get("relation_feedback")
            if feedback and self.mode in {"grounding_merge_once", "grounding_split_once"}:
                return result
            if feedback and self.mode == "grounding_merge_then_503":
                raise TransientProviderError()
            if feedback and self.mode == "grounding_merge_then_429":
                raise MinuteProviderError()
            if self.mode == "grounding_blank_reason":
                result.grounds[0].reason = " \t\n"
            elif self.mode == "grounding_extra_labels":
                if feedback:
                    context = json.loads(args[3])
                    result.splits = [
                        result.grounds[0].model_copy(
                            update={"label": context["seeds"][0]["initial_group"]}
                        )
                    ]
                else:
                    result.assignments = {key: i for i, key in enumerate(result.assignments)}
            elif self.mode.startswith("grounding_split_"):
                context = json.loads(args[3])
                ground = result.grounds[0].model_copy(
                    update={"label": context["seeds"][0]["initial_group"]}
                )
                result.assignments = {key: i for i, key in enumerate(result.assignments)}
                result.grounds = []
                result.splits = [ground]
                if self.mode == "grounding_split_duplicate":
                    result.splits.append(ground.model_copy())
                else:
                    ground.support_ids = [ground.support_ids[0], "private-outside-support"]
            elif self.mode in {
                "grounding_merge_missing",
                "grounding_merge_once",
                "grounding_merge_then_503",
                "grounding_merge_then_429",
            }:
                result.grounds = []
            else:
                result.grounds[0].support_ids = [
                    result.grounds[0].support_ids[0],
                    "private-outside-support",
                ]
        if self.mode == "stage_" + args[0]:
            if args[0] == "consolidate":
                result.assignments.pop(next(reversed(result.assignments)))
            elif args[0] == "reconcile":
                result.assignments.pop(next(reversed(result.assignments)))
            elif args[0] == "reconstruct":
                result.claims[0].citations[0].evidence_id = "synthetic-outside-id"
            elif args[0] == "audit":
                result.findings = []
            elif args[0] == "assess":
                return Discovery(seeds=[])  # Wrong structured result, never a partial success.
        if args[0] == "discover" and self.mode.startswith("reference_"):
            ref = args[2][0].id
            result.seeds[0].evidence_ids = {
                "reference_out_of_batch": ["synthetic-outside-id"],
                "reference_duplicate": [ref, ref],
                "reference_out_of_batch_duplicate": [
                    "synthetic-outside-id",
                    "synthetic-outside-id",
                ],
            }[self.mode]
        return result


def offline_factory(profile, gate):
    mode = profile.get("test_mode", "success")
    if profile.get("test_control_file"):
        mode = Path(profile["test_control_file"]).read_text().strip()
    if mode == "grounding_sdk_once":
        directory = Path(profile["test_directory"])

        class GroundingConnection(ModelConnection):
            def handle_request(self, request):
                body = json.loads(request.content)
                prompt = json.loads(body["contents"][0]["parts"][0]["text"])
                if prompt["task"] == "reconcile":
                    context = json.loads(prompt["context"])
                    index = len(list(directory.glob("relation-wire-*.json"))) + 1
                    (directory / f"relation-wire-{index}.json").write_text(json.dumps(body))
                    if "relation_feedback" not in context:
                        self.response_override = {
                            "assignments": {s["seed_id"]: 0 for s in context["seeds"]},
                            "grounds": [
                                {
                                    "label": 0,
                                    "reason": "private-response-reason",
                                    "support_ids": ["private-outside-support"],
                                }
                            ],
                            "splits": [],
                        }
                return super().handle_request(request)

        return GeminiExperienceModel(
            GeminiConfig(profile["model"], "synthetic-test-key", True),
            test_transport_factory=lambda: GroundingConnection(
                retries=0, verify=True, trust_env=False
            ),
            before_send=gate,
            quota=FreeTierQuota(directory / "quota.json"),
        )
    if mode.startswith("checkpoint_sdk"):

        class CheckpointConnection(ModelConnection):
            def handle_request(self, request):
                body = json.loads(request.read())
                task = json.loads(body["contents"][0]["parts"][0]["text"])["task"]
                if task == "audit" and mode in {"checkpoint_sdk503", "checkpoint_sdk500"}:
                    self.status = 503 if mode.endswith("503") else 500
                elif task == "audit" and mode == "checkpoint_sdk_failure":
                    self.error = httpx.ReadTimeout("Synthetic transport interruption")
                return super().handle_request(request)

        return GeminiExperienceModel(
            GeminiConfig(profile["model"], "synthetic-test-key", True),
            test_transport_factory=lambda: CheckpointConnection(
                retries=0, verify=True, trust_env=False
            ),
            before_send=gate,
            quota=FreeTierQuota(
                Path(profile["test_directory"]) / "quota.json",
                **(
                    dict(
                        clock=lambda: float(Path(profile["test_quota_clock"]).read_text()),
                        wait=lambda seconds: advance_test_clock(profile, seconds),
                    )
                    if profile.get("test_quota_clock")
                    else {}
                ),
            ),
        )
    if mode in {"sdk_read_timeout", "sdk_body_timeout", "sdk_cleanup"}:

        def diagnostic_transport():
            connection = ModelConnection(retries=0, verify=True, trust_env=False)
            if mode == "sdk_read_timeout":
                connection.error = httpx.ReadTimeout("private synthetic request/response")
            elif mode == "sdk_body_timeout":
                connection.chunks = [b'{"private":']
                connection.stream_error = httpx.ReadTimeout("private synthetic body")
                connection.stream_close_error = OSError("private response cleanup")
            connection.close_error = httpx.CloseError("private transport cleanup")
            return connection

        return GeminiExperienceModel(
            GeminiConfig(profile["model"], "synthetic-test-key", True),
            test_transport_factory=diagnostic_transport,
            before_send=gate,
            quota=FreeTierQuota(
                Path(profile["test_directory"]) / "quota.json",
                **(
                    dict(
                        clock=lambda: float(Path(profile["test_quota_clock"]).read_text()),
                        wait=lambda seconds: advance_test_clock(profile, seconds),
                    )
                    if profile.get("test_quota_clock")
                    else {}
                ),
            ),
        )
    if profile.get("test_mode") == "sdk503":
        calls = []

        def transport():
            connection = ModelConnection(retries=0, verify=True, trust_env=False)
            connection.status = 503 if not calls else 200
            calls.append(connection)
            return connection

        return GeminiExperienceModel(
            GeminiConfig(profile["model"], "synthetic-test-key", True),
            test_transport_factory=transport,
            before_send=gate,
        )
    return ControlledOffline(profile)


def advance_test_clock(profile, seconds):
    clock = Path(profile["test_quota_clock"])
    clock.write_text(str(float(clock.read_text()) + seconds))
    return False
