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

const fixtureRoots = new WeakMap<import('@playwright/test').Page,string>();
test.beforeEach(async ({context, baseURL, page, backend}) => {
  fixtureRoots.set(page,backend);
  await context.route('**/*', route => new URL(route.request().url()).origin === new URL(baseURL!).origin
    ? route.continue() : route.abort());
});

async function approve(page: import('@playwright/test').Page) {await page.getByRole('dialog').getByRole('button', {name:'확인',exact:true}).click();}
async function selectRoot(page: import('@playwright/test').Page, directory:string) {
  // Only the OS dialog is stubbed; diagnosis/register/reconnect use the real isolated Backend.
  await page.route('**/api/folders/picker',route=>route.request().method()==='POST'
    ? route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({cancelled:false,path:join(fixtureRoots.get(page)!,directory)})}) : route.continue());
  await page.getByRole('button',{name:'찾아보기',exact:true}).click();
  await expect(page.locator('.project-form button').filter({hasText:'처리 중…'})).toHaveCount(0);
}
async function skipMemo(page: import('@playwright/test').Page) {
  const modal=page.getByRole('dialog',{name:'수집 기록 제목·설명 입력',exact:true});
  await expect(modal).toBeVisible({timeout:15000});await modal.getByRole('button',{name:'건너뛰기',exact:true}).click();
}
async function register(page: import('@playwright/test').Page, directory='repo', inputAfterCompletion=true) {
  await page.goto('/');
  await page.getByRole('button',{name:'＋ 프로젝트 추가',exact:true}).click();
  await selectRoot(page,directory);
  await expect(page.getByLabel('기본 브랜치',{exact:true})).toHaveValue(directory==='empty-repo'?'start/here':'main');
  await page.getByRole('button',{name:'등록 후 첫 수집',exact:true}).click();
  await approve(page);
  if(inputAfterCompletion) await skipMemo(page);
}
async function completed(request: import('@playwright/test').APIRequestContext, pid?:string) {
  await expect.poll(async()=> (await (await request.get('/api/projects')).json()).length).toBeGreaterThan(0);
  const p = pid ? await (await request.get(`/api/projects/${pid}`)).json() : (await (await request.get('/api/projects')).json())[0];
  await expect.poll(async()=> (await (await request.get(`/api/projects/${p.id}`)).json()).collections[0]?.state, {timeout:15000}).toBe('completed');
  return (await (await request.get(`/api/projects/${p.id}`)).json());
}
async function collect(page: import('@playwright/test').Page) {await page.getByRole('button',{name:'지금 수집',exact:true}).click();await approve(page);await skipMemo(page);}

test('native chooser contract registration, dashboard design, atomic settings, reload and duplicate', async ({page,request,backend}, info)=>{
  const errors:string[]=[]; page.on('pageerror', e=>errors.push(e.message));
  await register(page); const p=await completed(request);
  await expect(page.locator('.collections-table tbody tr')).toHaveCount(1);
  await expect(page.locator('.stats .stat')).toHaveCount(3);
  await expect(page.locator('.project-info dt')).toHaveText(['프로젝트 이름','프로젝트 경로','기본 브랜치','등록일','최근 수집']);
  await expect(page.locator('.two-columns')).toContainText('사용자 추가 자료');
  expect((await (await request.get(`/api/projects/${p.id}`)).json()).collections[0].summary.preserved_document_bodies).toBe(1);
  await page.getByRole('button',{name:'프로젝트 설정'}).click();
  await page.getByLabel('프로젝트 이름',{exact:true}).fill('renamed'); await page.getByLabel('프로젝트 상태',{exact:true}).selectOption('completed');
  await page.locator('.sidebar').getByRole('button',{name:'대시보드',exact:true}).click(); await page.getByRole('dialog').getByRole('button',{name:'취소',exact:true}).click();
  await expect(page.getByLabel('프로젝트 이름',{exact:true})).toHaveValue('renamed');
  await page.getByRole('button',{name:'변경사항 저장'}).click(); await expect(page.getByRole('dialog')).toContainText('renamed'); await approve(page);
  await page.reload();await expect(page.getByRole('heading',{name:'등록된 프로젝트 목록'})).toBeVisible();await page.locator('.project-card').filter({hasText:'renamed'}).click();await expect(page.locator('.project-header')).toContainText('renamed');
  await page.getByRole('button',{name:/프로젝트 선택|▱ renamed/}).click(); await page.getByLabel('프로젝트 검색').fill('rename'); await page.getByRole('button',{name:'＋ 프로젝트 추가',exact:true}).click();
  await selectRoot(page,'repo'); await page.getByRole('button',{name:'등록 후 첫 수집'}).click(); await approve(page);await expect(page.locator('.project-form [role=alert]')).toContainText('이미 등록');
  await page.locator('.sidebar').getByRole('button',{name:'대시보드',exact:true}).click(); await approve(page);
  await expect(page.locator('.two-columns>.card').first().locator('tbody tr')).toHaveCount(1);
  await expect(page.locator('.card').filter({has:page.getByRole('heading',{name:'최근 커밋',exact:true})}).locator('tbody tr')).toHaveCount(1);
  await expect(page.locator('.user-materials')).not.toContainText('불러오는 중');
  await page.screenshot({path:info.outputPath('task6-dashboard.png'),fullPage:true});
  expect(errors).toEqual([]); expect(readFileSync(join(backend,'repo/docs/decisions/local.md'),'utf8')).toBe('Local development decision\n');
});

test('invalid root and registration succeeds even when first collection fails', async({page,request,backend})=>{
  await page.goto('/'); await page.getByRole('button',{name:'＋ 프로젝트 추가',exact:true}).click();
  await selectRoot(page,'.');await expect(page.locator('.project-form [role=alert]')).toContainText('Git 자료를 읽지 못');
  await selectRoot(page,'partial-repo'); await page.getByRole('button',{name:'등록 후 첫 수집'}).click(); await approve(page);
  await expect(page.getByRole('alert')).toContainText(/실패|정리/); const ps=await (await request.get('/api/projects')).json(); expect(ps).toHaveLength(1);
  await expect.poll(async()=> (await (await request.get(`/api/projects/${ps[0].id}`)).json()).collections.length).toBe(0);
  await expect(page.locator('.collections-table tbody tr')).toHaveCount(0); expect(existsSync(join(backend,'partial-repo/.git'))).toBe(true);
  await expect(page.getByRole('dialog',{name:'수집 기록 제목·설명 입력'})).toHaveCount(0);
});

test('commit and document details, historical content, file explorer and keyboard tooltip', async({page,request,backend},info)=>{
  await register(page); const p=await completed(request); const cid=p.collections[0].id;
  const commitCard=page.locator('.card').filter({has:page.getByRole('heading',{name:'최근 커밋',exact:true})});
  await commitCard.getByRole('button',{name:'상세 보기'}).first().click(); await expect(page.getByRole('heading',{name:'커밋 상세',exact:true})).toBeVisible();
  await page.locator('.file-changes button').first().click(); await expect(page.locator('.source-body')).toContainText('Test project');
  await page.getByRole('button',{name:'← 대시보드로 돌아가기'}).click();
  await page.getByRole('button',{name:'프로젝트 구조 보기',exact:true}).click(); await page.locator('.evidence-list').getByRole('button',{name:/README.md/}).click(); await expect(page.locator('.source-body')).toContainText('Changed after commit');
  await page.getByRole('button',{name:'← 대시보드로 돌아가기'}).click();
  const docs=page.locator('.two-columns>.card').first(); await docs.getByRole('button',{name:'상세 보기'}).click();
  await expect(page.locator('[aria-label=Breadcrumb]')).toContainText('decisions/local.md'); await expect(page.locator('.source-body')).toHaveText('Local development decision\n');
  writeFileSync(join(backend,'repo/docs/decisions/local.md'),'current replacement\n');
  await page.getByRole('button',{name:'← 대시보드로 돌아가기'}).click(); await collect(page); await expect.poll(async()=> (await (await request.get(`/api/projects/${p.id}`)).json()).collections.filter((c:{state:string})=>c.state==='completed').length).toBe(2);
  await expect(page.locator('.collections-table tbody tr')).toHaveCount(2);
  const first=page.locator('.collections-table tbody tr').filter({has:page.getByRole('cell',{name:'#1',exact:true})});
  await first.getByRole('button',{name:'이 수집으로 이동'}).click(); await approve(page); await docs.getByRole('button',{name:'상세 보기'}).click(); await expect(page.locator('.source-body')).toHaveText('Local development decision\n');
  await page.getByRole('button',{name:'← 대시보드로 돌아가기'}).click(); await commitCard.locator('td').first().locator('.full-text').focus(); await expect(commitCard.locator('.tooltip').first()).toBeVisible();
  await page.screenshot({path:info.outputPath('task6-history.png'),fullPage:true});
});

