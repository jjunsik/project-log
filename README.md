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
개발 경험 화면은 저장된 AI 후보·주장·UNKNOWN·보존 근거를 조회한다.
후보 수정·승인, RAG, Agent 대화 수집, Codex 실행, Remote 연동은 지원하지 않는다.
분석 종료 수집 선택·자료 준비·실제 전송 자료 확인·실행별 명시적 동의/접수·진행·취소·결과 이력을 제공한다.
수집·Git 원본 조회·AI 입력과 출력에서 개인정보·민감정보·Secret 자동 탐지·마스킹은 제공하지 않는다.
사용자가 자료·제공 권한·Provider 조건을 직접 확인한다. 실제 전송은 운영자 명시 설정과 실행별 동의 전까지 비활성이다.

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

개발 경험 UI만 검토하려면 Node 24 환경에서 `cd frontend && npm run dev:experiences`를 실행하고
`http://127.0.0.1:4179/experience-preview.html`을 연다. Backend proxy 없이 합성 데이터만 사용하며
화면 사례에서 동의·진행·취소·실패 등을 선택할 수 있다. 실제 DB·Gemini와 연결하지 않고 production build에는
이 미리보기 entry/합성 자료를 포함하지 않는다. 동일한 격리 브라우저 검증은 `npm run test:experiences`로 수행한다.
검증 명령은 4179 포트가 비어 있어야 하며 DB를 사용하는 제품 전체 E2E와 별개다.

화면 오른쪽 위의 `테마`에서 `시스템`·`라이트`·`다크`를 선택한다. 기본값인 시스템은 OS/브라우저의
색상 설정과 변경을 따르며, 라이트·다크는 직접 선택한 테마를 유지한다. 선택은 같은 브라우저·Origin의
localStorage에 저장해 새로고침 후에도 유지한다. 테마 전환은 현재 화면·수집 선택·입력·방문 기록을 바꾸지 않는다.
브라우저가 저장을 차단해도 현재 페이지의 전환은 사용할 수 있다.

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
개발 문서는 대시보드와 전체 보기 모두 수집 당시 수정일 최신순으로 표시한다. 수정일이 없으면 마지막에 표시한다.
선택 기록의 `프로젝트 구조 보기`로 당시 확보한 파일·문서 구조와 상태별 본문을 조회한다.
왼쪽 목록에서 파일을 선택하면 오른쪽 패널이 변경되며 현재 파일시스템의 파일로 대체하지 않는다.
수집 기록·커밋·개발 문서·사용자 추가 자료의 `전체 보기`는 페이지당 10개를 표시한다.
커밋과 문서는 독립 상세 화면으로 이동하며 목록의 페이지·스크롤 위치를 유지해 돌아온다. 커밋 상세는 변경 파일과 Diff를 좌우로 표시하며 좁은 화면은 세로로 전환한다.

프로젝트 화면의 `사용자 추가 자료`에서 파일 선택 또는 drag & drop으로 여러 파일을 추가한다.
UTF-8 텍스트/Markdown/로그/JSON/CSV, PDF/DOCX, PNG/JPEG를 지원하고 폴더는 지원하지 않는다.
최근 추가 순으로 표시하며 `다운로드`로 보관 사본을 가져온다. 삭제는 확인 후 영구 삭제하며 복구 기능은 없다.
같은 Project의 내용 중복·파일명 충돌을 거절한다. 충돌하면 PC 원본 이름을 바꿔 다시 추가한다.
여러 파일 중 첫 실패에서 중단하고 앞선 성공은 유지한다. 실패 파일과 이후 미처리 파일은 `업로드되지 않음`에 표시한다.
이 자료는 Project 소유이며 Collection 선택·수집량·증감과 독립이다. 현재 화면·수집 기능에서는
내용 추출/OCR/AI 분석을 하지 않는다. 별도 Backend 입력 준비 함수의 범위는 아래 운영자 검증 절을 따른다.

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
- non-ignored untracked regular text는 확장자 whitelist 없이 형식·크기를 확인해 body와
  raw-byte SHA-256을 보존한다. 일반 Source/Test/SQL/문서/config가 후보이며 config라는 이유로
  제외하거나 안전하다고 가정하지 않는다. 본문·파일명으로 개인정보·민감정보·Secret을 자동 판별하지 않는다.
  Binary·비 UTF-8·대용량은 body/hash 없이 Metadata와 명시적 reason을 남긴다.
  수집 전에 자료와 제공 권한을 사용자가 직접 점검해야 한다.
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
  현재 중요도를 판단하거나 AI로 요약하지 않는다. Git 내부 `.git` metadata는 본문 수집 대상에서 제외한다.
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
  원천 DB/API는 private local data이며 실제 프로젝트 자료의 외부 AI/Remote/제품 Codex 호출은 없다.

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


