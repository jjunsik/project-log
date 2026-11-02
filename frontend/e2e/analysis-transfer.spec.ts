import {writeFile} from 'node:fs/promises';
import {test,expect} from '@playwright/test';
import {collections,materials} from '../src/dev/experienceFixtures';

test.skip(process.env.PROJECT_LOG_TRANSFER_UI!=='1','Isolated synthetic harness only; never use a user service.');

for(const migration of [false,true])test(`shows complete transfer paths and binds ${migration?'contract transition':'normal preparation'} consent`,async({page},info)=>{
  const errors:string[]=[],external:string[]=[],submitted:Record<string,unknown>[]=[];
  page.on('pageerror',e=>errors.push(e.message));
  let current:Record<string,any>|undefined,mode='normal';
  const planningPolicy='full-wire-split-all-evidence-priority-v1';
  const transferDigest='a'.repeat(64),recoveryDigest='e'.repeat(64),text='api_key=syntheticsecretvalue /Users/synthetic-account/repo';
  const chunks=Array.from({length:21},(_,i)=>({id:`chunk-${i}`,sources:[{path_b64:Buffer.from('/Users/synthetic-account/repo').toString('base64')}],text:i===20?null:`${text} · ${i}`,offset:i*100,
    media:i===20?{name:'synthetic.png',mime_type:'image/png',sha256:'b'.repeat(64),size_bytes:3,provenance:{}}:null}));
  const transition={policy:'validated-prefix-to-grounding-contract-fix-v1',digest:'f'.repeat(64),source_contract:'old-contract',target_contract:'new-contract',previous_attempts:2,max_attempts:2,max_regenerations:1,scope:'one_target_execution_lineage_per_fixed_sources_then_normal_resume',old_response_or_feedback_reused:false};
  const resume={request_id:'old-run',checkpoint_count:270,inherited_attempts:277,transition,compatibility:{policy:transition.policy,reused_stage:'discover_consolidate',source_contract:'old-contract',target_contract:'new-contract'}};
  const descriptor={...(migration?{resume}:{}),end_collection_id:collections.at(-1)!.id,collection_ids:collections.map(c=>c.id),materials:materials.map(m=>({id:m.id})),transfer_digest:transferDigest,relation_recovery_digest:recoveryDigest,
    items:[{reference:'R1',outcome:'ready',reason:null,limitations:[]}],profile:{planning_policy:planningPolicy,provider:'Synthetic Gemini notice',model:'synthetic-offline',data_policy:'Unpaid Services · 합성 안내 · 자동 보호 없음',transfer_scope:'합성 고정 자료만 사용',relation_recovery:{max_attempts:2,max_regenerations:1}}};
  const transfer=(offset:number,download:boolean)=>({planning_policy:mode==='planning_mismatch'?'unknown':planningPolicy,policy:'synthetic-manual',...(migration?{relation_transition:mode==='transition_mismatch'?{...transition,max_attempts:3}:transition}:{}),relation_recovery_digest:recoveryDigest,relation_recovery:descriptor.profile.relation_recovery,input_digest:current!.input_digest,transfer_digest:transferDigest,system_instruction:'합성 공통 지시',task_instructions:{discover:'합성 발견 지시'},later_context:'고정 자료와 이번 AI 응답으로 후속 문맥을 만듭니다.',offset:download?0:offset,total:21,chunks:download?chunks:chunks.slice(offset,offset+20),all_chunks_included:download,chunk_text_truncated:false,media_bytes_included:download,...(download?{inline_media:{20:{mime_type:'image/png',base64:'YWJj'}}}:{})});
  await page.context().route('**/*',async route=>{
    const req=route.request(),url=new URL(req.url());
    if(url.origin!=='http://127.0.0.1:4189'){external.push(url.origin);return route.abort();}
    if(!url.pathname.startsWith('/api/'))return route.continue();
    const reply=(value:unknown,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(value)});
    if(url.pathname.endsWith('/analysis'))return reply({available:true,execution_available:mode!=='preparation_only',capacity:{state:'available'},executions:migration?[{id:'old-run',state:'failed',created_at:'2026-10-27T20:27:26+09:00',error_code:'candidate_relation_grounding_split_labels_m0_x4_d0',call_seq:2,checkpoint_count:270,inherited_attempts:275,scope:{end_collection_id:descriptor.end_collection_id,collection_ids:descriptor.collection_ids,materials:descriptor.materials}}]:[]});
    if(url.pathname.endsWith('/preparations')&&req.method()==='POST'){
      current={id:req.postDataJSON().preparation_id,state:'ready',input_digest:'c'.repeat(64),consent_digest:'d'.repeat(64),descriptor};return reply(current);
    }
    if(url.pathname.endsWith('/transfer')){
      if(mode==='preview_error')return reply({detail:'합성 전송 자료 조회 실패'},500);
      const offset=Number(url.searchParams.get('offset')??0),download=url.searchParams.get('download')==='true';
      const data=transfer(offset,download);
      return route.fulfill({contentType:'application/json',headers:download?{'Content-Disposition':'attachment; filename="synthetic-transfer.json"'}:{},body:JSON.stringify(data)});
    }
    if(url.pathname.endsWith('/transfer/media/20'))return route.fulfill({contentType:'image/png',headers:{'Content-Disposition':'attachment; filename="synthetic-media"'},body:Buffer.from('abc')});
    if(url.pathname.endsWith('/requests')&&req.method()==='POST'){
      submitted.push(req.postDataJSON());return reply({id:req.postDataJSON().request_id,state:'queued',created_at:'2026-10-19T00:00:00Z',phase:'queued',call_seq:0,scope:descriptor});
    }
    if(url.pathname.includes('/preparations/'))return reply(current);
    return reply({detail:'Unexpected synthetic request'},404);
  });
  await page.goto(process.env.PROJECT_LOG_TRANSFER_UI_PATH!);
  await page.getByRole('button',{name:migration?'이어서 분석 준비':'분석 시작',exact:true}).click();
  await expect(page.getByText('1–20 / 전체 21개 분할 자료 · 텍스트 생략 없음')).toBeVisible();
  await expect(page.getByText(/관계 근거 연결 검증 실패에 한해/)).toBeVisible();
  await expect(page.getByText(/전체 요청량 감소를 보장하지 않습니다/)).toBeVisible();
  mode='planning_mismatch';await page.getByRole('button',{name:'다음 전송 자료',exact:true}).click();
  await expect(page.getByRole('alert')).toContainText('요청 분할 정책이 일치하지 않습니다');
  await page.getByRole('button',{name:'전송 안내 확인',exact:true}).click();
  await expect(page.getByRole('dialog').getByRole('checkbox')).toBeDisabled();expect(submitted).toEqual([]);
  await page.getByRole('dialog').locator('footer').getByRole('button',{name:'닫기',exact:true}).click();
  mode='normal';await page.getByRole('button',{name:migration?'이어서 분석 준비':'분석 시작',exact:true}).click();
  await expect(page.getByText('1–20 / 전체 21개 분할 자료 · 텍스트 생략 없음')).toBeVisible();
  if(migration){
    await expect(page.getByRole('heading',{name:'검증된 구→신 계약 1회 전환'})).toBeVisible();
    mode='transition_mismatch';await page.getByRole('button',{name:'다음 전송 자료',exact:true}).click();
    await expect(page.getByRole('alert')).toContainText('계약 전환과 전송 확인 자료가 일치하지 않습니다');
    await page.getByRole('button',{name:'전송 안내 확인',exact:true}).click();
    await expect(page.getByRole('dialog').getByRole('checkbox')).toBeDisabled();expect(submitted).toEqual([]);
    await page.getByRole('dialog').locator('footer').getByRole('button',{name:'닫기',exact:true}).click();
    mode='normal';await page.getByRole('button',{name:'이어서 분석 준비',exact:true}).click();
    await expect(page.getByText('1–20 / 전체 21개 분할 자료 · 텍스트 생략 없음')).toBeVisible();
  }
  await page.getByText('분할 자료 1 · 텍스트 · 위치 0',{exact:true}).click();
  await expect(page.getByText(`${text} · 0`,{exact:true})).toBeVisible();
  await expect(page.getByText('출처 경로 해독 표시: /Users/synthetic-account/repo').first()).toBeVisible();
  await writeFile(process.env.PROJECT_LOG_TRANSFER_DOWNLOAD_FIXTURE!,JSON.stringify(transfer(0,true)));
  const downloadEvent=page.waitForEvent('download');
  await page.getByRole('link',{name:'전체 전송 재료 JSON 다운로드',exact:true}).click();
  const download=await downloadEvent,stream=await download.createReadStream();
  const parts:Buffer[]=[];for await(const part of stream!)parts.push(Buffer.from(part));
  const full=JSON.parse(Buffer.concat(parts).toString());
  expect(full.chunks).toHaveLength(21);expect(full.all_chunks_included).toBe(true);expect(full.inline_media['20'].base64).toBe('YWJj');
  await page.getByRole('button',{name:'다음 전송 자료',exact:true}).click();
  await expect(page.getByText('21–21 / 전체 21개 분할 자료 · 텍스트 생략 없음')).toBeVisible();
  await page.getByText('분할 자료 21 · synthetic.png · 위치 2000',{exact:true}).click();
  await expect(page.getByRole('link',{name:/실제 전송 비텍스트 자료 다운로드/})).toBeVisible();
  await page.getByRole('button',{name:'전송 안내 확인',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'외부 AI 전송 안내'}),checkbox=dialog.getByRole('checkbox'),submit=dialog.getByRole('button',{name:migration?'동의하고 이어서 분석 요청':'동의하고 분석 요청'});
  await expect(checkbox).not.toBeChecked();await expect(submit).toBeDisabled();
  await checkbox.check();await dialog.locator('footer').getByRole('button',{name:'닫기',exact:true}).click();
  mode='preview_error';await page.getByRole('button',{name:'이전 전송 자료',exact:true}).click();
  await expect(page.getByRole('alert')).toContainText('합성 전송 자료 조회 실패');
  await page.getByRole('button',{name:'전송 안내 확인',exact:true}).click();
  await expect(checkbox).not.toBeChecked();await expect(checkbox).toBeDisabled();expect(submitted).toEqual([]);
  await dialog.locator('footer').getByRole('button',{name:'닫기',exact:true}).click();
  if(migration){mode='normal';await page.getByRole('button',{name:'이어서 분석 준비',exact:true}).click();await expect(page.getByText('1–20 / 전체 21개 분할 자료 · 텍스트 생략 없음')).toBeVisible();}
  else {
  mode='preparation_only';await expect(page.getByRole('button',{name:'로컬 자료 준비',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'로컬 자료 준비',exact:true}).click();
  await expect(page.getByText('1–20 / 전체 21개 분할 자료 · 텍스트 생략 없음')).toBeVisible();
  await page.getByRole('button',{name:'전송 안내 확인',exact:true}).click();await expect(checkbox).toBeDisabled();
  await dialog.locator('footer').getByRole('button',{name:'닫기',exact:true}).click();mode='normal';
  }
  await expect(page.getByRole('button',{name:'분석 시작',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'전송 안내 확인',exact:true}).click();await checkbox.check();await submit.click();
  await expect.poll(()=>submitted.length).toBe(1);
  expect(submitted[0]).toMatchObject({preparation_id:current!.id,input_digest:current!.input_digest,consent_digest:current!.consent_digest,relation_recovery_digest:recoveryDigest,confirmed:true});
  if(migration)expect(submitted[0]).toMatchObject({relation_transition_digest:transition.digest});else expect(submitted[0]).not.toHaveProperty('relation_transition_digest');
  expect(await page.evaluate(()=>JSON.stringify(localStorage))).not.toContain('syntheticsecretvalue');
  expect(external).toEqual([]);expect(errors).toEqual([]);
  await page.screenshot({path:info.outputPath('manual-transfer.png'),fullPage:true});
});

test('registration explains temporary edits and historical collection limits without a new checklist',async({page})=>{
  await page.goto(process.env.PROJECT_LOG_TRANSFER_UI_PATH!+'?registration=1');
  await expect(page.getByText(/수집 전에 개인정보·Secret·기밀정보·제3자 자료/)).toBeVisible();
  await expect(page.getByRole('checkbox')).toHaveCount(0);
  await page.getByText('수집 범위와 임시 수정·복구 시 주의',{exact:true}).click();
  await expect(page.getByText(/과거 Commit·Diff·기존 Collection에는 소급 적용되지 않습니다/)).toBeVisible();
  await expect(page.getByText(/Project Log는 원본 파일을 자동 수정하거나 복구하지 않습니다/)).toBeVisible();
});