test('memo confirmation cancel and error retain input, popover, selected and unselected deletion', async({page,request})=>{
  await register(page); const p=await completed(request), cid=p.collections[0].id;
  await page.getByRole('button',{name:'편집'}).click(); await page.getByLabel('수집 기록 제목',{exact:true}).fill('title');
  const description='긴 설명입니다. '.repeat(18); await page.getByLabel('수집 기록 설명',{exact:true}).fill(description);
  await page.getByRole('dialog').getByRole('button',{name:'저장',exact:true}).click(); await page.getByRole('dialog').getByRole('button',{name:'취소',exact:true}).click(); await expect(page.getByLabel('수집 기록 설명',{exact:true})).toHaveValue(description);
  await page.route(`**/api/collections/${cid}`,route=>route.request().method()==='PATCH' ? route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'Synthetic save failure'})}) : route.continue());
  await page.getByRole('dialog').getByRole('button',{name:'저장',exact:true}).click(); await approve(page); await expect(page.getByRole('dialog')).toContainText('Synthetic save failure');
  await page.getByRole('dialog').getByRole('button',{name:'취소',exact:true}).click(); await expect(page.getByLabel('수집 기록 제목',{exact:true})).toHaveValue('title'); await page.unroute(`**/api/collections/${cid}`);
  await page.getByRole('dialog').getByRole('button',{name:'저장',exact:true}).click(); await approve(page); await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByText('더보기⌄',{exact:true}).click(); await expect(page.getByRole('region',{name:'전체 수집 설명'})).toHaveText(description);
  await collect(page); await expect.poll(async()=> (await (await request.get(`/api/projects/${p.id}`)).json()).collections.length).toBe(2); await expect(page.locator('.collections-table tbody tr')).toHaveCount(2);
  const old=page.locator('.collections-table tbody tr').filter({has:page.getByRole('cell',{name:'#1',exact:true})}); await old.getByRole('button',{name:'삭제',exact:true}).click(); await page.getByRole('dialog').getByRole('button',{name:'취소',exact:true}).click(); expect((await request.get(`/api/collections/${cid}`)).ok()).toBe(true);
  await old.getByRole('button',{name:'삭제',exact:true}).click(); await approve(page); await expect(page.locator('.collections-table tbody tr')).toHaveCount(1); await expect(page.locator('.move-slot')).toContainText('현재 선택됨');
  await page.locator('.collections-table').getByRole('button',{name:'삭제',exact:true}).click(); await approve(page); await expect(page.getByRole('heading',{name:'선택된 수집 기록 없음'})).toBeVisible();
});

test('project materials chooser, drop first-failure, download, confirmation and collection independence', async({page,request})=>{
  await register(page); const p=await completed(request);
  const area=page.locator('.user-materials'); await area.locator('input[type=file]').setInputFiles([{name:'A.txt',mimeType:'text/plain',buffer:Buffer.from('original A')},{name:'B.md',mimeType:'text/markdown',buffer:Buffer.from('B')}]);
  await expect(area.locator('tbody tr')).toHaveCount(2); const rows=await (await request.get(`/api/projects/${p.id}/materials`)).json(); expect(await (await request.get(`/api/projects/${p.id}/materials/${rows.find((r:{filename:string})=>r.filename==='A.txt').id}/file`)).text()).toBe('original A');
  await area.locator('.material-drop').evaluate(node=>{const dt=new DataTransfer(); for(const [name,body] of [['C.txt','C'],['bad.exe','bad'],['D.txt','D']])dt.items.add(new File([body],name,{type:'application/octet-stream'})); node.dispatchEvent(new DragEvent('drop',{dataTransfer:dt,bubbles:true,cancelable:true}));});
  await expect(area.locator('.upload-failure')).toContainText('bad.exe'); await expect(area.locator('.upload-failure')).toContainText('D.txt'); expect((await (await request.get(`/api/projects/${p.id}/materials`)).json()).map((r:{filename:string})=>r.filename)).toEqual(['C.txt','B.md','A.txt']);
  await collect(page); await expect.poll(async()=> (await (await request.get(`/api/projects/${p.id}`)).json()).collections.filter((c:{state:string})=>c.state==='completed').length).toBe(2); await expect(area.locator('tbody tr')).toHaveCount(3);
  await area.getByRole('button',{name:'C.txt 삭제'}).click(); await page.getByRole('dialog').getByRole('button',{name:'취소',exact:true}).click(); await expect(area.locator('tbody tr')).toHaveCount(3);
  await area.getByRole('button',{name:'C.txt 삭제'}).click(); await approve(page); await expect(area.locator('tbody tr')).toHaveCount(2); await expect(area.getByRole('button',{name:'상세 보기'})).toHaveCount(0);
});

test('all lists paginate, retain detail origin/page/scroll and clamp after deletion', async({page,request})=>{
  await register(page); const p=await completed(request);
  for(let i=0;i<10;i++){const response=await request.post(`/api/projects/${p.id}/collections`,{data:{}}); expect(response.ok()).toBe(true); await expect.poll(async()=> (await (await request.get(`/api/projects/${p.id}`)).json()).collection_busy).toBe(false);}
  await expect(page.locator('.collections-table tbody tr')).toHaveCount(5); const card=page.locator('.card').filter({has:page.getByRole('heading',{name:'최근 수집 기록',exact:true})}); await card.getByRole('button',{name:'전체 보기 →'}).click();
  await page.getByRole('navigation',{name:'목록 페이지'}).getByRole('button',{name:'2',exact:true}).click(); await expect(page.locator('.page-count')).toHaveText('2 / 2 · 총 11개'); await expect(page.locator('.collections-table tbody tr')).toHaveCount(1); await expect(page.getByRole('button',{name:'다음',exact:true})).toHaveCount(0);
  await page.locator('.collections-table').getByRole('button',{name:'삭제',exact:true}).click(); await approve(page); await expect(page.locator('.page-count')).toHaveText('1 / 1 · 총 10개');
  await page.locator('.sidebar').getByRole('button',{name:'대시보드',exact:true}).click(); const commitCard=page.locator('.card').filter({has:page.getByRole('heading',{name:'최근 커밋',exact:true})}); await commitCard.getByRole('button',{name:'전체 보기 →'}).click(); await page.getByRole('button',{name:'상세 보기',exact:true}).first().click(); await page.getByRole('button',{name:'← 커밋으로 돌아가기'}).click(); await expect(page.getByRole('heading',{name:'전체 커밋'})).toBeVisible();
  await page.locator('.sidebar').getByRole('button',{name:'대시보드',exact:true}).click(); await page.locator('.two-columns>.card').first().getByRole('button',{name:'전체 보기 →'}).click(); await page.getByRole('button',{name:'상세 보기'}).click(); await expect(page.locator('[aria-label=Breadcrumb]')).toContainText('decisions/local.md'); await page.getByRole('button',{name:'← 개발 문서로 돌아가기'}).click();
  await page.locator('.sidebar').getByRole('button',{name:'대시보드',exact:true}).click(); await page.locator('.user-materials').getByRole('button',{name:'전체 보기 →'}).click(); await expect(page.locator('.material-drop')).toBeVisible(); await expect(page.locator('.page-count')).toContainText('총 0개');
});

