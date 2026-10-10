import {useEffect, useRef, useState} from 'react';
import {collectionLabels, statusLabels, type Project} from './model';
import {Icon, Text, date} from './ui';

export default function ProjectCard({project:p, open, edit, remove}: {project:Project; open:()=>void; edit:()=>void; remove:()=>void}) {
  const [expanded,setExpanded]=useState(false), root=useRef<HTMLDivElement>(null), trigger=useRef<HTMLButtonElement>(null);
  useEffect(()=>{
    if(!expanded)return;
    root.current?.querySelector<HTMLButtonElement>('[role=menuitem]')?.focus();
    const outside=(e:PointerEvent)=>{if(!root.current?.contains(e.target as Node))setExpanded(false);};
    const escape=(e:KeyboardEvent)=>{if(e.key==='Escape'){setExpanded(false);trigger.current?.focus();}};
    window.addEventListener('pointerdown',outside);window.addEventListener('keydown',escape);
    return()=>{window.removeEventListener('pointerdown',outside);window.removeEventListener('keydown',escape);};
  },[expanded]);
  return <article className="project-card" onClick={open}>
    <button className="project-card-body" aria-label={`${p.name} 대시보드로 이동`} onClick={e=>{e.stopPropagation();open();}}><Icon name="folder"/><h2><Text text={p.name} focusable={false}/></h2><dl><dt>프로젝트 상태</dt><dd>{statusLabels[p.status]}</dd><dt>기본 브랜치</dt><dd><Text text={p.base_branch ?? '미설정'} focusable={false}/></dd><dt>최근 수집</dt><dd>{p.collection_state && p.collection_state!=='completed' ? collectionLabels[p.collection_state] : date(p.collection_observed_at ?? p.collection_created_at)}</dd></dl></button>
    <div className="project-card-menu" ref={root} onClick={e=>e.stopPropagation()}><button ref={trigger} aria-label={`${p.name} 프로젝트 메뉴`} aria-haspopup="menu" aria-expanded={expanded} onClick={()=>setExpanded(!expanded)}>⋮</button>{expanded && <div role="menu" aria-label={`${p.name} 프로젝트 작업`} onKeyDown={e=>{if(['ArrowDown','ArrowUp','Home','End'].includes(e.key)){e.preventDefault();const items=Array.from(e.currentTarget.querySelectorAll<HTMLButtonElement>('[role=menuitem]'));const i=items.indexOf(document.activeElement as HTMLButtonElement);items[e.key==='Home'?0:e.key==='End'?items.length-1:(i+(e.key==='ArrowDown'?1:-1)+items.length)%items.length]?.focus();}}}>
      <button role="menuitem" onClick={()=>{setExpanded(false);edit();}}>수정</button><button role="menuitem" className="danger" onClick={()=>{setExpanded(false);remove();}}>삭제</button>
    </div>}</div>
  </article>;
}
