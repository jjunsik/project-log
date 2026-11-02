import type {Collection} from '../model';
import {analysisPrefix, completedAnalysisCollections} from '../analysis';
import type {AnalysisExecution, AnalysisView, ExecutionStatus, MaterialInfo, PreparedAnalysis} from '../analysis';
import {collectionCaption} from '../model';

export const projectId='synthetic-project';
export const collections:Collection[]=[1,2,3].map(n=>({
  id:`synthetic-collection-${n}`,title:`합성 수집 ${n}`,state:'completed',created_at:`2026-10-${10+n}T09:00:00Z`,
  finished_at:`2026-10-${10+n}T09:01:00Z`,retry_of:null,summary:{},snapshot:{branch:'synthetic-branch'},
}));
export const materials:MaterialInfo[]=[
  {id:'synthetic-material-1',filename:'[합성] 검토 노트.md',size_bytes:100,added_at:'2026-10-14T09:00:00Z'},
  {id:'synthetic-material-2',filename:'[합성] 설명 문서.docx',size_bytes:200,added_at:'2026-10-14T09:01:00Z'},
  {id:'synthetic-material-3',filename:'[합성] 제외 자료.txt',size_bytes:50,added_at:'2026-10-14T09:02:00Z'},
];
export function prepare(endCollectionId=collections.at(-1)!.id):PreparedAnalysis {
  return {id:`synthetic-preparation-${endCollectionId}`,version:'synthetic-version-1',transferDigest:'synthetic-transfer-digest',endCollectionId,
    collectionIds:analysisPrefix(collections,endCollectionId).map(c=>c.id),materialIds:materials.map(m=>m.id),
    provider:'Gemini Developer API · 합성 안내',model:'합성 모델 · 실제 모델 아님',
    policy:'화면 검토용 합성 정책입니다. 서비스의 실제 데이터 처리 정책이 아니며 외부 전송은 발생하지 않습니다.',
    transferScope:'합성 텍스트와 문서의 처리 가능한 부분만 포함한 예시입니다. 제외된 합성 자료의 본문은 포함하지 않습니다.',
    items:[{id:'synthetic-text',name:materials[0].filename,outcome:'ready',description:'합성 텍스트 전체를 준비한 예시입니다.'},
      {id:'synthetic-docx',name:materials[1].filename,outcome:'partial',description:'합성 본문은 포함하고 첨부 객체 내부는 미처리인 예시입니다.'},
      {id:'synthetic-excluded',name:materials[2].filename,outcome:'excluded',description:'지원 형식으로 처리하지 못해 본문을 제외한 합성 사례입니다.'}],
  };
}
export function execution(status:ExecutionStatus,prepared=prepare()):AnalysisExecution {
  const end=completedAnalysisCollections(collections).find(c=>c.id===prepared.endCollectionId)!;
  return {id:'synthetic-execution',status,createdAt:'2026-10-15T09:00:00Z',endCollectionId:prepared.endCollectionId,endCollectionLabel:collectionCaption(end),
    collectionCount:prepared.collectionIds.length,materialCount:materials.length,
    phase:status==='running'?'개발 경험 복원 중 · 합성 진행':status==='queued'?'Worker 인계 대기 · 합성 진행':'실행 정리 · 합성 상태',
    calls:status==='queued'?0:2,message:status==='failed'?'합성 오류로 실패했습니다. 이전 성공 결과는 유지됩니다.':status==='cancelled'?'합성 실행의 취소가 완료됐습니다.':undefined};
}
export const scenarios={durable:'최종화 실패·독립 보존 이력',idle:'범위 선택부터 시작',ready:'자료 준비·전송 동의',preparing:'로컬 입력 준비 중',empty:'전송 가능한 입력 없음',stale:'접수 전 자료 변경',unknown:'접수 응답 유실',busy:'다른 프로젝트 분석 중',queued:'분석 대기',running:'분석 중',cancel_requested:'취소 정리 중',completed:'분석 완료',failed:'분석 실패',cancelled:'취소 완료',interrupted:'서버 재시작 후 실패',unavailable:'실제 화면의 미연동 상태',noCollections:'완료 수집 없음'};
export type Scenario=keyof typeof scenarios;
export function scenarioView(scene:Scenario):AnalysisView {
  const view:AnalysisView={preparation:{state:'idle'},capacity:{state:'available'},historyAvailable:true,executions:[]};
  if(scene==='unavailable')return {preparation:{state:'unavailable'},capacity:{state:'unknown'},historyAvailable:false,executions:[]};
  if(scene==='ready'||scene==='stale'||scene==='empty')view.preparation={state:scene,data:scene==='empty'?{...prepare(),items:prepare().items.filter(i=>i.outcome==='excluded')}:prepare()};
  if(scene==='preparing'||scene==='unknown')view.preparation={state:scene};
  if(scene==='busy')view.capacity={state:'busy',projectName:'합성 두 번째 프로젝트'};
  if(['queued','running','cancel_requested','completed','failed','cancelled','interrupted'].includes(scene)) {
    view.executions=[execution(scene==='interrupted'?'failed':scene as ExecutionStatus)];
    if(scene==='failed'||scene==='interrupted')view.executions[0].checkpointCount=2;
    if(scene==='interrupted')view.executions[0].message='서버 재시작으로 미완료 분석이 실패 처리됐습니다. 자동 재개하지 않습니다. · 합성 사례';
  }
  if(scene==='durable')view.executions=[{...execution('failed'),id:'synthetic-durable',eventCount:2,checkpointCount:4,message:'최종화 크기 검사 실패 · 합성 사례'}];
  return view;
}

