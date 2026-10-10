# Project Log

Project Log는 코드와 개발 Evidence를 연결해, 근거를 확인할 수 있는 Engineering Memory를
만드는 프로젝트다. 흩어진 변경·판단·검증 맥락을 개발자가 매번 일지로 작성하는 부담을 줄이고,
자동 정리된 결과를 개발자가 검토하는 것을 목표로 한다.

현재 제품 기능은 **프로젝트 등록, 원천 개발 정보 수집·보존과 수집 기록별 자료 조회, 사용자 추가 자료 보존**이다.
사용자 본인 한 명의 로컬 Web App이며, 1 Project에 1 Local Git Repository를 등록한다.
Git History와 index·Working Tree·safe untracked, Root `docs/`의 안전한 로컬 개발 문서를 보존하고,
`지금 수집`으로 현재 상태를
새 Collection에 다시 관측한다. 진행 중 수집 취소, 완료된 수집 영구 삭제와 제목·설명 메모를 제공한다.
정상 완료되지 않은 수집은 부분 결과까지 정리하며 실패 이력·재시도·Resume는 제공하지 않는다.
AI 분석·경험 발견·복원·승인, RAG, Agent 대화 수집, Codex 실행, Remote 연동은 지원하지 않는다.

## 공개 구조와 실행

- `src/project_log/`: FastAPI, Git 수집, PostgreSQL 저장, Background worker, SQL migrations.
- `frontend/`: React/Vite 대시보드·전체 목록·자료 상세·프로젝트 설정, Vitest/Playwright.
- `tests/`: 실제 PostgreSQL·임시 Git Repository 기반 테스트.
- `scripts/`: 격리 검증 도구.
- [`poc/phase0/`](poc/phase0/README.md): 보존된 synthetic Mock Harness와 Review UI. 제품 실행과 별개다.
- [`AGENTS.md`](AGENTS.md): Repository 전체의 개발 실행 규칙.

내부 제품·Architecture 문서는 local-only `docs/`에 있다. 실행에 필수는 아니다.
Root `current_status.md`와 `chatgpt/`는 개인 Session handoff 자료이며 장기 개발 문서와 구분한다.
`chatgpt/`는 Codex 구현의 Source of Truth가 아니다.
현재 구현·요구사항은 Source와 canonical 문서를 확인한다.
내부 기록·원천 데이터·로컬 환경·Secret은 Public Git에 포함하지 않는다.

## 준비와 실행

Root에서 실행한다. Python 3.13(`.python-version`), Node 24(`.node-version`), uv와
`127.0.0.1:5432`에서 이미 실행 중인 PostgreSQL 서버가 필요하다.
정확한 의존성은 Root `uv.lock`, `frontend/package-lock.json`을 따른다.
PoC의 `.venv`를 제품에 사용하지 않는다. 설치 명령은 Package 다운로드가 필요할 수 있다.

```sh
export UV_CACHE_DIR="$PWD/.cache/uv"
uv sync --frozen
export PATH="/사용할/Node24/bin:$PATH"
npm ci --prefix frontend --ignore-scripts --no-audit --no-fund
# project_log가 없을 때만 생성한다. 기존 DB는 소유를 확인하기 전 변경하지 않는다.
createdb -h 127.0.0.1 -p 5432 -U "$USER" project_log
make db-migrate
make frontend-build
make serve
```

`http://127.0.0.1:8000`에서 사용한다. Node가 PATH에 있으면 `NODE_BIN`을 지정하지 않아도 된다.
기존 개발 환경의 기본 `NODE_BIN`은 PoC 아래 설치된 Node 경로를 재사용할 뿐 제품이 PoC 코드에
의존하는 것은 아니다. 다른 경로는 `make NODE_BIN=/절대/Node/bin …`으로 지정한다.
Frontend 개발 서버는 `make dev-ui`이며 별도 Backend `make serve`가 필요하다.

