"""LangChain structured/multimodal adapter; live use requires an admitted execution gate."""

import base64
import json
import os
import ssl
from collections.abc import Callable, Sequence
from typing import Any

import httpx
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langsmith import tracing_context
from pydantic import BaseModel, ValidationError

from project_log.analysis_input import canonical
from project_log.document_media import MAX_INLINE_REQUEST_BYTES
from project_log.experiences import Chunk
from project_log.gemini import (
    HOST,
    REVIEWED_MODELS,
    GeminiConfig,
    _SyntheticTransport,
    error_details,
)
from project_log.gemini_free_tier import FreeTierQuota
from project_log.gemini_request import provider_schema, validate_schema, validate_wire
from project_log.git import CollectionError
from project_log.policy import sha256

GEMINI_REQUEST_TIMEOUT_SECONDS = 120
INVALID_REQUEST_DETAILS_BYTES = 8192


def _invalid_request(response: httpx.Response) -> CollectionError:
    """Retain finite provider categories, never its message, field path or raw body.

    A 400 is authoritative even when its diagnostic stream is malformed, too
    large or fails. Diagnostic failure must not turn it into a retryable error.
    These aliases describe an explicit provider report, not a local guess from
    schema size. Unknown historical errors cannot be retrospectively classified.
    """
    kind = ""
    try:
        error = error_details(response, INVALID_REQUEST_DETAILS_BYTES)
        # API key errors may be reported as 400 INVALID_ARGUMENT rather than 401.
        details = error.get("details", [])
        if error.get("code") == 400 and error.get("status") == "INVALID_ARGUMENT":
            for detail in details if isinstance(details, list) else []:
                if (
                    isinstance(detail, dict)
                    and detail.get("@type") == "type.googleapis.com/google.rpc.ErrorInfo"
                    and detail.get("domain") == "googleapis.com"
                    and detail.get("reason") == "API_KEY_INVALID"
                ):
                    return CollectionError(
                        "provider_authentication", "Gemini API Key 설정을 확인하세요."
                    )
        if error["code"] != 400 or error["status"] != "INVALID_ARGUMENT":
            raise ValueError
        message = error.get("message", "")
        if not isinstance(message, str):
            raise ValueError
        message = message.casefold()
        if "schema" in message and "maximum allowed nesting depth" in message:
            kind = "_schema_depth"
        elif "schema" in message and any(
            signature in message for signature in ("too many states", "too much branching")
        ):
            kind = "_schema_complexity"
        elif message.startswith("request payload size exceeds the limit"):
            kind = "_request_size"
    except Exception:
        pass  # Keep the observed HTTP 400; neither forward nor log provider data.
    return CollectionError(
        "provider_invalid_request" + kind,
        "Gemini가 HTTP 400으로 요청을 거절했습니다. 원인 확인·수정 전 재전송하지 않습니다.",
    )


def _provider_failure(exc: Exception, stage: str, status: int | None) -> CollectionError:
    """Finite aliases, never exception names/messages, request data or provider text."""
    cause: BaseException | None = exc
    for _ in range(6):
        if isinstance(cause, ssl.SSLCertVerificationError):
            return CollectionError(
                "provider_certificate_error", "Gemini TLS 인증서 검증에 실패했습니다."
            )
        cause = cause.__cause__ if cause is not None else None
    kinds = (
        (httpx.ConnectTimeout, "connect_timeout"),
        (httpx.ReadTimeout, "read_timeout"),
        (httpx.WriteTimeout, "write_timeout"),
        (httpx.PoolTimeout, "pool_timeout"),
        (httpx.TimeoutException, "timeout"),
        (httpx.ConnectError, "connect_error"),
        (httpx.ReadError, "read_error"),
        (httpx.WriteError, "write_error"),
        (httpx.CloseError, "close_error"),
        (httpx.LocalProtocolError, "local_protocol"),
        (httpx.RemoteProtocolError, "remote_protocol"),
        (httpx.ProtocolError, "protocol_error"),
        (httpx.DecodingError, "decoding_error"),
        (httpx.StreamConsumed, "stream_consumed"),
        (httpx.StreamClosed, "stream_closed"),
        (httpx.ResponseNotRead, "response_not_read"),
        (httpx.RequestNotRead, "request_not_read"),
        (httpx.StreamError, "stream_error"),
        (httpx.HTTPError, "http_error"),
        (TimeoutError, "os_timeout"),
        (OSError, "os_error"),
        (json.JSONDecodeError, "invalid_json"),
        (ValidationError, "validation_error"),
        (OutputParserException, "output_parser"),
    )
    kind = next((name for cls, name in kinds if isinstance(exc, cls)), "unexpected_error")
    if isinstance(exc, CollectionError) and exc.code == "provider_invalid_response":
        kind = "invalid_response"
    stages = {
        "request",
        "setup",
        "wire",
        "quota",
        "gate",
        "transport",
        "headers",
        "body_wait",
        "body_partial",
        "body_complete",
        "envelope",
        "quota_usage",
        "sdk",
        "validation",
        "response_close",
        "client_close",
        "transport_close",
        "rate_limit_body",
    }
    boundary = stage if stage in stages else "unknown"
    http = str(status) if type(status) is int and 100 <= status <= 599 else "unknown"
    return CollectionError(
        f"provider_{kind}_{boundary}_h{http}",
        "Gemini 호출·응답 처리 오류로 분석을 중단했습니다. 자동 재전송하지 않습니다.",
    )


