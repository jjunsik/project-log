import {test as base, expect} from '@playwright/test';
import {execFileSync, spawn} from 'node:child_process';
import {existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, renameSync, unlinkSync, writeFileSync} from 'node:fs';
import {join, resolve} from 'node:path';

const test = base.extend<{backend: string; legacy: boolean; slow: boolean}>({
  legacy: [false, {option: true}],
  slow: [false, {option: true}],
  backend: [async ({legacy, slow}, use, testInfo) => {
    const data = mkdtempSync('/private/tmp/pl-browser-');
    const root = resolve(import.meta.dirname, '../..');
    const origin = new URL(testInfo.project.use.baseURL!);
    const child = spawn(join(root, '.venv/bin/python'), ['scripts/browser_server.py', data, '--port', origin.port, ...(legacy ? ['--legacy'] : []), ...(slow ? ['--slow'] : [])], {
      cwd: root, stdio: ['ignore', 'pipe', 'pipe'],
      env: {...process.env, GEMINI_API_KEY: '', OPENAI_API_KEY: ''},
    });
    const closed = new Promise<void>(done => child.once('close', () => done()));
    let output = '';
    try {
      await new Promise<void>((ready, reject) => {
        const timeout = setTimeout(() => reject(new Error(output || 'Backend startup timeout')), 20_000);
        const read = (data: Buffer) => {
          output += data.toString();
          if (output.includes(`Uvicorn running on ${origin.origin}`)) {clearTimeout(timeout); ready();}
        };
        child.stdout.on('data', read); child.stderr.on('data', read);
        child.once('error', e => {clearTimeout(timeout); reject(e);});
        child.once('exit', code => {clearTimeout(timeout); reject(new Error(`Backend exited ${code}\n${output}`));});
      });
      testInfo.annotations.push({type: 'isolatedData', description: data});
      await use(data);
    } finally {
      if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM');
      const force = setTimeout(() => child.kill('SIGKILL'), 10_000);
      try {await closed;} finally {clearTimeout(force);}
      await testInfo.attach('backend-log', {body: output, contentType: 'text/plain'});
      const database = output.match(/Temporary verification database: (project_log_verify_[a-f0-9]+)/)?.[1];
      if (database) expect(output).toContain(`Temporary verification database removed: ${database}`);
    }
  }, {auto: true}],
});

test.beforeEach(async ({context, baseURL}) => {
  await context.route('**/*', route => new URL(route.request().url()).origin === new URL(baseURL!).origin
    ? route.continue() : route.abort());
});



test('invalid path is rejected and incomplete collection disappears without retry', async ({page, request, backend}) => {
  await page.goto('/');
  await page.getByLabel('프로젝트 경로', {exact: true}).fill(join(backend, 'missing'));
  await page.getByRole('button', {name: '등록하고 수집 시작'}).click();
  await expect(page.getByRole('alert')).toContainText('접근 가능한');
  await page.getByLabel('프로젝트 경로', {exact: true}).fill(join(backend, 'partial-repo'));
  await expect(page.getByLabel('기준 브랜치', {exact: true})).toHaveValue('main');
  await page.getByRole('button', {name: '등록하고 수집 시작'}).click();
  await expect(page.getByRole('heading', {name: '수집 기록이 없습니다'})).toBeVisible();
  await expect(page.getByRole('button', {name: '수집 재시도'})).toHaveCount(0);
  const before = await (await request.get('/api/projects')).json();
  const detail = await (await request.get(`/api/projects/${before[0].id}`)).json();
  expect(detail.collections).toHaveLength(0);
  expect(detail.collection_busy).toBe(false);
  unlinkSync(join(backend, 'partial-repo/nested'));
  renameSync(join(backend, 'partial-repo/original-nested'), join(backend, 'partial-repo/nested'));
  await page.getByRole('button', {name: '지금 수집', exact: true}).click();
  await expect(page.locator('.collection-state')).toContainText('수집 완료');
  expect((await (await request.get(`/api/projects/${before[0].id}`)).json()).collections).toHaveLength(1);
});

