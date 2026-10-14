import {useState} from 'react';
import {useData} from './data';
import {date, Text} from './ui';
import {analysisReason, mediaScope} from './analysisSupport';
import type {MediaInfo} from './analysisSupport';

interface Citation {evidence_id:string;quote:string|null;locator:string|null;page?:number|null}
interface Claim {section:string;statement:string;status:'EVIDENCED'|'INFERRED'|'UNKNOWN';citations:Citation[]}
interface Candidate {id:string;title:string;category:'strong'|'potential';rationale:string;claims:Claim[];evidence_ids:string[];limitations:string[];grounding_notes:string[];review_status:'unreviewed'}
interface Coverage {collections:string[];input_entries:number;discovery_chunks:number;excluded:{reference:string;reason:string}[];limitations:{reference:string;reasons:string[]}[];chunks:{id:string;references:string[];offset:number;media?:MediaInfo|null}[]}
interface Run {id:string;created_at:string;model:string;mode:'isolated'|'live';candidate_count:number;coverage:Coverage}
interface Detail {id:string;created_at:string;mode:'isolated'|'live';input_digest:string;result:{candidates:Candidate[];coverage:Coverage}}
interface Evidence {reference:string;source:Record<string,unknown>;body:string|null;reason:string|null;limitations:string[];media:MediaInfo[]}
const sections:Record<string,string>={background:'배경',problem:'문제',alternatives:'검토한 대안',decision:'선택과 이유',implementation:'구현',troubleshooting:'문제 해결 과정',verification:'검증',result:'결과',contribution:'기여',learning:'학습'};
const statuses={EVIDENCED:'본문 인용 근거 있음',INFERRED:'AI 해석 · 미확인',UNKNOWN:'UNKNOWN · 확인되지 않음'};
function EvidencePanel({project,run,reference,close}:{project:string;run:string;reference:string;close:()=>void}) {
  const path=`projects/${project}/experiences/${run}/evidence?reference=${encodeURIComponent(reference)}`;
  const {data,error}=useData<Evidence>(path);
  return <section className="card experience-evidence"><header><h3>보존된 근거</h3><button onClick={close}>닫기</button></header>{error && <p role="alert" className="alert">{error}</p>}{!data && !error && <p>근거를 불러오는 중…</p>}{data && <>
    <p><Text text={data.reference}/></p>{data.reason && <p className="notice">내용을 사용하지 못한 이유: {analysisReason(data.reason)}</p>}
    {data.body!==null && <pre className="experience-body">{data.body}</pre>}
    {data.media.map((media,index)=><p key={index}><a href={`/api/projects/${project}/experiences/${run}/media?reference=${encodeURIComponent(reference)}&index=${index}`}>{media.name} 보존 사본 다운로드</a> <span className="hint">{data.reason?'보존 사본 · 분석에서 제외':mediaScope(media)}</span>{media.provenance?.original_sha256 && <small className="hint"> · 연결된 원본 SHA-256: {media.provenance.original_sha256}</small>}</p>)}
    {!!data.limitations.length && <ul>{data.limitations.map(l=><li key={l}>{analysisReason(l)}</li>)}</ul>}
    <details><summary>출처·시점·원본 위치</summary><pre className="experience-body">{JSON.stringify(data.source,null,2)}</pre></details>
  </>}</section>;
}
function RunDetail({project,run}:{project:string;run:string}) {
  const {data,error}=useData<Detail>(`projects/${project}/experiences/${run}`),[reference,setReference]=useState(''),[selected,setSelected]=useState('');
  if(error)return <p role="alert" className="alert">{error}</p>;
  if(!data)return <p>분석 결과를 불러오는 중…</p>;
  const coverage=data.result.coverage,candidates=data.result.candidates,candidate=candidates.find(c=>c.id===selected) ?? candidates[0];
  return <>{data.mode==='isolated' && <p className="notice">격리 검증 결과입니다. 실제 Gemini 분석 품질을 확인한 결과가 아닙니다.</p>}
    <p className="hint">{date(data.created_at)} · 완료 수집 {coverage.collections.length}개 · 근거 항목 {coverage.input_entries}개 · 발견 입력 {coverage.discovery_chunks}개</p>
    {(!!coverage.limitations.length || !!coverage.excluded.length) && <p className="notice">미처리 자료 또는 처리·검증 한계가 있습니다. 원본 보존은 전체 내용의 AI 분석을 뜻하지 않으며 중요한 개발 경험이 누락될 수 있습니다.</p>}
    <details className="card"><summary>분석 범위 · 사용하지 못한 자료 {coverage.excluded.length}개 · 한계가 있는 자료 {coverage.limitations.length}개</summary><p>각 완료 수집의 출처를 유지하며 같은 본문은 한 번 분석합니다. PDF 분할 경계는 별개의 사건을 뜻하지 않습니다.</p><ul>{coverage.collections.map(c=><li key={c}><Text text={c}/></li>)}</ul>{coverage.excluded.map(e=><p key={e.reference}><button onClick={()=>setReference(e.reference)}>근거 확인</button> <Text text={e.reference}/> · {analysisReason(e.reason)}</p>)}{coverage.limitations.map(e=><p key={e.reference}><Text text={e.reference}/> · {e.reasons.map(analysisReason).join(' ')}</p>)}<h3>실제 분석 입력의 본문·이미지·페이지</h3><ul>{coverage.chunks.map(chunk=><li key={chunk.id}>{chunk.media?mediaScope(chunk.media):`추출 텍스트 · 시작 위치 ${chunk.offset}`} · {chunk.references.map(ref=><button key={ref} onClick={()=>setReference(ref)}>근거 확인 · <Text text={ref}/></button>)}</li>)}</ul><p className="hint">DOCX의 페이지는 변환 PDF 기준입니다. 원본 Word와 동일한 배치·전체 특수 기능 처리를 보장하지 않습니다.</p></details>
    {!candidates.length ? <section className="card"><p>발견된 개발 경험 후보가 없습니다. 미처리 자료와 근거 범위도 함께 확인하세요.</p></section> : <div className="experience-layout"><section className="card experience-list"><h3>개발 경험 후보 ({candidates.length})</h3>{candidates.map(c=><button key={c.id} className={c.id===candidate.id?'selected':''} onClick={()=>{setSelected(c.id);setReference('');}}><span className="badge">{c.category==='strong'?'유력':'잠재'}</span><Text text={c.title}/></button>)}</section><section className="card experience-candidate"><h2><Text text={candidate.title}/></h2><p className="hint">사용자 미검토 · AI가 발견·복원한 후보</p><p className="preserve-lines">활용 가치에 대한 AI 판단: {candidate.rationale}</p><p className="notice">본문 인용은 주장 추적을 위한 근거이며 사실 확인·승인을 뜻하지 않습니다. UNKNOWN과 AI 해석을 실제 성과로 사용하지 마세요.</p>
      {Object.entries(sections).map(([key,label])=><section key={key}><h3>{label}</h3>{candidate.claims.filter(c=>c.section===key).map((claim,i)=><div key={i} className="experience-claim"><span className="badge">{statuses[claim.status]}</span><p className="preserve-lines">{claim.statement}</p>{claim.citations.map((citation,j)=>{const chunk=coverage.chunks.find(c=>c.id===citation.evidence_id);return <div key={j} className="experience-citation">{citation.quote && <blockquote>{citation.quote}</blockquote>}{citation.page!=null && <p>{chunk?.media?.provenance?.transform==='docx-to-pdf'?'DOCX 변환 PDF':'원본 PDF'} {citation.page}페이지</p>}{citation.locator && <p>위치: {citation.locator}</p>}{chunk?.references.map(ref=><button key={ref} onClick={()=>setReference(ref)}>근거 확인 · <Text text={ref}/></button>)}</div>;})}</div>)}</section>)}
      {!!candidate.grounding_notes?.length && <details><summary>근거 검사에서 채택하지 않은 AI 주장</summary>{candidate.grounding_notes.map((n,i)=><p key={i}>{n}</p>)}</details>}
      {!!candidate.limitations.length && <ul>{candidate.limitations.map(l=><li key={l}>{analysisReason(l)}</li>)}</ul>}
    </section></div>}
    {reference && <EvidencePanel key={`${run}:${reference}`} project={project} run={run} reference={reference} close={()=>setReference('')}/>}
  </>;
}
export default function Experiences({projectId}:{projectId:string}) {
  const {data,error}=useData<{runs:Run[];transfer_enabled:boolean;execution_available:boolean}>(`projects/${projectId}/experiences`),[chosen,setChosen]=useState('');
  const run=data?.runs.find(r=>r.id===chosen) ?? data?.runs[0];
  return <section className="experiences"><header className="card"><div><h2>개발 경험</h2><p>완료된 수집과 추가 자료에서 발견·복원한 경험 후보와 근거를 확인합니다.</p></div><button className="primary" disabled>분석 시작</button><p className="hint">실제 자료의 외부 전송과 분석 실행은 아직 비활성입니다.</p></header>
    {error && <p role="alert" className="alert">{error}</p>}{!data && !error && <p>저장된 결과를 불러오는 중…</p>}{data && !data.runs.length && <section className="card"><p>저장된 분석 결과가 없습니다.</p></section>}
    {run && <><label className="card">저장된 분석 결과<select aria-label="저장된 분석 결과" value={run.id} onChange={e=>setChosen(e.target.value)}>{data!.runs.map(r=><option key={r.id} value={r.id}>{date(r.created_at)} · 후보 {r.candidate_count}개{r.mode==='isolated'?' · 격리 검증':''}</option>)}</select></label><RunDetail key={run.id} project={projectId} run={run.id}/></>}
  </section>;
}
