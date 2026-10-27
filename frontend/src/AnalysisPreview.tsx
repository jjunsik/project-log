import {useEffect, useRef, useState} from 'react';
import type {AnalysisActions, PreparedAnalysis, TransferPreview} from './analysis';
import {message} from './ui';

function sourcePaths(sources:Record<string,unknown>[]):string[] {
  return sources.flatMap(source=>['path_b64','old_path_b64','new_path_b64'].flatMap(key=>{
    if(typeof source[key]!=='string')return [];
    try{return [new TextDecoder('utf-8',{fatal:true}).decode(Uint8Array.from(atob(source[key]),c=>c.charCodeAt(0)))];}
    catch{return ['해독하지 못한 출처 경로입니다. 위 JSON의 원래 값을 확인하세요.'];}
  }));
}

export default function AnalysisPreview({prepared,actions,onReady}:{prepared:PreparedAnalysis;actions:AnalysisActions;onReady:(ready:boolean)=>void}) {
  const [offset,setOffset]=useState(0),[data,setData]=useState<TransferPreview>(),[error,setError]=useState('');
  const read=useRef(actions.preview);read.current=actions.preview;
  useEffect(()=>{
    let alive=true;onReady(false);setData(undefined);setError('');
    if(!read.current){setError('전송 본문 확인 경로가 연결되지 않았습니다.');return;}
    void read.current(prepared.id,offset).then(value=>{
      if(!alive)return;
      if(value.relation_transition?.digest!==prepared.resume?.transition?.digest || value.transfer_digest!==prepared.transferDigest || value.chunk_text_truncated!==false || (prepared.relationRecovery&&value.relation_recovery_digest!==prepared.relationRecovery.digest))throw new Error('준비한 입력과 전송 확인 자료가 일치하지 않습니다. 다시 준비하세요.');
      if(value.relation_transition){const expected=prepared.resume?.transition;const observed=value.relation_transition;if(!expected||(['policy','digest','source_contract','target_contract','previous_attempts','max_attempts','max_regenerations','scope','old_response_or_feedback_reused'] as const).some(key=>observed[key]!==expected[key]))throw new Error('준비한 계약 전환과 전송 확인 자료가 일치하지 않습니다. 다시 준비하세요.');}
      if(prepared.diagnostic && (!value.diagnostic_request || value.diagnostic_request.wire_digest!==prepared.diagnostic.wireDigest || value.diagnostic_request.wire_bytes!==prepared.diagnostic.wireBytes || value.diagnostic_request.max_new_calls!==1 || value.diagnostic_request.automatic_retries!==0 || value.diagnostic_request.continue_analysis!==false))throw new Error('고정된 1회 진단 요청을 확인할 수 없습니다. 다시 준비하세요.');
      setData(value);onReady(true);
    }).catch(e=>{if(alive)setError(message(e));});
    return()=>{alive=false;};
  },[prepared.id,prepared.version,prepared.transferDigest,offset,onReady]);
  return <section className="analysis-transfer"><h4>실제 전송 자료 확인</h4>
    {prepared.relationRecovery && <p className="notice">관계 근거 연결 검증 실패에 한해 같은 고정 자료와 비민감 오류 코드·개수·검증 규칙으로 최대 {prepared.relationRecovery.maxRegenerations}회 재생성할 수 있습니다. 통신 재시도를 포함해 논리적 관계 요청당 전송 진입은 최대 {prepared.relationRecovery.maxAttempts}회이며 날짜·새 실행·재시작으로 한도를 초기화하지 않습니다. 실패 응답 원문은 재전송하지 않습니다. feedback은 검증 결과와 같은 작업의 보존된 검증 정보에서만 구성됩니다. 아래 JSON의 생성 규칙·허용 필드·상한이 이번 동의 대상입니다.</p>}
    <p>현재 페이지의 분할 자료만 표시합니다. 전체 JSON에는 모든 분할 텍스트·출처·분석 지시와 전송할 비텍스트 bytes(base64)가 포함됩니다. 페이지 조회나 다운로드만으로 확인 완료로 간주하지 않습니다.</p>
    {actions.transferUrl && <a href={actions.transferUrl(prepared.id)} download>전체 전송 재료 JSON 다운로드</a>}
    {error && <p role="alert">{error}</p>}
    {!error && !data && <p role="status">고정 전송 자료를 불러오는 중…</p>}
    {data && <>
      {data.diagnostic_request && <section className="notice"><h4>이번 1회 진단의 정확한 발신 요청</h4><p>{data.diagnostic_request.model} · {data.diagnostic_request.wire_bytes.toLocaleString()}bytes · 최대 1회 · 자동 재시도/후속 분석 없음</p><p className="preserve-lines">실패 요청과 같은 SHA256: {data.diagnostic_request.wire_digest}</p><details><summary>발신 JSON·모델·API 확인</summary><p>{data.diagnostic_request.endpoint}</p><pre className="preserve-lines">{JSON.stringify(data.diagnostic_request.request,null,2)}</pre></details><p>과거 완료 응답에서 재구성한 문맥이 포함됩니다. 아래 원래 자료와 함께 직접 확인하세요. 수락되더라도 정상 분석 재개나 원인 해결을 뜻하지 않습니다.</p></section>}
      {data.relation_transition && <section className="notice"><h4>검증된 구→신 계약 1회 전환</h4><p>구 계약의 소진된 {data.relation_transition.previous_attempts}회는 보존합니다. 수정된 새 계약의 논리적 관계 요청당 통신 재시도·재생성을 공유해 최대 {data.relation_transition.max_attempts}회 전송할 수 있습니다. 새 계약의 예산은 다시 준비하거나 재시작해도 초기화되지 않습니다. 실패한 구 응답·feedback은 전송하지 않습니다.</p><details><summary>전환 계약·한도·확인 Digest</summary><pre className="preserve-lines">{JSON.stringify(data.relation_transition,null,2)}</pre></details></section>}
      <p>{data.total ? `${data.offset+1}–${data.offset+data.chunks.length}`:'0'} / 전체 {data.total}개 분할 자료 · 텍스트 생략 없음</p>
      {data.relation_recovery && <details><summary>조건부 재생성 전송 규칙</summary><pre className="preserve-lines">{JSON.stringify(data.relation_recovery,null,2)}</pre></details>}
      <p className="hint">{data.later_context} {prepared.diagnostic?'위 발신 요청만 전송하며 후속 호출은 없습니다.':'실행 전에는 아직 생성되지 않은 후속 요청 전체를 보여줄 수 없습니다.'}</p>
      <details><summary>전체 분석 지시·응답 형식</summary><p>각 호출의 system 지시는 아래 공통 지시와 작업별 지시를 이어 붙입니다.</p><pre className="preserve-lines">{data.system_instruction}</pre>{Object.entries(data.task_instructions).map(([task,text])=><div key={task}><strong>{task}</strong><pre className="preserve-lines">{text}</pre></div>)}<pre className="preserve-lines">{JSON.stringify({schemas:data.response_schemas,generation:data.generation,consolidation:data.consolidation},null,2)}</pre></details>
      {data.chunks.map((chunk,i)=><details key={chunk.id}><summary>분할 자료 {data.offset+i+1} · {chunk.media?.name??'텍스트'} · 위치 {chunk.offset}</summary>
        <pre className="preserve-lines">{JSON.stringify(chunk.sources,null,2)}</pre>
        {sourcePaths(chunk.sources).map((path,i)=><p className="preserve-lines" key={i}>출처 경로 해독 표시: {path}</p>)}
        {chunk.text!==null && <pre className="preserve-lines">{chunk.text}</pre>}
        {chunk.media && <><pre className="preserve-lines">{JSON.stringify(chunk.media,null,2)}</pre>{actions.transferUrl && <a href={actions.transferUrl(prepared.id,data.offset+i)} download>실제 전송 비텍스트 자료 다운로드 ({chunk.media.mime_type})</a>}</>}
      </details>)}
      <div className="pagination"><button disabled={!offset} onClick={()=>setOffset(Math.max(0,offset-20))}>이전 전송 자료</button><button disabled={offset+data.chunks.length>=data.total} onClick={()=>setOffset(offset+20)}>다음 전송 자료</button></div>
    </>}
  </section>;
}
