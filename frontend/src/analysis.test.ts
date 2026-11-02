import {describe,expect,it} from 'vitest';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import AnalysisFlow from './AnalysisFlow';
import type {AnalysisView} from './analysis';
import {analysisPrefix,completedAnalysisCollections,matchesPreparedScope} from './analysis';
import {collections,materials,prepare} from './dev/experienceFixtures';
import {analysisExecutionReason} from './analysisSupport';
import {preparedView} from './useAnalysis';

describe('resume preparation keeps original input and distinct consent metadata',()=>{
  it('exposes discovery-only compatibility as new consent metadata',()=>{
    const view=preparedView({id:'resume-prep',state:'ready',input_digest:'original-digest',consent_digest:'new-consent',descriptor:{end_collection_id:'original-end',collection_ids:['original-end'],materials:[],transfer_digest:'new-transfer',items:[],profile:{provider:'synthetic',model:'synthetic',data_policy:'synthetic',transfer_scope:'original'},resume:{request_id:'failed-id',checkpoint_count:264,inherited_attempts:266,compatibility:{policy:'known-v5-discovery-prefix-to-global-relations-v1',reused_stage:'discover',source_contract:'old',target_contract:'new'}}}});
    expect(view.resume?.compatibilityPolicy).toBe('known-v5-discovery-prefix-to-global-relations-v1');
    expect(view.resume?.inheritedAttempts).toBe(266);expect(view.transferDigest).toBe('new-transfer');
  });
  it('projects server-owned resume lineage and completed count without adding API attempts',()=>{
    const view=preparedView({id:'resume-prep',state:'ready',input_digest:'original-digest',consent_digest:'new-consent',descriptor:{end_collection_id:'original-end',collection_ids:['original-end'],materials:[],transfer_digest:'original-transfer',items:[],profile:{provider:'synthetic',model:'synthetic',data_policy:'synthetic',transfer_scope:'original'},resume:{request_id:'failed-id',checkpoint_count:185,inherited_attempts:187}}});
    expect(view.resume).toEqual({requestId:'failed-id',checkpointCount:185,inheritedAttempts:187});
    expect(view.transferDigest).toBe('original-transfer');expect(view.version).toBe('new-consent');
    expect(view.endCollectionId).toBe('original-end');
    expect(analysisExecutionReason('analysis_resume_consumed')).toContain('이미 재개 실행');
    expect(analysisExecutionReason('analysis_checkpoint_invalid')).toContain('자동으로 다시 호출하지 않습니다');
  });
});