첫 화면은 등록된 프로젝트의 카드 목록이다. 브랜드를 클릭하면 활성 선택을 해제하고 이 목록으로 돌아온다.
목록에서 프로젝트를 새로 선택하면 현재 남은 최신 완료 수집을 표시한다. 같은 프로젝트 안에서 상세·설정 화면을 오갈 때는 선택한 수집을 유지한다.
브라우저 뒤로·앞으로 가기는 방문 당시 화면·수집 선택·목록 페이지·스크롤을 복원한다. 프로젝트 목록에서 카드를 새로 선택하는 동작과 구분한다.
카드의 ⋮ 메뉴에서 `수정`으로 설정을 열거나 `삭제`로 영구 삭제 확인을 진행한다. 프로젝트명을 재입력해야 삭제할 수 있다.
삭제는 해당 Project Log 등록·수집 기록/보존 근거·사용자 추가 자료 사본을 영구 제거하며 복구 기능은 없다.
실제 Git Repository 폴더/원본 파일·다른 프로젝트 자료·기존 백업은 삭제하지 않는다. 수집/취소/실패 정리 중에는 삭제가 차단된다.
사이드바에서 프로젝트를 검색·전환하거나 `+ 프로젝트 추가`로 등록한다. 프로젝트 경로는
macOS 네이티브 `찾아보기`로 Git Repository root 하나를 선택한다. 폴더 내용을 업로드하지 않는다.
선택창을 취소하면 기존 입력을 유지한다. OS 접근 권한과 기존 Git/자료 저장소 검증을 적용한다.
`등록 후 첫 수집`은 등록과 수집을 순서대로 수행하며 수집 실패로 등록된 프로젝트를 삭제하지 않는다.

대시보드에서 수집 기록을 선택하고 자료 통계·최근 커밋·개발 문서를 확인한다.
선택 기록의 `프로젝트 구조 보기`로 당시 확보한 파일·문서 구조와 상태별 본문을 조회한다.
왼쪽 목록에서 파일을 선택하면 오른쪽 패널이 변경되며 현재 파일시스템의 파일로 대체하지 않는다.
수집 기록·커밋·개발 문서·사용자 추가 자료의 `전체 보기`는 페이지당 10개를 표시한다.
커밋과 문서는 독립 상세 화면으로 이동하며 목록의 페이지·스크롤 위치를 유지해 돌아온다. 커밋 상세는 변경 파일과 Diff를 좌우로 표시하며 좁은 화면은 세로로 전환한다.

프로젝트 화면의 `사용자 추가 자료`에서 파일 선택 또는 drag & drop으로 여러 파일을 추가한다.
UTF-8 텍스트/Markdown/로그/JSON/CSV, PDF/DOCX, PNG/JPEG를 지원하고 폴더는 지원하지 않는다.
최근 추가 순으로 표시하며 `다운로드`로 보관 사본을 가져온다. 삭제는 확인 후 영구 삭제하며 복구 기능은 없다.
같은 Project의 내용 중복·파일명 충돌을 거절한다. 충돌하면 PC 원본 이름을 바꿔 다시 추가한다.
여러 파일 중 첫 실패에서 중단하고 앞선 성공은 유지한다. 실패 파일과 이후 미처리 파일은 `업로드되지 않음`에 표시한다.
이 자료는 Project 소유이며 Collection 선택·수집량·증감과 독립이다. 내용 추출/OCR/AI 분석은 하지 않는다.

원본 bytes의 사본은 기본 `$XDG_DATA_HOME/project-log`(미설정 시 `~/.local/share/project-log`)에 저장한다.
`PROJECT_LOG_STORAGE_ROOT`로 관리 root를 변경할 수 있고 등록 Repository 내부 위치는 거절한다.
PC의 원본 파일은 수정/이동/삭제하지 않으며 원본이 없어져도 보관 사본은 유지한다.
기본 파일 한도 20 MiB는 `PROJECT_LOG_MATERIAL_MAX_BYTES`(bytes, 양의 정수)로 바꿀 수 있다.
기존 사본의 저장 위치를 바꿀 때는 관리 root의 데이터도 함께 옮겨야 한다. DB와 관리 root를 함께 백업한다.

