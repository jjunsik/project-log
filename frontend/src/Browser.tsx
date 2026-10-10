import {useEffect, useState} from 'react';
import {api, type Collection} from './model';
import {preferred, useData, type FileNode, type Observation, type Page} from './data';
import {EvidenceInfo, ObservationChoice, ObservationNotes} from './Observation';
import {Icon, Text, message} from './ui';

export const encodePath = (value: string) => btoa(String.fromCharCode(...new TextEncoder().encode(value)));
export default function Browser({collection}: {collection:Collection}) {
  const [directory,setDirectory]=useState(''),[trail,setTrail]=useState<{name:string;b64:string}[]>([]);
  const [list,setList]=useState<FileNode[] | null>(null),[error,setError]=useState('');
  const [file,setFile]=useState<FileNode | null>(null),[observation,setObservation]=useState<Observation | null>(null);
  const base=`collections/${collection.id}`;
  useEffect(()=>{
    const abort=new AbortController();setList(null);setError('');setFile(null);setObservation(null);
    void (async()=>{
      const items:FileNode[]=[];let offset=0,total=1;
      while(offset<total){
        const page=await api<Page<FileNode>>(`${base}/browse/structure?directory_b64=${encodeURIComponent(directory)}&limit=500&offset=${offset}`,'GET',undefined,abort.signal);
        if(abort.signal.aborted)return;
        total=page.total;items.push(...page.items);offset+=page.items.length;
        if(!page.items.length && offset<total)throw new Error('목록 일부를 조회하지 못했습니다. 다시 폴더를 선택하세요.');
      }
      if(!abort.signal.aborted)setList(items);
    })().catch(e=>{if(!abort.signal.aborted)setError(message(e));});
    return()=>abort.abort();
  },[base,directory,collection.state]);
  const contentPath=observation ? observation.layer==='head'
    ? `${base}/source?commit=${observation.metadata.commit}&path_b64=${encodeURIComponent(observation.metadata.path_b64)}`
    : `${base}/content/working_entries/${observation.id}` : null;
  const content=useData<{body:string | null;body_reason:string | null;provenance?:string;read_at?:string}>(contentPath);
  function folder(b64:string,next:{name:string;b64:string}[]) {setFile(null);setObservation(null);setDirectory(b64);setTrail(next);}
  return <section className="evidence-browser">
    <p className="hint">선택한 수집 당시 확보된 프로젝트 자료입니다. 현재 파일시스템의 파일을 직접 조회하지 않습니다.</p>
    <nav className="breadcrumbs structure-path" aria-label="현재 위치"><span>현재 위치:</span><button onClick={()=>folder('',[])}>프로젝트 루트</button>{trail.map((node,i)=><span key={node.b64}> › <button onClick={()=>folder(node.b64,trail.slice(0,i+1))}>{node.name}</button></span>)}</nav>
    {error && <p className="alert" role="alert">{error}</p>}
    <div className="structure-panels"><div className="structure-list">
      {!list ? <p>목록을 불러오는 중입니다…</p> : !list.length ? <p className="hint">이 디렉터리에 확보된 자료가 없습니다.</p> : <ul className="evidence-list">{list.map(node=><li key={node.path_b64}><button aria-pressed={!node.directory && file?.path_b64===node.path_b64} onClick={()=>{
        if(node.directory)folder(node.path_b64,[...trail,{name:node.name,b64:node.path_b64}]);
        else{setFile(node);setObservation(preferred(node));}
      }}><Icon name={node.directory?'folder':'file'}/><Text text={node.name}/></button></li>)}</ul>}
    </div><div className="structure-content" aria-live="polite">
      {!file || !observation ? <p className="hint">파일을 선택하면 상세 정보와 내용이 여기에 표시됩니다.</p> : <>
        <h2><Text text={file.path}/></h2>
        <ObservationChoice observations={file.observations} selected={observation} change={setObservation}/>
        <ObservationNotes observation={observation}/><EvidenceInfo observation={observation}/>
        <h3>파일 내용</h3>
        {observation.layer==='head' && <p className="hint">수집 당시 HEAD의 원본 Git 객체 조회입니다. 보존된 본문과 구분하며 현재 파일로 대체하지 않습니다.</p>}
        {content.error ? <p role="alert" className="notice">현재 조회 제한: {content.error}</p> : !content.data ? <p>본문을 불러오는 중입니다…</p> : content.data.body===null ? <p className="notice">{observation.layer==='untracked'?'메타데이터만 확보되어 파일 내용을 조회할 수 없습니다.':'본문을 확보하지 않았거나 현재 조회가 제한됩니다.'} 사유: {content.data.body_reason ?? observation.body_reason ?? '당시 본문 미확보'}. 다른 상태의 본문으로 대체하지 않습니다.</p> : <pre className="source-body">{content.data.body}</pre>}
        {content.data?.read_at && <p className="hint">원본 조회: {content.data.read_at} · {content.data.provenance}</p>}
      </>}
    </div></div>
  </section>;
}
