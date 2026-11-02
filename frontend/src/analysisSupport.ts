const executionErrors:Record<string,string>={analysis_result_invalid:'최종 처리 검증에서 실패했습니다. 과거 실행에 세부 검사 기록이 없으면 정확한 조건은 UNKNOWN입니다. 실행 상세·보존된 중간 결과를 확인하세요.',analysis_partial:'검증된 부분 결과와 미해결 항목을 저장했습니다. 전체 분석 성공이 아닙니다. 저장된 분석 결과에서 확인하세요.',analysis_capacity_full:'다른 분석이 진행 중입니다. 종료 후 다시 요청하세요. 대기열에는 추가되지 않았습니다.',analysis_input_changed:'준비 이후 자료가 변경됐습니다. 다시 준비하고 새로 동의하세요.',analysis_preparation_lost:'준비 입력이 만료되거나 서버가 재시작됐습니다. 다시 준비하고 동의하세요.',failed_server_interrupted:'서버 재시작으로 실패했습니다. 자동 재개하지 않습니다.',failed_server_stopped:'서버 종료로 실패했습니다. 자동 재개하지 않습니다.',provider_network_error:'외부 통신 오류로 실패했습니다. 당시 세부 유형과 응답 수신 단계는 확인할 수 없습니다.'};
const providerKinds:Record<string,string>={
  connect_timeout:'연결 제한시간 초과',read_timeout:'응답 읽기 제한시간 초과',write_timeout:'요청 쓰기 제한시간 초과',pool_timeout:'연결 풀 대기 제한시간 초과',timeout:'통신 제한시간 초과',
  connect_error:'연결 오류',read_error:'응답 읽기 오류',write_error:'요청 쓰기 오류',close_error:'연결 정리 오류',local_protocol:'클라이언트 Protocol 오류',remote_protocol:'상대측 Protocol 오류',protocol_error:'Protocol 오류',decoding_error:'응답 decoding 오류',
  stream_consumed:'이미 소비된 스트림 접근',stream_closed:'닫힌 스트림 접근',response_not_read:'아직 읽지 않은 응답 접근',request_not_read:'아직 읽지 않은 요청 접근',stream_error:'스트림 상태 오류',http_error:'HTTP 처리 오류',
  os_timeout:'OS 제한시간 초과',os_error:'OS 오류',invalid_json:'JSON 파싱 실패',validation_error:'구조화 응답 검증 실패',output_parser:'구조화 응답 파싱 실패',invalid_response:'응답 계약 위반',unexpected_error:'SDK 또는 내부 처리의 예상하지 못한 오류',
};
const providerStages:Record<string,string>={request:'요청 구성',setup:'호출 초기화',wire:'전송 입력 확인',quota:'로컬 호출량 예약',gate:'실행 동의·호출 승인',transport:'HTTP transport 호출 진입',headers:'응답 헤더 확인',body_wait:'응답 본문 읽기 시작',body_partial:'응답 본문 일부 수신',body_complete:'응답 본문 수신 완료',envelope:'응답 envelope 검사',quota_usage:'로컬 토큰 사용량 보정',sdk:'SDK 처리',validation:'최종 구조화 응답 검증',response_close:'응답 자원 정리',client_close:'SDK client 정리',transport_close:'transport 정리',rate_limit_body:'HTTP 429 상세 확인',unknown:'미확인'};
export function analysisExecutionReason(code?:string|null,singleRequest=false,requestContractChanged=false):string|undefined {
  if(requestContractChanged && code?.startsWith('provider_invalid_request'))return '이전 요청 계약에서 Gemini HTTP 400으로 실패했습니다. 구체적인 거절 사유는 UNKNOWN입니다. 새 요청 계약으로 완료 중간 결과를 재검증해 정상 분석을 준비할 수 있습니다. 새 Preview·동의가 필요하며 실제 Gemini 수락을 보장하지 않습니다. 진단 기회는 다시 열리지 않습니다.';
  if(!code)return undefined;
  if(singleRequest){
    const server=/^provider_server_error_h(500|502|504)$/.exec(code);
    if(server || code==='provider_temporarily_unavailable')return `Gemini HTTP ${server?.[1]??'503'} 응답으로 1회 진단을 종료했습니다. 자동 재시도·후속 분석은 실행하지 않습니다. 진단 기회를 다시 열지 않으며 일반 분석 재개는 별도 결정이 필요합니다.`;
    const limited:Record<string,string>={provider_minute_limit:'분당 요청·토큰 한도',provider_rate_limited:'종류가 불명확한 제공자 429 한도',gemini_free_daily_limit:'일일 할당량',provider_authentication:'Gemini 인증 오류'};
    if(limited[code])return `${limited[code]}로 1회 진단을 중단했습니다. 실제 호출 진입 수는 이력에서 확인하세요. 자동 재시도·기회 복원은 하지 않으며 원인 확인과 별도 결정이 필요합니다.`;
  }
  const diagnostic:Record<string,string>={analysis_diagnostic_unavailable:'운영자가 고정한 해당 HTTP 400 실패 요청만 1회 진단할 수 있습니다. 일반 재개 차단은 유지됩니다.',analysis_diagnostic_consumed:'이미 진단/재개 접수된 대상입니다. 두 번째 진단은 허용하지 않습니다.',analysis_diagnostic_wire_mismatch:'기존 실패 요청과 실제 발신 payload가 달라 전송 전에 차단했습니다. 다른 요청으로 대체하지 않습니다.',analysis_diagnostic_response_received:'1회 진단에서 HTTP 200 구조화 응답을 수신하고 종료했습니다. 후속 분석·새 중간 결과·최종 결과 저장은 하지 않았으며 기존 400 원인이 해결된 것은 아닙니다.',analysis_diagnostic_stopped:'1회 진단 이후의 분석 호출·일반 재개는 차단됩니다. 원인 확인과 별도 결정이 필요합니다.'};
  if(diagnostic[code])return diagnostic[code];
  const configuration:Record<string,string>={provider_authentication:'Gemini API Key가 유효하지 않거나 인증에 실패했습니다. 운영자 Key 설정을 확인하세요. 다시 요청할 때 새 준비·동의가 필요합니다.',provider_permission_denied:'Gemini 접근 권한이 없습니다. API 사용 권한·프로젝트·지역 및 Free Tier 설정을 확인하세요.',provider_model_unavailable:'Gemini 모델 또는 API 경로에 접근할 수 없습니다. 검토된 모델 이름·지원 API 버전과 접근 권한을 확인하세요.',provider_certificate_error:'Gemini TLS 인증서 검증에 실패했습니다. 로컬 인증서·네트워크 설정을 확인하세요. 인증서 검증을 끄지 않습니다.',ai_not_configured:'운영자 모델·Key·Free Tier 설정을 확인하세요.',operator_key_unavailable:'운영자 Key 파일의 존재·소유자·권한을 확인하세요.',gemini_free_state_unavailable:'로컬 호출량 기록을 안전하게 확인·보존할 수 없어 중단했습니다. 기록을 초기화하지 말고 파일·권한·저장 공간을 확인하세요.',gemini_free_request_limit:'단일 요청의 보수적 토큰 추정이 분당 한도를 초과합니다. 기다리는 것으로 해결되지 않으며 요청 크기 검토가 필요합니다.',gemini_free_tokens_unknown:'비텍스트 입력의 토큰량을 안전하게 사전 산정할 수 없어 중단했습니다. 완료 중간 결과는 보존됩니다.',analysis_request_schema_invalid:'전송 전 응답 Schema의 Gemini 요청 계약 위반을 확인했습니다. 외부 호출 없이 중단했으며 요청 수정·호환 검증이 필요합니다.',analysis_request_too_large:'로컬 요청 크기 제한을 초과해 전송 전에 중단했습니다. 요청 수정·호환 검증이 필요합니다.',provider_blocked:'Gemini가 입력을 차단했습니다. 입력·제공자 안전 정책을 확인하세요. 자동 재전송하지 않으며 완료 중간 결과는 보존됩니다.',provider_incomplete:'Gemini 응답이 정상 종료되지 않았습니다. 불완전한 결과는 저장하지 않으며 원인 확인 전 같은 계약으로 재개하지 않습니다.',provider_response_too_large:'응답 크기 제한을 초과했습니다. 해당 결과를 저장하지 않으며 요청·출력 범위를 확인해야 합니다.',analysis_call_outcome_unknown:'호출 진입 후 종료·중단되어 제공자 처리 결과는 미확인입니다. 응답·완료 결과를 추정하거나 호출량을 환급하지 않습니다.'};
  if(configuration[code])return `${configuration[code]} 자동 재전송은 하지 않습니다.`;
  if(code.startsWith('analysis_request_contract_limit_'))return '로컬 Gemini 요청의 크기·Schema 복잡도 또는 필수 문맥 한도를 초과해 해당 단위를 전송 전에 차단했습니다. 제공자 HTTP 400과 구분하며 완료 중간 결과·출처는 보존합니다. 전체 실행 상태와 상세 측정값을 확인하세요. 검토된 분할로도 표현할 수 없는 최소 단위는 미해결로 남깁니다.';
  if(['invalid_unknown_claim','unsupported_claim','invalid_evidence_reference','invalid_media_page','invalid_evidence_quote'].includes(code) || code.startsWith('invalid_discovery_reference_'))return '응답의 주장·근거 참조·인용 검증에 실패했습니다. 해당 결과는 저장하지 않으며 완료 중간 결과는 보존합니다. 원인 수정·호환 검증 전 같은 계약으로 재개하지 않습니다.';
  const server=/^provider_server_error_h(500|502|504)$/.exec(code);
  if(server)return `Gemini HTTP ${server[1]} 서버 오류로 제한된 재시도 후 중단했습니다. 완료 중간 결과와 호출 이력은 보존되며 나중에 새 준비·동의로 수동 재개할 수 있습니다.`;
  const http=/^provider_http_h([1-5][0-9]{2})$/.exec(code);
  if(http)return `Gemini HTTP ${http[1]} 요청 실패입니다. 구체적 원인은 미확인이며 자동 재전송하지 않습니다. 제공자·요청 설정을 확인하세요. 완료 중간 결과는 보존됩니다.`;
  const badRequests:Record<string,string>={provider_invalid_request:'제공자의 구체적 거절 사유는 확인할 수 없습니다.',provider_invalid_request_schema_depth:'제공자가 응답 Schema 깊이 제한을 보고했습니다.',provider_invalid_request_schema_complexity:'제공자가 응답 Schema의 상태 수·분기 복잡도를 보고했습니다.',provider_invalid_request_request_size:'제공자가 요청 크기 제한을 보고했습니다.'};
  if(badRequests[code])return `Gemini HTTP 400 요청 거절입니다. ${badRequests[code]} 일일 할당량이나 모델 응답 검증 오류와 구분하며, 원인 수정·호환 검증 전 재개하지 않습니다. 완료 중간 결과는 보존합니다.`;
  if(code==='candidate_relation_conflict')return '전역 검토의 병합·분리 관계가 서로 모순돼 중단했습니다. 임의로 합치거나 자동 재호출하지 않으며 완료된 중간 결과는 보존합니다.';
  if(code==='analysis_relation_transition_consumed')return '이 고정 자료의 새 계약 실행이 이미 접수됐습니다. 해당 새 실행의 상태와 재개 조건을 확인하세요. 구 계약에서 전환 기회를 다시 부여하지 않습니다.';
  if(code==='candidate_relation_grounding_invalid')return '그룹 관계의 설명·제공 근거 연결이 유효하지 않아 중단했습니다. 당시 세부 위반 조건은 보존되지 않았습니다. 잘못된 응답은 저장하지 않습니다.';
  const groundingLabels=/^candidate_relation_grounding_(merge|split)_labels_m([0-9]+)_x([0-9]+)_d([0-9]+)$/.exec(code);
  if(groundingLabels)return `그룹 관계 근거 항목 검증 실패(${groundingLabels[1]==='merge'?'병합':'분리'}): 필요한 label 누락 ${groundingLabels[2]}개, 불필요한 label ${groundingLabels[3]}개, 중복 항목 ${groundingLabels[4]}개입니다. 해당 응답은 검증된 결과로 채택하지 않았으며 자동 재호출하지 않습니다.`;
  const groundingRefs=/^candidate_relation_grounding_(merge|split)_refs_d([0-9]+)_e([0-9]+)_x([0-9]+)_u([0-9]+)$/.exec(code);
  if(groundingRefs)return `그룹 관계 근거 연결 검증 실패(${groundingRefs[1]==='merge'?'병합':'분리'}): 중복 참조 ${groundingRefs[2]}개, 구성원 수 초과 참조 ${groundingRefs[3]}개, 허용 근거 범위 밖 참조 ${groundingRefs[4]}개, 근거 미연결 구성원 ${groundingRefs[5]}개입니다. 해당 응답은 검증된 결과로 채택하지 않았으며 자동 재호출하지 않습니다.`;
  const groundingBlank=/^candidate_relation_grounding_(merge|split)_reason_blank_n([0-9]+)$/.exec(code);
  if(groundingBlank)return `그룹 관계 설명 검증 실패(${groundingBlank[1]==='merge'?'병합':'분리'}): 공백뿐인 설명 ${groundingBlank[2]}개입니다. 해당 응답은 검증된 결과로 채택하지 않았으며 자동 재호출하지 않습니다.`;
  if(code==='candidate_relation_schema_invalid')return '전역 그룹 검토의 응답 구조가 유효하지 않아 중단했습니다. 잘못된 응답은 저장하지 않습니다.';
  const relation=/^candidate_relation_invalid_m([0-9]+)_x([0-9]+)_v([0-9]+)$/.exec(code);
  if(relation)return `전역 그룹 배정 검증 실패: 누락 ${relation[1]}개, 미제공 ID ${relation[2]}개, 범위 밖 label ${relation[3]}개입니다. 해당 응답은 검증된 결과로 채택하지 않았으며 자동 재호출하지 않습니다.`;
  if(code==='candidate_coverage_invalid')return '후보 통합에서 Seed 처리 관계가 일치하지 않아 중단했습니다. 당시 누락·중복·잘못된 ID의 구체적 유형은 보존되지 않았습니다. 완료된 중간 결과는 유지되며 자동 재호출하지 않습니다.';
  const coverage=/^candidate_coverage_invalid_m([0-9]{1,3})_x([0-9]{1,6})_v([0-9]{1,6})_n([0-9]{1,3})$/.exec(code);
  if(coverage)return `후보 통합 배정 검증 실패: 입력 Seed ${coverage[4]}개, 누락 ${coverage[1]}개, 제공되지 않은 ID ${coverage[2]}개, 범위 밖 그룹 label ${coverage[3]}개입니다. 해당 응답은 검증된 결과로 채택하지 않았으며 자동 재호출하지 않습니다.`;
  const coverageSchema=/^candidate_coverage_invalid_schema_n([0-9]{1,3})$/.exec(code);
  if(coverageSchema)return `후보 통합 응답 구조가 유효하지 않습니다(입력 Seed ${coverageSchema[1]}개). 해당 응답은 검증된 결과로 채택하지 않았으며 자동 재호출하지 않습니다.`;
  const limits:Record<string,string>={
    analysis_legacy_budget_stop:'이전 정책의 누적 예산·사전 작업량 검사로 중단된 이력입니다. 실제 attempt 수와 구분하세요. 보존 결과는 새 정책의 준비·동의와 호환 검증 후 재사용할 수 있습니다.',
    gemini_free_daily_limit:'일일 할당량에 도달했습니다. 완료 중간 결과는 보존됩니다. 태평양 시간 자정 갱신 이후 새 준비·동의로 수동 재개하세요. 한국 고정 시각으로 판단하지 않습니다.',
    provider_minute_limit:'분당 요청·토큰 한도로 중단했습니다. 유한 대기·재시도 정책을 적용하며 실제 attempt 수는 실행 이력에서 확인하세요. 완료 결과는 보존되며 한도 확인 후 수동 재개할 수 있습니다.',
    provider_rate_limited:'제공자 429 한도의 종류가 불명확해 자동 재전송하지 않았습니다. AI Studio 한도를 확인한 뒤 수동 재개하세요.',
    provider_temporarily_unavailable:'Gemini 503의 제한된 재시도 후 중단했습니다. 완료 중간 결과를 보존하며 나중에 새 동의로 수동 재개할 수 있습니다.',
    analysis_budget_exceeded:'이번 실행의 유한한 실제 attempt 한도에 도달했습니다. 완료 결과와 누적 이력은 보존되며 새 준비·동의로 이어서 진행할 수 있습니다.',
    analysis_resume_daily_wait:'일일 할당량 갱신을 기다려야 합니다. 중간 결과는 유지되며 자동 재개하지 않습니다.',
    analysis_resume_minute_wait:'분당 한도 직후의 반복 재개를 잠시 제한합니다. 한도를 확인하고 다시 준비하세요.',
    analysis_resume_no_progress:'같은 제공자 날짜에 완료 결과가 늘지 않은 실패가 반복돼 재개를 차단했습니다. 원인을 확인하고 다음 제공자 날짜에 다시 준비하세요.',
    analysis_relation_attempts_exhausted:'이 미완료 관계 요청의 전송 진입 2회 한도가 소진됐습니다. 새 실행·날짜 변경으로 초기화하지 않으며 완료 중간 결과는 보존합니다.',
    analysis_relation_budget_unknown:'이전 관계 호출의 계보·예산·응답 상태를 확인할 수 없어 재전송을 차단했습니다. 완료 중간 결과는 보존합니다.',
    analysis_relation_feedback_invalid:'승인된 조건부 재생성 규칙과 요청이 일치하지 않아 전송을 차단했습니다.',
    analysis_resume_validation_required:'응답·근거 또는 계약 검증 오류는 기다리는 것으로 해결되지 않습니다. 원인 수정과 호환 검증이 필요하며 같은 계약의 재개는 차단합니다.',
    analysis_resume_request_fix_required:'HTTP 400 거절 또는 로컬 요청 계약·크기 오류는 할당량 갱신을 기다리는 것으로 해결되지 않습니다. 실패 요청의 원인 수정과 Checkpoint 호환 검증 전 재개를 차단하며 완료 중간 결과는 보존합니다.',
  };
  if(limits[code])return limits[code];
  const recovery:Record<string,string>={analysis_resume_unavailable:'재개할 수 있는 완료 중간 결과가 없습니다. 새 분석을 준비하세요.',analysis_resume_consumed:'이미 재개 실행이 접수됐습니다. 실행 이력에서 상태를 확인하세요.',analysis_checkpoint_invalid:'저장된 중간 결과의 순서·무결성·실행 계약을 확인할 수 없습니다. 자동으로 다시 호출하지 않습니다. 새 분석을 준비하세요.',analysis_policy_changed:'모델·분석 또는 전송 계약이 달라 재개할 수 없습니다. 새 분석을 준비하세요.',analysis_budget_exceeded:limits.analysis_budget_exceeded};
  if(recovery[code])return recovery[code];
  if(executionErrors[code])return executionErrors[code];
  if(code.startsWith('analysis_result_')||code.startsWith('analysis_partial_result_')||code.startsWith('analysis_event_'))return `결과 또는 이력 보존 처리에서 실패했습니다. 상세 이력의 단계·검사 조건·측정값과 검증된 부분 결과를 확인하세요. (${code})`;
  const parsed=/^provider_(.+)_(request|setup|wire|quota|gate|transport|headers|body_wait|body_partial|body_complete|envelope|quota_usage|sdk|validation|response_close|client_close|transport_close|rate_limit_body|unknown)_h(unknown|[1-5][0-9]{2})$/.exec(code);
  if(parsed && providerKinds[parsed[1]]){
    const [,kind,stage,status]=parsed;
    return `${providerKinds[kind]}로 분석을 중단했습니다. 마지막 확인 경계: ${providerStages[stage]}. HTTP 상태: ${status==='unknown'?'미확인':status}. 자동 재전송하지 않았습니다.`;
  }
  return `처리 사유: ${code}`;
}

