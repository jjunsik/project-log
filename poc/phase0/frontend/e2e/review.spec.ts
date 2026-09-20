import { test as base, expect } from '@playwright/test';
import { spawn } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const test = base.extend<{ isolatedBackend: void }>({
  isolatedBackend: [async ({}, use, testInfo) => {
    // Each test owns its Backend and synthetic records; retain records for failure diagnosis.
    const records = mkdtempSync(join(tmpdir(), 'project-log-browser-'));
    testInfo.annotations.push({type: 'testDataDir', description: records});
    const root = resolve(import.meta.dirname, '../..');
    const server = spawn(join(root, '.venv/bin/python'), ['-m', 'project_log'], {
      cwd: root,
      env: {...process.env, PROJECT_LOG_DATA_DIR: records, GEMINI_API_KEY: ''},
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    const closed = new Promise<void>(done => server.once('close', () => done()));
    let output = '';
    try {
      await new Promise<void>((ready, reject) => {
        const timeout = setTimeout(() => reject(new Error(`Backend startup timed out\n${output}`)), 10_000);
        const collect = (chunk: Buffer) => {
          output += chunk.toString();
          // Wait for this child to bind successfully; never reuse an existing server on this port.
          if (output.includes('Uvicorn running on http://127.0.0.1:8000')) {
            clearTimeout(timeout);
            ready();
          }
        };
        server.stdout.on('data', collect);
        server.stderr.on('data', collect);
        server.once('error', error => { clearTimeout(timeout); reject(error); });
        server.once('exit', code => {
          clearTimeout(timeout);
          reject(new Error(`Backend exited (${code})\n${output}`));
        });
      });
      await use();
    } finally {
      if (server.pid && server.exitCode === null && server.signalCode === null) server.kill('SIGTERM');
      const forceStop = setTimeout(() => server.kill('SIGKILL'), 5_000);
      try { await closed; } finally { clearTimeout(forceStop); }
      await testInfo.attach('backend-log', {body: output, contentType: 'text/plain'});
    }
  }, {auto: true}],
});

test.beforeEach(async ({context, request}) => {
  const readiness = await (await request.get('/api/readiness')).json();
  expect(readiness.credential_present).toBe(false);
  await context.route('**/*', route => {
    if (new URL(route.request().url()).origin !== 'http://127.0.0.1:8000') return route.abort();
    return route.continue();
  });
});

test('review, correction, approval and persistence retain hypothesis status', async ({page, request}, testInfo) => {
  await page.goto('/');
  await expect(page.getByLabel('검토할 사례').locator('option')).toHaveCount(4);
  await page.getByLabel('검토할 사례').selectOption('pilot-02');
  await expect(page.getByRole('heading', {name: '타임아웃과 미확정 원인'})).toBeVisible();
  await expect(page.locator('.evidence-card')).toHaveCount(3);
  await page.getByRole('button', {name: '초안 만들기'}).click();
  await expect(page.getByText('Connection Pool 부족이 실제 원인이다.', {exact: true})).toBeVisible();
  await page.getByRole('button', {name: '검토 시간 시작'}).click();
  await page.getByLabel('수정 요청', {exact: true}).fill('원인이 아직 확정되지 않았습니다. 가설로 표현해주세요.');
  await page.getByRole('button', {name: '수정 요청 · 새 Revision'}).click();
  await expect(page.getByText('Connection Pool 부족은 검증이 필요한 가설이다.', {exact: true})).toBeVisible();
  await page.route('**/api/cases/pilot-02/reviews', route => route.fulfill({
    status: 500, json: {detail: 'simulated review storage failure'},
  }));
  await page.getByRole('button', {name: '이 Revision 승인'}).click();
  await expect(page.getByRole('alert')).toContainText('simulated review storage failure');
  await expect(page.getByRole('status')).toHaveCount(0);
  const failedReview = await (await request.get('/api/cases/pilot-02')).json();
  expect(failedReview.reviews).toHaveLength(0);
  expect(failedReview.approvals).toHaveLength(0);
  await page.unroute('**/api/cases/pilot-02/reviews');
  await page.getByLabel('수정 요청', {exact: true}).fill('추가 근거 확인까지 보류');
  await page.getByRole('button', {name: '보류', exact: true}).click();
  await expect(page.getByRole('status')).toContainText('검토 기록을 저장했습니다.');
  const held = await (await request.get('/api/cases/pilot-02')).json();
  expect(held.reviews.at(-1).action).toBe('HOLD');
  expect(held.reviews.at(-1).review_status).toBe('REVIEW_REQUIRED');
  await page.getByRole('button', {name: '이 Revision 승인'}).click();
  await expect(page.getByText('VERIFIED', {exact: true})).toBeVisible();
  await page.getByRole('button', {name: '검토 일시정지'}).click();
  await expect.poll(async () => (await (await request.get('/api/cases/pilot-02')).json()).timing.active_ms).toBeGreaterThan(0);
  const reviewed = await (await request.get('/api/cases/pilot-02')).json();
  expect(reviewed.timing.wait_ms).toBeGreaterThan(0);
  expect(reviewed.approvals).toHaveLength(0);
  await page.reload();
  await page.getByLabel('검토할 사례').selectOption('pilot-02');
  await expect(page.getByText('VERIFIED', {exact: true})).toBeVisible();
  await expect(page.locator('.status-hypothesis')).toHaveText('HYPOTHESIS');
  await expect(page.getByLabel('Revision').locator('option')).toHaveCount(2);
  await page.getByLabel('Revision').selectOption({index: 0});
  await expect(page.getByRole('button', {name: '이 Revision 승인'})).toBeDisabled();
  await expect(page.getByText('Connection Pool 부족이 실제 원인이다.', {exact: true})).toBeVisible();
  await page.getByLabel('Revision').selectOption({index: 1});
  await page.screenshot({path: testInfo.outputPath('pilot-review.png'), fullPage: true});
});

test('Golden approval is explicit and metrics show mock limitations', async ({page, request}, testInfo) => {
  await page.goto('/');
  await page.getByRole('button', {name: '초안 만들기'}).click();
  await page.getByRole('button', {name: '03 · 지표 판정'}).click();
  await expect(page.getByRole('button', {name: '판정 저장 · 지표 계산'})).toBeDisabled();
  await page.getByRole('button', {name: '02 · Golden 검증'}).click();
  await expect(page.getByRole('button', {name: '저장된 Golden 승인'})).toBeDisabled();
  await page.getByRole('button', {name: 'Golden Candidate 저장'}).click();
  await page.getByLabel('Evidence를 확인했고 이 정답 버전을 승인합니다.').check();
  await page.getByRole('button', {name: '저장된 Golden 승인'}).click();
  await expect(page.getByRole('status')).toContainText('사용자 승인을 저장');
  const goldenState = await (await request.get('/api/cases/pilot-01')).json();
  expect(goldenState.approvals).toHaveLength(1);
  expect(goldenState.reviews).toHaveLength(0);
  await page.screenshot({path: testInfo.outputPath('golden.png'), fullPage: true});
  await page.getByRole('button', {name: '03 · 지표 판정'}).click();
  for (const checkbox of await page.getByLabel('표현된 확실성 수준까지 근거가 뒷받침함').all()) await checkbox.check();
  for (const checkbox of await page.getByLabel('내용이 정답과 일치함').all()) await checkbox.check();
  await page.getByLabel('모든 출력 Claim을 직접 판정했습니다.').check();
  await page.getByRole('button', {name: '판정 저장 · 지표 계산'}).click();
  await expect(page.getByText(/Evaluation · 데모 — AI 품질 근거 아님/)).toBeVisible();
  await page.screenshot({path: testInfo.outputPath('evaluation.png'), fullPage: true});
});

for (const [waitingFor, caseId] of [['save response', 'pilot-03'], ['state refresh', 'pilot-04']] as const) {
  test(`Golden edits during ${waitingFor} stay unsaved until saved again`, async ({page, request}) => {
    await page.goto('/');
    await page.getByLabel('검토할 사례').selectOption(caseId);
    await page.getByRole('button', {name: '02 · Golden 검증'}).click();
    const note = page.getByRole('textbox', {name: '판정 메모', exact: true});
    const save = page.getByRole('button', {name: 'Golden Candidate 저장', exact: true});
    const approve = page.getByRole('button', {name: '저장된 Golden 승인', exact: true});
    const acknowledge = page.getByLabel('Evidence를 확인했고 이 정답 버전을 승인합니다.');
    const first = 'A: simulated saved annotation';
    const second = 'B: simulated edit while saving';
    await note.fill(first);

    let release!: () => void;
    const paused = new Promise<void>(resolve => { release = resolve; });
    let reached!: () => void;
    const intercepted = new Promise<void>(resolve => { reached = resolve; });
    const pattern = `**/api/cases/${caseId}${waitingFor === 'save response' ? '/goldens' : ''}`;
    await page.route(pattern, async route => {
      const response = await route.fetch();
      expect(response.ok()).toBe(true);
      reached();
      await paused;
      await route.fulfill({response});
    });
    await save.click();
    await intercepted;
    try {
      await expect(note).toBeEnabled();
      await note.fill(second);
      await expect(approve).toBeDisabled();
    } finally { release(); }
    await expect(save).toBeEnabled();
    await expect(note).toHaveValue(second);
    const savedA = await (await request.get(`/api/cases/${caseId}`)).json();
    expect(savedA.goldens.at(-1).golden.annotation_notes).toBe(first);
    expect(savedA.approvals).toHaveLength(0);
    await acknowledge.check();
    await expect(approve).toBeDisabled();
    await expect(page.getByRole('status')).toContainText('추가 편집 내용은 아직 저장되지 않았습니다.');
    await page.unroute(pattern);

    await save.click();
    await expect(page.getByRole('status')).toHaveText('새 Candidate를 저장했습니다. 아직 승인되지 않았습니다.');
    await expect(note).toHaveValue(second);
    await expect(acknowledge).not.toBeChecked();
    await expect(approve).toBeDisabled();
    const savedB = await (await request.get(`/api/cases/${caseId}`)).json();
    expect(savedB.goldens.at(-1).golden.annotation_notes).toBe(second);
    expect(savedB.goldens.at(-1).id).not.toBe(savedA.goldens.at(-1).id);
    expect(savedB.approvals).toHaveLength(0);
    await acknowledge.check();
    await expect(approve).toBeEnabled();
    await approve.click();
    await expect(page.getByRole('status')).toContainText('사용자 승인을 저장');
    const approved = await (await request.get(`/api/cases/${caseId}`)).json();
    expect(approved.approvals).toHaveLength(1);
    expect(approved.approvals[0].candidate_id).toBe(savedB.goldens.at(-1).id);
    expect(approved.approvals[0].golden_hash).toBe(savedB.goldens.at(-1).golden_hash);
  });
}

test('unconfigured Gemini does not make a network call', async ({page, request}) => {
  await page.goto('/');
  await page.getByLabel('검토할 사례').selectOption('pilot-03');
  await page.getByLabel('응답 방식').selectOption('gemini');
  await page.getByRole('button', {name: '초안 만들기'}).click();
  await expect(page.getByRole('alert')).toContainText('Live AI is not configured');
  await expect(page.getByLabel('Revision')).toHaveCount(0);
  expect((await (await request.get('/api/cases/pilot-03')).json()).runs).toHaveLength(0);
});

test('case loading failure is visible and changing case allows recovery', async ({page}) => {
  await page.route('**/api/cases/pilot-04', route => route.fulfill({
    status: 500, json: {detail: 'simulated case load failure'},
  }));
  await page.goto('/');
  await page.getByLabel('검토할 사례').selectOption('pilot-04');
  await expect(page.getByRole('alert')).toContainText('simulated case load failure');
  await expect(page.getByText('검토 자료를 불러오는 중…')).toHaveCount(0);
  await page.unroute('**/api/cases/pilot-04');
  await page.getByLabel('검토할 사례').selectOption('pilot-03');
  await page.getByLabel('검토할 사례').selectOption('pilot-04');
  await expect(page.getByRole('heading', {name: 'Android·Backend 검색과 별도 설정 변경'})).toBeVisible();
  await expect(page.getByRole('alert')).toHaveCount(0);
});

test('timing acknowledgement failure stays visible and retries without duplicate records', async ({page, request}) => {
  const initial = await (await request.get('/api/cases/pilot-04/export')).json();
  expect(initial.approvals).toHaveLength(0);
  expect(initial.reviews).toHaveLength(0);
  expect(initial.timing_samples).toHaveLength(0);
  let failAcknowledgement = true;
  await page.route('**/api/cases/pilot-04/timing', async route => {
    if (!failAcknowledgement) return route.continue();
    const response = await route.fetch(); // Stored successfully, but its acknowledgement is lost.
    expect(response.ok()).toBe(true);
    await route.fulfill({status: 500, json: {detail: 'simulated lost timing acknowledgement'}});
  });
  await page.goto('/');
  await page.clock.install();
  await page.getByLabel('검토할 사례').selectOption('pilot-04');
  await expect(page.getByRole('heading', {name: 'Android·Backend 검색과 별도 설정 변경'})).toBeVisible();
  await expect(page.getByRole('alert')).toContainText('시간 저장 실패');
  await page.getByRole('button', {name: '검토 시간 시작'}).click();
  await page.clock.runFor(5000);
  await expect(page.getByRole('alert')).toContainText('시간 저장 실패');
  failAcknowledgement = false;
  await page.clock.runFor(5000);
  await expect(page.getByRole('alert')).toHaveCount(0);
  await expect.poll(async () => (await (await request.get('/api/cases/pilot-04')).json()).timing.active_ms).toBeGreaterThanOrEqual(5000);
  await page.getByRole('button', {name: '02 · Golden 검증'}).click();
  await page.clock.runFor(5000);
  await page.getByRole('button', {name: '01 · Claim 검토'}).click();
  await expect(page.getByRole('button', {name: '검토 시간 시작'})).toBeVisible();
  await expect.poll(async () => (await (await request.get('/api/cases/pilot-04')).json()).timing.paused_ms).toBeGreaterThanOrEqual(5000);
  const state = await (await request.get('/api/cases/pilot-04/export')).json();
  const ids = state.timing_samples.map((s: {session_id: string; sequence: number}) => `${s.session_id}:${s.sequence}`);
  expect(new Set(ids).size).toBe(ids.length);
  expect(state.approvals).toHaveLength(0);
  expect(state.reviews).toHaveLength(0);
});
