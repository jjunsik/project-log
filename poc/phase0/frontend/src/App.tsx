import { useCallback, useEffect, useRef, useState } from 'react';
import { ReviewClock, type Segment } from './timing';
import { statuses, type CaseState, type Golden, type Judgment, type PilotCase } from './types';

async function api<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`/api/${path}`, body === undefined ? undefined : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : JSON.stringify(result.detail));
  return result as T;
}

const seconds = (milliseconds: number) => `${(milliseconds / 1000).toFixed(1)}초`;
const kinds = ['HISTORICAL_FACT', 'HISTORICAL_DECISION', 'HISTORICAL_INSIGHT',
  'CURRENT_AI_RECOMMENDATION', 'USER_APPROVED_FUTURE_ACTION'];

function useReviewClock(caseId: string) {
  const [running, setRunning] = useState(false);
  const [timingError, setTimingError] = useState('');
  const timer = useRef<ReviewClock | null>(null);
  const queue = useRef<Segment[]>([]);
  const flushing = useRef(false);
  const flush = useCallback(async () => {
    if (flushing.current) return;
    flushing.current = true;
    try {
      while (queue.current.length) {
        await api(`cases/${caseId}/timing`, queue.current[0]);
        queue.current.shift();
      }
      setTimingError('');
    } catch {
      setTimingError('시간 저장 실패: 대기 중 기록을 재전송합니다. 종료하면 일부 시간이 미측정될 수 있습니다.');
    } finally { flushing.current = false; }
  }, [caseId]);
  useEffect(() => {
    const clock = new ReviewClock(crypto.randomUUID(), () => performance.now(), segment => {
      queue.current.push(segment);
      void flush();
    });
    timer.current = clock;
    const visible = () => clock.set({visible: document.visibilityState === 'visible' && document.hasFocus()});
    visible();
    const interval = window.setInterval(() => { clock.checkpoint(); void flush(); }, 5000);
    const close = () => clock.checkpoint(true);
    window.addEventListener('focus', visible);
    window.addEventListener('blur', visible);
    document.addEventListener('visibilitychange', visible);
    window.addEventListener('pagehide', close);
    return () => {
      clock.checkpoint(true);
      clearInterval(interval);
      window.removeEventListener('focus', visible);
      window.removeEventListener('blur', visible);
      document.removeEventListener('visibilitychange', visible);
      window.removeEventListener('pagehide', close);
      timer.current = null;
    };
  }, [flush]);
  const toggle = () => { timer.current?.set({running: !running}); setRunning(!running); };
  const pause = () => { timer.current?.set({running: false}); setRunning(false); };
  return {timer, running, toggle, pause, timingError};
}

export function App() {
  const [cases, setCases] = useState<PilotCase[]>([]);
  const [selected, setSelected] = useState('');
  const [error, setError] = useState('');
  useEffect(() => {
    api<PilotCase[]>('cases').then(list => { setCases(list); setSelected(list[0]?.id ?? ''); })
      .catch(e => setError(String(e)));
  }, []);
  return <>
    <header className="masthead"><div className="brand-mark">PL</div><div>
      <p className="eyebrow">ENGINEERING MEMORY · MEASUREMENT PILOT</p>
      <h1>Project Log</h1><p>근거를 읽고, 주장을 검토하고, 판단을 남깁니다.</p>
    </div><span className="local-badge">LOCAL · PILOT 01</span></header>
    <main>
      <div className="notice"><strong>측정 체계 검증 단계</strong>
        <span>4개 합성 사례 · 성능 Gate 미설정 · Golden은 사용자 승인 전까지 Candidate</span></div>
      {error && <p role="alert" className="error">{error}</p>}
      <label className="case-select">검토할 사례
        <select aria-label="검토할 사례" value={selected} onChange={e => setSelected(e.target.value)}>
          {cases.map((item, i) => <option key={item.id} value={item.id}>0{i + 1} / {item.title}</option>)}
        </select>
      </label>
      {selected && <Workspace key={selected} caseId={selected} />}
    </main><footer>Project Log — LLM/RAG 기반 Engineering Memory &amp; Learning System · 현재 구현: 파일 기반 Pilot</footer>
  </>;
}