test('repository copied while backend runs reconnects atomically without changing historical source', async({page,request,backend})=>{
  await register(page); const p=await completed(request), snapshot=p.collections[0].snapshot;
  execFileSync('cp',['-R',join(backend,'repo'),join(backend,'copy')]);
  await page.getByRole('button',{name:'프로젝트 설정'}).click(); await selectRoot(page,'copy'); await page.getByLabel('프로젝트 이름',{exact:true}).fill('copy project'); await page.getByRole('button',{name:'변경사항 저장'}).click(); await expect(page.getByRole('dialog')).toContainText('연속성이 확인'); await page.getByRole('dialog').getByRole('button',{name:'취소',exact:true}).click(); expect((await (await request.get(`/api/projects/${p.id}`)).json()).path).toBe(join(backend,'repo'));
  await page.getByRole('button',{name:'변경사항 저장'}).click(); await approve(page); await expect.poll(async()=> (await (await request.get(`/api/projects/${p.id}`)).json()).path).toBe(join(backend,'copy')); const after=await (await request.get(`/api/projects/${p.id}`)).json(); expect(after.name).toBe('copy project'); expect(after.collections[0].snapshot).toEqual(snapshot);
  expect(readFileSync(join(backend,'repo/README.md'),'utf8')).toBe('Changed after commit\n');
});