기본 연결은 `127.0.0.1:5432 / project_log`, User는 현재 OS 사용자(현재 개발 환경은 `jjun`)다.
Project Log 전용 PostgreSQL 서버를 생성·시작·종료하지 않는다. 기존 서버의 다른 DB는 변경하지 않는다.
DB 존재·소유 확인 → migration/schema 적용 → 제품 실행 → Project 등록 순서로 사용한다.
다른 환경의 PostgreSQL endpoint는 `PROJECT_LOG_DATABASE_URL`로 지정할 수 있다.
비밀번호 등 Secret을 Source·문서·명령 기록에 넣지 않는다. 서버의 기존 인증 정책을 사용한다.
`make db-migrate`와 앱 시작 시 migration을 transaction으로 적용하며 이미 적용한 SQL 변경은
checksum으로 거절한다. 소유가 불명확한 기존 DB에는 migration이나 앱 실행으로 write하지 않는다.
제품은 단일 Backend 프로세스로 실행한다. 같은 DB의 중복 worker는 거절한다.
SQL migration을 개발·검증할 때 제품 Backend를 먼저 종료한다. `--reload` 실행 중 파일을
추가하면 startup migration이 사용자 DB에 자동 적용될 수 있다. `make serve`는 reload 없이 실행한다.

## 수집 내용과 한계

- 등록 입력은 경로·이름·프로젝트 상태·기준 local 브랜치다. 실제 local main/master 또는 Commit 없는 최초 브랜치를
  초기값으로 제안하며 직접 수정할 수 있다. 기준 브랜치는 등록 후에도 변경할 수 있다.
  모든 상태에서 Commit/소스 파일 최소조건 없이 빈 Git Repository도 지원한다. 지속/Live 수집은 없다.
- 초기/지금 수집 요청 전에 유효한 기준 브랜치와 현재 checkout이 같아야 한다. 다르면 수집 기록을 만들지 않고
  두 브랜치와 checkout 후 새 수집 안내를 표시한다. 앱이 Repository를 checkout하거나 Working Tree를 변경하지 않는다.
  기존 Project의 기준 값은 자동으로 채우지 않으므로 미설정이면 프로젝트 설정에서 유효한 local 브랜치를 저장한다.
  과거 Collection의 branch/HEAD(Commit 없음 포함)는 기준 설정 변경 후에도 유지하며 legacy 미확보 값을 추정하지 않는다.
- 각 초기/manual Collection 당시 refs와 HEAD에 도달 가능한 전체 Commit, 부모별 파일 변경(merge 포함),
  작성자·시각·message, text diff, tree/blob locator와 HEAD 파일 Metadata를 확보한다.
  reflog-only/unreachable History는 포함하지 않는다. shallow/missing object로 정상 완료하지 못하면 해당 시도를 정리한다.
- clean 상태를 포함한 전체 index·tracked Working Tree 내용을 계층별로 보존한다. 단일 atomic snapshot은 아니며
  관측 시간과 감지한 변경·누락을 남긴다. 본문 예산은 파일당 1 MiB, 전체 16 MiB다.
  5초 경과 후 새 본문 읽기를 제한하지만 전체 등록 요청의 5초 완료를 보장하지 않는다.
  status와 clean index 경로를 합친 관측 대상 20,000항목, Git 조회 timeout/output 제한 초과도 실패 처리한다.