test('active latest collection blocks manual while viewing completed history', async ({page}) => {
  const terminal = {id: 'old', state: 'completed', retry_of: null, snapshot: {}, summary: {}, issues: []};
  const active = {id: 'active', state: 'running', created_at: '2026-10-04T12:00:00Z'};
  const project = {id: 'busy-project', name: '진행 중', path: '/fixture', status: 'ongoing',
    base_branch: 'main', collection_state: 'running', collections: [active, terminal]};
  await page.route('**/api/projects', route => route.fulfill({json: [project]}));
  await page.route('**/api/projects/busy-project', route => route.fulfill({json: project}));
  await page.route('**/api/collections/active', route => route.fulfill({json: {...active, snapshot: {}, summary: {}}}));
  await page.route('**/api/collections/old', route => route.fulfill({json: terminal}));
  await page.goto('/');
  await page.getByRole('button', {name: /진행 중.*History 수집 중/}).click();
  await expect(page.getByRole('button', {name: '지금 수집', exact: true})).toBeDisabled();
  await page.getByLabel('수집 기록').selectOption('old');
  await expect(page.getByRole('button', {name: '수집 재시도', exact: true})).toHaveCount(0);
  await expect(page.getByRole('button', {name: '지금 수집', exact: true})).toBeDisabled();
});

test('unobserved snapshot is not shown as an empty repository', async ({page}) => {
  // UI rendering contract only; the real interruption cleanup path is tested against PostgreSQL.
  const project = {id: 'unobserved', name: '초기 관측 중단', path: '/fixture', status: 'new',
    collection_state: 'completed', collections: [{id: 'attempt', state: 'completed'}]};
  await page.route('**/api/projects', route => route.fulfill({json: [project]}));
  await page.route('**/api/projects/unobserved', route => route.fulfill({json: project}));
  await page.route('**/api/collections/attempt', route => route.fulfill({json: {
    id: 'attempt', state: 'completed', snapshot: {}, summary: {}, issues: [
      {id: 1, phase: 'snapshot', code: 'snapshot_incomplete', message: '등록 시점 관측 중단'},
    ],
  }}));
  await page.goto('/');
  await page.getByRole('button', {name: /초기 관측 중단/}).click();
  await expect(page.locator('.metadata').getByText('미확보', {exact: true})).toHaveCount(2);
  await expect(page.locator('.metadata')).not.toContainText('Commit 없음');
  await expect(page.locator('.metadata')).not.toContainText('분리된 HEAD');
  await expect(page.locator('.issues')).toContainText('등록 시점 관측 중단');
});






