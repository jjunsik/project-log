# Project Log

Project Log는 코드와 개발 Evidence를 연결해, 근거를 확인할 수 있는 Engineering Memory를
만드는 프로젝트다. 흩어진 변경·판단·검증 맥락을 개발자가 매번 일지로 작성하는 부담을 줄이고,
자동 정리된 결과를 개발자가 검토하는 것을 목표로 한다.

## 공개 구조와 실행

- [`poc/phase0/`](poc/phase0/README.md): 보존된 synthetic Mock Harness와 Review UI. 제품 실행과 별개다.
- [`AGENTS.md`](AGENTS.md): Repository 전체의 개발 실행 규칙.

내부 제품·Architecture 문서는 local-only `docs/`에 있다. 실행에 필수는 아니다.
Root `current_status.md`와 `chatgpt/`는 개인 Session handoff 자료이며 장기 개발 문서와 구분한다.
`chatgpt/`는 Codex 구현의 Source of Truth가 아니다.
현재 구현·요구사항은 Source와 canonical 문서를 확인한다.
내부 기록·원천 데이터·로컬 환경·Secret은 Public Git에 포함하지 않는다.

`make db-migrate`와 앱 시작 시 migration을 transaction으로 적용하며 이미 적용한 SQL 변경은
checksum으로 거절한다.