function Workspace({caseId}: {caseId: string}) {
  const [state, setState] = useState<CaseState | null>(null);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState<'mock' | 'gemini'>('mock');
  const [tab, setTab] = useState<'review' | 'golden' | 'evaluation'>('review');
  const [revisionId, setRevisionId] = useState('');
  const [note, setNote] = useState('');
  const [golden, setGolden] = useState<Golden | null>(null);
  const [dirty, setDirty] = useState(false);
  const goldenEditVersion = useRef(0);
  const [acknowledged, setAcknowledged] = useState(false);
  const [judgments, setJudgments] = useState<Judgment[]>([]);
  const clock = useReviewClock(caseId);
  const load = useCallback(async () => {
    const data = await api<CaseState>(`cases/${caseId}`);
    setState(data);
    return data;
  }, [caseId]);
  useEffect(() => {
    load().then(data => {
      setRevisionId(data.revisions.at(-1)?.id ?? '');
      setGolden(structuredClone(data.goldens.at(-1)?.golden ?? data.case.golden_candidate));
    }).catch(e => setError(String(e)));
  }, [load]);
  const revision = state?.revisions.find(r => r.id === revisionId);
  const current = revisionId === state?.revisions.at(-1)?.id;
  useEffect(() => {
    clock.timer.current?.set({revision: revisionId || null});
    setJudgments(revision?.draft.claims.map(c => ({claim_id: c.id, supported: false,
      content_correct: false, severe_fabrication: false, note: ''})) ?? []);
    setAcknowledged(false);
  }, [revisionId]); // A revision switch starts fresh human judgments.

  async function perform(action: () => Promise<void>) {
    setBusy(true); setError(''); setMessage('');
    try { await action(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }
  function changeTab(next: typeof tab) {
    clock.pause(); setTab(next); setAcknowledged(false); setMessage('');
  }
  async function run(correction = false) {
    clock.timer.current?.set({waiting: true});
    try {
      const result = await api<{id: string}>(`cases/${caseId}/runs`, {
        mode, base_revision_id: state?.revisions.at(-1)?.id ?? null,
        correction: correction ? note : null,
      });
      await load(); setRevisionId(result.id); setNote('');
      setMessage(mode === 'mock' ? '미리 작성된 데모 응답을 저장했습니다. 자유로운 수정 요청의 AI 처리 결과가 아닙니다.' : '새 AI 초안을 저장했습니다.');
    } finally { clock.timer.current?.set({waiting: false}); }
  }
  async function review(action: string) {
    await api(`cases/${caseId}/reviews`, {revision_id: revisionId, action, note});
    await load(); setNote('');
    setMessage(action === 'APPROVE' ? '이 Revision을 승인했습니다. Claim의 Evidence Status는 유지됩니다.' : '검토 기록을 저장했습니다.');
  }
  function updateGolden(transform: (g: Golden) => void) {
    if (!golden) return;
    goldenEditVersion.current += 1;
    const copy = structuredClone(golden); transform(copy); setGolden(copy); setDirty(true); setAcknowledged(false);
  }
  if (!state || !golden) return error
    ? <p role="alert" className="error">{error}</p>
    : <p aria-live="polite">검토 자료를 불러오는 중…</p>;
  const candidate = state.goldens.at(-1);
  const approval = state.approvals.at(-1);
  const approvedGolden = state.goldens.find(g => g.id === approval?.candidate_id)?.golden;
  const latestReview = state.reviews.filter(r => r.revision_id === revisionId && ['APPROVE', 'HOLD'].includes(r.action)).at(-1);
  return <>
    <section className="case-heading"><div><p className="eyebrow">{state.case.project_key}</p>
      <h2>{state.case.title}</h2><p>{state.case.purpose}</p></div>
      <a className="quiet-link" href={`/api/cases/${caseId}/export`} download={`${caseId}.json`}>실험 기록 내보내기 ↗</a>
    </section>
    <nav aria-label="검토 단계" className="tabs">
      <button aria-current={tab === 'review' ? 'page' : undefined} onClick={() => changeTab('review')}>01 · Claim 검토</button>
      <button aria-current={tab === 'golden' ? 'page' : undefined} onClick={() => changeTab('golden')}>02 · Golden 검증</button>
      <button aria-current={tab === 'evaluation' ? 'page' : undefined} onClick={() => changeTab('evaluation')}>03 · 지표 판정</button>
    </nav>
    {state.fixture_changed && <p role="alert" className="error">Fixture가 변경되었습니다. 기존 결과를 보존하고 새 사례 버전으로 진행하세요.</p>}
    {state.incomplete_runs.length > 0 && <p className="notice">중단되었거나 아직 응답하지 않은 실행 {state.incomplete_runs.length}개가 있습니다. 자동 재호출하지 않습니다.</p>}
    {error && <p role="alert" className="error">{error}</p>}
    {clock.timingError && <p role="alert" className="error">{clock.timingError}</p>}
    {message && <p role="status" className="success">{message}</p>}
    <div className="columns">
      <aside className="evidence-panel"><div className="panel-label"><h3>Evidence</h3><span>{state.case.evidence.length}개 · 합성 자료</span></div>
        {state.case.evidence.map(e => <article id={`evidence-${e.id}`} className="evidence-card" key={e.id}>
          <div className="evidence-meta"><strong>{e.source_type}</strong><span>{e.repository_key}</span></div>
          <h4>{e.id}</h4><p className="small">{e.locator} · {e.occurred_at}</p>
          <pre>{e.content}</pre><p className="provenance">{e.provenance_note}</p>
        </article>)}
      </aside>
      <section className="work-panel">
        {tab === 'review' && <>
          <div className="panel-label"><h3>Claim 검토</h3><span>{latestReview?.review_status ?? 'REVIEW_REQUIRED'}</span></div>
          <div className="controls"><label>응답 방식<select aria-label="응답 방식" value={mode} disabled={busy} onChange={e => setMode(e.target.value as typeof mode)}>
            <option value="mock">오프라인 데모</option><option value="gemini">Gemini · 준비 필요</option>
          </select></label><button className="primary" disabled={busy || state.fixture_changed} onClick={() => void perform(() => run())}>{busy ? '처리 중…' : '초안 만들기'}</button></div>
          <p className="small">데모는 의도적인 오류가 있는 고정 응답입니다. AI 품질·자연어 수정 성능을 측정한 결과가 아닙니다.</p>
          <div className="timer-bar"><button onClick={clock.toggle}>{clock.running ? '검토 일시정지' : '검토 시간 시작'}</button>
            <span>{clock.running ? '● 읽기·판단 시간을 기록 중' : '일시정지'}</span>
            <button className="subtle" onClick={() => void perform(async () => { clock.timer.current?.checkpoint(); await load(); })}>저장 시간 확인</button>
          </div>
          <p className="small">Active {seconds(state.timing.active_ms)} · Wait {seconds(state.timing.wait_ms)} · 중첩 {seconds(state.timing.overlap_ms)}<br />
            Golden·지표 탭 이동 시 Active 기록을 멈춥니다. 중단된 세션의 마지막 구간은 미측정일 수 있습니다.</p>
          {state.revisions.length > 0 && <label className="revision-picker">Revision<select aria-label="Revision" value={revisionId} onChange={e => setRevisionId(e.target.value)}>
            {state.revisions.map((r, i) => <option key={r.id} value={r.id}>Revision {i + 1} · {r.mode} · {new Date(r.recorded_at).toLocaleTimeString()}</option>)}
          </select></label>}
          {revision ? <>
            {revision.correction && <p className="notice">이 Revision의 수정 요청: {revision.correction}</p>}
            {revision.draft.claims.map(claim => <article className="claim-card" key={claim.id}>
              <div className="claim-heading"><h4>{state.case.questions.find(q => q.id === claim.question_id)?.label}</h4>
                <span className={`status status-${claim.evidence_status.toLowerCase()}`}>{claim.evidence_status}</span></div>
              <p>{claim.text ?? claim.unknown_reason}</p><p className="small">{claim.record_kind}</p>
              <div className="citations">{claim.evidence_ids.map(id => <a key={id} href={`#evidence-${id}`}>↗ {id}</a>)}</div>
            </article>)}
            <div className="links-box"><h4>Event → Story 연결</h4>{revision.draft.links.map(l => <p key={l.event_id}>{l.event_id} → <strong>{l.story_key}</strong></p>)}</div>
            <label>수정 요청 / 보류 사유 / 추가 확인 질문<textarea aria-label="수정 요청" value={note} maxLength={4000} onChange={e => setNote(e.target.value)} placeholder="짧게 필요한 판단을 남겨주세요." /></label>
            <div className="actions">
              <button disabled={busy || !current || !note.trim()} onClick={() => void perform(() => run(true))}>수정 요청 · 새 Revision</button>
              <button className="primary" disabled={busy || !current} onClick={() => void perform(() => review('APPROVE'))}>이 Revision 승인</button>
              <button disabled={busy || !current} onClick={() => void perform(() => review('HOLD'))}>보류</button>
              <button disabled={busy || !current || !note.trim()} onClick={() => void perform(() => review('CLARIFICATION'))}>추가 확인 필요</button>
              <button disabled={busy || !current || !note.trim()} onClick={() => void perform(() => review('UI_FRICTION'))}>UI 불편 기록</button>
            </div><p className="small">승인해도 HYPOTHESIS·INFERRED는 그대로 유지됩니다. 과거 Revision은 읽기 전용입니다.</p>
          </> : <div className="empty"><span>01</span><h4>근거부터 살펴보세요.</h4><p>초안을 만든 뒤 주장과 Evidence를 함께 검토할 수 있습니다.</p></div>}
        </>}
        {tab === 'golden' && <>
          <div className="panel-label"><h3>Golden Candidate</h3><span>{approval ? '승인 이력 있음' : '사용자 승인 대기'}</span></div>
          <p>아래 내용은 정답 후보입니다. Evidence로 확인한 뒤 수정·저장하고 별도로 승인하세요.</p>
          {golden.draft.claims.map((claim, i) => <article className="claim-card" key={claim.id}>
            <h4>{state.case.questions.find(q => q.id === claim.question_id)?.label}</h4>
            <label>Evidence Status<select value={claim.evidence_status} onChange={e => updateGolden(g => {
              const c = g.draft.claims[i]; c.evidence_status = e.target.value as typeof c.evidence_status;
              if (c.evidence_status === 'UNKNOWN') { c.text = null; c.unknown_reason = '근거 부족'; g.important_question_ids = g.important_question_ids.filter(q => q !== c.question_id); }
              else { c.text = ''; c.unknown_reason = null; }
            })}>{statuses.map(s => <option key={s}>{s}</option>)}</select></label>
            <label>{claim.evidence_status === 'UNKNOWN' ? '판단 불가 사유' : '기대 Claim'}<textarea value={claim.text ?? claim.unknown_reason ?? ''} onChange={e => updateGolden(g => {
              if (claim.evidence_status === 'UNKNOWN') g.draft.claims[i].unknown_reason = e.target.value;
              else g.draft.claims[i].text = e.target.value;
            })} /></label>
            <label>기록 종류<select value={claim.record_kind} onChange={e => updateGolden(g => { g.draft.claims[i].record_kind = e.target.value; })}>{kinds.map(k => <option key={k}>{k}</option>)}</select></label>
            <div className="checks">{state.case.evidence.map(e => <label key={e.id}><input type="checkbox" checked={claim.evidence_ids.includes(e.id)} onChange={event => updateGolden(g => {
              g.draft.claims[i].evidence_ids = event.target.checked ? [...claim.evidence_ids, e.id] : claim.evidence_ids.filter(id => id !== e.id);
            })} />근거: {e.id}</label>)}</div>
            <label className="check"><input type="checkbox" disabled={claim.evidence_status === 'UNKNOWN'} checked={golden.important_question_ids.includes(claim.question_id)} onChange={e => updateGolden(g => {
              g.important_question_ids = e.target.checked ? [...g.important_question_ids, claim.question_id] : g.important_question_ids.filter(q => q !== claim.question_id);
            })} />Recall 분모에 포함할 중요 사실</label>
          </article>)}
          <div className="links-box"><h4>연결 정답: 같은 Story에는 같은 이름</h4>{golden.draft.links.map((link, i) => <label key={link.event_id}>{link.event_id}<input value={link.story_key} onChange={e => updateGolden(g => { g.draft.links[i].story_key = e.target.value; })} /></label>)}</div>
          <label>생성 금지 주장 · 한 줄에 하나<textarea value={golden.forbidden_claims.join('\n')} onChange={e => updateGolden(g => { g.forbidden_claims = e.target.value.split('\n').filter(Boolean); })} /></label>
          <label>판정 메모<textarea value={golden.annotation_notes} onChange={e => updateGolden(g => { g.annotation_notes = e.target.value; })} /></label>
          <button disabled={busy} onClick={() => void perform(async () => {
            const savedEditVersion = goldenEditVersion.current;
            await api(`cases/${caseId}/goldens`, golden); await load();
            const editedWhileSaving = goldenEditVersion.current !== savedEditVersion;
            setDirty(editedWhileSaving); setAcknowledged(false);
            setMessage(editedWhileSaving
              ? 'Candidate를 저장했지만 추가 편집 내용은 아직 저장되지 않았습니다. 다시 저장한 뒤 승인하세요.'
              : '새 Candidate를 저장했습니다. 아직 승인되지 않았습니다.');
          })}>Golden Candidate 저장</button>
          <label className="check"><input type="checkbox" checked={acknowledged} onChange={e => setAcknowledged(e.target.checked)} />Evidence를 확인했고 이 정답 버전을 승인합니다.</label>
          <button className="primary" disabled={busy || dirty || !candidate || !acknowledged} onClick={() => void perform(async () => {
            await api(`cases/${caseId}/golden-approvals`, {candidate_id: candidate!.id, candidate_hash: candidate!.golden_hash, acknowledged: true});
            await load(); setAcknowledged(false); setMessage('이 Golden 버전의 사용자 승인을 저장했습니다. 이전 버전도 보존됩니다.');
          })}>저장된 Golden 승인</button>
        </>}
        {tab === 'evaluation' && <>
          <div className="panel-label"><h3>사람의 판정 → 지표</h3><span>Gate 미설정</span></div>
          <p>정답 일치를 자동 추정하지 않습니다. 승인된 Golden과 현재 선택 Revision을 대조하세요.</p>
          {!approval && <p className="notice">먼저 Golden Candidate를 검토하고 승인해야 합니다.</p>}
          {!revision && <p className="notice">먼저 초안을 생성해야 합니다.</p>}
          {revision && <p className="small">대상: Revision {state.revisions.indexOf(revision) + 1} · {revision.mode}</p>}
          {judgments.map((judgment, i) => {
            const claim = revision!.draft.claims[i];
            const expected = approvedGolden?.draft.claims.find(c => c.question_id === claim.question_id);
            return <article className="claim-card" key={claim.id}><h4>{claim.question_id}</h4>
              <p>출력: {claim.text ?? claim.unknown_reason} ({claim.evidence_status})</p>
              <p className="expected">승인 정답: {expected?.text ?? expected?.unknown_reason ?? '미승인'} ({expected?.evidence_status ?? '—'})</p>
              {(['supported', 'content_correct', 'severe_fabrication'] as const).map((key, index) => <label className="check" key={key}>
                <input type="checkbox" checked={judgment[key]} onChange={e => setJudgments(old => old.map((j, n) => n === i ? {...j, [key]: e.target.checked} : j))} />
                {['표현된 확실성 수준까지 근거가 뒷받침함', '내용이 정답과 일치함', '중대한 수치·테스트·과거 Reasoning 날조'][index]}
              </label>)}
              <label>판정 이유<input value={judgment.note} onChange={e => setJudgments(old => old.map((j, n) => n === i ? {...j, note: e.target.value} : j))} /></label>
            </article>;
          })}
          <label className="check"><input type="checkbox" checked={acknowledged} onChange={e => setAcknowledged(e.target.checked)} />모든 출력 Claim을 직접 판정했습니다.</label>
          <button className="primary" disabled={busy || !approval || !revision || !acknowledged} onClick={() => void perform(async () => {
            await api(`cases/${caseId}/evaluations`, {revision_id: revisionId, golden_approval_id: approval!.id, judgments, acknowledged: true});
            await load(); setAcknowledged(false); setMessage('분자·분모와 판정을 저장했습니다.');
          })}>판정 저장 · 지표 계산</button>
          {state.evaluations.map(result => <details className="result" key={result.id}><summary>Evaluation · {result.mode === 'mock' ? '데모 — AI 품질 근거 아님' : 'Gemini'} · {result.id.slice(0, 8)}</summary><pre>{JSON.stringify(result.metrics, null, 2)}</pre></details>)}
        </>}
      </section>
    </div>
  </>;
}