class TransientProviderError(CollectionError):
    """An observed recoverable HTTP response; never inferred from a network timeout."""

    def __init__(self, status: int = 503) -> None:
        if status not in {500, 502, 503, 504}:
            raise ValueError("Not a reviewed transient status")
        super().__init__(
            "provider_temporarily_unavailable"
            if status == 503
            else f"provider_server_error_h{status}",
            "Gemini의 일시적 서버 오류입니다. 제한된 재시도만 허용합니다.",
        )


def validate_request_schema(schema: dict[str, Any]) -> None:
    """Check provable GenerateContent constraints, without rewriting the schema.

    This is not Google's schema compiler or a complexity/acceptance estimate.
    Walk schema positions only, never property names or enum data as keywords.
    """

    validate_schema(schema)


class _ExperienceTransport(_SyntheticTransport):
    def __init__(
        self,
        model: str,
        *,
        inner: httpx.BaseTransport | None,
        gate: Callable[[str], None] | None = None,
        expected_instruction: str,
        expected_parts: list[dict[str, Any]],
        expected_schema: dict[str, Any],
        quota: FreeTierQuota | None = None,
    ):
        super().__init__(model, inner=inner)
        self.gate = gate
        self.expected_instruction = expected_instruction
        self.expected_parts = expected_parts
        self.expected_schema = expected_schema
        self.quota = quota
        self.reservation: str | None = None

    def before_send(self, request: httpx.Request) -> None:
        def enter() -> None:
            self.stage = "gate"
            if self.gate:
                self.gate(sha256(request.content))
            self.stage = "quota"

        self.stage = "quota"
        if self.quota:
            self.reservation = self.quota.reserve(request.content, enter)
        else:
            enter()

    def response_error(self, response: httpx.Response) -> None:
        if response.status_code == 400:
            raise _invalid_request(response)
        if response.status_code == 429 and self.quota:
            self.stage = "rate_limit_body"
            self.quota.reject_429(response)
        if response.status_code in {500, 502, 503, 504}:
            raise TransientProviderError(response.status_code)
        if response.status_code not in {401, 403, 404, 429}:
            raise CollectionError(
                f"provider_http_h{response.status_code}",
                "Gemini가 요청을 거절했습니다. 상태 확인 전 자동 재전송하지 않습니다.",
            )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.stage = "wire"
        try:
            body = json.loads(request.content)
            if (
                set(body) != {"contents", "systemInstruction", "safetySettings", "generationConfig"}
                or body["safetySettings"] != []
                or body["contents"] != [{"role": "user", "parts": self.expected_parts}]
                or body["systemInstruction"] != {"parts": [{"text": self.expected_instruction}]}
                or body["generationConfig"].get("responseJsonSchema") != self.expected_schema
                or set(body["generationConfig"])
                != {"candidateCount", "maxOutputTokens", "responseMimeType", "responseJsonSchema"}
                or body["generationConfig"]["candidateCount"] != 1
                or body["generationConfig"]["maxOutputTokens"] != 8192
                or body["generationConfig"]["responseMimeType"] != "application/json"
            ):
                raise ValueError("Unexpected serialized input")
        except Exception:
            raise CollectionError(
                "analysis_wire_mismatch", "실제 전송 내용이 승인된 호출 입력과 일치하지 않습니다."
            ) from None
        # Measure the complete SDK wire request, including system/schema/metadata.
        # This local budget is not a claim about live model/account acceptance.
        if len(request.content) > MAX_INLINE_REQUEST_BYTES:
            raise CollectionError(
                "analysis_request_too_large", "전체 inline 요청 크기 제한을 초과했습니다."
            )
        validate_wire(body)
        response = super().handle_request(request)
        self.stage = "quota_usage"
        if self.quota and self.reservation:
            self.quota.usage(self.reservation, json.loads(self.usage_json).get("promptTokenCount"))
        # The buffered/envelope-checked response is now handed back to the SDK.
        # SDK internals may still fail before or during structured parsing.
        self.stage = "sdk"
        return response