- non-ignored untracked regular text는 확장자 whitelist 없이 안전 검사를 거쳐 body와
  raw-byte SHA-256을 보존한다. 일반 Source/Test/SQL/문서/config가 후보이며 config라는 이유로
  제외하거나 안전하다고 가정하지 않는다. sensitive path·Secret 의심·Binary·비 UTF-8·대용량은
  body/hash 없이 Metadata와 명시적 reason을 남긴다. 부분 마스킹한 본문을 원본으로 저장하지 않는다.
  일반 ignored는 Metadata-only이며 directory 내부를 재귀 수집하지 않는다. symlink를 따라가지 않고
  submodule 내부는 수집하지 않는다. 정책상 제외와 실제 read/collection 오류를 별도로 표시한다.
- Repository Root의 `docs/`는 장기 로컬 개발 문서 영역이다. Git ignored 상태여도 하위 디렉터리를
  재귀 관측하며 filename/확장자 allowlist를 적용하지 않는다. 문서에도 기존 안전 검사와 본문·snapshot
  한도를 적용한다. tracked 문서는 기존 index/Working 관측을 유지하고 중복 문서 관측을 만들지 않는다.
  그 외 로컬 문서는 별도 document 계층에서 body/raw-byte hash, Collection별 시각·출처를 보존한다.
  같은 내용을 다시 수집하면 content를 재사용하고 수정/신규/삭제도 비교한다.
  `docs/`는 local-only / Git 제외로 운용하며 직접적인 Secret/Credential/민감정보를 기록하지 않는다.
  single/multi-module 모두 Repository Root의 `docs/` 하나를 사용한다. module 내부 `docs/`는
  특별 재귀 수집하지 않지만 tracked/safe untracked 등 일반 Repository 수집은 그대로 적용한다.
  앱은 target Repository의 `.gitignore`를 자동 수정하지 않는다.
- 각 Collection은 HEAD/branch/refs/reachable History/HEAD tree/index/Working Tree/untracked를
  전체 재관측한다. 직전 HEAD와의 delta만 읽지 않는다. 동일 raw-byte body는 Project 내 content
  reference로 공유하고 immutable Git metadata도 재사용한다. unchanged라도 새 Collection의
  observation·시각·content reference는 남기며 사후 SQL DELETE cleanup을 사용하지 않는다.
  committed/staged identity는 Git blob/index OID, Working/untracked는 raw-byte SHA-256이다.
- 문서 후보는 이름·경로 기반 힌트다. 분류에 관계없이 Git 파일 사실을 보존한다.
  현재 중요도를 판단하거나 AI로 요약하지 않는다. Secret 검사는 완전한 탐지가 아니다.
- 과거 Source/문서는 저장한 Commit/tree/blob으로 **원본 Git에서 재조회**한다. 전체 Git backup은
  아니다. Repository 삭제·이동·object 소실 후에는 전체 Source를 복원하지 못할 수 있다.
- 수집 취소는 확인창에서 확정한 뒤 철회할 수 없다. 확정 상태를 먼저 DB에 commit하고 추가 저장을 막은 뒤
  이번 Collection과 전용 데이터를 정리한다. 오류·앱 비정상 종료의 미완료 수집도 정리하며 서버 재시작 후 Resume하지 않는다.
  정리 실패 시 내부 정리 상태를 유지하고 정리만 자동 반복한다. 정리가 끝날 때까지 같은 Project의 새 수집을 차단한다.
- 완료된 수집은 확인 후 영구 삭제한다. 유일한 수집도 삭제할 수 있고 Project/기준 브랜치/사용자 추가 자료/원본 Repository는 유지한다.
  취소·일반 실패 정리·완료 수집 삭제는 각각 하나의 PostgreSQL transaction으로 전용 observation과 참조되지 않는 공유 데이터를 제거한다.
  다른 Collection의 공유 자료는 유지하며 중간 실패는 전체 rollback한다. 휴지통·Undo는 없다.
