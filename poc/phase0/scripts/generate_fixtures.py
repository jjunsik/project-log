"""Build synthetic pilot candidates, never approved Golden data or model results.

The small Python verification artifacts are real executions of synthetic programs.
Conversation, diff and timeline artifacts are explicitly authored simulations.
"""

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FACT = "HISTORICAL_FACT"


def evidence(
    key: str,
    source: str,
    content: str,
    repo: str = "backend",
    day: int = 1,
    note: str = "Authored synthetic evidence; not an actual historical project artifact.",
) -> dict[str, Any]:
    return {
        "id": key,
        "repository_key": repo,
        "source_type": source,
        "locator": f"synthetic/{repo}/{key}",
        "content": content,
        "occurred_at": f"2026-01-{day:02d}T10:00:00Z",
        "synthetic": True,
        "provenance_note": note,
    }


def claim(
    question: str,
    text: str | None,
    ids: list[str],
    status: str = "CONFIRMED",
    kind: str = FACT,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": f"claim-{question}",
        "question_id": question,
        "text": text,
        "evidence_status": status,
        "record_kind": kind,
        "evidence_ids": ids,
        "unknown_reason": reason,
    }


def execution(program: str) -> str:
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)
    return json.dumps(
        {
            "command": ["python", "-c", program],
            "python_version": sys.version.split()[0],
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        },
        ensure_ascii=False,
    )


def case(
    number: int,
    title: str,
    purpose: str,
    repositories: list[str],
    questions: dict[str, str],
    evidences: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    events: list[dict[str, Any]],
    links: list[dict[str, str]],
    forbidden: list[str],
) -> dict[str, Any]:
    draft = {"title": title, "claims": claims, "links": links}
    return {
        "id": f"pilot-{number:02d}",
        "project_key": f"synthetic-project-{number}",
        "title": title,
        "purpose": purpose,
        "repositories": repositories,
        "questions": [{"id": key, "label": value} for key, value in questions.items()],
        "evidence": evidences,
        "events": events,
        "golden_candidate": {
            "draft": copy.deepcopy(draft),
            "important_question_ids": [
                c["question_id"] for c in claims if c["evidence_status"] != "UNKNOWN"
            ],
            "forbidden_claims": forbidden,
            "annotation_notes": "AI-authored candidate. Human approval pending. "
            "Questions define a bounded pilot; this is not open-domain extraction coverage.",
        },
        "mock_initial": copy.deepcopy(draft),
        "mock_corrected": copy.deepcopy(draft),
    }


