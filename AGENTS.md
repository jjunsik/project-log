# Repository 실행 규칙

## Context 활용과 문서

- 이 파일과 작업 영역의 nested `AGENTS.md`, 공개 README를 먼저 읽는다. 관련 Source /
  Test / Config와 Git 상태, 필요한 diff·history·untracked 파일, 로컬 환경만 선택적으로 확인한다.
- 현재 작업이 이전 상태·결정·맥락에 의존하면 local-only Root `current_status.md`가 있을 때
  필요한 범위에서 확인한다. 제품 목표·범위·제약 판단에는 `docs/project_spec.md`를 확인하고,
  제품 구현·수집 정책은 `docs/architecture.md`, PoC 작업은 `poc/phase0/` 내부 Context를 사용한다.
  Root `chatgpt/`는 사용자의 개인 handoff 영역이며 Product Source of Truth가 아니다.
  Decision / Change는 기록된 시점의 역사적 근거이며 현재 요구사항을 대신하지 않는다.
  최신 명시적 사용자 결정을 canonical 문서에 반영하고 과거를 소급 수정하지 않는다.
  상세 과거 맥락은 현재 Task와 직접 관련된
  Change / Decision / Evaluation / Architecture만 선택적으로 확인한다.
  이미 유효한 Context와 조회 결과를 활용하며 모든 과거 문서·Source·Test·Git History를
  관성적으로 다시 읽지 않는다.
  local 문서가 없는 clone도 정상이다. 필요한 과거 맥락이 없으면 명시하고 추측하지 않는다.
- 문서·Checkpoint와 Working Tree가 충돌하면 과거 의도를 추측하지 않고 불일치를 확인·보고한다.
  과거 관측과 현재 재검증을 구분한다.
  Conversation / Compact Summary를 장기 Source of Truth로 삼지 않는다.
- 설명은 한국어, 자연스러운 기술 용어는 영어로 쓴다. Fixture / Golden / Prompt / Run Data를
  문서 번역 대상으로 취급하지 않는다. 실제 내용이 필요할 때만 문서를 만들고 중복을 피한다.
- current_status는 현재 상태와 다음 작업을 위한 transient 문서다. 장기 사실은 성격에 맞는
  자료에 보존하며 현재 진행상태·일회성 작업 정보는 AGENTS에 누적하지 않는다.
  의존성·Runtime 버전의 기준은 Manifest / Lockfile / Runtime pin이다.
- 장기 개발 문서는 Repository 최상위 `docs/` 아래에 작성하고 local-only / Git 제외 영역으로
  운용한다. 직접적인 Secret/Credential/민감정보를 기록하지 않는다. `docs/`는 현재 위치
  Convention이다. 개인 Session handoff 문서는 대상이 아니다.
- README는 프로젝트의 목적·주요 기능·실행/사용 방법을 설명하는 대표 문서다.
  Current Status나 개발 일지로 사용하거나 매 Task 종료마다 의무 갱신하지 않는다.
  사용자-facing 프로젝트 설명·사용 방법 등 README가 설명할 사실이 바뀔 때 갱신한다.
  README는 개발 Evidence 후보이며 수집 대상에서 제외하지 않는다.

## 사실성과 경계

- Historical Intent, Alternative, Root Cause, 실행한 Test, Metric / Result를 만들어내지 않는다.
  현재 코드로 과거 이유를 역추론하지 않는다. 출처·시점·불확실성을 유지한다.
- VERIFIED는 CONFIRMED를 뜻하지 않는다. Review로 EvidenceStatus를 승격하지 않는다.
  Revision / Provenance / Project 격리를 보존하고 조용히 덮어쓰지 않는다.
- Golden Candidate 승인과 Golden의 의미 변경은 사용자 검토 대상이다. 자동 승인하지 않는다.
- 중요한 실패·중단·수정·평가 Evidence는 재시도나 Phase 종료 후에도 보존한다.
  임시 코드의 영구 유지는 요구하지 않으며, 의미 있는 교체·제거 이유는 실제 근거로 남긴다.
