import {useEffect, useRef, useState} from 'react';
import {api} from './model';

interface Material {id: string; filename: string; size_bytes: number; extension: string; added_at: string}
interface Policy {extensions: string[]; max_bytes: number}
export interface UploadItem {name: string; file: File | null}
export interface UploadFailure {filename: string; reason: string; notUploaded: string[]}
const message = (e: unknown) => e instanceof Error ? e.message : '요청에 실패했습니다.';
const size = (bytes: number) => bytes < 1024 ? `${bytes} B` : bytes < 1024 * 1024
  ? `${(bytes / 1024).toFixed(1)} KiB` : `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;

export async function uploadBatch(items: UploadItem[], upload: (file: File) => Promise<void>): Promise<UploadFailure | null> {
  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    try {
      if (!item.file) throw new Error('폴더 업로드는 지원하지 않습니다. 파일을 선택하세요.');
      await upload(item.file);
    } catch (e) {
      return {filename: item.name, reason: message(e), notUploaded: items.slice(i).map(item => item.name)};
    }
  }
  return null;
}

export default function Materials({projectId}: {projectId: string}) {
  const [materials, setMaterials] = useState<Material[]>([]);
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [error, setError] = useState('');
  const [failure, setFailure] = useState<UploadFailure | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const controller = useRef(new AbortController());
  const working = useRef(false);
  const path = `projects/${projectId}/materials`;

  async function refresh(signal: AbortSignal) {
    const list = await api<Material[]>(path, 'GET', undefined, signal);
    if (!signal.aborted) setMaterials(list);
  }
  useEffect(() => {
    const abort = new AbortController(); controller.current = abort;
    void Promise.all([refresh(abort.signal), api<Policy>('material-policy', 'GET', undefined, abort.signal).then(p => {
      if (!abort.signal.aborted) setPolicy(p);
    })]).catch(e => {if (!abort.signal.aborted) setError(message(e));})
      .finally(() => {if (!abort.signal.aborted) setLoading(false);});
    return () => {abort.abort();};
    // The parent keys this component by Project, never by Collection.
  }, [projectId]);

  async function add(items: UploadItem[]) {
    if (working.current || !policy || !items.length) return;
    working.current = true; setBusy(true); setError(''); setFailure(null);
    const signal = controller.current.signal;
    try {
      const result = await uploadBatch(items, async file => {
        if (signal.aborted) throw new Error('업로드가 중단됐습니다.');
        if (file.size > policy.max_bytes) throw new Error(`파일 크기 제한(${size(policy.max_bytes)})을 초과했습니다.`);
        const response = await fetch(`/api/${path}`, {method: 'PUT', signal,
          headers: {'Content-Type': 'application/octet-stream', 'X-File-Name': encodeURIComponent(file.name)}, body: file});
        const data = await response.json();
        if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '파일 업로드에 실패했습니다.');
        if (!signal.aborted) setMaterials(old => [data as Material, ...old]);
      });
      if (!signal.aborted) setFailure(result);
      await refresh(signal);
    } catch (e) {if (!signal.aborted) setError(message(e));}
    finally {working.current = false; if (!signal.aborted) setBusy(false);}
  }

  async function remove(material: Material) {
    if (working.current || !window.confirm(`“${material.filename}” 자료를 영구 삭제할까요?\n보관 사본과 정보가 삭제되며 복구할 수 없습니다. PC의 원본 파일은 유지됩니다.`)) return;
    working.current = true; setBusy(true); setError('');
    const signal = controller.current.signal;
    try {
      await api(`${path}/${material.id}`, 'DELETE', undefined, signal);
      if (!signal.aborted) setMaterials(old => old.filter(item => item.id !== material.id));
    } catch (e) {if (!signal.aborted) setError(message(e));}
    finally {working.current = false; if (!signal.aborted) setBusy(false);}
  }

  return <section className="user-materials" aria-label="사용자 추가 자료">
    <h3>사용자 추가 자료</h3>
    <p className="hint">이 프로젝트에 직접 추가한 자료입니다. 수집 기록과 별도로 보존하며 수집량·증감에는 포함하지 않습니다.</p>
    <div className={`material-drop ${dragging ? 'dragging' : ''}`} aria-label="파일 놓기 영역"
      onDragOver={e => {e.preventDefault(); if (!busy) setDragging(true);}}
      onDragLeave={() => setDragging(false)} onDrop={e => {
        e.preventDefault(); setDragging(false);
        const entries = Array.from(e.dataTransfer.items).filter(item => item.kind === 'file');
        const items: UploadItem[] = entries.length ? entries.map(item => {
          const entry = item.webkitGetAsEntry?.();
          const file = item.getAsFile();
          return {name: entry?.name ?? file?.name ?? '폴더', file: entry?.isDirectory ? null : file};
        }) : Array.from(e.dataTransfer.files).map(file => ({name: file.name, file}));
        void add(items);
      }}>
      <button disabled={busy || !policy} onClick={() => input.current?.click()}>파일 추가</button>
      <input ref={input} type="file" multiple hidden aria-label="추가할 파일" accept={policy?.extensions.join(',')}
        onChange={e => {void add(Array.from(e.target.files ?? []).map(file => ({name: file.name, file}))); e.target.value = '';}}/>
      <p>{busy ? '처리 중…' : '여기에 파일을 끌어 놓거나 파일 추가를 선택하세요. 폴더는 지원하지 않습니다.'}</p>
      {policy && <small>{policy.extensions.join(', ')} · 파일당 {size(policy.max_bytes)} 이하 · 원본 사본 보존</small>}
    </div>
    {error && <p className="alert" role="alert">{error}</p>}
    {failure && <div className="alert upload-failure" role="alert"><strong>{failure.filename}</strong>: {failure.reason}
      <p>업로드되지 않음</p><ul>{failure.notUploaded.map((name, i) => <li key={i}>{name}</li>)}</ul></div>}
    {loading ? <p className="hint">자료를 불러오는 중입니다.</p> : !materials.length && <p className="hint">추가한 자료가 없습니다.</p>}
    <ul className="material-list">{materials.map(material => <li key={material.id}>
      <div><a href={`/api/${path}/${material.id}/file`} download={material.filename}>{material.filename}</a>
        <small>{material.extension.slice(1).toUpperCase()} · {size(material.size_bytes)} · {new Date(material.added_at).toLocaleString('ko-KR')} 추가</small></div>
      <button disabled={busy} aria-label={`${material.filename} 삭제`} onClick={() => void remove(material)}>삭제</button>
    </li>)}</ul>
    <p className="hint">최근 추가한 자료부터 표시합니다. 파일명을 선택하면 보관한 원본 사본을 가져옵니다.</p>
  </section>;
}