## AI 개발 경험 분석·운영자 합성 검증

프로젝트별 `개발 경험` 화면에서 마지막 완료 Collection을 선택하고, 준비된 자료·제외 사유·전송 정책을
확인한 뒤 실행별로 동의해 분석을 요청한다. 접수된 입력으로 백그라운드 실행하며 상태·취소·이전 성공 결과와
보존 근거를 조회한다. 전체 프로젝트에서 활성 분석은 최대 1건이며 다른 요청은 대기열에 넣지 않는다.
분석 중 해당 프로젝트의 기존 자료·설정 변경은 제한하지만 신규 수집과 조회는 가능하다.

개발 경험 분석에 사용할 모델 설정은 `PROJECT_LOG_GEMINI_MODEL=gemini-3.5-flash-lite`다.
모델 자동 대체는 없으며 아래 `gemini-3.1-flash-lite` 예시는 별도의 고정 합성 연동 검증용이다.
설정·재시작·로컬 입력 준비와 실제 자료 전송/실행별 동의는 각각 승인 범위를 확인한 뒤 진행한다.

외부 전송 없이 자료 목록만 검토하려면 `PROJECT_LOG_AI_PREPARATION_ONLY=1`과 위 모델을 지정한다.
이 모드는 Key/Free Tier 확인 없이 준비하며 `PROJECT_LOG_AI_ENABLED=0`을 유지할 수 있다.
분석 접수·Worker 생성·호출 진입은 Backend에서 차단된다. 상태 API의 `available`은 준비 가능 여부,
`execution_available`은 실행 가능 여부다. 준비 시 metadata를 DB에 기록하고 본문 입력은 최대30분 메모리에만
유지한다. 이후 실행 설정으로 전환하면 다시 준비·동의해야 한다. 설정 반영/재시작·로컬 준비 승인과
실제 Key 사용·외부 전송 승인은 별개다.

실제 전송은 기본 비활성이다. 운영자가 `PROJECT_LOG_AI_ENABLED=1`, 검토된 `PROJECT_LOG_GEMINI_MODEL`,
`PROJECT_LOG_GEMINI_KEY_FILE` 경로와 `PROJECT_LOG_GEMINI_FREE_TIER_CONFIRMED=1`을 명시해야 한다.
서비스 Secret은 전용 `.project-log-secrets/` 폴더에 보관하고 실제 값을 코드에 작성하지 않는다.
폴더는 본인 소유·권한700, Key는 그 바로 아래 본인 소유 일반 파일·권한600이어야 한다. 폴더와 Key의 symlink는 거절한다.
`PROJECT_LOG_GEMINI_KEY_FILE`은 해당 파일 경로를 참조하며 준비 단계에서는 Key 본문을 읽지 않는다.
이 폴더명은 `.gitignore`에 등록돼 있고, 별도의 수집 경계로 Git History·index·Working Tree·untracked·Root docs/ 및
AI용 Git 원본 조회에서 제외된다. ignore를 우회해 tracked된 파일도 수집하지 않는다.
전용 폴더 내부를 Repository로 등록하는 것도 거절하며 symlink로 다른 이름을 지정해도 적용된다.
기존 다른 위치의 Key를 앱이 자동 이동하거나 기존 운영 설정을 변경하지 않는다.
이 설정은 Provider 접근 권한·Billing·비용 확인을 대신하지 않는다. 실행별 화면 동의도 필요하다.

`gemini-3.5-flash-lite` Free Tier의 선택적 호출 제어는 다음 두 설정으로 켠다. 기본은 꺼져 있다.
설정 반영·서비스 재시작과 실제 분석 실행은 각각 승인 범위를 확인한다.

```sh
export PROJECT_LOG_GEMINI_FREE_LIMITS=1
export PROJECT_LOG_GEMINI_FREE_LIMITS_STATE="$PWD/.local/ai/gemini-free-limits/usage.json"
```

