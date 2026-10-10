import {useEffect, useState} from 'react';
import {api, statusLabels, type Project, type ProjectStatus, type Settings} from './model';
import type {Collection} from './model';
import {message, Text, type Confirm} from './ui';
interface Review {message: string; missing_count: number; missing_objects: string[]; warning_token: string | null}
export default function ProjectForm({project, confirm, dirty, saved, created}: {project?: Project; confirm: Confirm; dirty: (value: boolean) => void; saved: (p: Project) => void; created: (p: Project, c: Collection | null, error: string) => void}) {
  const initial = {name: project?.name ?? '', path: project?.path ?? '', status: project?.status ?? 'new', base_branch: project?.base_branch ?? ''};
  const [form, setForm] = useState(initial), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  async function browse() {
    setBusy(true); setError('');
    try {
      const {token} = await api<{token:string}>('folders/picker');
      const chosen = await api<{cancelled:boolean;path:string | null}>('folders/picker','POST',{token},undefined,{'X-Project-Log-Intent':'choose-folder'});
      if(chosen.cancelled || !chosen.path) return;
      const info = await api<{path:string;suggested_base_branch:string | null}>('repositories/diagnose','POST',{path:chosen.path});
      setForm(old=>({...old,path:info.path,base_branch:old.base_branch || info.suggested_base_branch || '',name:old.name || info.path.split('/').at(-1) || ''}));
    } catch(e) {setError(message(e));} finally {setBusy(false);}
  }
  const changed = Object.keys(initial).some(key => form[key as keyof typeof form] !== initial[key as keyof typeof initial]);
  useEffect(() => {dirty(changed); return () => dirty(false);}, [changed, dirty]);
  useEffect(() => {const prevent = (e: BeforeUnloadEvent) => {if (changed) e.preventDefault();}; window.addEventListener('beforeunload', prevent); return () => window.removeEventListener('beforeunload', prevent);}, [changed]);
  async function requestSave() {
    setBusy(true); setError('');
    try {
      const review = project && form.path !== project.path ? await api<Review>(`projects/${project.id}/path-review`, 'POST', form) : null;
      const labels = {name: '프로젝트 이름', path: '프로젝트 경로', status: '프로젝트 상태', base_branch: '기본 브랜치'};
      const differences = Object.entries(form).filter(([key,value]) => value !== initial[key as keyof typeof initial]);
      confirm(project ? '정말 프로젝트 설정을 변경하시겠습니까?' : '프로젝트를 등록하고 수집하시겠습니까?', <><dl className="changes">{differences.map(([key,value]) => <div key={key}><dt>{labels[key as keyof typeof labels]}</dt><dd><Text text={String(initial[key as keyof typeof initial])}/><span> → </span><Text text={value}/></dd></div>)}</dl>{review && <div className={review.missing_count ? 'notice' : ''}><p>{review.message}</p>{!!review.missing_count && <p>확인된 누락 객체: {review.missing_count}개. 원본 조회 범위가 제한될 수 있습니다.</p>}</div>}</>, async () => {
        setBusy(true);
        try {
          if (project) {const p = await api<Project>(`projects/${project.id}`, 'PATCH', {...form, acknowledged_warnings: review?.warning_token ?? null, expected_updated_at: project.updated_at} satisfies Settings); dirty(false); saved(p);}
          else {const p = await api<Project>('projects', 'POST', {...form, collect: false}); let collection: Collection | null = null, failure = ''; try {collection = await api<Collection | null>(`projects/${p.id}/collections`, 'POST'); if (!collection) failure = '프로젝트 등록은 완료됐지만 첫 수집의 자료 확보에 실패했습니다.';} catch(e) {failure = `프로젝트 등록은 완료됐지만 첫 수집 시작에 실패했습니다: ${message(e)}`;} dirty(false); created(p, collection, failure);}
        } catch(e) {setError(message(e)); throw e;} finally {setBusy(false);}
      }, !project);
    } catch(e) {setError(message(e));} finally {setBusy(false);}
  }
  return <section className="card project-form"><h2>{project ? '프로젝트 설정' : '프로젝트 추가'}</h2><form onSubmit={e => {e.preventDefault(); void requestSave();}}>
    <label>프로젝트 이름<input required maxLength={200} value={form.name} onChange={e => setForm({...form,name:e.target.value})}/></label>
    <div className="field" role="group" aria-label="프로젝트 경로"><span>프로젝트 경로</span><div className="path-input"><Text text={form.path || 'Git Repository root를 선택하세요.'}/><button type="button" disabled={busy} onClick={() => void browse()}>찾아보기</button>{form.path && <button type="button" onClick={() => void navigator.clipboard.writeText(form.path).catch(() => setError('경로를 복사하지 못했습니다.'))}>경로 복사</button>}</div></div>
    <label>프로젝트 상태<select aria-label="프로젝트 상태" value={form.status} onChange={e => setForm({...form,status:e.target.value as ProjectStatus})}>{Object.entries(statusLabels).map(([value,label]) => <option key={value} value={value}>{label}</option>)}</select></label>
    <label>기본 브랜치<input required value={form.base_branch} onChange={e => setForm({...form,base_branch:e.target.value})}/></label>
    <p className="hint">프로젝트 상태는 사용자가 선언한 개발 상태입니다.</p>{error && <p className="alert" role="alert">{error}</p>}<footer><button className="primary" disabled={busy || !form.path || (project && !changed)}>{busy ? '처리 중…' : project ? '변경사항 저장' : '등록 후 첫 수집'}</button></footer>
  </form></section>;
}