describe('analysis provider diagnostics',()=>{
  it('requires a reviewed transition and preserves its explicit consent digest',()=>{
    const transition={policy:'validated-prefix-to-grounding-contract-fix-v1',digest:'transition-digest',source_contract:'old-contract',target_contract:'new-contract',previous_attempts:2,max_attempts:2,max_regenerations:1,scope:'one_target_execution_lineage_per_fixed_sources_then_normal_resume',old_response_or_feedback_reused:false as const};
    const row={id:'transition-prep',state:'ready',input_digest:'fixed',consent_digest:'new-consent',descriptor:{end_collection_id:'end',collection_ids:['end'],materials:[],transfer_digest:'transfer',items:[],profile:{provider:'synthetic',model:'synthetic',data_policy:'policy',transfer_scope:'fixed'},resume:{request_id:'old',checkpoint_count:270,inherited_attempts:277,transition}}};
    const projected=preparedView(row);
    expect(projected.resume?.transition).toEqual(transition);
    expect(()=>preparedView({...row,descriptor:{...row.descriptor,resume:{...row.descriptor.resume,transition:{...transition,digest:''}}}})).toThrow('계약 전환 확인값');
    const fixture={...prepare(),resume:projected.resume};
    const view:AnalysisView={executionAvailable:true,preparation:{state:'ready',data:fixture},capacity:{state:'available'},historyAvailable:true,executions:[]};
    const html=renderToStaticMarkup(createElement(AnalysisFlow,{collections,materials,view,refreshMaterials:()=>{},actions:{preview:async()=>{throw new Error('SSR must not transmit');}}}));
    expect(analysisExecutionReason('analysis_relation_transition_consumed')).toContain('이미 접수');
    expect(html).toContain('이전 실행까지 호출 진입 승인');
  });
  it('binds limited regeneration disclosure to the server prepared policy',()=>{
    const prepared=preparedView({id:'recovery-prep',state:'ready',input_digest:'fixed',consent_digest:'new-policy-consent',descriptor:{end_collection_id:collections.at(-1)!.id,collection_ids:collections.map(c=>c.id),materials:materials.map(m=>({id:m.id})),transfer_digest:'recovery-transfer',relation_recovery_digest:'policy-digest',items:[],profile:{provider:'synthetic',model:'synthetic',data_policy:'policy',transfer_scope:'fixed',relation_recovery:{max_attempts:2,max_regenerations:1}}}});
    expect(prepared.relationRecovery).toEqual({maxAttempts:2,maxRegenerations:1,digest:'policy-digest'});
    expect(prepared.version).toBe('new-policy-consent');
    const fixture={...prepare(),relationRecovery:prepared.relationRecovery};
    const view:AnalysisView={executionAvailable:true,preparation:{state:'ready',data:fixture},capacity:{state:'available'},historyAvailable:true,executions:[]};
    const html=renderToStaticMarkup(createElement(AnalysisFlow,{collections,materials,view,refreshMaterials:()=>{},actions:{preview:async()=>{throw new Error('SSR must not transmit');}}}));
    expect(html).toContain('비민감 오류 코드·개수·검증 규칙');
    expect(html).toContain('실패 응답 원문은 재전송하지 않습니다');
    expect(html).toContain('같은 작업의 보존된 검증 정보에서만 구성');
    expect(analysisExecutionReason('analysis_relation_attempts_exhausted')).toContain('2회 한도가 소진');
    expect(analysisExecutionReason('analysis_relation_budget_unknown')).toContain('재전송을 차단');
  });
  it('offers normal analysis from a historical diagnostic only after the server verifies a changed contract',()=>{
    const text=analysisExecutionReason('provider_invalid_request',true,true);
    expect(text).toContain('새 요청 계약으로');expect(text).toContain('정상 분석을 준비');
    expect(text).toContain('UNKNOWN');expect(text).toContain('진단 기회는 다시 열리지');
    expect(analysisExecutionReason('provider_invalid_request',true,false)).toContain('재개하지 않습니다');
    const view:AnalysisView={executionAvailable:true,preparation:{state:'idle'},capacity:{state:'available'},historyAvailable:true,executions:[{id:'diagnostic-failed',status:'failed',diagnostic:true,diagnosticAvailable:false,createdAt:'2026-10-23T12:00:00Z',endCollectionId:collections[0].id,endCollectionLabel:'fixed',collectionCount:1,materialCount:0,checkpointCount:270,calls:1,message:text}]};
    const html=renderToStaticMarkup(createElement(AnalysisFlow,{collections,materials,view,refreshMaterials:()=>{},actions:{prepare:()=>{throw new Error('Rendering must not prepare');}}}));
    expect(html).toContain('>이어서 분석 준비</button>');
    expect(html).not.toContain('>1회 진단 준비</button>');
    expect(html).toContain('새 요청 계약으로');
  });
  it('distinguishes local request budgets from an observed provider400',()=>{
    const reason=analysisExecutionReason('analysis_request_contract_limit_schema_depth');
    expect(reason).toContain('전송 전에');expect(reason).toContain('제공자 HTTP 400과 구분');
    expect(reason).toContain('최소 단위는 미해결로 남깁니다');
  });
  it('distinguishes observed HTTP400 categories from unknown historical refusal and quota',()=>{
    expect(analysisExecutionReason('provider_invalid_request')).toContain('구체적 거절 사유는 확인할 수 없습니다');
    expect(analysisExecutionReason('provider_invalid_request_schema_complexity')).toContain('상태 수·분기 복잡도');
    expect(analysisExecutionReason('provider_invalid_request_schema_depth')).toContain('깊이 제한');
    expect(analysisExecutionReason('provider_invalid_request_request_size')).toContain('요청 크기 제한');
    expect(analysisExecutionReason('provider_invalid_request')).toContain('완료 중간 결과는 보존');
    expect(analysisExecutionReason('analysis_resume_request_fix_required')).toContain('원인 수정과 Checkpoint 호환 검증 전');
  });
  it('shows relation failures separately from a recoverable per-execution budget stop',()=>{
    expect(analysisExecutionReason('candidate_relation_conflict')).toContain('임의로 합치거나');
    expect(analysisExecutionReason('candidate_relation_grounding_invalid')).toContain('제공 근거');
    expect(analysisExecutionReason('candidate_relation_invalid_m1_x2_v0')).toContain('누락 1개');
    expect(analysisExecutionReason('analysis_budget_exceeded')).toContain('이번 실행');
    expect(analysisExecutionReason('analysis_budget_exceeded')).toContain('새 준비·동의');
    expect(analysisExecutionReason('analysis_budget_exceeded')).not.toContain('소진');
  });
  it('shows merge and split grounding conditions without inventing historical details',()=>{
    const labels=analysisExecutionReason('candidate_relation_grounding_merge_labels_m2_x1_d3');
    expect(labels).toContain('병합');expect(labels).toContain('필요한 label 누락 2개');
    expect(labels).toContain('불필요한 label 1개');expect(labels).toContain('중복 항목 3개');
    const refs=analysisExecutionReason('candidate_relation_grounding_split_refs_d1_e2_x3_u4');
    expect(refs).toContain('분리');expect(refs).toContain('중복 참조 1개');
    expect(refs).toContain('구성원 수 초과 참조 2개');expect(refs).toContain('허용 근거 범위 밖 참조 3개');
    expect(refs).toContain('근거 미연결 구성원 4개');expect(refs).toContain('자동 재호출하지 않습니다');
    expect(analysisExecutionReason('candidate_relation_grounding_merge_reason_blank_n2')).toContain('공백뿐인 설명 2개');
    expect(analysisExecutionReason('candidate_relation_grounding_invalid')).toContain('당시 세부 위반 조건은 보존되지 않았습니다');
  });
  it('keeps grounding failures blocked according to the server and disables safe resume while busy',()=>{
    const view:AnalysisView={executionAvailable:true,preparation:{state:'idle'},capacity:{state:'available'},historyAvailable:true,executions:[{id:'grounding-failed',status:'failed',createdAt:'2026-10-25T13:37:16+09:00',endCollectionId:collections[0].id,endCollectionLabel:'fixed',collectionCount:1,materialCount:0,checkpointCount:270,calls:1,resumeBlockReason:'analysis_resume_validation_required'}]};
    const actions={prepare:()=>{throw new Error('Rendering must not prepare');}};
    for(const message of ['candidate_relation_grounding_invalid','candidate_relation_grounding_merge_labels_m1_x0_d0','candidate_relation_grounding_split_refs_d0_e0_x1_u1']){
      view.executions[0].message=analysisExecutionReason(message);
      const html=renderToStaticMarkup(createElement(AnalysisFlow,{collections,materials,view,refreshMaterials:()=>{},actions}));
      expect(html).not.toContain('>이어서 분석 준비</button>');
      expect(html).toContain('원인 수정과 호환 검증');
      expect(html).toContain('보존된 완료 중간 결과 270개');
    }
    view.executions[0].resumeBlockReason=undefined;
    view.capacity={state:'busy',projectName:'Synthetic project'};
    const html=renderToStaticMarkup(createElement(AnalysisFlow,{collections,materials,view,refreshMaterials:()=>{},actions}));
    expect(html).toMatch(/<button disabled="">이어서 분석 준비<\/button>/);
  });
  it('distinguishes counted consolidation failures from unknown historical violations',()=>{
    const counted=analysisExecutionReason('candidate_coverage_invalid_m2_x1_v3_n139');
    expect(counted).toContain('입력 Seed 139개');expect(counted).toContain('누락 2개');
    expect(counted).toContain('제공되지 않은 ID 1개');expect(counted).toContain('범위 밖 그룹 label 3개');
    expect(counted).toContain('자동 재호출하지 않습니다');
    expect(analysisExecutionReason('candidate_coverage_invalid_schema_n139')).toContain('응답 구조');
    expect(analysisExecutionReason('candidate_coverage_invalid')).toContain('구체적 유형은 보존되지 않았습니다');
  });
  it('shows only observed boundaries and known HTTP status',()=>{
    const body=analysisExecutionReason('provider_read_timeout_body_partial_h200');
    expect(body).toContain('응답 읽기 제한시간 초과');
    expect(body).toContain('응답 본문 일부 수신');
    expect(body).toContain('HTTP 상태: 200');
    expect(body).toContain('자동 재전송하지 않았습니다');
    expect(body).not.toContain('접수');
    const unknown=analysisExecutionReason('provider_read_timeout_transport_hunknown');
    expect(unknown).toContain('HTTP 상태: 미확인');
    expect(unknown).not.toContain('접수');
  });
  it('keeps SDK, cleanup and historical unknown failures distinct',()=>{
    expect(analysisExecutionReason('provider_unexpected_error_sdk_h200')).toContain('SDK 또는 내부 처리');
    expect(analysisExecutionReason('provider_close_error_transport_close_h200')).toContain('transport 정리');
    expect(analysisExecutionReason('provider_read_timeout_rate_limit_body_h429')).toContain('HTTP 429 상세 확인');
    expect(analysisExecutionReason('provider_network_error')).toContain('당시 세부 유형과 응답 수신 단계는 확인할 수 없습니다');
    expect(analysisExecutionReason('analysis_preparation_lost')).toContain('다시 준비');
    expect(analysisExecutionReason(null)).toBeUndefined();
  });
});