test.describe('slow collection',()=>{test.use({slow:true}); test('cancel confirmation and selection are independent, then fresh collection completes',async({page,request},info)=>{
  await register(page,'repo',false); await expect(page.getByRole('dialog',{name:'수집 기록 제목·설명 입력'})).toHaveCount(0); await expect(page.getByRole('button',{name:'수집 취소',exact:true})).toBeVisible(); const p=(await (await request.get('/api/projects')).json())[0];
  await page.getByRole('button',{name:'수집 취소',exact:true}).click(); await page.getByRole('dialog').getByRole('button',{name:'취소',exact:true}).click(); expect((await (await request.get(`/api/projects/${p.id}`)).json()).collection_busy).toBe(true);
  await page.getByRole('button',{name:'수집 취소',exact:true}).click(); await approve(page); await expect.poll(async()=> (await (await request.get(`/api/projects/${p.id}`)).json()).collection_busy,{timeout:15000}).toBe(false); await expect(page.locator('.collections-table tbody tr')).toHaveCount(0); await expect(page.getByRole('dialog',{name:'수집 기록 제목·설명 입력'})).toHaveCount(0);
  await collect(page); await completed(request,p.id); await expect(page.locator('.collections-table tbody tr')).toHaveCount(1); await page.setViewportSize({width:390,height:844}); await page.screenshot({path:info.outputPath('task6-mobile.png'),fullPage:true}); expect(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});});

test('empty repository completes without invented counts or documents',async({page,request})=>{await register(page,'empty-repo'); await completed(request); await expect(page.locator('.collections-table tbody tr')).toHaveCount(1); await expect(page.locator('.stat strong')).toHaveText(['0','0','0']); await expect(page.getByRole('button',{name:'프로젝트 구조 보기'})).toBeEnabled();});

test.describe('legacy project',()=>{test.use({legacy:true}); test('UNKNOWN remains unknown and settings restore explicit base branch',async({page,request})=>{await page.goto('/');await page.locator('.project-card').first().click();await expect(page.locator('.stat strong')).toHaveText(['미확보','미확보','미확보']);await expect(page.locator('.stat b')).toHaveText(['비교 불가','비교 불가','비교 불가']); await expect(page.getByRole('button',{name:'지금 수집',exact:true})).toBeDisabled(); await expect(page.locator('main')).toContainText('기본 브랜치가 미설정'); await page.getByRole('button',{name:'프로젝트 설정'}).click(); await page.getByLabel('기본 브랜치',{exact:true}).fill('main'); await page.getByRole('button',{name:'변경사항 저장'}).click(); await approve(page); await page.locator('.sidebar').getByRole('button',{name:'대시보드',exact:true}).click(); await collect(page); const p=await completed(request); expect(p.collections).toHaveLength(2); await expect(page.locator('.stat strong')).toHaveText(['1','2','1']);});});

test('page two and scroll survive commit/document detail return; materials use ten-item pages',async({page,request,backend})=>{
  const repo=join(backend,'repo');
  for(let i=0;i<11;i++){
    writeFileSync(join(repo,'README.md'),`revision ${i}\n`);
    execFileSync('git',['-C',repo,'-c','core.hooksPath=/dev/null','add','README.md']);
    execFileSync('git',['-C',repo,'-c','core.hooksPath=/dev/null','commit','-m',`commit ${i}\n\nBody ${i}`],{stdio:'ignore'});
    writeFileSync(join(repo,'docs',`doc-${i}.md`),`observed document ${i}\n`);
  }
  await register(page); const p=await completed(request);
  const commitCard=page.locator('.card').filter({has:page.getByRole('heading',{name:'최근 커밋',exact:true})});
  await commitCard.getByRole('button',{name:'전체 보기 →'}).click();
  await page.getByRole('button',{name:'2',exact:true}).click(); await expect(page.locator('.page-count')).toHaveText('2 / 2 · 총 12개');
  await page.evaluate(()=>window.scrollTo(0,120)); const before=await page.evaluate(()=>scrollY);
  await page.getByRole('button',{name:'상세 보기'}).first().click(); await page.getByRole('button',{name:'← 커밋으로 돌아가기'}).click(); await expect(page.locator('.page-count')).toHaveText('2 / 2 · 총 12개'); await expect.poll(()=>page.evaluate(()=>scrollY)).toBe(before);
  await page.locator('.sidebar').getByRole('button',{name:'대시보드',exact:true}).click(); await page.locator('.two-columns>.card').first().getByRole('button',{name:'전체 보기 →'}).click(); await page.getByRole('button',{name:'2',exact:true}).click(); await expect(page.locator('.page-count')).toHaveText('2 / 2 · 총 12개');
  await page.getByRole('button',{name:'상세 보기'}).first().click(); await page.getByRole('button',{name:'← 개발 문서로 돌아가기'}).click(); await expect(page.locator('.page-count')).toHaveText('2 / 2 · 총 12개');
  for(let i=0;i<11;i++)expect((await request.put(`/api/projects/${p.id}/materials`,{data:Buffer.from(`material ${i}`),headers:{'Content-Type':'application/octet-stream','X-File-Name':`file-${i}.txt`}})).ok()).toBe(true);
  await page.locator('.sidebar').getByRole('button',{name:'대시보드',exact:true}).click(); await page.locator('.user-materials').getByRole('button',{name:'전체 보기 →'}).click(); await expect(page.locator('.user-materials tbody tr')).toHaveCount(10); await page.getByRole('button',{name:'2',exact:true}).click(); await expect(page.locator('.user-materials tbody tr')).toHaveCount(1); await page.locator('.user-materials tbody').getByRole('button',{name:/삭제/}).click(); await approve(page); await expect(page.locator('.page-count')).toHaveText('1 / 1 · 총 10개');
});

test('historical object warning uses one confirmation, preserves draft on cancel and stored evidence on save',async({page,request,backend})=>{
  await register(page); const p=await completed(request), repo=join(backend,'repo');
  const git=(...args:string[])=>execFileSync('git',['-C',repo,'-c','core.hooksPath=/dev/null',...args],{stdio:'pipe'});
  git('checkout','-b','discarded'); writeFileSync(join(repo,'old.txt'),'old branch'); git('add','.'); git('commit','-m','old branch');
  expect((await request.patch(`/api/projects/${p.id}`,{data:{name:p.name,status:p.status,base_branch:'discarded'}})).ok()).toBe(true);
  expect((await request.post(`/api/projects/${p.id}/collections`,{data:{}})).ok()).toBe(true); await completed(request,p.id);
  git('checkout','main'); git('branch','-D','discarded'); expect((await request.patch(`/api/projects/${p.id}`,{data:{name:p.name,status:p.status,base_branch:'main'}})).ok()).toBe(true); expect((await request.post(`/api/projects/${p.id}/collections`,{data:{}})).ok()).toBe(true); const before=await completed(request,p.id);
  const copy=join(backend,'warning-copy'); execFileSync('cp',['-R',repo,copy]); execFileSync('git',['-C',copy,'reflog','expire','--expire=now','--all']); execFileSync('git',['-C',copy,'gc','--prune=now']);
  await page.getByRole('button',{name:'프로젝트 설정'}).click(); await selectRoot(page,'warning-copy'); await page.getByLabel('프로젝트 이름',{exact:true}).fill('confirmed copy'); await page.getByRole('button',{name:'변경사항 저장'}).click(); await expect(page.getByRole('dialog')).toHaveCount(1); await expect(page.getByRole('dialog')).toContainText('원본 조회가 제한'); await page.getByRole('dialog').getByRole('button',{name:'취소',exact:true}).click(); await expect(page.getByLabel('프로젝트 이름',{exact:true})).toHaveValue('confirmed copy'); expect((await (await request.get(`/api/projects/${p.id}`)).json()).path).toBe(repo);
  await page.getByRole('button',{name:'변경사항 저장'}).click(); await approve(page); await expect(page.getByRole('dialog')).toHaveCount(0); const after=await (await request.get(`/api/projects/${p.id}`)).json(); expect(after.path).toBe(copy); expect(after.collections).toEqual(before.collections);
});

test('Acceptance navigation, consecutive numbers, counter limits and top toast',async({page,request})=>{
  await register(page);const p=await completed(request);
  await expect(page.locator('[aria-label=Breadcrumb] .truncate')).toHaveText(['repo','대시보드']);
  await page.getByRole('button',{name:'프로젝트 선택: repo',exact:true}).click();
  await expect(page.locator('.project-menu [aria-current=true]')).toContainText('선택됨');
  await page.keyboard.press('Escape');await expect(page.locator('.project-menu')).toHaveCount(0);
  await page.getByRole('button',{name:'프로젝트 선택: repo',exact:true}).click();await page.locator('h1').click();await expect(page.locator('.project-menu')).toHaveCount(0);
  await page.getByRole('button',{name:'편집',exact:true}).click();
  await page.getByLabel('수집 기록 제목',{exact:true}).fill('😀'.repeat(51));await expect(page.getByLabel('수집 기록 제목',{exact:true})).toHaveValue('😀'.repeat(50));
  await page.getByLabel('수집 기록 설명',{exact:true}).fill('가'.repeat(201));await expect(page.getByLabel('수집 기록 설명',{exact:true})).toHaveValue('가'.repeat(200));
  await expect(page.getByRole('dialog')).toContainText('50 / 50자');await expect(page.getByRole('dialog')).toContainText('200 / 200자');
  await page.getByRole('dialog').getByRole('button',{name:'취소',exact:true}).click();
  for(let i=0;i<2;i++){await collect(page);await completed(request,p.id);}
  const table=page.locator('.collections-table');await expect(table.locator('tbody tr td:first-child')).toHaveText(['#3','#2','#1']);
  await table.locator('tbody tr').filter({has:page.getByRole('cell',{name:'#2',exact:true})}).getByRole('button',{name:'삭제',exact:true}).click();await approve(page);
  await expect(table.locator('tbody tr td:first-child')).toHaveText(['#2','#1']);
  await expect(page.locator('.toast')).toHaveText('삭제가 완료되었습니다.');const toast=await page.locator('.toast').boundingBox();expect(toast!.y).toBeLessThan(40);
  await page.getByRole('button',{name:'PROJECT LOG',exact:true}).click();await expect(page.locator('.project-header')).toHaveCount(0);await expect(page.getByRole('button',{name:'지금 수집'})).toHaveCount(0);
  await page.locator('.project-card').first().click();await page.locator('.card').filter({has:page.getByRole('heading',{name:'최근 커밋',exact:true})}).getByRole('button',{name:'상세 보기'}).click();
  await expect(page.locator('[aria-label=Breadcrumb] .truncate')).toHaveText(['repo','커밋 상세']);await expect(page.locator('[aria-label=Breadcrumb] button')).toHaveText(['대시보드','커밋']);await expect(page.locator('.project-header')).toHaveCount(0);
  await page.context().grantPermissions(['clipboard-read','clipboard-write']);await page.getByRole('button',{name:'커밋 해시 복사'}).click();
  const stored=(await (await request.get(`/api/collections/${(await completed(request,p.id)).collections[0].id}/commits`)).json()).items[0].oid;
  expect(await page.evaluate(()=>navigator.clipboard.readText())).toBe(stored);
});

test('Acceptance structure retains independent file states, document evidence, untracked reasons and all directory items',async({page,backend},info)=>{
  const repo=join(backend,'repo');writeFileSync(join(repo,'README.md'),'staged content\n');execFileSync('git',['-C',repo,'-c','core.hooksPath=/dev/null','add','README.md']);writeFileSync(join(repo,'README.md'),'working content\n');writeFileSync(join(repo,'untracked.bin'),Buffer.from([0,1,2]));
  await register(page);await page.getByRole('button',{name:'프로젝트 구조 보기',exact:true}).click();
  await expect(page.locator('.project-header')).toHaveCount(0);await expect(page.locator('.collection-basis')).toContainText('선택한 수집 기록: #1');
  const list=page.locator('.structure-list');await list.getByRole('button',{name:'README.md',exact:true}).click();
  await expect(page.locator('.source-body')).toHaveText('working content\n');
  await page.getByRole('radio',{name:/^스테이징된 파일/}).click();await expect(page.locator('.source-body')).toHaveText('staged content\n');
  await page.getByRole('radio',{name:'커밋된 파일',exact:true}).click();await expect(page.locator('.source-body')).toHaveText('Test project\n');
  await page.getByRole('radio',{name:'작업 파일',exact:true}).focus();await expect(page.locator('[role=radio]').filter({hasText:'작업 파일'}).locator('.tooltip')).toBeVisible();await page.keyboard.press('ArrowRight');
  await expect(page.getByRole('radio',{name:'Git 객체에서 확보한 파일',exact:true})).toHaveCount(0);
  await page.getByText('수집 근거·기술 정보',{exact:true}).click();await expect(page.locator('.evidence-info')).toContainText('body_reason');await page.getByText('원본 JSON 보기',{exact:true}).click();await expect(page.locator('.evidence-info pre')).toContainText('"layer":');
  await list.getByRole('button',{name:'untracked.bin',exact:true}).click();await expect(page.getByRole('radio',{name:'Git 미추적 파일',exact:true})).toBeVisible();await expect(page.locator('.structure-content')).toContainText('메타데이터만 확보되어 파일 내용을 조회할 수 없습니다.');
  await list.getByRole('button',{name:'docs',exact:true}).click();await expect(page.locator('.source-body')).toHaveCount(0);await list.getByRole('button',{name:'decisions',exact:true}).click();await list.getByRole('button',{name:'local.md',exact:true}).click();await expect(page.getByRole('radio',{name:'개발 문서',exact:true})).toBeVisible();await expect(page.locator('.source-body')).toHaveText('Local development decision\n');
  await page.getByRole('button',{name:'프로젝트 루트',exact:true}).click();
  const many=Array.from({length:501},(_,i)=>({name:`file-${i}.txt`,path:`file-${i}.txt`,path_b64:`file-${i}`,directory:false,observations:[]}));
  await page.route('**/api/collections/*/browse/structure?*',route=>{const params=new URL(route.request().url()).searchParams;const offset=Number(params.get('offset') ?? 0),limit=Number(params.get('limit') ?? 100);return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({total:501,items:many.slice(offset,offset+limit)})});});
  await page.getByRole('button',{name:'← 대시보드로 돌아가기',exact:true}).click();await page.getByRole('button',{name:'프로젝트 구조 보기',exact:true}).click();await expect(page.locator('.structure-list li')).toHaveCount(501);await expect(page.getByRole('button',{name:'file-500.txt',exact:true})).toBeAttached();await expect(page.locator('.pager')).toHaveCount(0);
  await page.screenshot({path:info.outputPath('acceptance-structure.png'),fullPage:true});
});