현재 로컬 서비스는 위 두 설정을 유지한 정상 제품 API 운영 모드다(2026-10-20).
`AI_PREPARATION_ONLY=0`·`AI_ENABLED=1`·`GEMINI_FREE_TIER_CONFIRMED=1`을 적용했다(각각 `PROJECT_LOG_` prefix).
준비 전용 launcher의 Credential/외부 통신 차단은 적용하지 않으며, 실행별 동의·Digest·접수 검사는 유지한다.
운영 전환 검증에서 실제 Gemini 호출은 수행하지 않았다. 새 분석 준비와 실제 Key 사용·외부 전송은 각각 별도 승인 대상이다.

실제 SDK 전송 직전에 최근60초 RPM15·입력 TPM250,000을 확인하고 필요한 시간만 취소 가능하게 대기한다.
요청 UTF-8 bytes+1,024로 토큰을 보수적으로 예약하고 응답의 실제 입력 토큰 수로 보정한다.
정확한 tokenizer는 아니며 단일 추정이 TPM을 넘거나 비텍스트 토큰을 사전 산정할 수 없으면
자료를 빼지 않고 실행을 실패 처리한다. 기능을 끄면 기존 비텍스트 경로를 포함한 호출 동작을 사용한다.
RPD500 또는 명시적 일일429는 완료 Checkpoint를 보존하고 종료한다. 제공자 날짜가 갱신된 뒤
새 준비·Preview·명시적 동의로 수동 재개한다. 일일 갱신은 America/Los_Angeles 자정이며 DST를 반영한다.
명시적 분당429만 제공자 지연 안내(없으면60초)에 따라 최대60초 대기 후 한 번 재시도할 수 있다.
명시적 HTTP500·502·503·504 재시도와 합쳐 요청당 최대2attempt이며, 미상429·전송 불명 network는 자동 재전송하지 않는다.
관계 검토(`reconcile`)에서 구조 검증을 통과한 응답의 분류된 근거 연결 오류에만 조건부 재생성을 최대1회 허용한다.
원래 고정 자료에 비민감 검증 코드·개수·고정 규칙만 추가하며, 실패 응답 원문은 다시 보내지 않는다.
이 추가 전송의 범위·조건·한도를 새 Preview와 Digest로 확인하고 실행별로 동의해야 한다.
통신 재시도와 재생성은 같은2회 예산을 공유하며 동일 미완료 작업의 사용량은 날짜·새 실행·서버 재시작으로 초기화되지 않는다.
재생성 응답도 같은 엄격한 근거 검증을 통과해야 한다. 나머지 잘못된 응답과 한도 소진/불명 상태는 보존 후 중단한다.
서버 오류의 대기는30초이며 각 attempt를 별도 기록한다.401·403·404와 API Key 오류는 자동 반복하지 않고 설정·권한을 안내한다.
로컬 quota 대기도 유한하며 계속할 수 없으면 보존 후 중단한다. 자동 다음 날 재개는 없다.
다른 클라이언트와 공유되는 Google 한도를 로컬 제어만으로 보장하지 않는다.
하나의 Google 프로젝트·모델에는 재시작/Key 교체 후에도 같은 상태 경로를 사용한다.
이 파일과 `.lock`은 계수·시각·예약 ID만 저장하며 Key·분석 본문은 저장하지 않는다.
실행 중 삭제·초기화하지 않는다. 사용 이력이 있는 상태가 유실·손상되면 호출을 중단한다.
기능을 완전히 제거하려면 `gemini_free_tier.py`·`test_gemini_free_tier.py`,
`analysis_child.py`의 생성 연결과 `experience_gemini.py`의 선택적 quota 연결, 위 두 설정을 제거한다.
실행이 없는 상태에서 이 기능 전용 사용량 파일·lock·임시 파일만 정리할 수 있으며 사용자 DB/Migration 변경은 없다.

서버 재시작은 미완료 분석을 실패 처리하며 자동 재개하지 않는다. 새 Source 적용 전 DB Migration 순서와
기존 데이터 보존을 확인해야 한다. 기본 테스트는 임시 DB·Storage·합성 Repository/Mock 모델을 사용하며
실제 Gemini 분석 품질을 입증하지 않는다. 별도로 **고정 합성 자료만** 보내는 운영자 검증 명령이 있다.