describe('analysis scope uses completed UUIDs, never display numbers',()=>{
  it('includes the completed prefix and orders equal timestamps by UUID',()=>{
    const tied=[{...collections[0],id:'b'},{...collections[0],id:'a'},{...collections[2],state:'running' as const}];
    expect(completedAnalysisCollections(tied).map(c=>c.id)).toEqual(['a','b']);
    expect(analysisPrefix(tied,'a').map(c=>c.id)).toEqual(['a']);
    expect(analysisPrefix(tied,collections[2].id)).toEqual([]);
  });
  it('does not replace a deleted endpoint or include a later completed collection',()=>{
    expect(analysisPrefix(collections.slice(1),collections[0].id)).toEqual([]);
    const prepared=prepare(collections[1].id);
    expect(matchesPreparedScope(prepared,collections,collections[1].id,materials)).toBe(true);
    expect(matchesPreparedScope(prepared,collections.slice(1),collections[1].id,materials)).toBe(false);
  });
  it('requires the whole material set and a known current list before using a preparation',()=>{
    const prepared=prepare();
    expect(matchesPreparedScope(prepared,collections,collections[2].id,undefined)).toBe(false);
    expect(matchesPreparedScope(prepared,collections,collections[2].id,materials.slice(1))).toBe(false);
    expect(matchesPreparedScope(prepared,collections,collections[2].id,[...materials].reverse())).toBe(true);
  });
});


