import {useEffect, useRef, useState} from 'react';
import {api} from './model';
import type {Collection} from './model';

type Page<T> = {items: T[]; total: number};
type Meta = Record<string, unknown>;
interface Observation {id: number; layer: string; metadata: Meta & {commit?: string; path_b64: string; path: string}; body_reason: string | null; observed_at: string; body_observed_at?: string; provenance: Meta; sha256?: string; repaired?: boolean; original_body_reason?: string}
interface FileNode {name: string; path: string; path_b64: string; directory: boolean; observations: Observation[]}
interface Commit {oid: string; metadata: {message?: string; message_reason?: string; parents: string[]; author?: {name?: string; email?: string; timestamp?: number; timezone?: string}; committer?: {timestamp?: number; timezone?: string}}; observed_at: string}
interface Change {id: number; parent_oid: string | null; body_reason: string | null; metadata: {old: {path: string}; new: {path: string}; status: string}}
type Counts = Record<'commits' | 'files' | 'documents', number | null>;
interface Overview {baseline_id: string | null; selected: {counts: Counts; observed: Counts}; previous: {counts: Counts} | null; delta: Counts}
const labels = {commits: '버전 관리 기록', files: '프로젝트 파일', documents: '개발 문서'};
const layers: Record<string, string> = {head: 'HEAD · Git object', index: 'index · staged', working: 'Working Tree', untracked: 'untracked', ignored: 'ignored · Metadata', document: '로컬 문서'};
const formatDate = (value?: string | null) => value ? new Date(value).toLocaleString('ko-KR') : '—';
const errorMessage = (e: unknown) => e instanceof Error ? e.message : '자료를 불러오지 못했습니다.';
export const encodePath = (value: string) => btoa(String.fromCharCode(...new TextEncoder().encode(value)));

function Pager({offset, total, onChange}: {offset: number; total: number; onChange: (n: number) => void}) {
  return <nav className="pager" aria-label="목록 페이지"><button disabled={!offset} onClick={() => onChange(Math.max(0, offset - 100))}>이전 페이지</button><span>{total ? `${offset + 1}–${Math.min(offset + 100, total)} / ${total}` : '0건'}</span><button disabled={offset + 100 >= total} onClick={() => onChange(offset + 100)}>다음 페이지</button></nav>;
}

