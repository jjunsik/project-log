# Phase 0 실행 규칙

Root 규칙에 다음 차이만 추가한다.

- 범위는 합성 사례 4개, 최소 Harness와 Review 흐름이다. 전체 Collector / 실제 Backfill /
  RAG / DB 구축 / Dashboard / 인증 / Profile / 복잡한 Agent 구현으로 확장하지 않는다.
- Fixture / Golden Candidate는 평가 기준이다. 일반 Build / UI 오류 때문에 생성기를 실행하거나
  덮어쓰지 않는다. 생성기 검증이 필요하면 격리 위치에서 비교하고 의미 변경은 먼저 검토받는다.
- Mock Correction은 사전 작성 응답 재생이다. Mock / E2E 성공을 실제 Gemini 품질,
  Groundedness, Human Review 부담·시간 절감, Product Gate 통과로 해석하지 않는다.
- 이 디렉터리에서 `make check test-e2e`와 관련 검증을 수행한다. 먼저 Manifest와 Makefile을
  확인한다. Python `.venv`, Node `.tools`, Cache는 이 PoC 기준이며 설치·실행의
  `PLAYWRIGHT_BROWSERS_PATH`를 일치시킨다. 로컬 자료가 없는 clone은 README를 따른다.
- 자동 Test / E2E는 격리된 임시 Storage를 쓰고 실제 `.local/pilot`과 승인 상태를 오염시키지
  않는다. 기존 서버를 재사용하지 않으며 자동 승인·시간 기록은 모의 사용자 행동으로 구분한다.
- Live Gemini, 실제 사용자 Golden 승인·Human Review, Gate 설정은 별도 단계다.
  필요한 사용자 검토·정책·권한이 확보되기 전 진행하지 않는다.
- 내부 상태가 있으면 `docs/current_status.md`부터 필요한 자료만 복원한다. 상세 결과는
  local-only Evaluation / Artifact에, 중요한 구현 변화는 Change에 두고 상태 문서와 복제하지 않는다.