const coverage={collections:collections.map(c=>c.id),input_entries:3,discovery_chunks:1,excluded:[],limitations:[],chunks:[{id:'synthetic-chunk',references:['synthetic:note'],offset:0}]};
const partialCoverage={...coverage,analysis_status:'partial',recovered_unit_ids:['recovered'],unresolved:[{id:'failed-relation',task:'reconcile',code:'analysis_relation_attempts_exhausted',chunk_ids:['synthetic-chunk'],seed_ids:['seed_000000','seed_000001']}],consolidation:{seeds:[{seed_id:'seed_000000',title:'검증된 합성 후보',evidence_ids:['synthetic-chunk']},{seed_id:'seed_000001',title:'관계 미해결 합성 후보',evidence_ids:['synthetic-chunk']}]}};
export const runs=[{id:'synthetic-success-new',created_at:'2026-10-14T09:00:00Z',model:'synthetic-model',mode:'isolated',candidate_count:1,coverage},
  {id:'synthetic-success-old',created_at:'2026-10-13T09:00:00Z',model:'synthetic-model',mode:'isolated',candidate_count:1,coverage},
  {id:'synthetic-partial',created_at:'2026-10-12T09:00:00Z',model:'synthetic-model',mode:'isolated',candidate_count:1,coverage:partialCoverage}];
export function fixtureResponse(path:string):unknown {
  if(path.includes('/analysis/requests/synthetic-durable/events?'))return {total:2,next_after:2,historical_detail_unknown:false,events:[{event_seq:1,kind:'finalization_failed',phase:'result_size',call_seq_observed:4,created_at:'2026-10-28T12:00:00Z',data:{code:'analysis_result_too_large',stage:'result_size',condition:'serialized_bytes_gt_limit',cause_status:'CONFIRMED',measured_bytes:33554433,limit_bytes:33554432}},{event_seq:2,kind:'execution_finished',phase:'terminal',call_seq_observed:4,created_at:'2026-10-28T12:00:01Z',data:{state:'failed',partial:true}}]};
  if(path.includes('/analysis/requests/synthetic-durable/events/1'))return {event_seq:1,data:{condition:'serialized_bytes_gt_limit',measured_bytes:33554433,limit_bytes:33554432}};
  if(path.includes('/analysis/requests/synthetic-durable/partial?'))return {total:1,offset:0,stage_verified:true,complete_analysis:false,unresolved_relations:[{id:'relation-1',task:'reconcile',code:'candidate_relation_grounding_merge_refs_d0_e0_x1_u1'}],graph_summary:{status:'partial',unresolved_total:1,unresolved:[{id:'relation-1',task:'reconcile',code:'candidate_relation_grounding_merge_refs_d0_e0_x1_u1'}],stop_code:null,pending_work:null},candidates:[{id:'certified-synthetic',title:'필수 검증을 마친 합성 후보',rationale:'실제 성과가 아닌 합성 검증 사례',limitations:['일부 관계 미해결 · 합성 사례'],claims:[{statement:'보존된 합성 인용',status:'EVIDENCED',citations:[{evidence_id:'synthetic-chunk',quote:'보존된 합성 인용',locator:null}]}]}],chunks:[{id:'synthetic-chunk',references:['synthetic:note']}]};
  if(path===`/api/projects/${projectId}/materials`)return materials;
  if(path===`/api/projects/${projectId}/experiences`)return {runs,transfer_enabled:false,execution_available:false};
  if(path.includes('/evidence?'))return {reference:'synthetic:note',source:{kind:'user_material',filename:materials[0].filename,added_at:materials[0].added_at},body:'합성 입력을 사용한 화면 검토입니다.',reason:null,limitations:[],media:[]};
  const run=runs.find(r=>path===`/api/projects/${projectId}/experiences/${r.id}`);
  if(run)return {...run,input_digest:'synthetic-input',result:{coverage:run.coverage,candidates:[{id:'synthetic-candidate',title:run.id===runs[0].id?'최근 합성 개발 경험 후보':'이전 합성 개발 경험 후보',category:'potential',rationale:'화면 구조 검토를 위한 합성 설명입니다.',
    claims:[{section:'background',statement:'합성 입력을 사용한 화면 검토입니다.',status:'EVIDENCED',citations:[{evidence_id:'synthetic-chunk',quote:'합성 입력을 사용한 화면 검토입니다.',locator:null}]},
      {section:'result',statement:'UNKNOWN: 실제 성과를 나타내는 데이터가 아닙니다.',status:'UNKNOWN',citations:[]}],evidence_ids:['synthetic:note'],limitations:['실제 사용자 자료·AI 품질 검증 결과가 아닙니다.'],grounding_notes:[],review_status:'unreviewed'}]}};
  throw new Error('합성 미리보기에서 제공하지 않는 조회입니다.');
}