- 지금 수집/등록 후 첫 수집의 실제 완료를 확인하면 제목·설명 선택 입력창을 표시한다. 완료 기록에서는 편집으로 함께 수정하고 확인 후 저장한다. 각각 최대 50자·200자다.
  같은 제목을 허용하며 사용자 메모이고 AI Evidence가 아니다. 취소·오류 시 입력을 유지한다.
  글자 수를 표시하고 초과 입력을 차단한다. 입력창을 닫거나 건너뛰어도 완료 기록은 유지된다.
  취소·실패·정리 중인 기록의 메모 저장은 거절한다. 선택 기록 삭제 후 최신 완료 기록을 선택하고 기록이 없으면 empty state를 보여준다.
  표시 번호는 완료 기록을 생성 시각·UUID 순으로 오래된 것부터 1부터 계산하며 삭제 후 다시 계산한다. 실제 식별은 UUID를 유지한다.
- 수집 기록을 선택해 `버전 관리 기록`의 Commit→부모별 변경 파일→보존 diff,
  `프로젝트 파일`과 `개발 문서`의 실제 경로 구조를 탐색한다. root docs/는 개발 문서에만 포함한다.
  수집 당시 프로젝트 구조는 개발 문서도 함께 표시한다. 실제 Working Tree/Index/HEAD/document/untracked 상태를 파일 안에서 구분한다.
  본문을 확보하지 않은 관측은 본문이 있는 다른 상태로 대신 표시하지 않는다.
  본문 미보존·제외 사유, 관측·본문 시각과 출처를 제공하며 과거 Working/문서를 현재 파일로 대체하지 않는다.
  HEAD 내용은 기존 immutable Git object 조회이므로 지금 읽은 시각과 원본 object 필요 여부를 구분한다.
  세 통계는 현재 남아 있는 같은 프로젝트의 직전 완료 수집과 비교한다. 중간 기록을 삭제하면 남은 직전 기록을 기준으로 다시 계산한다.
  이전 완료 기록이 없으면 `-`, 현재/이전의 확보 범위를 확정할 수 없으면 `미확보`/`비교 불가`로 구분한다. 당시 확보한 근거는 다시 쓰지 않는다.
  폴더 목록은 서버 제한을 넘는 항목도 나누어 조회해 모두 접근할 수 있게 하며 submodule Metadata는 내부 파일과 구분한다.
  `수집 근거·기술 정보`를 펼치면 경로/관측/본문 관측/미확보 사유/출처/복구 여부 6개 설명과 값을 확인한다. 내부 `원본 JSON 보기`에서 전체 원본 값을 확인한다. 일반 해시는 짧게 표시하고 Hover/키보드로 전체 값을 확인한다.
- 수집 당시의 항목 비교는 직전 새 관측(initial/manual)을 baseline으로
  index·Working·untracked의 `(계층, 경로, stage)` 항목을 비교한다. 신규/사라짐은 관측 항목의
  등장/부재이며 Git rename 판정이나 고유 파일 수가 아니다. 제외된 body의 동일성은 UNKNOWN이다.
  첫 관측이나 미완성 snapshot에는 비교 수치를 만들지 않는다. legacy Collection은 clean tracked
  관측이 없을 수 있으므로 정책 확대 후 신규 항목을 실제 파일 생성이라고 해석하지 않는다.
- 파일/diff의 1 MiB 본문 제외는 정책상 제한이다. 총 snapshot 예산·Git 실행 제한·object 조회
  오류·미완성 snapshot으로 정상 완료하지 못한 Collection은 전용 부분 자료까지 정리한다.
- Git 조회는 hook·fsmonitor·external diff·textconv·자동 fetch와 설정된 clean/process filter를
  차단한다. Filter를 사용하지 않은 status는 사용자 정의 변환을 적용한 일반 Git status와 다를 수 있다.
- 현재 중복 판정은 canonical common Git directory 기준이므로 linked worktree도 함께 거절한다.
  별도 worktree의 제품상 등록 단위는 미확정이며 이번 기능에서 정책을 확대하지 않는다.
