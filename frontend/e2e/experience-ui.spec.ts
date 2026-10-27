import {test,expect,type Page} from '@playwright/test';

const observed=new WeakMap<Page,{requests:string[];errors:string[];allowedApi?:boolean}>();

test.beforeEach(async({page})=>{
  const log={requests:[] as string[],errors:[] as string[],allowedApi:false};observed.set(page,log);
  page.on('pageerror',error=>log.errors.push(error.message));
  page.on('request',request=>{const url=new URL(request.url());if((url.pathname.startsWith('/api/')&&!log.allowedApi) || url.origin!=='http://127.0.0.1:4179')log.requests.push(request.url());});
  // No backend fixture server, DB or service connection. Only local Vite assets allowed.
  await page.route('**/*',route=>new URL(route.request().url()).origin==='http://127.0.0.1:4179' && !new URL(route.request().url()).pathname.startsWith('/api/') ? route.continue() : route.abort());
  await page.goto('/experience-preview.html');
  await expect(page.getByRole('heading',{name:'개발 경험 · 합성 UI 미리보기'})).toBeVisible();
});
test.afterEach(async({page})=>{expect(observed.get(page)?.requests).toEqual([]);expect(observed.get(page)?.errors).toEqual([]);});

test('resume adapter preserves source on reload and resolves lost submit response without a second POST',async({page})=>{
  observed.get(page)!.allowedApi=true;
  const preparationPosts:Record<string,unknown>[]=[],submissions:Record<string,unknown>[]=[];
  const descriptor={end_collection_id:'synthetic-collection-1',collection_ids:['synthetic-collection-1'],materials:[{id:'synthetic-material-1'},{id:'synthetic-material-2'},{id:'synthetic-material-3'}],transfer_digest:'original-transfer',resume:{request_id:'failed-original',checkpoint_count:2,inherited_attempts:3},items:[{reference:'original',outcome:'ready',reason:null,limitations:[]}],profile:{provider:'synthetic',model:'synthetic',data_policy:'synthetic',transfer_scope:'original'}};
  const failed={id:'failed-original',state:'failed',created_at:'2026-10-22T00:00:00Z',call_seq:3,checkpoint_count:2,scope:descriptor};
  let prep:Record<string,any>|undefined,queued:Record<string,any>|undefined;
  await page.route('**/api/**',async route=>{
    const request=route.request(),url=new URL(request.url());
    const reply=(value:unknown)=>route.fulfill({contentType:'application/json',body:JSON.stringify(value)});
    if(url.pathname.endsWith('/analysis'))return reply({available:true,execution_available:true,capacity:{state:queued?'busy':'available'},executions:queued?[queued,{...failed,resumed_by_request_id:queued.id}]:[failed]});
    if(url.pathname.endsWith('/preparations')&&request.method()==='POST'){
      preparationPosts.push(request.postDataJSON());prep={id:request.postDataJSON().preparation_id,state:'ready',input_digest:'c'.repeat(64),consent_digest:'d'.repeat(64),descriptor};return reply(prep);
    }
    if(url.pathname.endsWith('/transfer'))return reply({policy:'synthetic',input_digest:prep!.input_digest,transfer_digest:'original-transfer',system_instruction:'original',task_instructions:{discover:'original'},later_context:'original',chunks:[{id:'original-chunk',sources:[],text:'ORIGINAL FIXED INPUT',offset:0,media:null}],offset:0,total:1,all_chunks_included:true,chunk_text_truncated:false,media_bytes_included:false});
    if(url.pathname.includes('/preparations/'))return reply(prep);
    if(url.pathname.endsWith('/requests')&&request.method()==='POST'){
      const body=request.postDataJSON();submissions.push(body);queued={id:body.request_id,state:'queued',created_at:'2026-10-22T00:01:00Z',phase:'queued',call_seq:0,reused_checkpoints:2,inherited_attempts:3,resume_from_request_id:'failed-original',scope:descriptor};return route.abort();
    }
    if(queued&&url.pathname.endsWith('/requests/'+queued.id))return reply(queued);
    throw new Error('Unexpected synthetic adapter request: '+url.pathname);
  });
  await page.goto('/experience-preview.html?adapter=1');
  await page.getByRole('button',{name:'이어서 분석 준비',exact:true}).click();
  await expect(page.getByLabel('분석 종료 Collection',{exact:true})).toHaveValue('synthetic-collection-1');
  await page.reload();
  await expect(page.getByLabel('분석 종료 Collection',{exact:true})).toHaveValue('synthetic-collection-1');
  await expect(page.getByText('1–1 / 전체 1개 분할 자료 · 텍스트 생략 없음')).toBeVisible();
  await page.getByText('분할 자료 1 · 텍스트 · 위치 0',{exact:true}).click();
  await expect(page.getByText('ORIGINAL FIXED INPUT',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'전송 안내 확인',exact:true}).click();
  const dialog=page.getByRole('dialog');await expect(dialog.getByRole('checkbox')).not.toBeChecked();
  await dialog.getByRole('checkbox').check();await dialog.getByRole('button',{name:'동의하고 이어서 분석 요청'}).click();
  await expect(page.locator('.analysis-active')).toContainText('재개 실행');
  expect(preparationPosts).toHaveLength(1);expect(preparationPosts[0]).toMatchObject({end_collection_id:'synthetic-collection-1',resume_from_request_id:'failed-original'});
  expect(submissions).toHaveLength(1);expect(submissions[0]).toMatchObject({preparation_id:prep!.id,input_digest:'c'.repeat(64),consent_digest:'d'.repeat(64),confirmed:true});
  expect(await page.evaluate(()=>JSON.stringify(localStorage))).not.toContain('ORIGINAL FIXED INPUT');
});