describe('multi-day Free Tier recovery reasons',()=>{
  it('distinguishes daily, minute, unknown and validation gates',()=>{
    expect(analysisExecutionReason('gemini_free_daily_limit')).toContain('태평양 시간 자정');
    expect(analysisExecutionReason('provider_minute_limit')).toContain('유한 대기');
    expect(analysisExecutionReason('analysis_legacy_budget_stop')).toContain('사전 작업량');
    expect(analysisExecutionReason('provider_rate_limited')).toContain('종류가 불명확');
    expect(analysisExecutionReason('analysis_resume_validation_required')).toContain('같은 계약의 재개는 차단');
    expect(analysisExecutionReason('analysis_resume_no_progress')).toContain('완료 결과가 늘지 않은');
  });
  it('retains the all-stages budget-only migration notice without calling it discovery-only',()=>{
    const view=preparedView({id:'p',state:'ready',input_digest:'fixed',consent_digest:'new',descriptor:{end_collection_id:'end',collection_ids:['end'],materials:[],transfer_digest:'t',items:[],profile:{provider:'synthetic',model:'synthetic',data_policy:'synthetic',transfer_scope:'fixed'},resume:{request_id:'failed',checkpoint_count:603,inherited_attempts:610,compatibility:{policy:'unchanged-graph-per-execution-budget-v1',reused_stage:'all_completed',source_contract:'old',target_contract:'new'}}}});
    expect(view.resume?.reusedStage).toBe('all_completed');
    expect(view.resume?.checkpointCount).toBe(603);expect(view.resume?.inheritedAttempts).toBe(610);
  });
});