test('edit base branch, reject mismatch without writes, and preserve historical branch', async ({page, request, backend}, testInfo) => {
  const root = join(backend, 'repo');
  const git = (...args: string[]) => execFileSync('git', ['-c', 'core.hooksPath=/dev/null', '-C', root, ...args]).toString().trim();
  git('branch', 'release/delivery'); // Synthetic Repository owned by this E2E only.
  await page.goto('/');
  await page.getByLabel('프로젝트 경로', {exact: true}).fill(root);
  await expect(page.getByLabel('기준 브랜치', {exact: true})).toHaveValue('main');
  await page.getByRole('button', {name: '등록하고 수집 시작'}).click();
  await expect(page.locator('.collection-state')).toContainText('수집 완료');
  const [project] = await (await request.get('/api/projects')).json();
  const initial = await (await request.get(`/api/projects/${project.id}`)).json();
  const cid = initial.collections[0].id;
  await page.getByRole('button', {name: '설정 변경'}).click();
  await page.locator('.edit').getByLabel('기준 브랜치', {exact: true}).fill('missing');
  await page.getByRole('button', {name: '설정 저장'}).click();
  await expect(page.getByRole('alert')).toContainText('local 기준 브랜치 "missing"가 없습니다');
  expect((await (await request.get(`/api/projects/${project.id}`)).json()).base_branch).toBe('main');
  await page.locator('.edit').getByLabel('기준 브랜치', {exact: true}).fill('release/delivery');
  await page.getByRole('button', {name: '설정 저장'}).click();
  await expect(page.locator('.project-branch')).toContainText('release/delivery');
  const before = {branch: git('symbolic-ref', 'HEAD'), index: readFileSync(join(root, '.git/index')), body: readFileSync(join(root, 'README.md'))};
  await page.getByRole('button', {name: '지금 수집', exact: true}).click();
  await expect(page.getByRole('alert')).toContainText('기준 브랜치: "release/delivery"');
  await expect(page.getByRole('alert')).toContainText('현재 checkout: "main"');
  await expect(page.getByRole('alert')).toContainText('checkout한 뒤 다시 수집');
  expect((await (await request.get(`/api/projects/${project.id}`)).json()).collections).toHaveLength(1);
  expect(git('symbolic-ref', 'HEAD')).toBe(before.branch);
  expect(readFileSync(join(root, '.git/index'))).toEqual(before.index);
  expect(readFileSync(join(root, 'README.md'))).toEqual(before.body);
  git('checkout', 'release/delivery');
  await page.getByRole('button', {name: '지금 수집', exact: true}).click();
  await expect(page.locator('.collection-state')).toContainText('현재 시점의 수동 수집');
  await expect(page.locator('.collection-state')).toContainText('수집 완료');
  await expect(page.locator('.metadata')).toContainText('release/delivery');
  await page.getByLabel('수집 기록').selectOption(cid);
  await expect(page.locator('.metadata')).toContainText('main');
  await expect(page.locator('.project-branch')).toContainText('release/delivery');
  expect((await (await request.get(`/api/collections/${cid}`)).json()).snapshot).toEqual(initial.collections[0].snapshot);
  await page.screenshot({path: testInfo.outputPath('base-branch-history.png'), fullPage: true});
});

test.describe('legacy project configuration', () => {
  test.use({legacy: true});
  test('unset legacy project is isolated, settings restore collection and preserve UNKNOWN', async ({page, request, backend}) => {
    await page.goto('/');
    await page.getByRole('button', {name: /기존 프로젝트.*개발 완료/}).click();
    await expect(page.locator('.detail')).toContainText('기준 브랜치가 미설정입니다');
    await expect(page.getByRole('button', {name: '지금 수집', exact: true})).toBeDisabled();
    await expect(page.locator('.metadata').getByText('미확보', {exact: true})).toHaveCount(2);
    const [legacy] = await (await request.get('/api/projects')).json();
    expect(legacy.base_branch).toBeNull();
    const before = await (await request.get(`/api/projects/${legacy.id}`)).json();
    await page.getByLabel('프로젝트 경로', {exact: true}).fill(join(backend, 'empty-repo'));
    await expect(page.getByLabel('기준 브랜치', {exact: true})).toHaveValue('start/here');
    await page.getByRole('button', {name: '등록하고 수집 시작'}).click();
    await expect(page.locator('.detail').getByRole('heading', {name: 'empty-repo', exact: true})).toBeVisible();
    await expect(page.locator('.collection-state')).toContainText('수집 완료');
    await page.getByRole('button', {name: /기존 프로젝트.*개발 완료/}).click();
    await page.getByRole('button', {name: '설정 변경'}).click();
    await expect(page.locator('.edit').getByLabel('기준 브랜치', {exact: true})).toHaveValue('');
    await page.locator('.edit').getByLabel('기준 브랜치', {exact: true}).fill('main');
    await page.getByRole('button', {name: '설정 저장'}).click();
    await expect(page.getByRole('button', {name: '지금 수집', exact: true})).toBeEnabled();
    await page.getByRole('button', {name: '지금 수집', exact: true}).click();
    await expect(page.locator('.collection-state')).toContainText('현재 시점의 수동 수집');
    await expect(page.locator('.collection-state')).toContainText('수집 완료');
    await page.getByLabel('수집 기록').selectOption(before.collections[0].id);
    await expect(page.locator('.metadata').getByText('미확보', {exact: true})).toHaveCount(2);
    expect((await (await request.get(`/api/collections/${before.collections[0].id}`)).json()).snapshot).toEqual({});
  });
});