class GeminiExperienceModel:
    def __init__(
        self,
        config: GeminiConfig,
        *,
        test_transport_factory: Callable[[], httpx.BaseTransport] | None = None,
        before_send: Callable[[str], None] | None = None,
        quota: FreeTierQuota | None = None,
    ):
        self.config = config
        self.test_transport_factory = test_transport_factory
        self.before_send = before_send
        self.quota = quota
        self.usage: list[dict[str, Any]] = []

    def complete(
        self,
        task: str,
        instruction: str,
        chunks: Sequence[Chunk],
        context: str,
        schema: type[BaseModel],
    ) -> BaseModel:
        if os.environ.get("PROJECT_LOG_AI_PREPARATION_ONLY") == "1":
            raise CollectionError("analysis_transfer_disabled", "로컬 준비 전용입니다.")
        # Live transfers require the worker's durable consent/mutation-lock gate.
        # A boolean or past synthetic consent cannot enable live transfers.
        if self.test_transport_factory is None and self.before_send is None:
            raise CollectionError(
                "analysis_transfer_disabled",
                "실제 자료 전송은 아직 비활성입니다. 실행별 동의·입력 고정 연결이 필요합니다.",
            )
        if self.config.model not in REVIEWED_MODELS or not self.config.free_tier_confirmed:
            raise CollectionError("ai_not_configured", "검토된 무료 모델 설정이 필요합니다.")
        if any(
            os.environ.get(name, "").lower() == "true"
            for name in ("GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_ENTERPRISE")
        ):
            raise CollectionError("ai_not_configured", "Developer API 설정만 허용합니다.")
        if any(
            c.media is not None
            and (
                not c.media.use_for_analysis
                or c.media.mime_type not in {"application/pdf", "image/png", "image/jpeg"}
            )
            for c in chunks
        ):
            raise CollectionError(
                "analysis_media_unavailable", "지원하지 않는 비텍스트 전송 자료입니다."
            )
        transport: _ExperienceTransport | None = None
        model: ChatGoogleGenerativeAI | None = None
        failed = True
        stage = "request"
        try:
            text = canonical(
                {"task": task, "context": context, "evidence": [c.payload() for c in chunks]}
            )
            content: list[str | dict[str, Any]] = [{"type": "text", "text": text}]
            expected_parts: list[dict[str, Any]] = [{"text": text}]
            for chunk in chunks:
                if chunk.media is not None:
                    encoded = base64.b64encode(chunk.media.raw).decode("ascii")
                    content.append(
                        {"type": "media", "mime_type": chunk.media.mime_type, "data": encoded}
                    )
                    expected_parts.append(
                        {
                            "inlineData": {
                                "mime_type": chunk.media.mime_type,
                                "data": base64.urlsafe_b64encode(chunk.media.raw).decode("ascii"),
                            }
                        }
                    )
            # Cheap preliminary bound; final SDK serialization is checked by the transport.
            # Files API, URL downloads, caching and tools remain unavailable.
            if len(canonical(content).encode()) > MAX_INLINE_REQUEST_BYTES:
                raise CollectionError(
                    "analysis_request_too_large",
                    "분할 입력의 inline 전송 크기 제한을 초과했습니다.",
                )
            stage = "setup"
            expected_schema = provider_schema(schema.model_json_schema())
            transport = _ExperienceTransport(
                self.config.model,
                inner=self.test_transport_factory() if self.test_transport_factory else None,
                gate=self.before_send,
                expected_instruction=instruction,
                expected_parts=expected_parts,
                expected_schema=expected_schema,
                quota=self.quota,
            )
            with tracing_context(enabled=False):
                model = ChatGoogleGenerativeAI(
                    model=self.config.model,
                    api_key=self.config.api_key,
                    vertexai=False,
                    base_url=f"https://{HOST}",
                    api_version="v1beta",
                    temperature=None,
                    max_tokens=8192,
                    timeout=GEMINI_REQUEST_TIMEOUT_SECONDS,
                    max_retries=0,
                    disable_streaming=True,
                    cache=False,
                    callbacks=[],
                    client_args={
                        "transport": transport,
                        "trust_env": False,
                        "follow_redirects": False,
                    },
                )
                transport.stage = "sdk"
                result = model.with_structured_output(expected_schema, method="json_schema").invoke(
                    [SystemMessage(content=instruction), HumanMessage(content=content)],
                    config={"callbacks": []},
                    http_options={"retry_options": {"attempts": 1}},
                )
            transport.stage = "validation"
            if not isinstance(result, dict):
                raise CollectionError("provider_invalid_response", "구조화 응답 형식 불일치입니다.")
            validated = schema.model_validate(result)
            self.usage.append({"task": task, "tokens": json.loads(transport.usage_json)})
            failed = False
            return validated
        except CollectionError as exc:
            if exc.code == "provider_invalid_response":
                raise _provider_failure(
                    exc,
                    transport.stage if transport else stage,
                    transport.status_code if transport else None,
                ) from None
            raise
        except Exception as exc:
            raise _provider_failure(
                exc,
                transport.stage if transport else stage,
                transport.status_code if transport else None,
            ) from None
        finally:
            cleanup: CollectionError | None = None
            try:
                if transport:
                    transport.stage = "client_close"
                if model is not None and model.client is not None:
                    model.client.close()
            except Exception as exc:
                if not failed:
                    cleanup = _provider_failure(
                        exc,
                        transport.stage if transport else "client_close",
                        transport.status_code if transport else None,
                    )
            try:
                if transport:
                    transport.close()
            except Exception as exc:
                if not failed and cleanup is None:
                    cleanup = _provider_failure(
                        exc, "transport_close", transport.status_code if transport else None
                    )
            if cleanup is not None:
                raise cleanup from None