test('Acceptance native cancellation preserves draft and slow request dismisses confirmation without false success',async({page})=>{
  await register(page);await page.getByRole('button',{name:'프로젝트 설정',exact:true}).click();const before=await page.locator('.path-input .truncate').textContent();
  await page.route('**/api/folders/picker',route=>route.request().method()==='POST'?route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({cancelled:true,path:null})}):route.continue());
  await page.getByRole('button',{name:'찾아보기',exact:true}).click();await expect(page.locator('.path-input .truncate')).toHaveText(before!);await expect(page.getByRole('button',{name:'변경사항 저장',exact:true})).toBeDisabled();
  await page.locator('.sidebar').getByRole('button',{name:'대시보드',exact:true}).click();let requests=0;
  await page.route('**/api/projects/*/collections',async route=>{requests++;await new Promise(r=>setTimeout(r,800));await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'Synthetic start failure'})});});
  await page.getByRole('button',{name:'지금 수집',exact:true}).click();await approve(page);await expect(page.getByRole('dialog')).toHaveCount(0);await expect(page.getByRole('button',{name:'수집 요청 중…',exact:true})).toBeDisabled();await expect(page.getByRole('alert')).toContainText('Synthetic start failure');expect(requests).toBe(1);await expect(page.getByRole('dialog',{name:'수집 기록 제목·설명 입력'})).toHaveCount(0);
});

test('Acceptance commit change pages clear the old Diff selection',async({page,backend})=>{
  const repo=join(backend,'repo');for(let i=0;i<11;i++)writeFileSync(join(repo,`change-${i}.txt`),`changed ${i}\n`);
  execFileSync('git',['-C',repo,'-c','core.hooksPath=/dev/null','add',...Array.from({length:11},(_,i)=>`change-${i}.txt`)]);execFileSync('git',['-C',repo,'-c','core.hooksPath=/dev/null','commit','-m','Eleven changed files\n\nCommit body']);
  await register(page);await page.locator('.card').filter({has:page.getByRole('heading',{name:'최근 커밋',exact:true})}).getByRole('button',{name:'상세 보기'}).first().click();
  const changes=page.locator('.card').filter({has:page.getByRole('heading',{name:'변경 파일',exact:true})});
  await expect(changes.locator('.page-count')).toHaveText('1 / 2 · 총 11개');await expect(changes.locator('.file-changes li')).toHaveCount(10);
  await changes.locator('.file-changes button').first().click();await expect(page.locator('.source-body')).toContainText('+changed');
  await changes.getByRole('button',{name:'다음',exact:true}).click();await expect(changes.locator('.page-count')).toHaveText('2 / 2 · 총 11개');await expect(changes.locator('.file-changes li')).toHaveCount(1);await expect(page.locator('.source-body')).toHaveCount(0);
  await expect(changes.getByRole('button',{name:'다음',exact:true})).toHaveCount(0);await expect(changes.getByRole('button',{name:'마지막',exact:true})).toHaveCount(0);
  await changes.locator('.file-changes button').click();await expect(page.locator('.source-body')).toContainText('+changed');
  await changes.getByRole('button',{name:'처음',exact:true}).click();await expect(changes.getByRole('button',{name:'이전',exact:true})).toHaveCount(0);await expect(page.locator('.source-body')).toHaveCount(0);
});

test('Follow-up project selection, non-overlapping sidebar and isolated permanent deletion',async({page,request,backend},info)=>{
  await register(page);const first=await completed(request);
  await page.getByRole('button',{name:'PROJECT LOG',exact:true}).click();
  await expect(page.getByRole('button',{name:'대시보드',exact:true})).toBeDisabled();await expect(page.getByRole('button',{name:'프로젝트 설정',exact:true})).toBeDisabled();
  await expect(page.getByRole('button',{name:'프로젝트 선택: 없음',exact:true})).toContainText('프로젝트 선택');
  const checkSwitcher=async()=>{await page.getByRole('button',{name:'프로젝트 선택: 없음',exact:true}).click();await expect(page.locator('.project-menu [aria-current]')).toHaveCount(0);const menu=await page.locator('.project-menu').boundingBox(),nav=await page.locator('.sidebar nav').boundingBox();expect(menu!.y+menu!.height).toBeLessThanOrEqual(nav!.y);await page.keyboard.press('Escape');await expect(page.locator('.project-menu')).toHaveCount(0);};
  await checkSwitcher();await page.reload();await expect(page.locator('.project-card')).toHaveCount(1);await checkSwitcher();
  await page.getByRole('button',{name:'프로젝트 선택: 없음',exact:true}).click();await page.getByLabel('프로젝트 검색').fill('repo');await page.locator('.project-menu').getByRole('button',{name:'repo',exact:true}).click();await expect(page.getByRole('button',{name:'지금 수집',exact:true})).toBeEnabled();
  await page.getByRole('button',{name:'PROJECT LOG',exact:true}).click();await page.getByRole('button',{name:'repo 프로젝트 메뉴',exact:true}).click();await expect(page.getByRole('menu')).toBeVisible();await page.keyboard.press('Escape');await expect(page.getByRole('menu')).toHaveCount(0);await expect(page.getByRole('button',{name:'repo 프로젝트 메뉴',exact:true})).toBeFocused();
  await page.getByRole('button',{name:'repo 프로젝트 메뉴',exact:true}).click();await page.getByRole('heading',{name:'등록된 프로젝트 목록'}).click();await expect(page.getByRole('menu')).toHaveCount(0);
  await page.getByRole('button',{name:'repo 프로젝트 메뉴',exact:true}).click();await page.getByRole('menuitem',{name:'수정',exact:true}).click();await expect(page.getByLabel('프로젝트 이름',{exact:true})).toHaveValue('repo');await expect(page.locator('.project-header')).toHaveCount(0);
  await page.getByRole('button',{name:'PROJECT LOG',exact:true}).click();await page.getByRole('button',{name:'repo 프로젝트 메뉴',exact:true}).click();await page.getByRole('menuitem',{name:'삭제',exact:true}).click();await expect(page.getByRole('dialog')).toContainText('실제 Git Repository');await expect(page.getByRole('button',{name:'영구 삭제',exact:true})).toBeDisabled();await page.getByRole('dialog').getByRole('button',{name:'취소',exact:true}).click();expect((await request.get(`/api/projects/${first.id}`)).ok()).toBe(true);
  await page.getByRole('button',{name:'＋ 프로젝트 추가',exact:true}).click();await selectRoot(page,'empty-repo');await page.getByRole('button',{name:'등록 후 첫 수집',exact:true}).click();await approve(page);await skipMemo(page);const projects=await (await request.get('/api/projects')).json();const second=projects.find((p:{id:string})=>p.id!==first.id);await completed(request,second.id);
  const material=await request.put(`/api/projects/${second.id}/materials`,{data:Buffer.from('isolated owned copy'),headers:{'content-type':'application/octet-stream','x-file-name':'owned.txt'}});expect(material.ok()).toBe(true);const owned=await material.json();
  await page.getByRole('button',{name:'PROJECT LOG',exact:true}).click();await page.getByRole('button',{name:'empty-repo 프로젝트 메뉴',exact:true}).click();await page.getByRole('menuitem',{name:'삭제',exact:true}).click();await page.getByLabel('삭제 확인 프로젝트명').fill('empty-repo');await page.getByRole('button',{name:'영구 삭제',exact:true}).click();await expect(page.getByRole('dialog')).toHaveCount(0);await expect(page.locator('.toast')).toHaveText('삭제가 완료되었습니다.');await expect(page.locator('.project-card')).toHaveCount(1);expect((await request.get(`/api/projects/${second.id}`)).status()).toBe(404);expect(existsSync(join(backend,'storage/materials',second.id,owned.id))).toBe(false);expect(existsSync(join(backend,'empty-repo/.git'))).toBe(true);expect((await request.get(`/api/projects/${first.id}`)).ok()).toBe(true);
  await page.setViewportSize({width:390,height:844});await checkSwitcher();await page.screenshot({path:info.outputPath('followup-project-list-mobile.png'),fullPage:true});
  await page.getByRole('button',{name:'repo 프로젝트 메뉴',exact:true}).click();await page.getByRole('menuitem',{name:'삭제',exact:true}).click();await page.getByLabel('삭제 확인 프로젝트명').fill('repo');await page.getByRole('button',{name:'영구 삭제',exact:true}).click();await expect(page.getByRole('heading',{name:'등록된 프로젝트가 없습니다.'})).toBeVisible();await expect(page.getByRole('button',{name:'대시보드',exact:true})).toBeDisabled();
});

