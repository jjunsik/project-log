# Phase 0 / Feasibility PoC

Project Log의 합성 사례 4개용 Pilot Harness와 Review UI다. Evidence / Claim / Revision,
수정·승인·보류, 별도 Golden 승인, 평가와 시간 기록의 기능적 동작을 검증한다.
현재 응답은 기본적으로 Mock 재생이며 전체 제품 구현이나 실제 AI 품질 평가 완료를 뜻하지 않는다.

## 구조

- `src/project_log/`: Python API, 파일 저장, Mock / Gemini Adapter, Evaluation 구현.
- `frontend/`: React / Vite UI, Vitest와 Playwright E2E.
- `tests/`: Backend 동작·평가 경계 테스트.
- `fixtures/pilot/`: 합성 Evidence, Golden Candidate, Mock 응답.
- `scripts/`: 합성 Fixture 생성기. 실행 준비나 검증을 위해 재생성하지 않는다.
- `Makefile`, Manifest / Lockfile / Runtime pin: 실행과 의존성의 기준.

## 환경 준비

모든 명령은 이 PoC 디렉터리에서 실행한다. Python은 `.python-version`, Node는
`.node-version`, 의존성은 `uv.lock`과 `frontend/package-lock.json`에 맞춘다.
Python, uv, 해당 OS용 Node/npm이 필요하다. 환경 준비는 Package / Browser 다운로드가
필요할 수 있으며, 기존 설치가 있는 환경에서는 아래 설치를 반복할 필요가 없다.

Makefile 기본 Node 위치는 `.tools/node-v24.21.0-darwin-arm64/bin`이다. 다른 OS나
별도 설치를 사용하면 해당 Node/npm의 절대 bin 경로를 `make NODE_BIN=/절대/경로/bin …`으로
전달한다. 전역 Runtime 변경은 필요하지 않다.

```sh
cd poc/phase0                         # Repository Root에서 시작할 때
export PATH="$PWD/.tools/node-v24.21.0-darwin-arm64/bin:$PATH"
export UV_CACHE_DIR="$PWD/.cache/uv"
export npm_config_cache="$PWD/.cache/npm"
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.cache/playwright"
uv sync --frozen
(cd frontend && npm ci)
(cd frontend && node node_modules/playwright/cli.js install chromium)
```

다른 Node 위치를 선택했다면 위 PATH도 동일하게 맞춘다. 설치와 E2E의 browser cache 경로를
일치시킨다. Makefile은 PoC 내부 npm / Playwright cache를 기본값으로 사용한다.
`.venv`는 절대 경로를 포함하므로 다른 위치에서 복사한 환경을 그대로 실행하지 않고 복원한다.

## 검증과 UI 실행

준비된 환경에서 다음 명령은 Package를 설치하거나 외부 AI를 호출하지 않는다.

```sh
make check             # Backend lint/format/type/tests + Frontend build/unit tests
make test-e2e          # build 후 격리 Backend와 Chromium E2E
make check test-e2e    # 종합 검증; 같은 호출에서 build를 공유
make serve            # 127.0.0.1:8000; 먼저 make frontend-build 필요
```

UI에서 사례와 Evidence를 확인하고 Mock 초안을 만든 뒤 Revision 검토를 진행한다.
Story Revision 승인과 Golden 승인은 별도다. `make dev-ui`는 Vite 개발 서버이며,
API를 위해 별도 `make serve`가 필요하다. 환경에 따라 로컬 서버·브라우저 실행 권한이 필요할 수 있다.

## 데이터와 한계

- 기본 사용자 기록은 Git 제외 `.local/pilot`에 저장된다. Review가 EvidenceStatus를
  승격시키지 않으며, Golden Candidate는 실제 사용자 검토 후 승인한다.
- 자동 테스트는 OS 임시 Storage를 사용한다. E2E는 기존 서버를 재사용하지 않고 API Key를
  비운다. 자동 승인·시간 측정은 실제 사용자 Pilot 결과가 아니다.
- E2E report / trace / screenshot은 local-only `frontend/test-results/`에 실행별로 저장된다.
  내부 `docs/`, `.local/`과 설치·cache·build 결과는 Public Git에 포함하지 않는다.
- Fixture 생성기는 평가 기준을 덮어쓸 수 있다. Fixture / Golden / Prompt를 번역하거나
  일반 Build / UI 오류 해결을 위해 재생성하지 않는다.
- Mock Correction은 사전 작성 응답 재생이다. 실제 Extraction / Correction / Groundedness,
  Human Review 부담·시간, Product Gate는 별도 검증 대상이다.
- Live Gemini에는 사용자 Key와 현재 검토된 Model / Data / Cost 정책이 필요하다.
  `GEMINI_API_KEY`의 실제 값은 Git에서 제외되는 `.env.local`에 설정한다. Secret은 Commit하지 않는다.
  정책 파일이 없는 기본 상태에서는 Live 실행이 거절된다.

개발 실행 규칙은 [AGENTS.md](AGENTS.md)를 따른다. 내부 문서는 있을 때만 선택적으로
활용하며, 공개 PoC의 기본 사용을 위해 local-only 자료가 필수인 것은 아니다.