describe('resume history eligibility is projected without preparing or consenting',()=>{
  it('shows the verified discovery and consolidation prefix separately from changed relation context',()=>{
    const prepared={...prepare(),resume:{requestId:'failed',checkpointCount:270,inheritedAttempts:273,compatibilityPolicy:'validated-discovery-consolidation-prefix-to-bounded-wire-v1',reusedStage:'discover_consolidate'}};
    const view:AnalysisView={preparation:{state:'ready',data:prepared},capacity:{state:'available'},executions:[],historyAvailable:true};
    const html=renderToStaticMarkup(createElement(AnalysisFlow,{collections,materials,view,refreshMaterials:()=>{},actions:{}}));
    expect(html).toContain('발견·후보 통합의 완료 prefix만 재사용');
    expect(html).toContain('이전 관계 검토 이후 결과는 재사용하지 않으며');
    expect(html).not.toContain('호환 검증된 발견 결과만 재사용합니다');
  });
  for(const blocked of [undefined,'analysis_resume_daily_wait','analysis_resume_validation_required','analysis_resume_request_fix_required']){
    it(`renders the correct resume action for ${blocked??'eligible'}`,()=>{
      const view:AnalysisView={executionAvailable:true,preparation:{state:'idle'},capacity:{state:'available'},historyAvailable:true,executions:[{id:'failed',status:'failed',createdAt:'2027-01-15T12:00:00Z',endCollectionId:collections[0].id,endCollectionLabel:'fixed',collectionCount:1,materialCount:0,checkpointCount:603,inheritedAttempts:610,calls:0,resumeBlockReason:blocked,resumeAfter:blocked==='analysis_resume_daily_wait'?'2027-01-16T00:00:00-08:00':undefined}]};
      let calls=0;
      const html=renderToStaticMarkup(createElement(AnalysisFlow,{collections,materials,view,refreshMaterials:()=>{},actions:{prepare:()=>{calls++;}}}));
      expect(html.includes('>이어서 분석 준비</button>')).toBe(!blocked);
      expect(html).toContain('보존된 완료 중간 결과');
      if(blocked)expect(html).toContain(analysisExecutionReason(blocked));
      if(blocked==='analysis_resume_daily_wait')expect(html).toContain('제공자 기준 갱신 시각');
      expect(calls).toBe(0);
    });
  }
});

 describe('provider recovery classification',()=>{
  it('gives configuration actions without confusing them with daily quota',()=>{
    expect(analysisExecutionReason('provider_authentication')).toContain('Key 설정');
    expect(analysisExecutionReason('provider_permission_denied')).toContain('접근 권한');
    expect(analysisExecutionReason('provider_model_unavailable')).toContain('API 버전');
    expect(analysisExecutionReason('provider_certificate_error')).toContain('인증서 검증');
    expect(analysisExecutionReason('gemini_free_state_unavailable')).toContain('초기화하지 말고');
    expect(analysisExecutionReason('analysis_request_schema_invalid')).toContain('외부 호출 없이');
  });
  it('distinguishes explicit recoverable server responses, unknown codes and interrupted calls',()=>{
    for(const status of [500,502,504]){
      const message=analysisExecutionReason(`provider_server_error_h${status}`);
      expect(message).toContain(`HTTP ${status}`);expect(message).toContain('제한된 재시도');
      expect(message).toContain('새 준비·동의');
    }
    expect(analysisExecutionReason('provider_http_h413')).toContain('자동 재전송하지 않습니다');
    expect(analysisExecutionReason('analysis_call_outcome_unknown')).toContain('제공자 처리 결과는 미확인');
    expect(analysisExecutionReason('provider_incomplete')).toContain('같은 계약으로 재개하지 않습니다');
    expect(analysisExecutionReason('invalid_evidence_quote')).toContain('주장·근거 참조·인용');
    expect(analysisExecutionReason('provider_minute_limit')).toContain('실제 attempt 수');
  });
 });