test('Follow-up record fields, one-line tables, commit split pagination and nested raw JSON',async({page,request,backend},info)=>{
  const repo=join(backend,'repo');for(let i=0;i<23;i++)writeFileSync(join(repo,`change-${i}.txt`),`exact diff ${i}\n`);execFileSync('git',['-C',repo,'-c','core.hooksPath=/dev/null','add','.']);const timestamp=Number(execFileSync('git',['-C',repo,'show','-s','--format=%ct','HEAD'],{encoding:'utf8'}).trim())+60;execFileSync('git',['-C',repo,'-c','core.hooksPath=/dev/null','commit','-m','many files\n\nfull body'],{env:{...process.env,GIT_AUTHOR_DATE:`@${timestamp} +0000`,GIT_COMMITTER_DATE:`@${timestamp} +0000`}});
  await register(page);const p=await completed(request);const cid=p.collections[0].id;
  const record=page.locator('.selected-collection');await expect(record).toContainText('수집한 브랜치:');await expect(record).toContainText('main');await expect(record).toContainText('제목 없음');await expect(record).toContainText('설명 없음');await expect(record).not.toContainText('#1');await expect(page.locator('.collections-table tbody tr td:first-child')).toHaveText(['#1']);
  const cells=page.locator('.table-scroll th,.table-scroll td');expect(await cells.evaluateAll(nodes=>nodes.every(n=>getComputedStyle(n).whiteSpace==='nowrap'))).toBe(true);
  await page.locator('.card').filter({has:page.getByRole('heading',{name:'최근 커밋',exact:true})}).getByRole('button',{name:'상세 보기'}).first().click();await expect(page.locator('.file-changes li')).toHaveCount(10);const left=await page.locator('.commit-files').boundingBox(),right=await page.locator('.commit-diff').boundingBox();expect(left!.x+left!.width).toBeLessThan(right!.x);expect(right!.width).toBeGreaterThan(left!.width);
  await page.locator('.file-changes button').filter({hasText:'change-0.txt'}).click();await expect(page.locator('.commit-diff .source-body')).toContainText('exact diff 0');await expect(page.locator('.file-changes [aria-pressed=true]')).toHaveCount(1);await page.getByRole('button',{name:'다음',exact:true}).click();await expect(page.locator('.commit-diff')).toContainText('변경 파일을 선택하세요.');await expect(page.locator('.commit-diff .source-body')).toHaveCount(0);await page.locator('.file-changes button').first().click();await expect(page.locator('.commit-diff .source-body')).toBeVisible();
  await page.screenshot({path:info.outputPath('followup-commit-desktop.png'),fullPage:true});await page.setViewportSize({width:390,height:844});await page.locator('.file-changes button').first().click();await expect(page.locator('.commit-diff')).toBeInViewport();expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.getByRole('button',{name:'← 대시보드로 돌아가기',exact:true}).click();await page.getByRole('button',{name:'프로젝트 구조 보기',exact:true}).click();await page.locator('.structure-list').getByRole('button',{name:'README.md',exact:true}).click();const observation=(await (await request.get(`/api/collections/${cid}/browse/structure?limit=500`)).json()).items.find((f:{name:string})=>f.name==='README.md').observations.find((o:{layer:string})=>o.layer==='working');
  await expect(page.locator('.evidence-info')).not.toHaveAttribute('open','');await page.getByText('수집 근거·기술 정보',{exact:true}).click();await expect(page.locator('.evidence-info dt code')).toHaveText(['metadata.path','observed_at','body_observed_at','body_reason','provenance.kind','repaired']);await expect(page.locator('.raw-observation')).not.toHaveAttribute('open','');await page.getByText('원본 JSON 보기',{exact:true}).click();expect(JSON.parse((await page.locator('.raw-observation pre').textContent())!)).toEqual(observation);
  const checkTimes=async(o:Record<string,unknown>)=>{for(const field of ['observed_at','body_observed_at']){const value=o[field];await expect(page.locator('.evidence-info dl>div').filter({has:page.getByText(field,{exact:true})}).locator('dd .truncate')).toHaveText(value==null?'null':String(value).replace('T',' ').replace(/(?:Z|[+-]\d{2}:\d{2})$/,''));}};
  await checkTimes(observation);
  await page.getByRole('button',{name:'← 대시보드로 돌아가기',exact:true}).click();await page.locator('.two-columns>.card').first().getByRole('button',{name:'상세 보기'}).click();await page.getByText('수집 근거·기술 정보',{exact:true}).click();await page.getByText('원본 JSON 보기',{exact:true}).click();const documents=await (await request.get(`/api/collections/${cid}/documents?limit=10`)).json();const doc=documents.items[0].observations.find((o:{layer:string})=>o.layer==='document');expect(JSON.parse((await page.locator('.raw-observation pre').textContent())!)).toEqual(doc);await checkTimes(doc);
});

test('Completion memo: first registration waits for the exact completed collection, preserves limits and does not repeat',async({page,request},info)=>{
  let state='capturing',cid='',pid='',reads=0;
  await page.route('**/api/projects/*/collections',async route=>{
    const response=await route.fetch(),c=await response.json();cid=c.id;pid=new URL(route.request().url()).pathname.split('/')[3];await route.fulfill({response});
  });
  await page.route(/\/api\/projects\/[^/]+$/,async route=>{
    const response=await route.fetch(),p=await response.json();
    if(p.id===pid && cid){p.collections=p.collections.map((c:{id:string})=>c.id===cid?{...c,state}:c);p.collection_busy=state!=='completed';reads++;}
    await route.fulfill({response,json:p});
  });
  await register(page,'repo',false);
  const modal=page.getByRole('dialog',{name:'수집 기록 제목·설명 입력',exact:true});
  for(const [next,label] of [['capturing','현재 자료 확보 중'],['queued','수집 대기'],['running','History 수집 중']]){
    state=next;await expect(page.locator('.selected-collection .notice')).toContainText(label);await expect(modal).toHaveCount(0);await expect(page.getByRole('button',{name:'수집 취소',exact:true})).toBeEnabled();
  }
  await completed(request,pid);state='completed';await expect(modal).toBeVisible();await expect(page.getByRole('button',{name:'지금 수집',exact:true})).toBeEnabled();
  await page.getByLabel('수집 기록 제목',{exact:true}).fill('😀'.repeat(51));await expect(page.getByLabel('수집 기록 제목',{exact:true})).toHaveValue('😀'.repeat(50));await expect(modal).toContainText('50 / 50');
  await page.getByLabel('수집 기록 설명',{exact:true}).fill('가'.repeat(201));await expect(page.getByLabel('수집 기록 설명',{exact:true})).toHaveValue('가'.repeat(200));await expect(modal).toContainText('200 / 200');
  await modal.getByRole('button',{name:'저장',exact:true}).click();await approve(page);await expect(modal).toHaveCount(0);
  const saved=await (await request.get(`/api/collections/${cid}`)).json();expect(saved.title).toBe('😀'.repeat(50));expect(saved.description).toBe('가'.repeat(200));
  const before=reads;await expect.poll(()=>reads).toBeGreaterThan(before+1);await expect(modal).toHaveCount(0);
  const structure=page.getByRole('button',{name:'프로젝트 구조 보기',exact:true});expect(await structure.evaluate(n=>getComputedStyle(n).backgroundColor)).toBe('rgb(255, 255, 255)');await page.keyboard.press('Tab');await structure.focus();await expect(structure).toBeFocused();expect(await structure.evaluate(n=>getComputedStyle(n).outlineStyle)).not.toBe('none');await structure.hover();expect(await structure.evaluate(n=>getComputedStyle(n).borderTopColor)).toBe('rgb(0, 121, 101)');
  await page.screenshot({path:info.outputPath('completion-memo-dashboard.png'),fullPage:true});await structure.click();await expect(page.getByRole('heading',{name:'수집 당시 프로젝트 구조',exact:true})).toBeVisible();
  await page.reload();await page.locator('.project-card').first().click();await expect(page.locator('.selected-collection')).toContainText('😀'.repeat(50));await expect(modal).toHaveCount(0);
});