- Public Git은 프로젝트 코드와 이해·실행·사용에 필요한 공개 자료를 위한 공간이다.
  내부 docs, Session Context, 상세 실행 기록, private/raw Evidence는 local-only 또는 별도 저장한다.
  보존 필요성이 공개 권한을 뜻하지 않으며, ignore는 삭제가 아니다.
- Secret / Credential 실제 값은 Git·문서·로그·브라우저 번들에 넣지 않는다.
  외부 AI 사용에는 사용자 Credential과 현재 검토된 모델·전송 데이터·비용 정책이 필요하다.
  기본 검증은 외부 AI를 호출하지 않는다.
- Phase 0 자산은 `poc/phase0/`에서 관리한다. actual product는 별도로 시작하며 PoC 구조를
  제품 Architecture로 자동 승격하지 않는다. 제품 선택 상태는 AGENTS에 누적하지 않는다.

## 실행과 검토

- 제품은 Root `src/`, `frontend/`, `tests/`, SQL migration에서 개발한다. Root README의
  준비 절차와 `make check`, `make test-e2e`를 사용한다. PoC 명령·환경과 구분한다.
- 수집 대상 Repository에는 쓰지 않는다. Git hook / 외부 filter / external diff / textconv /
  자동 fetch를 실행하지 않는다. 본문 수집·제외는 canonical Architecture의 현재 안전 정책을
  따르며 정책 변경을 제품 요구사항으로 몰래 확정하지 않는다.
  원천 근거와 AI 해석을 분리하고 본문 제한 사유와 출처를 남긴다.
- 변경을 작게 유지하고 실제 실행 정의와 부작용을 확인한 뒤 관련 검증을 수행한다.
  일반적인 실패는 원인 확인·최소 수정·재검증한다. 결과와 한계를 사실대로 보고한다.
- Scope / Architecture / 주요 Dependency·Runtime / 평가 기준 변경, 비용·Credential,
  파괴적 작업, 원인이 불명확한 반복 실패는 사용자에게 Escalate한다.
- 불필요한 전역 환경 변경·Homebrew 전체 업데이트와 Agent / Framework / 추상화를 피한다.
- Commit은 사용자 승인 범위에서만 수행한다. 먼저 실제 staged diff와 공개 경계를 확인한다.
  메시지는 `Type: 요약` 형식으로 작성하며 scope는 사용하지 않는다.
  Type은 실제 변경 내용에 맞게 자연스럽게 선택하며 고정 목록이나 별도의 대소문자 규칙을 만들지 않는다.
  본문은 필수가 아니다. 제목 한 줄로 충분하면 제목만 작성하고, 추가 설명이 실제로 필요한 경우에만
  제목 다음에 빈 줄 한 줄을 두고 본문을 작성한다. 확인되지 않은 이유 / 성과 / 결과를 메시지에 만들지 않는다.

## Bootstrap Capture — 조건부 적용 중

- Git / Test로 복원하기 어려운 중요한 맥락을 사용자의 반복 지시 없이 논리적 작업 경계에서
  적절한 local-only canonical 자료에 누적한다. Routine 변경을 중복 기록하거나 병행 Log를
  미리 만들지 않는다. Bootstrap 기록도 출처와 한계를 가진 Evidence이며 자동 정답이 아니다.
- 큰 작업·Phase / Session 전환·Context 유실 위험 전에 누적 자료로 작은 Checkpoint를 한다.
  Context 부족으로 검증 생략·완료 간주·Evidence 삭제를 하지 않는다. 임의 Compact / Session
  종료·전환 없이 근거를 들어 사용자에게 제안한다.
- 실제 self-dogfooding과 Bootstrap 자료의 비교 검증 후 사용자 명시적 승인으로만 종료한다.
  중대한 Fabrication 0건과 Recall / Missing / Unsupported / 상태 충실도 / Review 부담을
  함께 판단한다. 나머지 종료 기준은 관측 근거로 제안하며 임의 Threshold를 만들지 않는다.
- 종료 승인과 근거를 보존한 뒤 신규 Capture를 중단하고 Bootstrap 전용 규칙을 제거·교체한다.
  기존 Evidence는 남긴다. 운영 정책의 의미 있는 변경과 이유도 추적 가능하게 기록한다.
