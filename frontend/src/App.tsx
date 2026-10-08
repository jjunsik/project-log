import {useEffect, useRef, useState} from 'react';
import Browser from './Browser';
import Materials from './Materials';
import {api, ApiError, collectionLabels, collectionOption, collectionSelection, hasActiveCollection, suggestedName, statusLabels} from './model';
import type {Collection, Project, ProjectStatus, Settings} from './model';

const defaults: Settings = {name: '', status: 'ongoing', base_branch: ''};
const date = (value?: string | null) => value ? new Date(value).toLocaleString('ko-KR') : '—';
const message = (error: unknown) => error instanceof Error ? error.message : '요청에 실패했습니다.';

function Fields({value, onChange}: {value: Settings; onChange: (value: Settings) => void}) {
  return <>
    <label>프로젝트 이름<input required maxLength={200} value={value.name} onChange={e => onChange({...value, name: e.target.value})} /></label>
    <label>프로젝트 상태<select value={value.status} onChange={e => onChange({...value, status: e.target.value as ProjectStatus})}>
      {Object.entries(statusLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
    </select></label>
    <label>기준 브랜치<input required maxLength={4096} placeholder="local 브랜치 이름" value={value.base_branch} onChange={e => onChange({...value, base_branch: e.target.value})}/></label>
  </>;
}

export default function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [path, setPath] = useState('');
  const [settings, setSettings] = useState<Settings>(defaults);
  const [selected, setSelected] = useState('');
  const [selectedCollection, setSelectedCollection] = useState('');
  const [project, setProject] = useState<Project | null>(null);
  const [collection, setCollection] = useState<Collection | null>(null);
  const [memo, setMemo] = useState<{title: string; description: string} | null>(null);
  const [edit, setEdit] = useState<Settings | null>(null);
  const [error, setError] = useState('');
  const [pollError, setPollError] = useState('');
  const [workerError, setWorkerError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [diagnosing, setDiagnosing] = useState(false);
  const [diagnosisError, setDiagnosisError] = useState('');
  const branchEdited = useRef(false);
  const active = hasActiveCollection(project);

  useEffect(() => {
    if (!path.trim()) {setDiagnosing(false); setDiagnosisError(''); return;}
    const controller = new AbortController();
    setDiagnosing(true); setDiagnosisError('');
    const timer = setTimeout(async () => {
      try {
        const info = await api<{suggested_base_branch: string | null}>('repositories/diagnose', 'POST', {path}, controller.signal);
        if (!controller.signal.aborted && !branchEdited.current) setSettings(old => ({...old, base_branch: info.suggested_base_branch ?? ''}));
      } catch (e) {if (!controller.signal.aborted) setDiagnosisError(message(e));}
      finally {if (!controller.signal.aborted) setDiagnosing(false);}
    }, 300);
    return () => {controller.abort(); clearTimeout(timer);};
  }, [path]);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const list = await api<Project[]>('projects', 'GET', undefined, controller.signal);
        const health = await api<{worker_error: string | null; worker_running: boolean}>('health', 'GET', undefined, controller.signal);
        if (!controller.signal.aborted) {
          setProjects(list); setPollError('');
          setWorkerError(health.worker_error ?? (health.worker_running ? '' : 'Background 수집이 실행되고 있지 않습니다. 앱을 확인하세요.'));
        }
      } catch (e) { if (!controller.signal.aborted) setPollError(message(e)); }
      if (!controller.signal.aborted) timer = setTimeout(poll, 1200);
    }
    void poll();
    return () => {controller.abort(); clearTimeout(timer);};
  }, []);

  useEffect(() => {setProject(null); setEdit(null); setMemo(null);}, [selected]);

  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    setCollection(null);
    async function poll() {
      try {
        let p = await api<Project>(`projects/${selected}`, 'GET', undefined, controller.signal);
        let id = collectionSelection(p.collections ?? [], selectedCollection);
        let c: Collection | null = null;
        if (id) {
          try {c = await api<Collection>(`collections/${id}`, 'GET', undefined, controller.signal);}
          catch (e) {
            if (!(e instanceof ApiError) || e.status !== 404) throw e;
            // A deletion/cleanup can commit between the list and detail reads.
            p = await api<Project>(`projects/${selected}`, 'GET', undefined, controller.signal);
            id = collectionSelection(p.collections ?? [], id);
          }
        }
        if (!controller.signal.aborted) {setProject(p); setCollection(c); if (id !== selectedCollection) {setSelectedCollection(id); setMemo(null);}}
      } catch (e) { if (!controller.signal.aborted) setError(message(e)); }
      if (!controller.signal.aborted) timer = setTimeout(poll, 900);
    }
    void poll();
    return () => {controller.abort(); clearTimeout(timer);};
  }, [selected, selectedCollection]);

  async function perform(task: () => Promise<void>) {
    setBusy(true); setError(''); setNotice('');
    try {await task();} catch (e) {setError(message(e));} finally {setBusy(false);}
  }

  return <main>
    <header><a className="brand" href="/">PROJECT LOG<span>개발의 근거를 남기다</span></a><span className="local-badge">LOCAL · 개인 작업 공간</span></header>
    <section className="intro"><p className="eyebrow">첫 기록은 프로젝트에서 시작합니다</p><h1>개발의 흔적을, 놓치지 않도록.</h1><p>로컬 프로젝트를 연결해 수집한 변경 이력과 프로젝트 자료를 수집 기록별로 확인합니다.</p></section>
    {(error || pollError || workerError) && <div className="alert" role="alert">{[error, pollError, workerError].filter(Boolean).join(' ')}</div>}
    {notice && <div className="notice" role="status">{notice}</div>}
    <p className="quality-guide">수집된 정보가 적으면 이후 결과물의 품질이 낮을 수 있습니다. 수집 자료 현황에서 확보한 정보량을 확인하세요.</p>
    <div className="layout">
      <aside>
        <section className="panel"><h2>프로젝트 등록</h2>
          <form onSubmit={e => {e.preventDefault(); void perform(async () => {
            const p = await api<Project>('projects', 'POST', {...settings, path});
            setSelected(p.id); setSelectedCollection(''); setNotice('프로젝트를 등록했습니다. 수집 상태를 확인하세요.');
            setPath(''); setSettings(defaults);
          });}}>
            <label>프로젝트 경로<input required placeholder="/Users/me/projects/my-project" value={path} onChange={e => {
              const next = e.target.value;
              branchEdited.current = false;
              setSettings(old => ({...old, base_branch: '', name: !old.name || old.name === suggestedName(path) ? suggestedName(next) : old.name}));
              setPath(next);
            }} /></label>
            <p className="hint">현재 Project Log는 Git으로 관리되는 프로젝트를 지원합니다. Repository 최상위 폴더 경로를 입력하세요. 모든 프로젝트 상태에서 Commit이나 소스 파일이 없어도 등록할 수 있습니다.</p>
            <Fields value={settings} onChange={next => {if (next.base_branch !== settings.base_branch) branchEdited.current = true; setSettings(next);}}/>
            <p className="hint">{diagnosing ? 'local 브랜치를 확인하는 중입니다.' : 'local main, master 또는 Commit 없는 최초 브랜치를 초기값으로 제안합니다. 직접 수정할 수 있으며 수집할 때 기준 브랜치가 checkout되어 있어야 합니다.'}</p>
            {diagnosisError && <p className="alert" role="alert">{diagnosisError}</p>}
            <button className="primary" disabled={busy || diagnosing}>{busy ? '처리 중…' : '등록하고 수집 시작'}</button>
          </form>
          <details className="policy"><summary>어떤 자료를 보존하나요?</summary><p>Git 이력과 안전한 text diff, index·Working Tree·untracked의 안전한 text 본문을 확보합니다. Repository Root의 docs/는 Git에서 제외되어 있어도 하위 문서를 수집합니다. 일반 ignored 영역은 경로와 상태만 남기며 재귀 수집하지 않습니다. 문서에도 Secret 의심·Binary·대용량 등 같은 안전 검사를 적용합니다.</p><p>과거 전체 파일 조회에는 원본 Git Repository가 필요합니다. 이 기능은 Repository 백업이 아닙니다.</p></details>
        </section>
        <section className="project-list"><h2>내 프로젝트 <span>{projects.length}</span></h2>
          {!projects.length && <p className="hint">등록된 프로젝트가 없습니다.</p>}
          {projects.map(p => <button key={p.id} className={`project-card ${selected === p.id ? 'selected' : ''}`} onClick={() => {setSelected(p.id); setSelectedCollection(''); setError('');}}>
            <strong>{p.name}</strong><span>{statusLabels[p.status]} · {p.collection_state ? collectionLabels[p.collection_state] : p.collection_busy ? '수집 정리 중' : '수집 기록 없음'}</span><small>{p.path}</small>
          </button>)}
        </section>
      </aside>
      <section className="panel detail">
        {!project ? <div className="empty"><span className="empty-symbol">↗</span><h2>{selected ? '프로젝트를 불러오는 중입니다' : '프로젝트의 첫 수집을 시작하세요'}</h2><p>등록 후 수집 진행 상황과 확보한 자료의 종류를<br/>여기서 확인할 수 있습니다.</p></div> : <>
          <div className="section-title"><div><p className="eyebrow">PROJECT</p><h2>{project.name}</h2></div><button onClick={() => setEdit(edit ? null : {name: project.name, status: project.status, base_branch: project.base_branch ?? ''})}>설정 변경</button></div>
          <p className="path">{project.path}</p><p className="hint">{statusLabels[project.status]}</p>
          <p className="project-branch hint">현재 기준 브랜치: {project.base_branch || '미설정'}</p>
          {!project.base_branch && <p className="alert">기준 브랜치가 미설정입니다. 설정 변경에서 유효한 local 브랜치를 저장한 뒤 수집하세요. 과거 수집 기록은 그대로 확인할 수 있습니다.</p>}
          <button className="primary" disabled={busy || active || !project.base_branch} onClick={() => void perform(async () => {
            const c = await api<Collection | null>(`projects/${project.id}/collections`, 'POST');
            const p = await api<Project>(`projects/${project.id}`);
            setProject(p); setCollection(c); setSelectedCollection(c?.id ?? collectionSelection(p.collections ?? [], selectedCollection)); setMemo(null);
            setNotice(c ? '현재 상태의 새 수집을 시작했습니다. 이전 기록은 보존됩니다.' : '이번 수집이 중단되었습니다.');
          })}>지금 수집</button>
          {project.collections?.[0] && <p className="latest-collection" role="status">최신 수집: {date(project.collections[0].created_at)} · {collectionLabels[project.collections[0].state]}{active ? ' · 수집 또는 정리가 끝나면 다시 수집할 수 있습니다.' : ''}</p>}
          {edit && <form className="edit" onSubmit={e => {e.preventDefault(); void perform(async () => {
            const p = await api<Project>(`projects/${project.id}`, 'PATCH', edit); setProject(p);
            setProjects(old => old.map(item => item.id === p.id ? {...item, name: p.name, status: p.status, base_branch: p.base_branch} : item));
            setEdit(null); setNotice('프로젝트 설정을 저장했습니다.');
          });}}><Fields value={edit} onChange={setEdit}/><button disabled={busy}>설정 저장</button></form>}
          {project.collections && project.collections.length > 0 && <label>수집 기록<select aria-label="수집 기록" value={selectedCollection || project.collections[0].id} onChange={e => {setSelectedCollection(e.target.value); setMemo(null);}}>
            {project.collections.map(c => <option key={c.id} value={c.id}>{collectionOption(c)}</option>)}
          </select></label>}
          {collection && <>
            <div className={`collection-state ${collection.state}`} role="status"><strong>{collectionLabels[collection.state]}</strong><span>{collection.retry_of ? '원래 관측 자료로 재시도' : collection.kind === 'manual' ? '현재 시점의 수동 수집' : '등록 시점의 초기 수집'}</span></div>
            <h3>기본 정보</h3>
            <dl className="metadata"><div><dt>수집 날짜/시간</dt><dd>{date(collection.created_at)}</dd></div><div><dt>수집 상태</dt><dd>{collectionLabels[collection.state]}</dd></div><div><dt>관측 시점 HEAD</dt><dd>{collection.snapshot.head === undefined ? '미확보' : collection.snapshot.head ?? 'Commit 없음'}</dd></div><div><dt>수집 당시 Branch</dt><dd>{collection.snapshot.branch === undefined ? '미확보' : collection.snapshot.branch ?? '분리된 HEAD'}</dd></div><div><dt>상태 관측 시간</dt><dd>{date(collection.snapshot.started_at)} — {date(collection.snapshot.finished_at)}</dd></div></dl>
            {collection.snapshot.branch === undefined && <p className="hint">과거 브랜치 정보가 미확보된 기록입니다. 현재 기준 브랜치로 추정하지 않습니다.</p>}
            <Browser key={collection.id} collection={collection}/>
            <details className="policy"><summary>수집 상세 집계</summary><h3>확보한 원천 자료</h3>
            <div className="counts">{[
              ['Commit', collection.summary.commits], ['파일 변경', collection.summary.changes],
              ['HEAD 파일', collection.summary.head_files], ['HEAD 문서 후보', collection.summary.document_candidates],
              ['미커밋 상태 기록', collection.summary.working_entries], ['보존한 미커밋 본문', collection.summary.preserved_working_bodies],
              ['보존한 Working 본문', collection.summary.preserved_tracked_working_bodies],
              ['보존한 untracked 본문', collection.summary.preserved_untracked_bodies],
              ['보존한 로컬 문서 본문', collection.summary.preserved_document_bodies], ['수집 오류', collection.summary.errors],
            ].map(([label, count]) => <div key={String(label)}><strong>{count ?? '—'}</strong><span>{label}</span></div>)}</div>
            <p className="hint">수집 중 집계는 달라질 수 있습니다. 문서 후보는 경로·이름 기반 힌트이며 위 카테고리 집계와 다른 단위입니다.</p></details>
            {collection.summary.comparison?.available ? <section className="comparison"><h3>이전 관측과 비교</h3><div className="counts">{[
              ['신규', collection.summary.comparison.new], ['변경', collection.summary.comparison.changed],
              ['동일', collection.summary.comparison.unchanged], ['사라짐', collection.summary.comparison.deleted],
              ['본문 동일성 미확인', collection.summary.comparison.unknown],
            ].map(([label, count]) => <div key={String(label)}><strong>{count}</strong><span>{label}</span></div>)}</div><p className="hint">index·Working Tree·untracked·로컬 문서의 경로별 관측 항목을 비교합니다. 신규·사라짐은 관측 항목의 등장·부재이며 rename 판정이 아닙니다.</p></section> : <p className="comparison hint">{collection.summary.comparison?.reason === 'incomplete_snapshot' ? '미완성 관측이 있어 비교 결과를 확정할 수 없습니다.' : collection.summary.comparison?.reason === 'baseline_deleted' ? '비교 대상 수집이 삭제되었습니다.' : '비교할 이전 관측이 없습니다.'}</p>}
            {!!collection.issues?.length && <section className="issues"><h3>확인이 필요한 항목</h3><ul>{collection.issues.map(issue => <li key={issue.id}>{issue.message}
              {issue.context?.path && <small>{typeof issue.context.path === 'string' ? issue.context.path : issue.context.path.path}</small>}
              <small>{issue.phase} · {issue.code}</small></li>)}</ul></section>}
            {!!collection.summary.body_exclusions?.length && <details className="policy"><summary>본문 보존 제한 내역</summary><ul>{collection.summary.body_exclusions.map(item => <li key={item.reason}>{item.reason}: {item.count}건</li>)}</ul><p>정책상 제외 사유입니다. 수집 오류는 위 확인 항목에서 별도로 확인할 수 있습니다. Metadata와 가능한 Git Locator는 남깁니다.</p></details>}
            {!!collection.summary.body_errors?.length && <p className="hint">오류로 미확보한 본문: {collection.summary.body_errors.map(item => `${item.reason} ${item.count}건`).join(', ')}</p>}
            {collection.state === 'completed' && <section className="collection-memo">
              <h3>수집 기록 메모</h3>
              <p>{collection.title || '제목 없음'}</p><p className="memo-description">{collection.description || '설명 없음'}</p>
              <button disabled={busy} onClick={() => setMemo(memo ? null : {title: collection.title ?? '', description: collection.description ?? ''})}>제목·설명 편집</button>
              {memo && <form className="edit" onSubmit={e => {e.preventDefault(); void perform(async () => {
                const updated = await api<Collection>(`collections/${collection.id}`, 'PATCH', memo);
                setCollection(updated); setProject(await api<Project>(`projects/${project.id}`)); setMemo(null); setNotice('수집 기록 메모를 저장했습니다.');
              });}}><label>수집 제목<input maxLength={200} value={memo.title} onChange={e => setMemo({...memo, title: e.target.value})}/></label>
                <label>수집 설명<textarea maxLength={4000} value={memo.description} onChange={e => setMemo({...memo, description: e.target.value})}/></label>
                <button disabled={busy}>메모 저장</button></form>}
              <button disabled={busy} onClick={() => {
                if (!window.confirm('이 수집 기록과 전용 자료를 영구 삭제합니다. 복구할 수 없습니다. 삭제할까요?')) return;
                void perform(async () => {
                  await api(`collections/${collection.id}`, 'DELETE');
                  const p = await api<Project>(`projects/${project.id}`); setProject(p); setCollection(null); setMemo(null);
                  setSelectedCollection(collectionSelection(p.collections ?? [], selectedCollection)); setNotice('수집 기록을 삭제했습니다.');
                });
              }}>수집 기록 삭제</button>
            </section>}
            {['capturing', 'queued', 'running'].includes(collection.state) && <button disabled={busy} onClick={() => {
              if (!window.confirm('진행 중 수집을 취소하고 이번 수집 자료를 제거합니다. 취소 확정 후 철회할 수 없습니다. 취소할까요?')) return;
              void perform(async () => {
                await api(`collections/${collection.id}/cancel`, 'POST');
                setProject(await api<Project>(`projects/${project.id}`)); setNotice('취소를 확정했습니다. 이번 수집 자료를 정리합니다.');
              });
            }}>수집 취소</button>}
          </>}
          {!collection && <div className="empty"><h3>{active ? '수집 자료 정리 중입니다' : '수집 기록이 없습니다'}</h3><p>{active ? '정리가 끝나면 새로 수집할 수 있습니다.' : '지금 수집으로 자료를 확보하세요.'}</p></div>}
          <Materials key={project.id} projectId={project.id}/>
        </>}
      </section>
    </div>
    <footer>원천 근거를 보존하는 첫 단계입니다. AI 분석·개발 경험 생성은 아직 지원하지 않습니다.</footer>
  </main>;
}