const reasons:Record<string,string>={
  technical_code_identifier:'구문 근거가 있는 코드 식별자는 보존했습니다.',
  bounded_account_relation:'명시적인 계정 관계의 정확한 값만 가명 처리했습니다.',
  nonessential_personal_field:'검토된 불필요 개인정보 필드를 전송 입력에서만 제외했습니다.',
  structured_person_name:'구조화된 이름을 일관된 별칭으로 처리했습니다.',
  email_address:'이메일을 분석용 별칭으로 처리했습니다.',
  technical_placeholder:'기술적 placeholder를 보존했습니다.',
  media_size_limit:'현재 AI 입력 준비의 원본 용량 제한을 넘었습니다. 업로드된 사본은 보존됩니다.',
  docx_tracked_changes_requires_review:'변경 추적이 있어 문서 전체 분석을 보류했습니다.',
  docx_text_only_layout_not_preserved:'추출한 XML 텍스트에는 배치 정보가 없습니다. 시각 보완 여부는 PDF 페이지 범위를 확인하세요.',
  docx_generated_numbering_unprocessed:'XML 텍스트의 자동 번호는 복원하지 않았습니다. 변환 PDF도 동일 재현을 보장하지 않습니다.',
  docx_image_layout_and_text_association_not_preserved:'개별 이미지와 XML 본문의 배치 관계는 보존되지 않습니다. 파생 PDF는 시각적 보완 근거입니다.',
  docx_rendering_fidelity_unverified:'DOCX 변환 PDF는 원본 Word의 배치·특수 기능과 동일한지 검증되지 않았습니다.',
  docx_embedded_attachments_unprocessed:'DOCX 첨부 객체 내부 내용은 분석하지 않았습니다.',
  docx_altchunk_unprocessed:'DOCX altChunk로 연결된 내용은 분석하지 않았습니다.',
  docx_images_or_attachments_unprocessed:'DOCX 이미지 또는 첨부를 처리하지 못했습니다.',
  docx_tracked_changes_present:'변경 추적이 있는 문서입니다.',
  docx_external_hyperlinks_not_loaded:'외부 hyperlink는 불러오지 않고 표시 텍스트만 보존했습니다.',
  docx_renderer_unavailable:'승인된 로컬 DOCX 변환 Runtime 또는 서비스 구성을 준비할 수 없습니다.',
  docx_renderer_platform_unsupported:'이 환경에는 검증된 DOCX 변환 Runtime이 없습니다.',
  docx_embedded_objects_render_deferred:'첨부·OLE 객체가 있어 문서의 시각 변환을 보류했습니다.',
  docx_altchunk_render_deferred:'altChunk가 있어 문서의 시각 변환을 보류했습니다.',
  docx_word_format_render_deferred:'시각 변환을 검증하지 않은 Word XML 형식입니다.',
  docx_external_content_render_deferred:'외부 연결 자료를 불러오지 않기 위해 시각 변환을 보류했습니다.',
  docx_active_content_requires_review:'매크로·활성 콘텐츠 의심 요소가 있어 시각 변환을 보류했습니다.',
  docx_unsafe_package:'안전하게 변환할 수 없는 DOCX 내부 구조입니다.',
  docx_media_expansion_limit:'DOCX 내부 이미지의 압축 해제 용량 제한을 넘었습니다.',
  docx_render_expansion_limit:'DOCX 변환의 압축 해제 용량 제한을 넘었습니다.',
  docx_render_xml_limit:'DOCX 시각 변환의 XML 용량 제한을 넘었습니다.',
  docx_conversion_failed:'DOCX의 PDF 변환 결과를 확보하지 못했습니다.',
  invalid_docx:'DOCX의 안전한 텍스트 추출에 실패했습니다. 문서 손상으로만 단정할 수는 없습니다.',
  pdf_parse_failed:'PDF 구조를 확인하지 못해 페이지 분석에 사용하지 않았습니다.',
  pdf_encrypted_requires_review:'암호화된 PDF는 분석하지 않았습니다.',
  pdf_page_count_limit:'PDF 페이지 수가 로컬 처리 범위를 넘었습니다.',
  pdf_single_page_size_limit:'PDF 한 페이지가 분할 입력 용량 제한을 넘었습니다.',
  pdf_derivative_size_limit:'PDF 파생본의 처리·보존 용량 제한을 넘었습니다.',
  pdf_embedded_attachments_unprocessed:'PDF 첨부 파일 내부 내용은 분석하지 않았습니다.',
  pdf_article_threads_not_preserved:'PDF 분할본에는 페이지 간 article thread 구조가 없습니다. 원본 사본은 보존됩니다.',
  media_sandbox_unavailable:'안전한 로컬 문서 처리 격리를 적용할 수 없습니다.',
  media_resource_isolation_unavailable:'문서 처리 자원 제한·감시를 적용할 수 없습니다.',
  media_processing_failed:'격리된 문서 처리 실행에 실패했습니다.',
  media_processing_timeout:'문서 처리 시간 제한을 넘었습니다.',
  media_memory_limit:'문서 처리 메모리 제한을 넘었습니다.',
  media_scratch_limit:'문서 처리 임시 공간 제한을 넘었습니다.',
  native_media_requires_manual_review:'시각적 개인정보·기밀성 및 전송 권한은 별도 확인이 필요합니다.',
  native_media_content_not_locally_verified:'원본 이미지·PDF의 내용을 로컬에서 사실 검증한 것은 아닙니다.',
  suspected_secret:'과거 수집 정책의 Secret 의심 검사로 확보하지 않은 본문입니다. 현재 자동 탐지 결과가 아니며 복원하지 않습니다.',
  sensitive_path:'과거 수집 정책의 민감 경로 제외로 확보하지 않은 본문입니다. 현재 자동 탐지 결과가 아니며 복원하지 않습니다.',
  system_path:'서비스 Secret 전용 폴더 또는 Git 내부 저장소 접근은 제한됩니다.',
  commit_personal_headers_excluded:'커밋 작성자 이름·email 등 개인 header는 AI 입력에서 제외했습니다.',
};
export function analysisReason(code:string):string {
  const prefix='docx_visual_unprocessed:';
  if(code.startsWith(prefix))return `DOCX 시각적 내용 미처리: ${analysisReason(code.slice(prefix.length))}`;
  if(code.startsWith('docx_unsupported_media:'))return `개별 이미지로 분석하지 못한 DOCX 내부 자료: ${code.slice('docx_unsupported_media:'.length)}. 파생 PDF에서도 동일 재현을 보장하지 않습니다.`;
  return reasons[code] ?? `처리 한계 또는 제외 사유: ${code}`;
}
export interface MediaInfo {name:string;mime_type:string;sha256:string;use_for_analysis?:boolean;provenance?:{transform?:string;page_start?:number;page_end?:number;document_pages?:number;original_sha256?:string;renderer_version?:string}}
export function mediaScope(media:MediaInfo):string {
  if(media.use_for_analysis===false)return media.provenance?.transform==='original-docx'?'원본 DOCX 사본 · 직접 AI 입력에는 사용하지 않음':'원본 PDF 사본 · 직접 AI 입력에는 사용하지 않음';
  const p=media.provenance;
  if(p?.page_start!==undefined)return `${p.transform==='docx-to-pdf'?'DOCX에서 변환한 PDF':'PDF'} · ${p.page_start}–${p.page_end} / 전체 ${p.document_pages}페이지 · 분석 입력`;
  return '개별 이미지 · 분석 입력';
}
export const uploadAnalysisNotice='파일 추가는 원본 사본 보존입니다. AI 분석 지원·완료를 뜻하지 않습니다. DOCX는 텍스트·내부 이미지와 로컬 PDF 변환으로 분석 입력을 준비하며, 변환 실행 실패·변경 추적·첨부·외부 연결·특수 기능으로 일부 또는 전체 분석이 보류될 수 있습니다. PDF·이미지도 구조·페이지·용량·보안 제한으로 처리하지 못한 내용이 있을 수 있으며 결과에서 범위와 사유를 확인할 수 있습니다.';
