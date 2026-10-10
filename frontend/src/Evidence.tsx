import {useEffect, useRef, useState} from 'react';
import {collectionCaption, type Collection} from './model';
import {useData, preferred, type Commit, type FileNode, type Page} from './data';
import {Icon, date, size, Text, Hash, Pagination, PageCount} from './ui';
import {EvidenceInfo, ObservationChoice} from './Observation';

export function Commits({collection, compact = false, all, open}: {collection: Collection; compact?: boolean; all?: () => void; open: (c: Commit) => void}) {
  const [page, setPage] = useState(1);
  const {data, error} = useData<Page<Commit>>(`collections/${collection.id}/commits?limit=${compact ? 3 : 10}&offset=${compact ? 0 : (page-1)*10}`, collection.state);
  useEffect(() => {if (data) setPage(p => Math.min(p, Math.max(1, Math.ceil(data.total/10))));}, [data]);
  return <section className="card"><header><h2><Icon name="branch"/> {compact ? '최근 커밋' : '전체 커밋'}</h2>{compact ? <button onClick={all}>전체 보기 →</button> : <PageCount page={page} total={data?.total ?? 0}/>}</header>{error && <p role="alert">{error}</p>}
    <div className="table-scroll"><table className="commits-table"><thead><tr><th>메시지</th><th>해시</th><th>작성일</th><th>작업</th></tr></thead><tbody>{data?.items.map(c => <tr key={c.oid}><td><Text className="commit-message" text={c.metadata.message ?? `메시지 미확보: ${c.metadata.message_reason ?? 'UNKNOWN'}`}/></td><td><Hash value={c.oid} peers={data.items.map(c=>c.oid)}/></td><td>{date(c.metadata.author?.timestamp)}</td><td><button onClick={() => open(c)}>상세 보기</button></td></tr>)}</tbody></table></div>{data && !data.total && <p className="hint">확보된 커밋이 없습니다.</p>}{!compact && <Pagination page={page} total={data?.total ?? 0} change={setPage}/>}
  </section>;
}
export function Documents({collection, compact = false, all, open}: {collection: Collection; compact?: boolean; all?: () => void; open: (file: FileNode) => void}) {
  const [page, setPage] = useState(1);
  const {data, error} = useData<Page<FileNode>>(`collections/${collection.id}/documents?limit=${compact ? 3 : 10}&offset=${compact ? 0 : (page-1)*10}`, collection.state);
  useEffect(() => {if (data) setPage(p => Math.min(p, Math.max(1, Math.ceil(data.total/10))));}, [data]);
  return <section className="card"><header><h2><Icon name="book"/> {compact ? '개발 문서' : '전체 개발 문서'}</h2>{compact ? <button onClick={all}>전체 보기 →</button> : <PageCount page={page} total={data?.total ?? 0}/>}</header>{error && <p role="alert">{error}</p>}
    <div className="table-scroll"><table className="documents-table"><thead><tr><th>파일명</th><th>수정일</th><th>크기</th><th>작업</th></tr></thead><tbody>{data?.items.map(file => {const o = preferred(file); return <tr key={file.path_b64}><td><Text text={file.name}/></td><td>{o?.metadata.mtime_ns == null ? '—' : new Date(o.metadata.mtime_ns/1e6).toLocaleDateString('ko-KR')}</td><td>{size(o?.metadata.size)}</td><td><button onClick={() => open(file)}>상세 보기</button></td></tr>;})}</tbody></table></div>{data && !data.total && <p className="hint">확보된 개발 문서가 없습니다.</p>}{!compact && <Pagination page={page} total={data?.total ?? 0} change={setPage}/>}
  </section>;
}
interface Change {id: number; parent_oid: string | null; body_reason?: string; metadata: {status: string; old: {path: string}; new: {path: string}}}
interface Content {body: string | null; body_reason?: string | null; provenance?: string; read_at?: string}
export function CommitDetail({collection, oid, parent, notify}: {collection: Collection; oid: string; parent: (oid: string) => void; notify:(text:string)=>void}) {
  const base = `collections/${collection.id}`;
  const {data: commit, error} = useData<Commit>(`${base}/commits/${oid}`);
  const [page,setPage]=useState(1),[selected,setSelected]=useState<Change | null>(null),[copyError,setCopyError]=useState('');
  useEffect(()=>{setPage(1);setSelected(null);},[oid]);
  const {data:changes,error:changesError}=useData<Page<Change>>(`${base}/commits/${oid}/changes?limit=10&offset=${(page-1)*10}`);
  const {data:content,error:contentError}=useData<Content>(selected ? `${base}/content/changes/${selected.id}` : null);
  const diffPanel=useRef<HTMLElement>(null);
  const choose=(c:Change)=>{setSelected(c);if(window.matchMedia('(max-width: 900px)').matches)requestAnimationFrame(()=>{diffPanel.current?.scrollIntoView({block:'start',behavior:'smooth'});diffPanel.current?.focus({preventScroll:true});});};
  const [subject,...body]=(commit?.metadata.message ?? '').split('\n');
  const hashes=[oid,...(commit?.metadata.parents ?? []),...(changes?.items.map(c=>c.parent_oid).filter((h):h is string=>!!h) ?? [])];
  const statuses:Record<string,string>={M:'수정',A:'추가',D:'삭제',R:'이름 변경',C:'복사',T:'유형 변경',U:'병합 충돌'};
  return <><section className="card"><h2>커밋 상세</h2>{error && <p role="alert">{error}</p>}{copyError && <p role="alert">{copyError}</p>}<p className="actions">커밋 해시: <Hash value={oid} peers={hashes}/><button aria-label="커밋 해시 복사" onClick={()=>void navigator.clipboard.writeText(oid).then(()=>notify('커밋 해시를 복사했습니다.')).catch(()=>setCopyError('커밋 해시 복사에 실패했습니다.'))}><Icon name="copy"/></button></p><div className="commit-full-message"><h3>{subject || '메시지 미확보'}</h3><pre>{body.join('\n')}</pre></div>{commit?.metadata.message == null && <p>사유: {commit?.metadata.message_reason ?? commit?.body_reason ?? 'UNKNOWN'}</p>}<p>작성일: {date(commit?.metadata.author?.timestamp)}</p><p>이 커밋을 확인한 수집 기록: <Text text={collectionCaption(collection)}/></p><details><summary>부모 커밋·기술 정보</summary><div className="actions">{commit && !commit.metadata.parents.length && '최초 커밋(부모 없음)'}{commit?.metadata.parents.map(p=><button key={p} onClick={()=>parent(p)}><Hash value={p} peers={hashes}/></button>)}</div></details></section>
    <div className="commit-panels"><section className="card commit-files"><header><h2>변경 파일</h2><PageCount page={page} total={changes?.total ?? 0}/></header>{changesError && <p role="alert">{changesError}</p>}<ul className="file-changes">{changes?.items.map(c=><li key={c.id}><button aria-pressed={selected?.id===c.id} onClick={()=>choose(c)}><Text text={`${statuses[c.metadata.status[0]] ?? c.metadata.status} · ${c.metadata.old.path===c.metadata.new.path ? c.metadata.new.path : `${c.metadata.old.path} → ${c.metadata.new.path}`}`}/>{(commit?.metadata.parents.length ?? 0)>1 && <span> · 비교 부모: {c.parent_oid ? <Hash value={c.parent_oid} peers={hashes}/> : '부모 없음'}</span>}</button></li>)}</ul>{changes && !changes.total && <p className="hint">확보된 변경 파일이 없습니다.</p>}<Pagination page={page} total={changes?.total ?? 0} change={p=>{setSelected(null);setPage(p);}}/></section>
    <section className="card commit-diff" ref={diffPanel} tabIndex={-1}><h2>Diff</h2>{selected && <p>비교 기준: {selected.parent_oid ? <Hash value={selected.parent_oid} peers={hashes}/> : '최초 커밋(부모 없음)'}</p>}{contentError && <p role="alert">{contentError}</p>}{!selected ? <p className="hint">변경 파일을 선택하세요.</p> : !content ? <p>불러오는 중…</p> : content.body==null ? <p className="notice">본문 미확보: {content.body_reason ?? selected.body_reason ?? 'UNKNOWN'}</p> : <pre className="source-body">{content.body}</pre>}</section></div></>;
}
export function DocumentDetail({collection,file}: {collection:Collection;file:FileNode}) {
  const [index,setIndex]=useState(()=>file.observations.indexOf(preferred(file)));
  const o=file.observations[index],base=`collections/${collection.id}`;
  const path=o?.layer==='head' ? `${base}/source?commit=${o.metadata.commit}&path_b64=${encodeURIComponent(o.metadata.path_b64)}` : o ? `${base}/content/working_entries/${o.id}` : null;
  const {data,error}=useData<Content>(path);
  return <><section className="card"><h2><Text text={file.name}/></h2><dl className="info"><dt>원본 경로</dt><dd><Text text={collection.snapshot.path ? `${collection.snapshot.path}/${file.path}` : `${file.path} · 원본 root 미확보`}/></dd><dt>수정일</dt><dd>{o?.metadata.mtime_ns==null ? '—' : date(new Date(o.metadata.mtime_ns/1e6).toISOString())}</dd><dt>크기</dt><dd>{size(o?.metadata.size)}</dd><dt>문서를 확보한 수집 기록</dt><dd><Text text={collectionCaption(collection)}/></dd></dl>{file.observations.length>1 && <ObservationChoice observations={file.observations} selected={o} change={v=>setIndex(file.observations.indexOf(v))}/>} {o?.repaired && <p className="notice">수집 후 재관측한 본문입니다. 최초 본문 미확보 사유: {o.original_body_reason ?? '—'}. 당시 bytes와의 동일성은 UNKNOWN입니다.</p>}<EvidenceInfo observation={o}/></section>
    <section className="card"><h2>문서 본문</h2>{o?.layer==='head' && <p className="hint">당시 Commit의 원본 Git 객체 조회입니다. 현재 파일 내용으로 대체하지 않습니다.</p>}{error ? <p className="alert" role="alert">현재 조회 제한: {error}</p> : !data ? <p>본문을 불러오는 중…</p> : data.body==null ? <p className="notice">본문 미확보: {data.body_reason ?? o?.body_reason ?? 'UNKNOWN'}</p> : <pre className="source-body">{data.body}</pre>}{data?.read_at && <p className="hint">조회: {date(data.read_at)} · {data.provenance}</p>}</section></>;
}