test('failure resume requires original input preview and fresh consent, and cancellation cannot resume',async({page},info)=>{
  await page.getByLabel('합성 화면 사례').selectOption('failed');
  await page.getByRole('button',{name:'이어서 분석 준비',exact:true}).click();
  await expect(page.locator('.analysis-preparation')).toContainText('실패 실행의 원래 고정 입력');
  await expect(page.locator('.analysis-preparation')).toContainText('중간 결과 재사용은 API 호출로 차감하지 않습니다');
  await page.getByRole('button',{name:'전송 안내 확인',exact:true}).click();
  const dialog=page.getByRole('dialog'),submit=dialog.getByRole('button',{name:'동의하고 이어서 분석 요청',exact:true});
  await expect(dialog.getByRole('checkbox')).not.toBeChecked();
  await expect(submit).toBeDisabled();
  await dialog.getByRole('checkbox').check();await submit.click();
  await expect(page.locator('.analysis-active')).toContainText('재개 실행 · 재사용한 중간 결과 2개');
  await expect(page.getByRole('button',{name:'이어서 분석 준비',exact:true})).toHaveCount(0);
  await page.screenshot({path:info.outputPath('resumed-desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.screenshot({path:info.outputPath('resumed-mobile.png'),fullPage:true});
  await page.getByLabel('합성 화면 사례').selectOption('cancelled');
  await expect(page.getByRole('button',{name:'이어서 분석 준비',exact:true})).toHaveCount(0);
});

test('selects the completed prefix, requires consent and keeps frozen scope after navigation',async({page},info)=>{
  const end=page.getByLabel('분석 종료 Collection',{exact:true});
  await expect(end).toHaveValue('synthetic-collection-3');
  await end.selectOption('synthetic-collection-1');
  await expect(page.locator('.analysis-range')).toContainText('완료 수집 1개');
  await expect(page.locator('.analysis-materials')).toContainText('정상 접수 당시 이 프로젝트에 존재하는 전체 자료');
  await page.getByRole('button',{name:'분석 시작',exact:true}).click();
  await expect(page.locator('.analysis-counts')).toContainText(/분석 가능\s*1개/);
  await expect(page.locator('.analysis-counts')).toContainText(/부분 처리\s*1개/);
  await expect(page.locator('.analysis-counts')).toContainText(/제외\s*1개/);
  await page.getByText('처리 자료·제외 사유 상세',{exact:true}).click();
  await expect(page.locator('.analysis-item-list')).toContainText('지원 형식으로 처리하지 못해 본문을 제외');
  await page.getByRole('button',{name:'전송 안내 확인',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'외부 AI 전송 안내'}),submit=dialog.getByRole('button',{name:'동의하고 분석 요청'});
  await expect(submit).toBeDisabled();
  await expect(dialog).toContainText('합성 모델 · 실제 모델 아님');
  await dialog.getByRole('checkbox').check();
  await submit.click();
  await expect(page.locator('.analysis-active')).toContainText('분석 대기');
  await expect(page.locator('.analysis-active')).toContainText('완료 수집 1개');
  await page.getByRole('button',{name:'다른 화면으로 이동 (합성)',exact:true}).click();
  await page.getByRole('button',{name:'개발 경험으로 돌아오기 (합성)',exact:true}).click();
  await expect(end).toHaveValue('synthetic-collection-1');
  await page.getByRole('button',{name:'실행 중으로 (합성)',exact:true}).click();
  await expect(page.locator('.analysis-active')).toContainText('신규 수집·완료와 기존 자료 조회는 가능합니다');
  await page.getByRole('button',{name:'분석 취소 요청',exact:true}).click();
  await expect(page.getByRole('button',{name:'취소 정리 중',exact:true})).toBeDisabled();
  await expect(page.locator('.analysis-active')).toContainText('실제 취소 완료까지 자료 변경 제한이 유지');
  await page.getByRole('button',{name:'취소 완료로 (합성)',exact:true}).click();
  await expect(page.locator('.analysis-history')).toContainText('취소 완료');
  await expect(page.getByRole('heading',{name:'최근 합성 개발 경험 후보',exact:true})).toBeVisible();
  await page.screenshot({path:info.outputPath('cancelled-light.png'),fullPage:true});
});

test('blocks stale/empty/busy requests and clears consent when the selected range changes',async({page})=>{
  const scene=page.getByLabel('합성 화면 사례');
  await scene.selectOption('ready');
  await page.getByRole('button',{name:'전송 안내 확인',exact:true}).click();
  await page.getByRole('dialog').getByRole('checkbox').check();
  await page.getByRole('dialog').locator('footer').getByRole('button',{name:'닫기',exact:true}).click();
  await page.getByLabel('분석 종료 Collection',{exact:true}).selectOption('synthetic-collection-1');
  await expect(page.getByRole('alert')).toContainText('다시 준비한 뒤 새로 동의');
  await page.getByRole('button',{name:'전송 안내 확인',exact:true}).click();
  await expect(page.getByRole('dialog').getByRole('checkbox')).not.toBeChecked();
  await expect(page.getByRole('dialog').getByRole('button',{name:'동의하고 분석 요청'})).toBeDisabled();
  await page.getByRole('dialog').locator('footer').getByRole('button',{name:'닫기',exact:true}).click();
  await scene.selectOption('empty');
  await expect(page.getByRole('alert')).toContainText('실제 전송 가능한 입력이 없습니다');
  await page.getByRole('button',{name:'전송 안내 확인',exact:true}).click();
  await expect(page.getByRole('dialog').getByRole('checkbox')).toBeDisabled();
  await page.getByRole('dialog').locator('footer').getByRole('button',{name:'닫기',exact:true}).click();
  await scene.selectOption('busy');
  await expect(page.getByRole('button',{name:'분석 시작',exact:true})).toBeDisabled();
  await expect(page.locator('.analysis-intro')).toContainText('요청은 접수되지 않으며 자료 변경도 제한되지 않습니다');
  await expect(page.locator('.analysis-active')).toHaveCount(0);
});

test('shows server interruption, uncertain acceptance and earlier successful evidence independently',async({page},info)=>{
  await page.getByLabel('합성 화면 사례').selectOption('interrupted');
  await expect(page.locator('.analysis-history')).toContainText('자동 재개하지 않습니다');
  await expect(page.locator('.analysis-history')).toContainText('자료 변경 제한은 해제');
  await page.getByLabel('저장된 분석 결과',{exact:true}).selectOption('synthetic-success-old');
  await expect(page.getByRole('heading',{name:'이전 합성 개발 경험 후보',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'근거 확인 · synthetic:note',exact:true}).first().click();
  await expect(page.locator('.experience-evidence')).toContainText('합성 입력을 사용한 화면 검토입니다.');
  await page.getByLabel('합성 화면 사례').selectOption('unknown');
  await expect(page.getByRole('alert')).toContainText('접수 여부를 확인 중');
  await expect(page.getByRole('button',{name:'분석 시작',exact:true})).toBeDisabled();
  await page.getByRole('button',{name:'상태 다시 확인'}).click();
  await expect(page.locator('.analysis-active')).toContainText('분석 대기');
  await page.getByLabel('합성 화면 사례').selectOption('running');
  await page.getByLabel('테마',{exact:true}).selectOption('dark');
  await page.screenshot({path:info.outputPath('running-dark.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.screenshot({path:info.outputPath('running-mobile.png'),fullPage:true});
});

test('unimplemented real-screen boundary stays read-only even with capability flags, and empty scopes cannot start',async({page},info)=>{
  await page.getByLabel('합성 화면 사례').selectOption('unavailable');
  await page.evaluate(()=>{
    const original=window.fetch;
    const audit=window as typeof window & {analysisWrites:string[]};audit.analysisWrites=[];
    window.fetch=async(input,init)=>{
      if(init?.method && init.method!=='GET'){audit.analysisWrites.push(init.method);throw new Error('Unexpected write from UI');}
      const response=await original(input,init);
      if(String(input).endsWith('/experiences')){const body=await response.json();return Response.json({...body,transfer_enabled:true,execution_available:true});}
      return response;
    };
  });
  await page.getByRole('button',{name:'목록 새로고침',exact:true}).click();
  await expect(page.getByRole('button',{name:'분석 시작',exact:true})).toBeDisabled();
  await expect(page.locator('.analysis-counts')).toContainText('확인 전');
  await expect(page.locator('.analysis-execution')).toContainText('아직 조회할 수 없습니다');
  await page.screenshot({path:info.outputPath('unavailable-light.png'),fullPage:true});
  await page.getByRole('button',{name:'전송 안내 확인',exact:true}).click();
  await expect(page.getByRole('dialog').getByRole('checkbox')).toBeDisabled();
  await expect(page.getByRole('dialog').getByRole('button',{name:'동의하고 분석 요청'})).toBeDisabled();
  await page.getByRole('dialog').locator('footer').getByRole('button',{name:'닫기',exact:true}).click();
  await page.getByLabel('합성 화면 사례').selectOption('noCollections');
  await expect(page.getByLabel('분석 종료 Collection',{exact:true})).toBeDisabled();
  await expect(page.getByRole('button',{name:'분석 시작',exact:true})).toBeDisabled();
  expect(await page.evaluate(()=>(window as typeof window & {analysisWrites:string[]}).analysisWrites)).toEqual([]);
});

test('one-shot diagnostic requires exact preview and fresh consent without enabling ordinary resume',async({page},info)=>{
  observed.get(page)!.allowedApi=true;
  const preparations:Record<string,unknown>[]=[],submissions:Record<string,unknown>[]=[];
  const wire='e'.repeat(64);
  const descriptor={end_collection_id:'synthetic-collection-1',collection_ids:['synthetic-collection-1'],materials:[1,2,3].map(i=>({id:`synthetic-material-${i}`})),transfer_digest:'diagnostic-preview',diagnostic_retry:true,diagnostic:{wire_digest:wire,wire_bytes:121299},resume:{request_id:'fixed-failure',checkpoint_count:270,inherited_attempts:273},items:[{reference:'fixed',outcome:'ready',reason:null,limitations:[]}],profile:{provider:'synthetic',model:'synthetic',data_policy:'synthetic',transfer_scope:'fixed'}};
  const failed={id:'fixed-failure',state:'failed',created_at:'2026-10-23T00:00:00Z',call_seq:7,checkpoint_count:270,resume_block_reason:'analysis_resume_request_fix_required',diagnostic_available:true,scope:descriptor};
  let prep:Record<string,any>|undefined,queued:Record<string,any>|undefined,corrupt=true;
  await page.route('**/api/**',async route=>{
    const request=route.request(),url=new URL(request.url());
    const reply=(value:unknown)=>route.fulfill({contentType:'application/json',body:JSON.stringify(value)});
    if(url.pathname.endsWith('/analysis'))return reply({available:true,execution_available:true,capacity:{state:queued?'busy':'available'},executions:queued?[queued,{...failed,diagnostic_available:false,resumed_by_request_id:queued.id}]:[failed]});
    if(url.pathname.endsWith('/preparations')&&request.method()==='POST'){
      preparations.push(request.postDataJSON());prep={id:request.postDataJSON().preparation_id,state:'ready',input_digest:'c'.repeat(64),consent_digest:'d'.repeat(64),descriptor};return reply(prep);
    }
    if(url.pathname.endsWith('/transfer'))return reply({policy:'synthetic',input_digest:prep!.input_digest,transfer_digest:'diagnostic-preview',system_instruction:'fixed',task_instructions:{reconcile:'fixed'},later_context:'fixed',diagnostic_request:{model:'synthetic',endpoint:'https://provider.invalid/synthetic',wire_digest:corrupt?'f'.repeat(64):wire,wire_bytes:121299,max_new_calls:1,automatic_retries:0,continue_analysis:false,request:{synthetic_text:'SYNTHETIC EXACT OUTGOING REQUEST'}},chunks:[{id:'fixed',sources:[],text:'SYNTHETIC FIXED INPUT',offset:0,media:null}],offset:0,total:1,all_chunks_included:true,chunk_text_truncated:false,media_bytes_included:false});
    if(url.pathname.includes('/preparations/'))return reply(prep);
    if(url.pathname.endsWith('/requests')&&request.method()==='POST'){
      submissions.push(request.postDataJSON());queued={id:request.postDataJSON().request_id,state:'queued',created_at:'2026-10-23T00:01:00Z',phase:'queued',call_seq:0,reused_checkpoints:270,inherited_attempts:273,resume_from_request_id:'fixed-failure',diagnostic:true,scope:descriptor};return reply(queued);
    }
    if(queued&&url.pathname.endsWith('/requests/'+queued.id))return reply(queued);
    throw new Error('Unexpected synthetic diagnostic request: '+url.pathname);
  });
  await page.goto('/experience-preview.html?adapter=1');
  await expect(page.getByRole('button',{name:'이어서 분석 준비',exact:true})).toHaveCount(0);
  await page.getByRole('button',{name:'1회 진단 준비',exact:true}).click();
  await expect(page.getByRole('alert').filter({hasText:'고정된 1회 진단 요청'})).toBeVisible();
  await page.getByRole('button',{name:'전송 안내 확인',exact:true}).click();
  let dialog=page.getByRole('dialog');await expect(dialog.getByRole('checkbox')).toBeDisabled();
  await expect(dialog.getByRole('button',{name:'동의하고 1회 진단 요청'})).toBeDisabled();
  expect(submissions).toHaveLength(0);
  corrupt=false;await page.reload();
  await expect(page.getByRole('heading',{name:'이번 1회 진단의 정확한 발신 요청'})).toBeVisible();
  await page.getByText('발신 JSON·모델·API 확인',{exact:true}).click();
  await expect(page.getByText('SYNTHETIC EXACT OUTGOING REQUEST',{exact:false})).toBeVisible();
  await page.getByRole('button',{name:'전송 안내 확인',exact:true}).click();
  dialog=page.getByRole('dialog');await expect(dialog.getByRole('checkbox')).not.toBeChecked();
  await expect(dialog.getByRole('button',{name:'동의하고 1회 진단 요청'})).toBeDisabled();
  await dialog.getByRole('checkbox').check();await dialog.getByRole('button',{name:'동의하고 1회 진단 요청'}).click();
  await expect(page.locator('.analysis-active')).toBeVisible();
  expect(preparations).toHaveLength(1);expect(preparations[0]).toMatchObject({diagnostic_retry:true,resume_from_request_id:'fixed-failure'});
  expect(submissions).toHaveLength(1);expect(submissions[0]).toMatchObject({confirmed:true,consent_digest:'d'.repeat(64),input_digest:'c'.repeat(64)});
  await expect(page.getByRole('button',{name:'1회 진단 준비',exact:true})).toHaveCount(0);
  expect(await page.evaluate(()=>JSON.stringify(localStorage))).not.toContain('SYNTHETIC EXACT OUTGOING REQUEST');
  await page.screenshot({path:info.outputPath('diagnostic-synthetic.png'),fullPage:true});
});