완료·검증된 중간 결과가 있는 실패 실행은 실행 이력의 `이어서 분석 준비`에서 수동으로 이어갈 수 있다.
이전 실패 실행의 원래 고정 입력을 다시 확인하고 새로 동의하면 별도 실행을 생성한다. 완료된 중간 결과는
다시 검증해 재사용하고 남은 작업만 호출한다. 원천 자료·모델·실행 계약이 달라지면 재개를 거절하고 새 분석을
안내한다. 취소 실행과 중간 결과가 없는 과거 실패 실행은 재개하지 않는다. 현재 Source의 Free Tier 실패·429/선별한5xx 재시도를
포함한 **실행별** 실제 attempt 상한은512회다. 예상 전체 작업량이 이를 넘더라도 순차적으로 진행하며 완료 결과를
보존한다. 새 동의의 수동 재개 계열 누적은512회를 넘을 수 있고 기존 이력·quota를 임의로 초기화하지 않는다.
중간 결과 복사·검증·재사용은 API 호출이나 Free Tier 계수에서 차감하지 않는다. 같은 제공자 날짜에 완료 결과가
늘지 않은 실패가3회 연속되면 같은 지점 재개를 제한한다. 응답/근거/계약 검증 오류는 시간 경과로 해제하지 않으며
원인 해결·검증된 호환 전환이 필요하다. 동일 계약의 HTTP400·로컬 요청 Schema/크기 오류는 기다리는 것만으로 재개하지 않는다.
확인된 관계 계약 수정에는 검토된 구→신 한정 전환만 제공한다. 구2회 소진 이력은 보존하고 완료된 발견·후보 통합
결과를 엄격하게 재검증한다. 새 계약의 논리 요청당 공유2회와 전환 Digest를 새 Preview·실제 전송 자료에서 확인하고
실행별로 동의해야 한다. 같은 고정 자료·대상 계약의 접수 이력이 있으면 날짜·재시작·취소로 구 전환을 다시 주지 않는다.
호출 진입 뒤 종료되어 결과가 미확정이면 ledger에 UNKNOWN 코드로 남기며 제공자 처리나 환급을 추정하지 않는다.
재시도 정책만 바뀐 계약은 완료 응답·출처를 검증해 재사용할 수 있다. 위 예외 처리 정책은 현재 Source 기준이며 운영자는 서비스에 같은 Source가 반영됐는지 확인한다.
준비의30분 유효기간과 영속 Checkpoint의 수명은 별개다.
현재 Source는 모든 분석 단계에서 전송 전 로컬 요청 계약을 확인한다. 텍스트를 포함한 JSON framing은128KiB,
canonical 응답 Schema는32KiB·해결된 참조의 깊이12/방문 node1024·enum occurrence512/값32KiB로 제한한다.
이는 제품의 보수적 경계이며 Google의 미공개 Schema compiler 상한이나 제공자 수락을 보장하지 않는다.
Schema의 필수 ID·enum·숫자 범위는 유지하고 배열 상한/표시용 title만 제공자 문법에서 제외한다.
원래 Pydantic 배열 상한과 모든 Seed 배정·근거 검증은 수신 후 그대로 적용한다. JSON 다운로드에는 두 Schema와
요청 정책을 동의 Digest에 함께 묶는다. 관계 검토는 반복 출처를 손실 없는 열/값 표로 보내고 크기 초과 창을
나눌 때 원래 검색 창의 모든 Seed 쌍을 보존한다. 필수 한 쌍도 구성할 수 없으면 전송 전에 중단한다.
새 요청 계약으로 전환할 때는 정확한 논리 요청과 응답을 재검증한 발견·후보 통합 prefix만 재사용한다.
이전 관계 검토 이후 결과는 재사용하지 않는다. 알려진 이전 요청 계약의 HTTP400은 현재의 검증된 요청 계약으로
전환할 때 새 Preview·Digest·동의 아래 정상 재개를 준비할 수 있다. 같은 계약에서400이 재발하면 다시 차단한다.
이미 진단을 접수한 원래 실행은 그대로 두고 최신 진단 실패의 완료 prefix를 새 정상 실행이 이어받는다.
소진된 진단 기회는 다시 열리지 않으며 정상 재개는1회 진단과 달리 후속 분석까지 진행한다. 실제 제공자 성공은 보장하지 않는다.
특정 HTTP400의 원인 확인이 필요한 경우 운영자가 local-only `PROJECT_LOG_GEMINI_DIAGNOSTIC_TARGET_FILE`에
고정한 실패 실행만 `1회 진단 준비`를 사용할 수 있다. 일반적인400 재개 차단은 유지한다. 새 Preview에는 원래 자료와
과거 완료 응답에서 재구성한 **정확한 발신 요청·모델/API·wire Digest**가 포함된다. 이를 직접 확인하고 새로 동의한 뒤
`동의하고 1회 진단 요청`을 누른 경우에만 접수한다. 발신 payload가 실패 요청과 다르면 호출 전에 차단한다.
신규 제공자 호출은 최대1회이며 자동 재시도·Fallback·응답 후 후속 분석/결과 저장은 하지 않는다.
진단 접수 후 취소·서버 종료·전송 불명확·호출 전 중단이어도 같은 대상의 진단 기회는 다시 열리지 않는다.
HTTP200 진단도 정상 분석 성공이나 기존400의 원인 해결을 뜻하지 않는다. 이후 일반 분석은 원인 해결과 별도 결정이 필요하다.
중간 결과는 최종 성공 결과로 표시하지 않으며 실행 기록과 함께 보존하고 프로젝트 삭제 시 함께 제거한다.
별도 자동 만료·중간 결과만 삭제하는 기능은 없다. 재개할 때 실행별 사본을 보존하므로 저장량이 증가한다.

