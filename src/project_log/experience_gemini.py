"""LangChain structured/multimodal adapter. Live project transfer is disabled in stage 2."""

import base64
import json
import os
from collections.abc import Callable, Sequence
from typing import Any

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langsmith import tracing_context
from pydantic import BaseModel

from project_log.analysis_input import canonical
from project_log.document_media import MAX_INLINE_REQUEST_BYTES
from project_log.experiences import Chunk
from project_log.gemini import HOST, REVIEWED_MODELS, GeminiConfig, _SyntheticTransport
from project_log.git import CollectionError


class _ExperienceTransport(_SyntheticTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # Measure the complete SDK wire request, including system/schema/metadata.
        # This local budget is not a claim about live model/account acceptance.
        if len(request.content) > MAX_INLINE_REQUEST_BYTES:
            raise CollectionError(
                "analysis_request_too_large", "전체 inline 요청 크기 제한을 초과했습니다."
            )
        return super().handle_request(request)


class GeminiExperienceModel:
    def __init__(
        self,
        config: GeminiConfig,
        *,
        test_transport_factory: Callable[[], httpx.BaseTransport] | None = None,
    ):
        self.config = config
        self.test_transport_factory = test_transport_factory
        self.usage: list[dict[str, Any]] = []

    def complete(
        self,
        task: str,
        instruction: str,
        chunks: Sequence[Chunk],
        context: str,
        schema: type[BaseModel],
    ) -> BaseModel:
        # Product API supplies no factory. A boolean or past synthetic consent cannot
        # enable live transfers. Stage 3 must integrate durable consent + mutation locks.
        if self.test_transport_factory is None:
            raise CollectionError(
                "analysis_transfer_disabled",
                "실제 자료 전송은 아직 비활성입니다. 실행 동의·자료 보호 연결이 필요합니다.",
            )
        if self.config.model not in REVIEWED_MODELS or not self.config.free_tier_confirmed:
            raise CollectionError("ai_not_configured", "검토된 무료 모델 설정이 필요합니다.")
        if any(
            os.environ.get(name, "").lower() == "true"
            for name in ("GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_ENTERPRISE")
        ):
            raise CollectionError("ai_not_configured", "Developer API 설정만 허용합니다.")
        content: list[str | dict[str, Any]] = [
            {
                "type": "text",
                "text": canonical(
                    {
                        "task": task,
                        "context": context,
                        "evidence": [c.payload() for c in chunks],
                    }
                ),
            }
        ]
        for chunk in chunks:
            if chunk.media:
                if not chunk.media.use_for_analysis or chunk.media.mime_type not in {
                    "application/pdf",
                    "image/png",
                    "image/jpeg",
                }:
                    raise CollectionError(
                        "analysis_media_unavailable",
                        "원본 사본 또는 미지원 형식은 AI에 전송하지 않습니다.",
                    )
                content.extend(
                    [
                        {"type": "text", "text": "다음 비텍스트 근거 id: " + chunk.id},
                        {
                            "type": "file",
                            "source_type": "base64",
                            "mime_type": chunk.media.mime_type,
                            "data": base64.b64encode(chunk.media.raw).decode("ascii"),
                        },
                    ]
                )
        # Cheap preliminary bound; final SDK serialization is checked by the transport.
        # Files API, URL downloads, caching and tools remain unavailable.
        if len(canonical(content).encode()) > MAX_INLINE_REQUEST_BYTES:
            raise CollectionError(
                "analysis_request_too_large", "분할 입력의 inline 전송 크기 제한을 초과했습니다."
            )
        transport = _ExperienceTransport(self.config.model, inner=self.test_transport_factory())
        model: ChatGoogleGenerativeAI | None = None
        try:
            with tracing_context(enabled=False):
                model = ChatGoogleGenerativeAI(
                    model=self.config.model,
                    api_key=self.config.api_key,
                    vertexai=False,
                    base_url=f"https://{HOST}",
                    api_version="v1beta",
                    temperature=None,
                    max_tokens=8192,
                    timeout=60,
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
                result = model.with_structured_output(schema, method="json_schema").invoke(
                    [SystemMessage(content=instruction), HumanMessage(content=content)],
                    config={"callbacks": []},
                    http_options={"retry_options": {"attempts": 1}},
                )
            if not isinstance(result, schema):
                raise ValueError("Unexpected structured output")
            self.usage.append({"task": task, "tokens": json.loads(transport.usage_json)})
            return result
        except CollectionError:
            raise
        except (OSError, httpx.HTTPError):
            raise CollectionError(
                "provider_network_error", "Gemini 연결 또는 응답 수신 실패입니다."
            ) from None
        except Exception:
            # Do not include SDK exceptions that can contain prompt/headers/provider body.
            raise CollectionError(
                "provider_invalid_response", "Gemini 구조화 응답 검증 실패입니다."
            ) from None
        finally:
            if model is not None and model.client is not None:
                model.client.close()
            transport.close()