test('Completion memo: dashboard ignores other completions, failure/cleanup and project switches; close and skip are final',async({page,request,backend})=>{
  test.setTimeout(65000);await register(page);const p=await completed(request);
  let state='capturing',cid='',reads=0,postCount=0;
  await page.route('**/api/projects/*/collections',async route=>{postCount++;await new Promise(r=>setTimeout(r,700));const response=await route.fetch(),c=await response.json();cid=c.id;await route.fulfill({response});});
  await page.route(new RegExp(`/api/projects/${p.id}$`),async route=>{
    const response=await route.fetch(),project=await response.json();
    if(cid){project.collections=project.collections.flatMap((c:{id:string})=>c.id!==cid?[c]:state==='gone'?[]:[{...c,state}]);project.collection_busy=state!=='completed' && state!=='gone';reads++;}
    await route.fulfill({response,json:project});
  });
  const modal=page.getByRole('dialog',{name:'수집 기록 제목·설명 입력',exact:true});
  const begin=async()=>{cid='';state='capturing';await page.getByRole('button',{name:'지금 수집',exact:true}).click();await approve(page);await expect(page.getByRole('dialog')).toHaveCount(0);await expect(page.getByRole('button',{name:'수집 요청 중…',exact:true})).toBeDisabled();await expect(page.getByRole('button',{name:'수집 취소',exact:true})).toBeEnabled();await expect(modal).toHaveCount(0);};
  await begin();expect(postCount).toBe(1);
  for(const next of ['queued','running','partial','failed','cleanup_pending','cancel_pending']){state=next;const before=reads;await expect.poll(()=>reads).toBeGreaterThan(before);await expect(modal).toHaveCount(0);}
  state='completed';let before=reads;await expect.poll(()=>reads).toBeGreaterThan(before+1);await expect(modal).toHaveCount(0); // Late completed response after a terminal/cleanup state.
  state='gone';before=reads;await expect.poll(()=>reads).toBeGreaterThan(before);await expect(modal).toHaveCount(0);state='completed';await expect(page.getByRole('button',{name:'지금 수집',exact:true})).toBeEnabled();
  await begin();await completed(request,p.id);state='completed';await expect(modal).toBeVisible();const exact=cid;await modal.getByRole('button',{name:'건너뛰기',exact:true}).click();before=reads;await expect.poll(()=>reads).toBeGreaterThan(before+1);await expect(modal).toHaveCount(0);expect((await (await request.get(`/api/collections/${exact}`)).json()).state).toBe('completed');
  await begin();await completed(request,p.id);state='completed';await expect(modal).toBeVisible();await modal.getByRole('button',{name:'닫기',exact:true}).click();before=reads;await expect.poll(()=>reads).toBeGreaterThan(before+1);await expect(modal).toHaveCount(0);
  expect((await request.post('/api/projects',{data:{name:'다른 프로젝트',path:join(backend,'empty-repo'),status:'new',base_branch:'start/here',collect:false}})).ok()).toBe(true);
  await begin();await page.getByRole('button',{name:/프로젝트 선택:/}).click();await page.locator('.project-menu').getByRole('button',{name:'다른 프로젝트',exact:true}).click();state='completed';await expect(page.locator('.project-header')).toContainText('다른 프로젝트');await expect(modal).toHaveCount(0);await page.getByRole('button',{name:'PROJECT LOG',exact:true}).click();await expect(page.getByRole('heading',{name:'등록된 프로젝트 목록'})).toBeVisible();await page.locator('.project-card').filter({has:page.getByRole('heading',{name:'repo',exact:true})}).locator('.project-card-body').click();await expect(page.getByRole('button',{name:'지금 수집',exact:true})).toBeEnabled();before=reads;await expect.poll(()=>reads).toBeGreaterThan(before+1);await expect(modal).toHaveCount(0);
});

test.describe('Completion memo cancellation race',()=>{test.use({slow:true});test('cancel intent suppresses a late completed response before the cancel request finishes',async({page,request})=>{
  await register(page,'repo',false);await expect(page.getByRole('button',{name:'수집 취소',exact:true})).toBeEnabled();
  const p=(await (await request.get('/api/projects')).json())[0],current=await (await request.get(`/api/projects/${p.id}`)).json(),c=current.collections[0];let late=false,reads=0,release:()=>void=()=>{};
  const gate=new Promise<void>(resolve=>release=resolve);
  await page.route(new RegExp(`/api/projects/${p.id}$`),async route=>{if(late){reads++;await route.fulfill({json:{...current,collection_busy:false,collections:[{...c,state:'completed'}]}});}else await route.continue();});
  await page.route(`**/api/collections/${c.id}/cancel`,async route=>{late=true;await gate;await route.continue();});
  await page.getByRole('button',{name:'수집 취소',exact:true}).click();await approve(page);await expect.poll(()=>reads).toBeGreaterThan(0);release();await expect(page.getByRole('dialog')).toHaveCount(0);const before=reads;await expect.poll(()=>reads).toBeGreaterThan(before+1);await expect(page.getByRole('dialog',{name:'수집 기록 제목·설명 입력'})).toHaveCount(0);
});});

test('Project re-entry resets to latest completed while internal detail and settings keep selection',async({page,request},info)=>{
  info.setTimeout(100_000);
  await register(page);await collect(page);await collect(page);
  const p=await completed(request),[latest,middle,oldest]=p.collections;
  for(const [c,title] of [[latest,'A latest'],[middle,'A middle'],[oldest,'A oldest']] as const) {
    expect((await request.patch(`/api/collections/${c.id}`,{data:{title,description:''}})).ok()).toBe(true);
  }
  const record=page.locator('.selected-collection');
  const selected=async(title:string)=>{await expect(record).toBeVisible();await expect(record.locator('dd').nth(1).locator('.truncate')).toHaveText(title);};
  const dashboard=async()=>{await page.locator('.sidebar').getByRole('button',{name:'대시보드',exact:true}).click();};
  const list=async()=>{await page.getByRole('button',{name:'PROJECT LOG',exact:true}).click();await expect(page.getByRole('heading',{name:'등록된 프로젝트 목록',exact:true})).toBeVisible();};
  const historical=async()=>{await page.locator('.collections-table tbody tr').filter({has:page.getByRole('cell',{name:'#1',exact:true})}).getByRole('button',{name:'이 수집으로 이동',exact:true}).click();await approve(page);await selected('A oldest');};
  await selected('A latest');await historical();
  const commitCard=page.locator('.card').filter({has:page.getByRole('heading',{name:'최근 커밋',exact:true})});
  await commitCard.getByRole('button',{name:'상세 보기',exact:true}).first().click();await expect(page.locator('main')).toContainText('#1 · A oldest');await page.getByRole('button',{name:'← 대시보드로 돌아가기',exact:true}).click();await selected('A oldest');
  await page.locator('.two-columns>.card').first().getByRole('button',{name:'상세 보기',exact:true}).click();await expect(page.locator('main')).toContainText('#1 · A oldest');await page.getByRole('button',{name:'← 대시보드로 돌아가기',exact:true}).click();await selected('A oldest');
  await page.getByRole('button',{name:'프로젝트 구조 보기',exact:true}).click();await expect(page.locator('.collection-basis')).toContainText('#1 · A oldest');await page.getByRole('button',{name:'← 대시보드로 돌아가기',exact:true}).click();await selected('A oldest');
  await page.locator('.sidebar').getByRole('button',{name:'프로젝트 설정',exact:true}).click();await dashboard();await selected('A oldest');
  await page.locator('.user-materials').getByRole('button',{name:'전체 보기 →',exact:true}).click();await dashboard();await selected('A oldest');
  await list();await page.getByRole('button',{name:'repo 대시보드로 이동',exact:true}).click();await selected('A latest');
  await historical();await list();await page.getByRole('button',{name:'프로젝트 선택: 없음',exact:true}).click();await page.getByLabel('프로젝트 검색').fill('repo');await page.locator('.project-menu').getByRole('button',{name:'repo',exact:true}).click();await selected('A latest');
  await historical();await list();await page.getByRole('button',{name:'repo 프로젝트 메뉴',exact:true}).click();await page.getByRole('menuitem',{name:'수정',exact:true}).click();await expect(page.getByLabel('프로젝트 이름',{exact:true})).toHaveValue('repo');await dashboard();await selected('A latest');
  await historical();await list();expect((await request.delete(`/api/collections/${latest.id}`)).ok()).toBe(true);await page.getByRole('button',{name:'repo 대시보드로 이동',exact:true}).click();await selected('A middle');
  expect((await (await request.get(`/api/collections/${oldest.id}`)).json()).snapshot).toEqual(oldest.snapshot);
});