호출은 `langchain-google-genai`의 `ChatGoogleGenerativeAI`와 Pydantic 구조화 검증을 사용한다.
합성 검증은 단일 요청·자동 재시도 금지·SDK 파싱 전 64 KiB body 제한을 유지한다.
로컬 입력 준비는 모든 완료 Collection의 보존 본문과 읽을 수 있는 과거 Git 객체, 지원 텍스트 추가 자료,
native PDF/PNG/JPEG 및 DOCX text·내부 PNG/JPEG·로컬 변환 PDF를 다룬다. 미확보/보안 제외·형식/크기 제한과
PDF 입력은 원본 사본·분할 페이지와 출처를 연결한다. 이미지 원본은 자동 축소하지 않는다.
수집·Git 원본 조회·AI 입력과 출력의 개인정보·민감정보·Secret 자동 탐지, 마스킹·가명 처리,
민감성 추정에 따른 제외·전체 차단은 제공하지 않는다. `.env`·인증서 등 이름만으로도 민감성을 판정하지 않는다.
서비스 Secret 전용 폴더·Git 내부 저장소의 접근 제한과 수집 범위/ignore·형식·크기·symlink·오류 처리는 유지한다.
과거 제외 reason과 미확보 본문은 보존하며 새 정책으로 기존 Collection을 복원·변경하지 않는다.
전송 자료·Digest·실행별 명시 동의·접수·SDK 전송 경계는 유지한다. 변경 전 동의는 새 안내에 재사용하지 않으며,
Checkpoint는 입력·출처·계약의 기존 호환 검증을 통과해야 재사용할 수 있다.

등록·수집 전에 개인정보·Secret·기밀·제3자 자료와 제공 권한을 직접 점검한다. Git History·Diff·문서·미커밋 자료도
수집될 수 있다. 필요하면 파일을 직접 임시 수정하고 수집 완료 후 직접 복구할 수 있지만, 현재 파일 수정은 과거 Commit·Diff나
기존 Collection에 소급되지 않는다. 수집 당시 자료가 보존되며 앱은 원본 파일을 자동 수정·복구하지 않는다.

분석 준비 후 `실제 전송 자료 확인`에서 분할 텍스트·전송 출처·지시/응답 Schema와 PDF·이미지를 확인한다.
전체 전송 재료 JSON은 모든 분할 자료와 비텍스트 bytes(base64)를 포함하고, 각 비텍스트 자료는 별도로 다운로드할 수 있다.
페이지 일부와 전체를 구분하며 본문을 발췌·생략하지 않는다. 조회/다운로드만으로 확인 완료로 간주하지 않는다.
이 자료는 고정된 전송 재료다. 이후 AI 응답으로 만들어질 후속 호출 문맥은 실행 전에 전체를 보여줄 수 없다.
사용자가 전체 자료·제외/처리 한계·제공 권한과 Gemini 조건을 직접 확인한 뒤 이번 입력의 외부 전송에 명시적으로 동의한다.
사용자 확인이 서비스의 법적·기술적 책임을 모두 면제하지는 않는다. 이전 자동 보호 정책의 준비·동의·검토 기록은
보존하지만 현재 실행에 재사용하지 않으며 새 정책으로 다시 준비하고 동의해야 한다.