export default function Browser({collection}: {collection: Collection}) {
  const [category, setCategory] = useState<'commits' | 'files' | 'documents'>('commits');
  const [directory, setDirectory] = useState('');
  const [trail, setTrail] = useState<{path: string; b64: string}[]>([]);
  const [offset, setOffset] = useState(0);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [list, setList] = useState<Page<FileNode | Commit> | null>(null);
  const [commit, setCommit] = useState<Commit | null>(null);
  const [changes, setChanges] = useState<Page<Change> | null>(null);
  const [changeOffset, setChangeOffset] = useState(0);
  const [file, setFile] = useState<FileNode | null>(null);
  const [observation, setObservation] = useState<Observation | null>(null);
  const [change, setChange] = useState<Change | null>(null);
  const [content, setContent] = useState<{body: string | null; body_reason: string | null; provenance?: string; read_at?: string} | null>(null);
  const [error, setError] = useState('');
  const parentRequest = useRef<AbortController | null>(null);
  useEffect(() => () => parentRequest.current?.abort(), []);
  const base = `collections/${collection.id}`;
  useEffect(() => {
    const controller = new AbortController();
    void api<Overview>(`${base}/overview`, 'GET', undefined, controller.signal).then(setOverview).catch(e => {if (!controller.signal.aborted) setError(errorMessage(e));});
    return () => controller.abort();
  }, [base, collection.state]);
  useEffect(() => {
    const controller = new AbortController();
    setList(null); setError('');
    const path = category === 'commits' ? `${base}/commits?offset=${offset}` : `${base}/browse/${category}?directory_b64=${encodeURIComponent(directory)}&offset=${offset}`;
    void api<Page<FileNode | Commit>>(path, 'GET', undefined, controller.signal).then(setList).catch(e => {if (!controller.signal.aborted) setError(errorMessage(e));});
    return () => controller.abort();
  }, [base, category, directory, offset, collection.state]);
  useEffect(() => {
    if (!commit) return;
    const controller = new AbortController(); setChanges(null);
    void api<Page<Change>>(`${base}/commits/${commit.oid}/changes?offset=${changeOffset}`, 'GET', undefined, controller.signal).then(setChanges).catch(e => {if (!controller.signal.aborted) setError(errorMessage(e));});
    return () => controller.abort();
  }, [base, commit, changeOffset]);
  useEffect(() => {
    setContent(null);
    if (!change && !observation) return;
    const controller = new AbortController();
    const path = change ? `${base}/content/changes/${change.id}` : observation!.layer === 'head'
      ? `${base}/source?commit=${observation!.metadata.commit}&path_b64=${encodeURIComponent(observation!.metadata.path_b64)}`
      : `${base}/content/working_entries/${observation!.id}`;
    void api<{body: string | null; body_reason: string | null; provenance?: string; read_at?: string}>(path, 'GET', undefined, controller.signal).then(setContent).catch(e => {if (!controller.signal.aborted) {setError(errorMessage(e)); setContent({body: null, body_reason: errorMessage(e)});}});
    return () => controller.abort();
  }, [base, observation, change]);
  const clearDetail = () => {parentRequest.current?.abort(); setFile(null); setObservation(null); setCommit(null); setChange(null); setContent(null); setChangeOffset(0);};
  const chooseCategory = (next: typeof category) => {setList(null); setCategory(next); setDirectory(''); setTrail([]); setOffset(0); clearDetail();};
  async function openParent(oid: string) {
    parentRequest.current?.abort();
    const controller = new AbortController(); parentRequest.current = controller;
    setError(''); setChange(null); setChangeOffset(0);
    try {const parent = await api<Commit>(`${base}/commits/${oid}`, 'GET', undefined, controller.signal); if (!controller.signal.aborted) setCommit(parent);} catch (e) {if (!controller.signal.aborted) setError(errorMessage(e));}
  }
  return <section className="evidence-browser">
    <h3>수집 자료 현황</h3>
    <div className="table-scroll"><table className="inventory"><thead><tr><th>자료 · 집계 단위</th><th>이전 수집</th><th>선택한 수집</th><th>증감</th></tr></thead><tbody>{(Object.keys(labels) as (keyof Counts)[]).map(key => <tr key={key}><th>{labels[key]}<small>{key === 'commits' ? 'Commit 수' : key === 'files' ? '고유 프로젝트 파일 수' : 'root docs/ 하위 문서 파일 수'}</small></th><td>{overview?.previous?.counts[key] ?? '—'}</td><td>{overview?.selected.counts[key] ?? '—'}</td><td>{overview?.delta[key] == null ? '—' : `${overview.delta[key]! > 0 ? '+' : ''}${overview.delta[key]}`}</td></tr>)}</tbody></table></div>
    <p className="hint">선택한 수집의 HEAD·index·Working 등의 고유 파일 경로를 셉니다. docs/는 개발 문서에만 포함합니다. ignored 디렉터리 내부와 submodule 내부는 집계하지 않습니다. 이전 수집은 기존 baseline을 따르며 재시도는 새 비교 기준이 아닙니다.</p>
    {overview && (!overview.previous || Object.values(overview.selected.counts).some(n => n === null) || Object.values(overview.previous.counts).some(n => n === null)) && <p className="hint">—: 비교 대상 없음 또는 수집 중·불완전 관측·당시 문서 수집 범위 미확보로 정확한 수치를 확정할 수 없습니다. 확보된 자료는 아래에서 확인할 수 있습니다.</p>}
    {overview && Object.values(overview.selected.counts).some(n => n === null) && <p className="hint">현재까지 확인된 관측 자료: Commit {overview.selected.observed.commits}개 · 고유 프로젝트 파일 {overview.selected.observed.files}개 · docs/ 문서 파일 {overview.selected.observed.documents}개. 전체 범위의 확보 완료를 뜻하지 않습니다.</p>}
    <h3>선택한 수집 자료 탐색</h3>
    <p className="hint">{formatDate(collection.created_at)}의 보존 자료입니다. 아래 조회는 현재 Working 파일을 읽지 않습니다.</p>
    <div className="tabs" role="tablist" aria-label="자료 카테고리">{(Object.keys(labels) as (keyof Counts)[]).map(key => <button key={key} role="tab" aria-selected={category === key} onClick={() => chooseCategory(key)}>{labels[key]}</button>)}</div>
    {error && <p className="alert" role="alert">{error}</p>}
    {category !== 'commits' && <nav className="breadcrumbs" aria-label="파일 경로"><button onClick={() => {setDirectory(''); setTrail([]); setOffset(0); clearDetail();}}>{category === 'documents' ? 'docs/' : 'Repository Root'}</button>{trail.map((node, index) => <button key={node.b64} onClick={() => {setDirectory(node.b64); setTrail(trail.slice(0, index + 1)); setOffset(0); clearDetail();}}>{node.path}</button>)}</nav>}
    {!list ? <p className="hint">목록을 불러오는 중입니다…</p> : <>
      {!list.total && <p className="hint">이 수집 기록에 확보된 {labels[category]}가 없습니다.{collection.state !== 'completed' ? ' 수집 상태와 오류를 함께 확인하세요.' : ''}</p>}
      <ul className="evidence-list">{list.items.map(item => category === 'commits' ? <li key={(item as Commit).oid}><button onClick={() => {clearDetail(); setCommit(item as Commit);}}><strong>{(item as Commit).metadata.message ?? 'Commit message 미확보'}</strong><small>{(item as Commit).oid}</small></button></li> : <li key={(item as FileNode).path_b64}><button onClick={() => {
        const node = item as FileNode; clearDetail();
        if (node.directory) {setDirectory(node.path_b64); setTrail([...trail, {path: node.name, b64: node.path_b64}]); setOffset(0);}
        else {setFile(node); setObservation(node.observations.find(o => ['working', 'document', 'untracked'].includes(o.layer)) ?? node.observations[0]);}
      }}>{(item as FileNode).directory ? '▸ ' : ''}{(item as FileNode).name}<small>{(item as FileNode).directory ? '폴더' : [...new Set((item as FileNode).observations.map(o => layers[o.layer]))].join(' · ')}</small></button></li>)}</ul>
      <Pager offset={offset} total={list.total} onChange={setOffset}/>
    </>}
    {commit && <section className="record-detail"><h3>Commit 상세</h3><p className="path">{commit.oid}</p><pre>{commit.metadata.message ?? `Message 미확보: ${commit.metadata.message_reason ?? 'UNKNOWN'}`}</pre><p className="hint">작성자: {commit.metadata.author?.name ?? '미확보'} {commit.metadata.author?.email} · {commit.metadata.author?.timestamp == null ? '시각 미확보' : formatDate(new Date(commit.metadata.author.timestamp * 1000).toISOString())} · 원본 timezone {commit.metadata.author?.timezone ?? '—'}</p>
      <div className="parents">Parent: {!commit.metadata.parents.length ? 'root Commit' : commit.metadata.parents.map(parent => <button key={parent} onClick={() => void openParent(parent)}>{parent}</button>)}</div>
      <h3>변경 파일 · 부모별 diff</h3>{changes ? <><ul className="evidence-list">{changes.items.map(row => <li key={row.id}><button onClick={() => {setContent(null); setObservation(null); setChange(row);}}>{row.metadata.status} · {row.metadata.old.path === row.metadata.new.path ? row.metadata.new.path : `${row.metadata.old.path} → ${row.metadata.new.path}`}<small>Parent: {row.parent_oid ?? 'root Commit'}</small></button></li>)}</ul>{!changes.total && <p className="hint">확보된 변경 파일이 없습니다.</p>}<Pager offset={changeOffset} total={changes.total} onChange={setChangeOffset}/></> : <p className="hint">변경 파일을 불러오는 중입니다…</p>}</section>}
    {file && <section className="record-detail"><h3>{file.path}</h3><label>캡처 상태<select value={`${observation?.layer}:${observation?.id}`} onChange={e => {setContent(null); setObservation(file.observations.find(o => `${o.layer}:${o.id}` === e.target.value)!);}}>{file.observations.map(o => <option key={`${o.layer}:${o.id}`} value={`${o.layer}:${o.id}`}>{layers[o.layer]}{o.metadata.stage != null ? ` · stage ${o.metadata.stage}` : ''}{o.body_reason ? ` · ${o.body_reason}` : ''}</option>)}</select></label>
      {observation && <><p className="hint">관측: {formatDate(observation.observed_at)} · 본문 관측: {formatDate(observation.body_observed_at)}</p>{observation.repaired && <p className="hint">이 본문은 원래 수집 후 Acceptance 보완에서 재관측한 자료입니다. 원래 수집 시점 bytes와의 동일성은 UNKNOWN입니다. 최초 본문 사유: {observation.original_body_reason ?? '—'}</p>}<details><summary>Metadata · 출처</summary><pre>{JSON.stringify(observation, null, 2)}</pre></details></>}
    </section>}
    {(observation || change) && <section className="body-detail"><h3>{change ? '보존된 diff' : '파일 내용'}</h3>{observation?.layer === 'head' && <p className="hint">수집 당시 보존한 Commit/blob locator로 원본의 immutable Git object를 지금 조회합니다. 당시 저장한 Working 본문과 구분되며 원본 object가 없으면 조회할 수 없습니다.</p>}{!content ? <p className="hint">본문을 불러오는 중입니다…</p> : content.body === null ? <p className="hint">본문이 보존되지 않았거나 조회가 제한되었습니다. 사유: {content.body_reason ?? '당시 본문 미확보'} · 현재 파일로 대체하지 않습니다.</p> : <pre className="source-body">{content.body}</pre>}{content?.read_at && <p className="hint">Git object 조회: {formatDate(content.read_at)} · {content.provenance}</p>}</section>}
  </section>;
}