- 프로젝트 설정에서 이름·상태·기본 브랜치·경로를 확인 후 함께 저장한다. 이동 또는 전체 복사한
  Repository를 다시 선택할 수 있으며 파일시스템·Backend 재시작 여부로 제한하지 않는다.
  최신 완료 Collection의 HEAD(없으면 등록 HEAD), 관측된 Git 정보와 local 브랜치 이력의 연속성,
  Git 정상성·중복·storage 경계를 검증한다. 충분한 근거가 없거나 수집·정리 중이면 경로 변경을 거절한다.
  일부 과거 객체만 없으면 조회 제한을 안내하고 명시적 확인 후 허용한다. 이전 Collection의 경로·본문·Diff와
  사용자 추가 자료는 유지한다. Git 이력이 같은 복사본의 물리적 출처를 증명하는 기능은 아니다.
  Git fetch·checkout·설정 복구를 자동 수행하지 않는다.
- HTTP는 loopback에만 bind하며 외부 Origin/Host를 차단한다. 로그인·다중 사용자 기능은 없다.
  원천 DB/API는 private local data이며 외부 AI/Remote/제품 Codex 호출은 없다.

## 검증

```sh
make check             # Ruff/format/mypy + 실제 PostgreSQL Backend tests + build/Vitest
make test-e2e          # 격리 DB/Repository/Backend + Chromium UI 테스트
```

Backend/E2E는 동일한 `127.0.0.1:5432` 서버에 UUID 이름의 temporary verification database를
생성하고 검증 후 삭제한다. 현재 OS PostgreSQL role에 CREATE DATABASE 권한이 필요하다.
제품 URL override와 분리하므로 `project_log`나 다른 기존 DB에 검증 데이터를 쓰지 않는다.
별도 PostgreSQL 서버를 띄우거나 기존 서버를 중지하지 않는다. 합성 Git fixture와 자료 storage도 임시 경로에 만든다.
운영 Backend가 실행 중일 때 build 검증은 `PROJECT_LOG_FRONTEND_DIST`에 절대 임시 경로를 지정해
실제 제공 중인 `frontend/dist`를 변경하지 않을 수 있다. 같은 값을 `make check`와 `make test-e2e`에 전달하고
E2E 포트는 `PROJECT_LOG_E2E_PORT=5173` 등 운영 Backend와 다른 포트를 사용한다.
TCP·Backend·Browser 실행 권한이 필요한 환경에서는 명령 승인이 필요할 수 있다. 외부 AI는 사용하지 않는다.
Chromium이 없다면 Node PATH를 설정하고 아래 명령으로 설치한다. 설치·실행 cache 경로를 맞춘다.

```sh
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.cache/playwright"
cd frontend
node node_modules/playwright/cli.js install chromium
```

실제 Project Log Repository 검증 도구는 제품 API/worker를 새 검증 DB에 실행하고 전체 reachable
Commit·변경·보존 diff·HEAD tree·미커밋 본문을 Git/파일과 대조한다. 문서·Source·Config의 대표
blob을 API로 재조회하고 Git/index/refs와 non-ignored 파일의 전후 hash·mode를 비교한다.
--base-branch에는 실제 대상 Repository의 기준 local 브랜치를 지정하며 해당 branch가 checkout되어 있어야 한다.
동일한 5432 서버의 temporary verification database는 검증 후 삭제하며 최종 제품 DB를 오염시키지 않는다.
출력 report에는 로컬 자료가 있으므로 공개하지 않는다. 검증 중 대상 파일을 편집하지 않는다.

```sh
.venv/bin/python scripts/verify_repository.py "$PWD" --base-branch main \
  --report .local/verification/self-collection.json
```

PoC의 과거 성공은 제품 검증 결과를 대신하지 않으며, 이 기능의 수집 성공도 AI 품질이나
개발 경험 복원 가능성의 검증 완료를 뜻하지 않는다.