입력 Digest·범위 고정·실제 SDK 본문/비텍스트 bytes 무결성·호출 진입·취소/잠금·준비 전용 차단과 운영자 Credential 관리는 유지한다.
형식·크기·미확보·문서 변환 실패 등 처리 한계는 항목별로 표시한다. 원본 보존과 AI 분석 지원·완료는 별개다.
DOCX 시각 변환은 검증된 안전 실행 환경이 필요하며 전체 형식 지원 완료를 뜻하지 않는다.
변경 추적이 있는 DOCX는 전체 분석을 보류한다. 첨부·OLE·altChunk·활성/외부 연결 등은
시각 변환을 보류하며, 보안 검사를 통과한 텍스트·이미지만 부분 입력으로 사용할 수 있다.
macOS 변환은 별도 임시 프로필과 작업 내부 Unix IPC를 사용한다. 외부 네트워크·GUI 서비스는
차단하며 PDF 변환에 불필요한 native 맞춤법 서비스는 작업용 registry 사본에서 제외한다.

macOS Apple Silicon의 로컬 DOCX 변환 Runtime 설치 도구는
`.venv/bin/python scripts/install_libreoffice.py`다. 공식 배포 SHA-256과 서명을 확인해
pin된 LibreOffice를 프로젝트 `.local/runtimes/`에 복사한다. 최초 실행에는 다운로드와
일시적인 readonly DMG mount 권한이 필요하다. `/Applications` 설치·앱 등록은 하지 않는다.
Runtime 설치 성공은 DOCX 변환이나 실제 AI 분석 품질 검증 성공을 뜻하지 않는다.
비텍스트 원본의 개인정보·Secret도 자동 검사하지 않으므로 전송 전에 사용자가 직접 검토해야 한다.
기존 직접 HTTPS 동의/hash는 새 합성 payload와 별개다.

[Google AI Studio](https://aistudio.google.com/api-keys)에서 운영자 소유의 Billing 미연결 Free tier
Project와 사용 가능한 모델/한도를 확인한다. 무료 입력·응답은 Google 제품 개선 및 사람 검토 대상이며,
민감·기밀·개인정보를 보내면 안 된다([정책](https://ai.google.dev/gemini-api/terms)).
API Key만으로 무료 tier를 확인할 수 없으므로 Billing이 연결된 Project에는 아래 명령을 실행하지 않는다.
운영자 Key를 Git 제외 영역의 소유자 전용 파일에 안전하게 저장한다. 이용자에게 Key 입력을 요구하지 않는다.
Key 실제 값을 명령 인자·Source·로그·채팅에 넣지 않는다.
저장 위치는 Project Log Root의 `.project-log-secrets/gemini-api-key`이며 Key 원문만 저장하고 권한은 `600`이다.
전용 폴더는 권한 `700`으로 만들고 Git ignore·자료 수집 제외를 적용한다. 최초 발급·저장과 기존 Key 이동은 운영자가 직접 진행한다.

```sh
# 지정 파일에는 Key만 저장하고 chmod 600으로 소유자 접근만 허용한다.
export PROJECT_LOG_GEMINI_KEY_FILE="$PWD/.project-log-secrets/gemini-api-key"
# 가격표의 무료 모델 검토 범위 안에서 명시적으로 선택한다. 제품 모델 확정이 아니다.
export PROJECT_LOG_GEMINI_MODEL="gemini-3.1-flash-lite"
# 동의 전에는 안내만 출력하며 외부 호출이나 Key 조회를 하지 않는다.
.venv/bin/python -m scripts.verify_gemini
# 현재 고정 합성 입력 전송·무료 데이터 정책과 Free tier를 확인한 경우에만 실행한다.
.venv/bin/python -m scripts.verify_gemini --confirm-free-tier --confirm-synthetic-transfer
```

검토한 무료 Standard text 모델은 `gemini-2.5-flash-lite`, `gemini-2.5-flash`,
`gemini-3.1-flash-lite`, `gemini-3.5-flash-lite`다([가격](https://ai.google.dev/gemini-api/docs/pricing)).
계정별 사용 가능 여부와 RPM/TPM/RPD는 [AI Studio](https://ai.google.dev/gemini-api/docs/rate-limits)에서
직접 확인한다. 자동 재시도·유료 fallback·도구·파일 업로드는 사용하지 않는다.
검증 성공은 합성 API 연동·응답 처리 확인이며 제품 AI 경험 분석의 품질 검증이 아니다.
