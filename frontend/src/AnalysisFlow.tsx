import {useEffect, useState} from 'react';
import {collectionCaption, type Collection} from './model';
import {Modal, date} from './ui';
import AnalysisPreview from './AnalysisPreview';
import {analysisExecutionReason} from './analysisSupport';
import {analysisPrefix, completedAnalysisCollections, executionLabels, isActiveAnalysis, matchesPreparedScope} from './analysis';
import type {AnalysisActions, AnalysisView, MaterialInfo} from './analysis';

export default function AnalysisFlow({collections,materials,materialError,view,actions={},refreshMaterials}:{
  collections:Collection[];materials?:MaterialInfo[];materialError?:string;view:AnalysisView;actions?:AnalysisActions;refreshMaterials:()=>void;
}) {
  const ordered=completedAnalysisCollections(collections),[end,setEnd]=useState<string|null>(null);
  const [dialog,setDialog]=useState(false),[agreed,setAgreed]=useState(false),[previewReady,setPreviewReady]=useState(false);
  const preparation=view.preparation,prepared=preparation.data;
  const active=view.executions.find(e=>isActiveAnalysis(e.status));
  useEffect(()=>{if(prepared?.resume)setEnd(prepared.endCollectionId);},[prepared?.id,prepared?.endCollectionId,prepared?.resume?.requestId]);
  useEffect(()=>{if(end===null){const initial=active?.endCollectionId??prepared?.endCollectionId??ordered.at(-1)?.id;if(initial)setEnd(initial);}},[end,ordered,active,prepared]);
  const selected=end??active?.endCollectionId??prepared?.endCollectionId??ordered.at(-1)?.id??'';
  const prefix=analysisPrefix(collections,selected),valid=prefix.length>0;
  const matching=!!prepared && matchesPreparedScope(prepared,collections,selected,materials);
  const usable=!!prepared?.items.some(item=>item.outcome!=='excluded');
  const ready=preparation.state==='ready' && matching && usable;
  const stale=preparation.state==='stale' || (preparation.state==='ready' && !matching);
  const approvalKey=`${selected}:${preparation.state}:${prepared?.id}:${prepared?.version}:${ready}:${view.capacity.state}:${active?.id}:${view.executionAvailable}`;
  useEffect(()=>setAgreed(false),[approvalKey]);
  useEffect(()=>{if(!ready)setPreviewReady(false);},[ready]);
  useEffect(()=>{if(!previewReady)setAgreed(false);},[previewReady]);
  const canPrepare=valid && !!actions.prepare && view.capacity.state==='available' && !active && !['preparing','unknown'].includes(preparation.state);
  const canSubmit=view.executionAvailable!==false && ready && previewReady && agreed && !!actions.submit && !active && view.capacity.state==='available';
  const counts=(['ready','partial','excluded'] as const).map(status=>prepared?.items.filter(i=>i.outcome===status).length);
  return <div className="analysis-flow" id="analysis-start">
    <section className="card analysis-intro"><header><div><h3>새 분석</h3><p>범위를 선택하고 전송할 자료를 확인한 뒤 분석을 요청합니다.</p></div>
      <button className="primary" disabled={!canPrepare} onClick={()=>{if(canPrepare)actions.prepare?.(selected);}}>{view.executionAvailable===false?'로컬 자료 준비':'분석 시작'}</button></header>
      {view.executionAvailable===false && preparation.state!=='unavailable' && <p className="notice">로컬 준비 전용입니다. 자료 목록은 확인할 수 있으며 분석 접수·실행·외부 전송은 차단돼 있습니다.</p>}
      {preparation.state==='unavailable' && <p className="notice">분석 실행을 사용할 수 없습니다. 서버 설정·상태 안내를 확인하세요. 저장된 결과 조회는 가능합니다.</p>}
      {view.capacity.state==='busy' && !active && <p role="status" className="notice">{view.capacity.projectName??'다른 프로젝트'}에서 AI 분석이 진행 중입니다. 종료 후 다시 요청하세요. 이 프로젝트의 요청은 접수되지 않으며 자료 변경도 제한되지 않습니다.</p>}
      <ol className="analysis-steps"><li>범위 선택</li><li>자료·전송 안내 확인</li><li>동의 후 요청</li><li>진행·결과 확인</li></ol>
    </section>

    <div className="analysis-columns">
      <section className="card analysis-scope"><h3>1. 분석 범위</h3>
        <label>마지막 완료 Collection<select aria-label="분석 종료 Collection" value={selected} disabled={!ordered.length || !!active} onChange={e=>setEnd(e.target.value)}>
          {!valid && <option value={selected}>{selected?'선택한 수집이 없어졌습니다. 다시 선택하세요.':'완료된 수집 없음'}</option>}
          {[...ordered].reverse().map(c=><option key={c.id} value={c.id}>{collectionCaption(c)}</option>)}
        </select></label>
        {!ordered.length ? <p className="hint">완료된 수집 기록이 있어야 분석을 요청할 수 있습니다.</p> : !valid ? <p role="alert" className="notice">선택한 종료점이 더 이상 유효하지 않습니다. 다른 완료 수집을 선택하세요.</p> : <>
          <p className="analysis-range"><strong>최초 완료 수집부터 선택한 수집까지</strong><span>현재 목록 기준 완료 수집 {prefix.length}개 · 선택한 한 건만 분석하지 않습니다.</span></p>
          <details><summary>포함될 수집 목록 ({prefix.length}개)</summary><ul>{prefix.map(c=><li key={c.id}>{collectionCaption(c)}<small className="hint"> · ID: {c.id}</small></li>)}</ul></details>
        </>}
        <div className="analysis-materials"><header><h4>사용자 추가 자료</h4><button onClick={refreshMaterials}>목록 새로고침</button></header>
          <p>종료 Collection과 관계없이 <strong>정상 접수 당시 이 프로젝트에 존재하는 전체 자료</strong>가 대상입니다.</p>
          {materialError ? <p role="alert" className="alert">추가 자료 목록을 확인하지 못했습니다. {materialError}</p> : materials===undefined ? <p className="hint">등록된 자료 목록을 확인하는 중…</p> : <details><summary>현재 등록된 자료 {materials.length}개</summary>{materials.length ? <ul>{materials.map(m=><li key={m.id}>{m.filename} <small className="hint">· 등록 {date(m.added_at)}</small></li>)}</ul> : <p>현재 등록된 추가 자료가 없습니다.</p>}</details>}
          <p className="hint">이 목록은 처리 가능 여부나 실제 전송 범위를 확정한 결과가 아닙니다. 업로드 시각과 개발 사건 시점도 구분합니다.</p>
        </div>
      </section>

      <section className="card analysis-preparation"><h3>2. 자료 확인·외부 전송 동의</h3>
        <p>지원 형식·처리 한계와 실제 전송 자료를 직접 확인합니다. 개인정보·민감정보·Secret의 자동 탐지·마스킹은 제공하지 않습니다.</p>
        <dl className="analysis-counts">{['분석 가능','부분 처리','제외'].map((label,i)=><div key={label}><dt>{label}</dt><dd>{prepared?`${counts[i]}개`:'확인 전'}</dd></div>)}</dl>
        {preparation.state==='preparing' && <p role="status">로컬에서 입력을 준비하고 있습니다. 아직 분석 접수나 외부 전송이 아닙니다.</p>}
        {prepared?.resume && !prepared.diagnostic && <p className="notice">실패 실행의 원래 고정 입력을 확인합니다. 검증된 중간 결과 {prepared.resume.checkpointCount}개를 재사용하고 남은 작업만 호출합니다. 이전 실행까지 호출 진입 승인 {prepared.resume.inheritedAttempts}회입니다. 이번 실행의 실제 attempt는 최대 512회이며, 여러 실행의 누적 이력은 유지합니다. 일일 한도에 도달하면 보존 후 종료하고 새 준비·동의로 수동 재개합니다. 중간 결과 재사용은 API 호출로 차감하지 않습니다. 전송 자료를 다시 확인하고 이번 실행에 새로 동의하세요.</p>}
        {!prepared?.diagnostic && prepared?.resume?.compatibilityPolicy && prepared.resume.reusedStage==='discover' && <p className="notice">이전 계약과 호환 검증된 발견 결과만 재사용합니다. 후보 통합과 전역 관계 검토부터 새 분석 계약으로 수행합니다. 이전 통합 이후 결과는 재사용하지 않습니다.</p>}
        {!prepared?.diagnostic && prepared?.resume?.reusedStage==='discover_consolidate' && <p className="notice">정확한 요청·응답과 근거 검증을 통과한 발견·후보 통합의 완료 prefix만 재사용합니다. 전역 관계 검토부터 출처 사전 참조·요청 크기·Schema 제어 계약을 적용합니다. 이전 관계 검토 이후 결과는 재사용하지 않으며 새 Preview·동의가 필요합니다.</p>}
        {!prepared?.diagnostic && prepared?.resume?.reusedStage==='all_completed' && <p className="notice">분석 입력·지시·응답 계약이 같은 완료 결과를 호환 검증해 재사용합니다. 실행별 예산·한도·재시도 처리 정책을 새 동의에 적용하며 원본 호출 이력은 유지합니다.</p>}
        {prepared?.diagnostic && <p className="notice">기존 실패 요청의 1회 진단입니다. 완료 중간 결과 {prepared.resume?.checkpointCount}개를 재생하고 동일한 발신 요청 {prepared.diagnostic.wireBytes.toLocaleString()}bytes를 최대 1회만 전송합니다. 자동 재시도·후속 분석은 실행하지 않습니다. 접수 후 취소·종료·결과 미확정이어도 진단 기회는 다시 열리지 않습니다. 새 전송 요청을 확인하고 동의하세요.</p>}
        {stale && <p role="alert" className="notice">준비 이후 분석 범위나 자료가 변경됐습니다. 유효한 종료 선택을 유지하고 다시 준비한 뒤 새로 동의하세요.</p>}
        {(preparation.state==='empty' || (preparation.state==='ready' && matching && !usable)) && <p role="alert" className="notice">실제 전송 가능한 입력이 없습니다. 제외 사유를 확인하세요. 분석 요청은 접수하지 않습니다.</p>}
        {preparation.state==='unknown' && actions.retry && <button onClick={actions.retry}>같은 요청 ID로 다시 확인·전송</button>}
        {preparation.state==='unknown' && <p role="alert" className="notice">접수 여부를 확인 중입니다. 응답이 없었다는 이유로 다시 접수하지 않습니다. 서버 기록을 확인해야 합니다.</p>}
        {preparation.message && <p className="hint">{preparation.message}</p>}
        <details><summary>처리 자료·제외 사유 상세</summary>{prepared ? <ul className="analysis-item-list">{prepared.items.map(item=><li key={item.id}><span className="badge">{{ready:'분석 가능',partial:'부분 처리',excluded:'제외'}[item.outcome]}</span> <strong>{item.name}</strong><p>{item.description}</p></li>)}</ul> : <p className="hint">아직 입력을 준비하지 않아 실제 처리 자료와 제외 사유를 확인할 수 없습니다.</p>}</details>
        {prepared && ready && <AnalysisPreview key={prepared.id} prepared={prepared} actions={actions} onReady={setPreviewReady}/>}
        <dl className="analysis-notice"><dt>제공자</dt><dd>{prepared?.provider??'입력 준비 후 확인'}</dd><dt>모델</dt><dd>{prepared?.model??'확인 전'}</dd><dt>실제 전송 범위·데이터 정책</dt><dd>{prepared?'준비 결과의 상세 안내에서 확인':'입력 준비 후 확인할 수 있습니다.'}</dd></dl>
        <button onClick={()=>setDialog(true)}>전송 안내 확인</button>
        <p className="hint">접수 전 입력·정책이 달라지면 재준비·새 동의가 필요합니다. 정상 접수 후에는 고정 입력만 사용합니다.</p>
      </section>
    </div>

    <section className="card analysis-execution"><header><h3>3. 분석 진행·실행 이력</h3>{actions.refresh && <button onClick={actions.refresh}>상태 다시 확인</button>}</header>
      {!view.historyAvailable ? <p className="hint">실행 상태와 실패·취소 이력은 아직 조회할 수 없습니다. 저장된 성공 결과는 아래에서 확인할 수 있습니다.</p> : !view.executions.length ? <p>접수된 분석 실행이 없습니다.</p> : <>
        {active && <div className="analysis-active" role="status"><span className={`badge analysis-status-${active.status}`}>{executionLabels[active.status]}</span><h4>{active.phase??'분석 실행 상태 확인 중'}</h4>
          <p>종료 수집: {active.endCollectionLabel} · 완료 수집 {active.collectionCount}개 · 접수 당시 추가 자료 {active.materialCount}개</p>
          {active.calls!==undefined && <p>호출 진입 승인 {active.calls}회 · Provider 수신·비용 미확인</p>}
          {active.resumeFromRequestId && <p>재개 실행 · 재사용한 중간 결과 {active.reusedCheckpoints??0}개 · 이전 실행까지 호출 진입 승인 {active.inheritedAttempts??0}회</p>}
          <p>접수 당시 입력으로 진행합니다. 이후 완료된 수집이나 외부 Git 변경은 자동으로 포함하지 않습니다.</p>
          <p>이 프로젝트의 기존 수집 수정·삭제/수집 취소, 자료 업로드·삭제, 설정 변경·프로젝트 삭제는 제한됩니다. <strong>신규 수집·완료와 기존 자료 조회는 가능합니다.</strong></p>
          <button className="danger" disabled={!actions.cancel || active.status==='cancel_requested'} onClick={()=>actions.cancel?.(active.id)}>{active.status==='cancel_requested'?'취소 정리 중':'분석 취소 요청'}</button>
          {active.status==='cancel_requested' && <p>새 AI 호출을 시작하지 않고 정리합니다. 실제 취소 완료까지 자료 변경 제한이 유지됩니다.</p>}
          <p className="hint">이미 외부로 전송된 요청의 즉시 중단이나 비용 회피는 보장하지 않습니다.</p>
        </div>}
        <ul className="analysis-history">{view.executions.map(execution=><li key={execution.id}><span className={`badge analysis-status-${execution.status}`}>{executionLabels[execution.status]}</span><span>{date(execution.createdAt)} · {execution.endCollectionLabel}</span>{execution.message && <p>{execution.message}</p>}{execution.status==='failed' && (execution.checkpointCount??0)>0 && !execution.resumedByRequestId && !execution.resumeBlockReason && <button disabled={!canPrepare || view.executionAvailable===false} onClick={()=>{setEnd(execution.endCollectionId);actions.prepare?.(execution.endCollectionId,execution.id);}}>이어서 분석 준비</button>}{execution.diagnosticAvailable && <button disabled={!canPrepare || view.executionAvailable===false} onClick={()=>{setEnd(execution.endCollectionId);actions.prepare?.(execution.endCollectionId,execution.id,true);}}>1회 진단 준비</button>}{execution.diagnostic && <span className="badge">1회 진단 · 후속 분석 없음</span>}{execution.resumeBlockReason && <p className="notice">{analysisExecutionReason(execution.resumeBlockReason)}{execution.resumeAfter && <> 제공자 기준 갱신 시각: {date(execution.resumeAfter)}</>}</p>}{(execution.checkpointCount??0)>0 && <p className="hint">보존된 완료 중간 결과 {execution.checkpointCount}개 · 이번 실행 attempt {execution.calls??0}회 · 이전 누적 {execution.inheritedAttempts??0}회</p>}{execution.resumedByRequestId && <p className="hint">재개 실행이 접수됐습니다. 해당 실행의 상태를 확인하세요.</p>}{execution.resumeFromRequestId && <p className="hint">이전 실패에서 이어진 실행 · 중간 결과 {execution.reusedCheckpoints??0}개 재사용</p>}{['completed','failed','cancelled'].includes(execution.status) && <small className="hint">{active?'이 실행은 종료됐으며 이전 성공 결과는 보존됩니다. 현재 자료 제한은 진행 중인 실행 상태를 따릅니다.':'이 실행의 자료 변경 제한은 해제됐습니다. 이전 성공 결과는 보존됩니다.'}</small>}</li>)}</ul>
      </>}
      <details className="analysis-policy"><summary>진행·종료 정책</summary><ul>
        <li>전체 프로젝트에서 정상 접수된 대기·실행·취소 정리 중 분석은 최대 1건입니다. 다른 요청은 대기열에 넣지 않습니다.</li>
        <li>정상 접수된 분석은 화면 이동·브라우저 종료와 독립적으로 서버에서 계속됩니다. 재접속해 상태를 확인합니다.</li>
        <li>서버가 실행되는 로컬 PC를 종료하면 작업도 중단됩니다. 서버 재시작 시 미완료 실행은 실패 처리하고 자동 재개하지 않습니다.</li>
        <li>완료 중간 결과가 있는 실패 실행은 원래 고정 입력 확인·새 동의 후 수동 재개할 수 있습니다. 취소 실행은 재개하지 않습니다. 일일 한도는 태평양 시간 자정에 갱신되며 새 준비·동의로 여러 날 수동 재개할 수 있습니다. 검증 오류는 원인 해결·호환 검증이 필요합니다.</li>
        <li>완료·실패·취소 완료 확정 후 자료 변경 제한을 해제하며 기존 성공 결과를 유지합니다.</li>
        <li>신규 수집 실패는 기존 수집 정리 정책을 따릅니다. 분석 실패나 취소와 결합하지 않습니다.</li>
      </ul></details>
    </section>

    {dialog && <Modal title="외부 AI 전송 안내" close={()=>setDialog(false)}>
      {prepared?.resume?.transition && <p className="notice">검증된 구→신 계약의 1회 전환입니다. 구 계약의 소진된 {prepared.resume.transition.previous_attempts}회는 그대로 남으며, 새 계약의 관계 요청에는 추가로 최대 {prepared.resume.transition.max_attempts}회가 허용됩니다. 통신 재시도와 조건부 재생성이 이 한도를 공유합니다. 과거 사용량·누적 이력은 초기화하지 않습니다. 이 전환 조건까지 확인하고 새로 동의하세요.</p>}
      {prepared?.relationRecovery && <p className="notice">이번 동의에는 관계 근거 검증 실패 시 동일 고정 자료와 비민감 오류 코드·개수·검증 규칙을 추가 전송해 최대 {prepared.relationRecovery.maxRegenerations}회 재생성하는 조건이 포함됩니다. 통신 재시도와 공유하는 전송 진입 상한은 논리적 관계 요청당 {prepared.relationRecovery.maxAttempts}회입니다. 추가 호출도 사용량에 포함되며 잘못된 응답은 저장하지 않습니다.</p>}
      <p>수집 자료는 선택한 종료점까지, 추가 자료는 정상 접수 당시 전체가 대상입니다. 실제 전송 입력과 제외·한계를 확인하세요.</p>
      <dl className="analysis-notice"><dt>제공자·모델</dt><dd>{prepared?`${prepared.provider} / ${prepared.model}`:'아직 확인되지 않았습니다.'}</dd><dt>실제 전송 범위</dt><dd>{prepared?.transferScope??'입력 준비 후 확인할 수 있습니다.'}</dd><dt>데이터 처리 정책</dt><dd className="preserve-lines">{prepared?.policy??'정책을 확인할 수 없어 동의와 분석 요청을 진행할 수 없습니다.'}</dd></dl>
      {!ready && <p className="notice">현재 유효한 준비 결과가 없습니다. 자료·제외 사유·모델·정책을 확인할 수 있어야 동의할 수 있습니다.</p>}
      <label className="analysis-consent"><input type="checkbox" checked={agreed} disabled={view.executionAvailable===false || !ready || !previewReady || !actions.submit || view.capacity.state!=='available' || !!active} onChange={e=>setAgreed(e.target.checked)}/><span>전체 전송 자료 확인 경로에서 텍스트·출처·PDF/이미지와 제외·한계를 직접 점검했습니다. 제공 권한과 Gemini 데이터 처리 조건을 확인했고, 자동 보호 없이 이번 입력을 외부 AI에 전송하는 데 동의합니다.</span></label>
      <p className="hint">준비 완료나 동의만으로 접수되지 않습니다. 서버가 범위와 동의를 최종 검증해 정상 접수한 때 입력을 고정합니다.</p>
      <footer><button onClick={()=>setDialog(false)}>닫기</button><button className="primary" disabled={!canSubmit} onClick={()=>{if(canSubmit && prepared){actions.submit?.(prepared);setAgreed(false);setDialog(false);}}}>{prepared?.diagnostic?'동의하고 1회 진단 요청':prepared?.resume?'동의하고 이어서 분석 요청':'동의하고 분석 요청'}</button></footer>
    </Modal>}
  </div>;
}