test('Project re-entry isolates projects, empty and unfinished records, and aborted same-project responses',async({page,request,backend},info)=>{
  info.setTimeout(90_000);
  await register(page);await collect(page);const a=await completed(request),[newest,oldest]=a.collections;
  for(const [c,title] of [[newest,'A latest'],[oldest,'A oldest']] as const) expect((await request.patch(`/api/collections/${c.id}`,{data:{title,description:''}})).ok()).toBe(true);
  const response=await request.post('/api/projects',{data:{name:'B',path:join(backend,'empty-repo'),status:'new',base_branch:'start/here',collect:false}});expect(response.ok()).toBe(true);const b=await response.json();
  const list=async()=>{await page.getByRole('button',{name:'PROJECT LOG',exact:true}).click();};
  const enter=async(name:string)=>{await page.getByRole('button',{name:`${name} 대시보드로 이동`,exact:true}).click();};
  const selected=async(title:string)=>{await expect(page.locator('.selected-collection dd').nth(1).locator('.truncate')).toHaveText(title);};
  await selected('A latest');await list();await expect(page.locator('.project-card')).toHaveCount(2);await enter('B');await expect(page.getByRole('heading',{name:'선택된 수집 기록 없음',exact:true})).toBeVisible();await expect(page.locator('.selected-collection')).toHaveCount(0);
  expect((await request.post(`/api/projects/${b.id}/collections`,{data:{}})).ok()).toBe(true);await completed(request,b.id);
  expect((await request.post(`/api/projects/${b.id}/collections`,{data:{}})).ok()).toBe(true);const bp=await completed(request,b.id),bc=bp.collections[0];expect((await request.patch(`/api/collections/${bc.id}`,{data:{title:'B latest',description:''}})).ok()).toBe(true);
  await list();await enter('B');await selected('B latest');await list();await enter('repo');await selected('A latest');
  // An earlier poll carries an older response for the same Project. Re-entry must abort it even in A → list → A.
  let held=false,release:()=>void=()=>{};const gate=new Promise<void>(done=>{release=done;});
  await page.route(new RegExp(`/api/projects/${a.id}$`),async route=>{
    if(!held){held=true;await gate;await route.fulfill({json:{...a,collections:[{...oldest,title:'A oldest'}]}});}
    else await route.continue();
  });
  await expect.poll(()=>held).toBe(true);const aborted=page.waitForEvent('requestfailed',r=>new URL(r.url()).pathname===`/api/projects/${a.id}`);
  await list();await aborted;await enter('repo');await selected('A latest');release();await page.unroute(new RegExp(`/api/projects/${a.id}$`));await selected('A latest');
  // State fixtures exercise default selection without changing the Backend lifecycle or creating user data.
  for(const state of ['capturing','queued','running','cancel_pending']) {
    await list();await page.route(new RegExp(`/api/projects/${a.id}$`),async route=>{const result=await route.fetch(),p=await result.json();await route.fulfill({response:result,json:{...p,collection_busy:true,collections:[{...newest,id:'unfinished',state,created_at:'2099-01-01T00:00:00Z'},...p.collections]}});});
    await enter('repo');await selected('A latest');await expect(page.locator('.project-header button')).toHaveText(state==='cancel_pending'?'취소 정리 중':'수집 취소');await page.unroute(new RegExp(`/api/projects/${a.id}$`));
  }
  await list();await enter('B');await selected('B latest');
});

test('Overview deletion rebases saved evidence across selections, reload, full history and external deletion',async({page,request,backend})=>{
  test.setTimeout(180000);
  const repo=join(backend,'repo'),commit=(message:string)=>{execFileSync('git',['-C',repo,'-c','core.hooksPath=/dev/null','add','.']);execFileSync('git',['-C',repo,'-c','core.hooksPath=/dev/null','commit','-m',message]);};
  for(let i=0;i<48;i++)writeFileSync(join(repo,`source-${i}.txt`),`source ${i}\n`);
  for(let i=0;i<4;i++)writeFileSync(join(repo,`docs/note-${i}.md`),`note ${i}\n`);
  commit('second');writeFileSync(join(repo,'README.md'),'third\n');commit('third');
  const stats=async(counts:string[],delta:string[])=>{await expect(page.locator('.stat strong')).toHaveText(counts);await expect(page.locator('.stat b')).toHaveText(delta);};
  await register(page);const p=await completed(request),a=p.collections[0].id;await stats(['3','50','5'],['-','-','-']);
  for(let i=48;i<54;i++)writeFileSync(join(repo,`source-${i}.txt`),`source ${i}\n`);commit('fourth');writeFileSync(join(repo,'README.md'),'fifth\n');commit('fifth');await collect(page);let current=await completed(request,p.id),b=current.collections[0].id;await stats(['5','56','5'],['+2','+6','0']);
  for(let i=54;i<58;i++)writeFileSync(join(repo,`source-${i}.txt`),`source ${i}\n`);for(let i=4;i<7;i++)writeFileSync(join(repo,`docs/note-${i}.md`),`note ${i}\n`);commit('sixth');await collect(page);current=await completed(request,p.id);const c=current.collections[0].id,snapshot=current.collections[0].snapshot;await stats(['6','60','8'],['+1','+4','+3']);
  const row=(number:string)=>page.locator('.collections-table tbody tr').filter({has:page.getByRole('cell',{name:number,exact:true})});
  await row('#2').getByRole('button',{name:'삭제',exact:true}).click();await page.getByRole('dialog').getByRole('button',{name:'취소',exact:true}).click();expect((await request.get(`/api/collections/${b}`)).ok()).toBe(true);
  await row('#2').getByRole('button',{name:'삭제',exact:true}).click();await approve(page);await expect(page.locator('.collections-table tbody tr td:first-child')).toHaveText(['#2','#1']);await stats(['6','60','8'],['+3','+10','+3']);
  await row('#1').getByRole('button',{name:'이 수집으로 이동',exact:true}).click();await approve(page);await stats(['3','50','5'],['-','-','-']);await row('#2').getByRole('button',{name:'이 수집으로 이동',exact:true}).click();await approve(page);await stats(['6','60','8'],['+3','+10','+3']);
  await page.reload();await page.locator('.project-card-body').first().click();await stats(['6','60','8'],['+3','+10','+3']);
  for(let i=0;i<9;i++){expect((await request.post(`/api/projects/${p.id}/collections`,{data:{}})).ok()).toBe(true);await expect.poll(async()=>(await (await request.get(`/api/projects/${p.id}`)).json()).collection_busy).toBe(false);}
  await page.locator('section.card').filter({has:page.getByRole('heading',{name:'최근 수집 기록',exact:true})}).getByRole('button',{name:'전체 보기 →',exact:true}).click();await page.getByRole('button',{name:'2',exact:true}).click();await expect(page.locator('.page-count')).toHaveText('2 / 2 · 총 11개');await row('#1').getByRole('button',{name:'이 수집으로 이동',exact:true}).click();await approve(page);await stats(['3','50','5'],['-','-','-']);
  await page.locator('section.card').filter({has:page.getByRole('heading',{name:'최근 수집 기록',exact:true})}).getByRole('button',{name:'전체 보기 →',exact:true}).click();await page.getByRole('button',{name:'2',exact:true}).click(); // C is on page 1; A is on page 2.
  await page.getByRole('button',{name:'1',exact:true}).click();await row('#2').getByRole('button',{name:'이 수집으로 이동',exact:true}).click();await approve(page);await stats(['6','60','8'],['+3','+10','+3']);
  // Another tab's delete changes history, not the selected UUID. Polling must invalidate the overview.
  expect((await request.delete(`/api/collections/${a}`)).ok()).toBe(true);await stats(['6','60','8'],['-','-','-']);expect((await (await request.get(`/api/collections/${c}`)).json()).snapshot).toEqual(snapshot);
  await collect(page);await stats(['6','60','8'],['0','0','0']);
});