describe('pinned one-request diagnostic projection and notices',()=>{
  it('keeps the original resume blocked while exposing a distinct diagnostic preparation',()=>{
    const view:AnalysisView={executionAvailable:true,preparation:{state:'idle'},capacity:{state:'available'},historyAvailable:true,executions:[{id:'failed',status:'failed',createdAt:'2026-10-23T00:00:00Z',endCollectionId:collections[0].id,endCollectionLabel:'fixed',collectionCount:1,materialCount:0,checkpointCount:270,calls:7,resumeBlockReason:'analysis_resume_request_fix_required',diagnosticAvailable:true}]};
    const html=renderToStaticMarkup(createElement(AnalysisFlow,{collections,materials,view,actions:{prepare:()=>{}},refreshMaterials:()=>{}}));
    expect(html).toContain('1회 진단 준비');
    expect(html).not.toContain('이어서 분석 준비');
    expect(html).toContain('원인 수정과 Checkpoint 호환 검증 전');
  });
  it('projects a fresh consent binding the exact outgoing wire and single-call purpose',()=>{
    const view=preparedView({id:'new',state:'ready',input_digest:'original',consent_digest:'new-consent',descriptor:{end_collection_id:'end',collection_ids:['end'],materials:[],transfer_digest:'exact-preview',diagnostic:{wire_digest:'a'.repeat(64),wire_bytes:121299},items:[],profile:{provider:'synthetic',model:'synthetic',data_policy:'synthetic',transfer_scope:'fixed'}}});
    expect(view.version).toBe('new-consent');
    expect(view.diagnostic).toEqual({wireDigest:'a'.repeat(64),wireBytes:121299});
    expect(view.transferDigest).toBe('exact-preview');
  });
  it('describes HTTP200 diagnosis separately from a completed analysis and blocks repeated diagnosis',()=>{
    expect(analysisExecutionReason('analysis_diagnostic_response_received')).toContain('후속 분석·새 중간 결과·최종 결과 저장은 하지 않았');
    expect(analysisExecutionReason('analysis_diagnostic_response_received')).toContain('원인이 해결된 것은 아닙니다');
    expect(analysisExecutionReason('analysis_diagnostic_wire_mismatch')).toContain('전송 전에 차단');
    expect(analysisExecutionReason('analysis_diagnostic_consumed')).toContain('두 번째 진단은 허용하지 않습니다');
  });
});

 it('pinned one-request diagnostic never advertises retry or ordinary resume after a server or quota error',()=>{
  for(const code of ['provider_temporarily_unavailable','provider_server_error_h500','provider_minute_limit','provider_rate_limited','gemini_free_daily_limit','provider_authentication']){
    const text=analysisExecutionReason(code,true);
    expect(text).toContain('1회 진단');expect(text).not.toContain('재시도 후');expect(text).not.toContain('수동 재개할 수');
    expect(text).toContain('별도 결정');
  }
 });
