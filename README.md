# Project Log

Project Log는 코드와 개발 Evidence를 연결해, 근거를 확인할 수 있는 Engineering Memory를
만드는 프로젝트다. 흩어진 변경·판단·검증 맥락을 개발자가 매번 일지로 작성하는 부담을 줄이고,
자동 정리된 결과를 개발자가 검토하는 것을 목표로 한다.

Phase 0 PoC 사용자 검토는 현재 지점에서 마감했으며, 다음 단계는 **실제 제품 소프트웨어 설계**다.
설계와 소스 코드 구현은 아직 시작하지 않았다. 이번 마감은 실제 AI 품질이나 제품의 핵심
가능성 검증 완료를 뜻하지 않는다. 공개된 구현은 합성 사례와 Mock을 사용하는 Pilot Harness 및
Review UI이며, PoC 구조가 실제 제품 Architecture의 확정안은 아니다.

## 공개 구조와 실행

- [`poc/phase0/`](poc/phase0/README.md): PoC 목적, 범위, 설치·실행 방법과 한계.
- [`AGENTS.md`](AGENTS.md): Repository 전체의 개발 실행 규칙.

현재 실행 가능한 코드는 PoC 안에 있다. Backend / Frontend / Test / 합성 Fixture /
Manifest와 Lockfile을 함께 공개하며, 내부 개발 문서·실행 기록·로컬 환경·사용자 데이터는
Public Git에 포함하지 않는다. local-only 자료가 없어도 공개 README와 코드로 PoC를 시작할 수 있다.