def build() -> list[dict[str, Any]]:
    cases = []
    cases.append(
        case(
            1,
            "태그 중복 제거",
            "의도·코드·실행 결과의 근거를 구분합니다.",
            ["backend"],
            {
                "intent": "변경 목적은 무엇인가?",
                "implementation": "무엇이 변경되었나?",
                "verification": "어떤 검증이 실행되었나?",
                "alternative": "검토한 대안이 있는가?",
            },
            [
                evidence(
                    "request",
                    "CODEX_USER",
                    "사용자: 같은 태그가 중복 저장되지 않게 해줘. 입력 순서는 유지해줘.",
                ),
                evidence("diff", "GIT_DIFF", "-return tags\n+return list(dict.fromkeys(tags))"),
                evidence(
                    "test",
                    "TEST",
                    execution(
                        "tags=['ai','python','ai']; result=list(dict.fromkeys(tags)); "
                        "assert result==['ai','python']; "
                        "print('deduplicate and preserve order: PASS')"
                    ),
                    note="Actual Python execution on synthetic input. No performance benchmark.",
                ),
            ],
            [
                claim(
                    "intent",
                    "태그 중복을 제거하면서 입력 순서를 유지하려고 요청했다.",
                    ["request"],
                    kind="HISTORICAL_DECISION",
                ),
                claim("implementation", "dict.fromkeys를 사용해 태그 중복을 제거했다.", ["diff"]),
                claim(
                    "verification",
                    "합성 입력의 중복 제거와 순서 유지 assertion이 통과했다.",
                    ["test"],
                ),
                claim(
                    "alternative",
                    None,
                    [],
                    "UNKNOWN",
                    reason="당시 검토한 대안은 제공된 기록에 없다.",
                ),
            ],
            [{"id": "change", "evidence_ids": ["request", "diff", "test"]}],
            [{"event_id": "change", "story_key": "dedup"}],
            ["성능 개선 수치", "set과 비교 실험했다는 주장"],
        )
    )
    cases[0]["mock_initial"]["claims"] = cases[0]["mock_initial"]["claims"][:2] + [
        cases[0]["mock_initial"]["claims"][3]
    ]  # Deliberate omission to exercise the missing-claim metric.
    cases.append(
        case(
            2,
            "타임아웃과 미확정 원인",
            "미확정 원인·미측정 성과·현재 권고를 구분합니다.",
            ["backend"],
            {
                "problem": "관측된 문제는?",
                "cause": "원인이 확인되었나?",
                "metric": "개선 수치가 측정되었나?",
                "recommendation": "현재 추가 검증 권고는?",
            },
            [
                evidence("log", "LOG", "request failed: database connection acquisition timeout"),
                evidence(
                    "agent",
                    "CODEX_ASSISTANT",
                    "풀 부족일 수 있다. 아직 동시 요청 수나 풀 지표는 측정하지 않았다.",
                ),
                evidence("config", "CONFIG", "pool_size: 5"),
            ],
            [
                claim("problem", "DB 연결 획득 타임아웃이 기록되었다.", ["log"]),
                claim(
                    "cause",
                    "Connection Pool 부족은 검증이 필요한 가설이다.",
                    ["agent", "config"],
                    "HYPOTHESIS",
                ),
                claim("metric", None, [], "UNKNOWN", reason="변경 전후 측정 결과가 없다."),
                claim(
                    "recommendation",
                    "동시 요청과 연결 풀 사용량을 측정해 가설을 검증할 수 있다.",
                    ["agent", "log"],
                    "INFERRED",
                    "CURRENT_AI_RECOMMENDATION",
                ),
            ],
            [{"id": "timeout", "evidence_ids": ["log", "agent", "config"]}],
            [{"event_id": "timeout", "story_key": "investigation"}],
            ["풀이 실제 원인으로 확정됨", "응답 속도가 특정 비율로 향상됨"],
        )
    )
    cases[1]["mock_initial"]["claims"][1].update(
        text="Connection Pool 부족이 실제 원인이다.",
        evidence_status="CONFIRMED",
    )  # Intentionally bad mock, not a measured model failure.
    cases.append(
        case(
            3,
            "빈 입력 평균 계산의 실패와 수정",
            "최초 실패·코드 변경·재검증을 구분합니다.",
            ["backend"],
            {
                "initial": "최초 실행 결과는?",
                "implementation": "무엇을 수정했나?",
                "final": "재검증 결과는?",
                "prediction": "문제를 사전에 예상했나?",
            },
            [
                evidence(
                    "first",
                    "TEST",
                    execution("values=[]; print(sum(values)/len(values))"),
                    note="Actual failing execution on synthetic code; intentional failure.",
                ),
                evidence(
                    "diff",
                    "GIT_DIFF",
                    "-return sum(values)/len(values)\n"
                    "+return sum(values)/len(values) if values else None",
                ),
                evidence(
                    "rerun",
                    "TEST",
                    execution(
                        "mean=lambda v:sum(v)/len(v) if v else None; "
                        "assert mean([]) is None; assert mean([2,4])==3; "
                        "print('2 assertions: PASS')"
                    ),
                    note="Actual successful synthetic execution. Does not prove all inputs.",
                ),
            ],
            [
                claim(
                    "initial", "빈 입력의 평균 계산 실행이 ZeroDivisionError로 실패했다.", ["first"]
                ),
                claim("implementation", "빈 입력이면 None을 반환하는 분기를 추가했다.", ["diff"]),
                claim(
                    "final", "빈 입력과 [2,4]에 대한 두 assertion이 재실행에서 통과했다.", ["rerun"]
                ),
                claim(
                    "prediction",
                    None,
                    [],
                    "UNKNOWN",
                    reason="사전 예측 여부를 보여주는 기록이 없다.",
                ),
            ],
            [
                {"id": "failure", "evidence_ids": ["first"]},
                {"id": "fix", "evidence_ids": ["diff", "rerun"]},
            ],
            [
                {"event_id": "failure", "story_key": "empty-input"},
                {"event_id": "fix", "story_key": "empty-input"},
            ],
            ["모든 입력의 정확성이 증명됨", "개발자가 사전에 실패를 예상함"],
        )
    )
    cases.append(
        case(
            4,
            "Android·Backend 검색과 별도 설정 변경",
            "긴 간격의 같은 Story와 같은 파일의 다른 Story를 구분합니다.",
            ["android", "backend"],
            {
                "backend": "서버의 변경은?",
                "android": "앱의 변경은?",
                "separate": "별개 작업은?",
                "integration": "통합 실행 결과는?",
            },
            [
                evidence(
                    "api",
                    "GIT_DIFF",
                    "검색 기능: GET /items에 q 필터 추가. Settings.py의 search_enabled=true.",
                    day=1,
                ),
                evidence(
                    "app",
                    "CODEX_USER",
                    "이전 검색 API의 q 파라미터에 Android 검색 입력을 연결해줘.",
                    "android",
                    8,
                ),
                evidence(
                    "app-diff",
                    "GIT_DIFF",
                    "client.get('/items', query={'q': searchText})",
                    "android",
                    8,
                ),
                evidence(
                    "logs",
                    "GIT_DIFF",
                    "별도 로깅 작업: Settings.py의 log_level을 INFO로 변경.",
                    day=8,
                ),
            ],
            [
                claim("backend", "Backend 검색 API에 q 필터를 추가했다.", ["api"]),
                claim("android", "Android 검색 입력을 q 파라미터에 연결했다.", ["app", "app-diff"]),
                claim(
                    "separate", "같은 설정 파일의 로그 수준 변경은 별도 로깅 작업이다.", ["logs"]
                ),
                claim(
                    "integration",
                    None,
                    [],
                    "UNKNOWN",
                    reason="양쪽을 함께 실행한 통합 테스트 결과는 없다.",
                ),
            ],
            [
                {"id": "backend-search", "evidence_ids": ["api"]},
                {"id": "android-search", "evidence_ids": ["app", "app-diff"]},
                {"id": "log-config", "evidence_ids": ["logs"]},
            ],
            [
                {"event_id": "backend-search", "story_key": "search"},
                {"event_id": "android-search", "story_key": "search"},
                {"event_id": "log-config", "story_key": "logging"},
            ],
            ["통합 테스트가 통과했다는 주장", "7일 동안 계속 작업했다는 주장"],
        )
    )
    cases[3]["mock_initial"]["links"] = [
        {"event_id": "backend-search", "story_key": "backend"},
        {"event_id": "android-search", "story_key": "android"},
        {"event_id": "log-config", "story_key": "backend"},
    ]
    return cases


if __name__ == "__main__":
    from project_log.models import Case

    destination = ROOT / "fixtures/pilot"
    destination.mkdir(parents=True, exist_ok=True)
    for item in build():
        validated = Case.model_validate(item)
        path = destination / f"{validated.id}.json"
        path.write_text(validated.model_dump_json(indent=2) + "\n")
        print(f"Wrote synthetic candidate: {path.relative_to(ROOT)} (NOT approved Golden)")
