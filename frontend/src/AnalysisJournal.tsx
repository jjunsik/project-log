import {useEffect, useRef, useState} from 'react';
import {useData} from './data';
import {date} from './ui';
import {analysisExecutionReason} from './analysisSupport';
import {executionLabels,isActiveAnalysis,type ExecutionStatus} from './analysis';

type Event={event_seq:number;kind:string;phase:string;call_seq_observed:number;created_at:string;data:{code?:string;stage?:string;condition?:string;cause_status?:string;measured_bytes?:number;limit_bytes?:number;[key:string]:unknown}};
type Claim={statement:string;status:string;citations:{evidence_id:string;quote:string|null;locator:string|null}[]};
type FailedUnit={id:string;task:string;code:string};
type Partial={total:number;offset:number;complete_analysis:false;stage_verified:boolean;candidates:{id:string;title:string;rationale:string;claims:Claim[];limitations:string[]}[];chunks:{id:string;references:string[]}[];unresolved_relations?:FailedUnit[];graph_summary?:{status:string;unresolved_total:number;unresolved:FailedUnit[];stop_code:string|null;pending_work:unknown}|null};
const names:Record<string,string>={commit_outcome_resolved:'저장 결과 재확인',pass_started:'검토 시작',partition:'검증된 관계와 후보 연결',candidate_started:'후보 필수 단계 검토 시작',candidate_validated:'필수 단계 검증 후보 보존',unit_started:'분석 단위 시작',unit_validated:'중간 응답 검증 통과',unit_reused:'검증된 응답 재사용',unit_failed:'개별 검증 실패',unit_isolated:'실패 단위 격리',unit_recovered:'실패 단위 복구',attempt_retry_scheduled:'허용된 재시도·재생성',pass_finished:'검토 구간 종료',execution_paused:'전송 중단·미처리 범위 보존',graph_finished:'독립 분석·복구 처리 종료',provider_validation_failed:'Provider 응답 구조 검증 실패',finalization_started:'결과 최종화 시작',finalization_failed:'최종화 오류 보존',execution_finished:'실행 종료',execution_interrupted:'실행 중단'};
function Evidence({path}:{path:string}) {
  const [retry,setRetry]=useState(0);
  const {data,error,loading}=useData<{body:string|null;source:unknown}>(path,retry);
  return <section className="card"><h4>고정 입력의 보존 근거</h4>{loading&&<p role="status">보존 근거를 불러오는 중…</p>}{error&&<><p role="alert">{error}</p><button onClick={()=>setRetry(n=>n+1)}>근거 다시 조회</button></>}{!loading&&data&&<><pre className="experience-body">{data.body??'텍스트 본문 없음 · 비텍스트/제외 자료의 원래 범위를 확인하세요.'}</pre><details><summary>출처</summary><pre className="experience-body">{JSON.stringify(data.source,null,2)}</pre></details></>}</section>;
}
function EventDetail({path}:{path:string}) {
  const [retry,setRetry]=useState(0);
  const {data,error,loading}=useData<unknown>(path,retry);
  return <>{loading&&<p role="status">이벤트 근거를 불러오는 중…</p>}{error&&<><p role="alert">{error}</p><button onClick={()=>setRetry(n=>n+1)}>이벤트 다시 조회</button></>}{!loading&&data&&<pre className="experience-body">{JSON.stringify(data,null,2)}</pre>}</>;
}
export default function AnalysisJournal({project,request,count,execution,close}:{project:string;request:string;count:number;execution:{status:ExecutionStatus;createdAt:string};close:()=>void}) {
  const panel=useRef<HTMLElement|null>(null),[retry,setRetry]=useState(0);
  const panelId=`analysis-journal-${request}`,active=isActiveAnalysis(execution.status)||execution.status==='checking';
  useEffect(()=>{panel.current?.scrollIntoView({block:'start'});panel.current?.focus({preventScroll:true});},[]);
  const [after,setAfter]=useState(0),[offset,setOffset]=useState(0),[selected,setSelected]=useState<number|null>(null),[reference,setReference]=useState('');
  const base=`projects/${project}/analysis/requests/${request}`;
  const history=useData<{events:Event[];total:number;next_after:number;historical_detail_unknown:boolean}>(`${base}/events?after=${after}&limit=20`,`${count}:${retry}`);
  const partial=useData<Partial>(`${base}/partial?offset=${offset}&limit=5`,`${count}:${retry}`);
  const historyData=history.loading?undefined:history.data,partialData=partial.loading?undefined:partial.data;
  return <section ref={panel} id={panelId} tabIndex={-1} role="region" aria-labelledby={`${panelId}-title`} className="card analysis-journal"><header><h3 id={`${panelId}-title`}>실행 상세·보존된 부분 결과</h3><div><button disabled={(history.loading||partial.loading)&&!history.error&&!partial.error} onClick={()=>setRetry(n=>n+1)}>다시 조회</button><button onClick={close}>이력 닫기</button></div></header>
    <p>선택 실행: {date(execution.createdAt)} · {executionLabels[execution.status]} · {request}</p>
    {history.loading&&<p role="status">실행 이력을 불러오는 중…</p>}
    {partial.loading&&<p role="status">검증된 부분 결과를 불러오는 중…</p>}
    <p className="notice">운영 실행 기록입니다. 과거 개발 과정의 대안·판단 기록과 구분합니다. 후보는 사용자 미검토이며 사실 승인이 아닙니다. 전체 진행 상태·미해결 항목과 함께 검토하세요.</p>
    {history.error&&<p role="alert">{history.error}</p>}
    {!active&&historyData?.historical_detail_unknown&&<p className="notice">이 과거 실행의 세부 검사·복구 이력은 보존되지 않아 UNKNOWN입니다. 기존 호출 이력과 완료 Checkpoint를 대신 추측해 채우지 않습니다.</p>}
    {partialData&&<section><h4>잔여 미해결·미처리 범위</h4>{partialData.graph_summary?<><p>독립 분석 종료 기록: {partialData.graph_summary.status} · 미해결 {partialData.graph_summary.unresolved_total}개</p>{partialData.graph_summary.stop_code&&<p>{analysisExecutionReason(partialData.graph_summary.stop_code)}</p>}<ul>{partialData.graph_summary.unresolved.map(unit=><li key={unit.id}>{unit.task} · {analysisExecutionReason(unit.code)}</li>)}</ul>{partialData.graph_summary.pending_work!=null&&<details><summary>중단 위치·미처리 범위</summary><pre className="experience-body">{JSON.stringify(partialData.graph_summary.pending_work,null,2)}</pre></details>}</>:<p>{active?'분석 진행 중입니다. 독립 분석의 종료 범위는 아직 확정되지 않았습니다.':'독립 분석의 종료 범위는 UNKNOWN입니다. 아래 보존 이력에서 실제 중단·검증 단계를 확인하세요.'}</p>}{!!partialData.unresolved_relations?.length&&<p className="notice">검증된 현재 연결에도 미해결 관계 {partialData.unresolved_relations.length}개가 있습니다. 후보를 독립 경험으로 확정하지 않습니다.</p>}</section>}
    {partialData&&<><h4>필수 단계 검증 후보 {partialData.total}개</h4>{!partialData.total&&<p>{active?'아직 필수 단계 검증을 마친 후보가 없습니다. 진행 중인 후보는 완성된 경험으로 표시하지 않습니다.':'지금 제공할 수 있는 검증된 완성 후보가 없습니다. 완료 중간 근거와 실패 이력은 별도로 보존됩니다.'}</p>}{partialData.stage_verified&&partialData.candidates.map(candidate=><details key={candidate.id}><summary>{candidate.title} · 사용자 미검토</summary><p>{candidate.rationale}</p>{candidate.limitations.map((text,i)=><p className="notice" key={i}>{text}</p>)}{candidate.claims.map((claim,i)=><div className="experience-claim" key={i}><span className="badge">{claim.status==='EVIDENCED'?'본문 인용 근거 있음':claim.status==='INFERRED'?'AI 해석 · 미확인':'UNKNOWN'}</span><p>{claim.statement}</p>{claim.citations.map((citation,j)=><div key={j}>{citation.quote&&<blockquote>{citation.quote}</blockquote>}{citation.locator&&<p>{citation.locator}</p>}{partialData!.chunks.find(c=>c.id===citation.evidence_id)?.references.map(ref=><button key={ref} onClick={()=>setReference(ref)}>보존 입력 확인 · {ref}</button>)}</div>)}</div>)}</details>)}{partialData.total>5&&<div className="pagination"><button disabled={!offset} onClick={()=>setOffset(Math.max(0,offset-5))}>이전 후보</button><button disabled={offset+5>=partialData.total} onClick={()=>setOffset(offset+5)}>다음 후보</button></div>}</>}
    {partial.error&&<p role="alert">{partial.error}</p>}{reference&&<Evidence key={reference} path={`${base}/partial/evidence?reference=${encodeURIComponent(reference)}`}/>}
    {historyData&&<><p>보존된 이벤트 {historyData.total}개{historyData.total>0&&<> · 현재 {after+1}부터</>}</p>{historyData.total===0&&!historyData.historical_detail_unknown&&<p>{active?'아직 보존된 실행 이벤트가 없습니다. 분석 진행에 따라 갱신됩니다.':'이 실행에 보존된 세부 이벤트가 없습니다. 없는 기록을 추측해 채우지 않습니다.'}</p>}<ol start={after+1}>{historyData.events.map(event=><li key={event.event_seq}><p>{names[event.kind]??event.kind} · {event.phase} · {date(event.created_at)}</p>{event.data.code&&<p>{analysisExecutionReason(event.data.code)}</p>}{event.data.condition&&<p>검사 조건: {event.data.condition} · 원인 확인: {event.data.cause_status??'UNKNOWN'}</p>}{event.data.measured_bytes!==undefined&&<p>측정 {event.data.measured_bytes.toLocaleString()} bytes{event.data.limit_bytes!==undefined&&<> / 한도 {event.data.limit_bytes.toLocaleString()} bytes</>}</p>}<button onClick={()=>setSelected(selected===event.event_seq?null:event.event_seq)}>검증 근거 {event.event_seq} {selected===event.event_seq?'닫기':'보기'}</button>{selected===event.event_seq&&<EventDetail path={`${base}/events/${event.event_seq}`}/>}</li>)}</ol><div className="pagination"><button disabled={!after} onClick={()=>{setAfter(Math.max(0,after-20));setSelected(null);}}>이전 이력</button><button disabled={historyData.next_after>=historyData.total} onClick={()=>{setAfter(historyData!.next_after);setSelected(null);}}>다음 이력</button></div></>}
  </section>;
}
