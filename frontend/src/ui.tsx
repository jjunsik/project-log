import {useEffect, useRef, type ReactNode} from 'react';

export const message = (e: unknown) => e instanceof Error ? e.message : '요청에 실패했습니다.';
export const date = (value?: string | number | null) => value == null ? '—' : new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('ko-KR');
export const size = (value?: number | null) => value == null ? '—' : value < 1024 ? `${value} B` : value < 1048576 ? `${(value/1024).toFixed(1)} KB` : `${(value/1048576).toFixed(1)} MB`;
const icons = {
  folder: 'M3 7V5a1 1 0 0 1 1-1h5l2 3h9a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1Z M3 8h18',
  home: 'm3 10 9-7 9 7v10H3Z M9 20v-7h6v7',
  settings: 'm9 3-1 3-3 1-2 4 2 2-1 3 3 3 3-1 2 3h4l1-3 3-1 2-4-2-2 1-3-3-3-3 1-2-3Z M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
  branch: 'M6 6v12 M6 10h7a5 5 0 0 0 5-5 M6 2a2 2 0 1 1 0 4 2 2 0 0 1 0-4 M6 18a2 2 0 1 1 0 4 2 2 0 0 1 0-4 M18 1a2 2 0 1 1 0 4 2 2 0 0 1 0-4',
  file: 'M5 2h9l5 5v15H5Z M14 2v6h5 M8 12h8 M8 16h8',
  book: 'M12 5C8 2 4 3 2 4v16c3-2 6-2 10 0 4-2 7-2 10 0V4c-2-1-6-2-10 1Z M12 5v15',
  clip: 'm8 15 8-8a3 3 0 0 1 4 4L9 22a5 5 0 0 1-7-7L14 3a4 4 0 0 1 6 6L9 20a2 2 0 0 1-3-3l9-9',
  calendar: 'M3 5h18v16H3Z M3 10h18 M7 2v5 M17 2v5',
  copy: 'M8 8h13v13H8Z M4 16H2V2h14v2',
  edit: 'm4 16 12-12 4 4L8 20H4Z M13 7l4 4',
  play: 'm7 3 14 9-14 9Z',
  stop: 'M5 5h14v14H5Z',
  download: 'M12 2v13 m-5-5 5 5 5-5 M4 16v6h16v-6',
  check: 'm4 12 5 5 11-11',
} as const;
export function Icon({name}: {name: keyof typeof icons}) {
  return <svg className={`icon icon-${name}`} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={icons[name]}/></svg>;
}
export function Text({text, display, className = '', focusable = true}: {text: string; display?: string; className?: string; focusable?: boolean}) {
  return <span className={`full-text ${className}`} tabIndex={focusable ? 0 : undefined} aria-label={text}><span className="truncate">{display ?? (text || '—')}</span>{text && <span className="tooltip" role="tooltip" tabIndex={0}>{text}</span>}</span>;
}
export function shortHash(value: string, peers: string[] = []) {
  let length = Math.min(7,value.length);
  while(length < value.length && peers.some(p => p !== value && p.slice(0,length) === value.slice(0,length))) length++;
  return value.slice(0,length);
}
export function Hash({value, peers = []}: {value: string; peers?: string[]}) {
  return <Text text={value} display={shortHash(value,peers)} className="hash"/>;
}
export const limitText = (value: string, maximum: number) => Array.from(value).slice(0,maximum).join('');
export function MemoFields({title, description, change}: {title: string; description: string; change: (title:string,description:string) => void}) {
  return <><label>제목 (선택)<input aria-label="수집 기록 제목" value={title} onChange={e=>change(limitText(e.target.value,50),description)}/><small aria-live="polite">{Array.from(title).length} / 50자</small></label><label>설명 (선택)<textarea aria-label="수집 기록 설명" value={description} onChange={e=>change(title,limitText(e.target.value,200))}/><small aria-live="polite">{Array.from(description).length} / 200자</small></label></>;
}
export function Modal({title, children, close, busy = false}: {title: string; children: ReactNode; close: () => void; busy?: boolean}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {const dialog = ref.current!; const previous = document.activeElement as HTMLElement | null; dialog.showModal(); return () => {dialog.close(); previous?.focus();};}, []);
  return <dialog ref={ref} onCancel={e => {e.preventDefault(); if (!busy) close();}} aria-label={title}><header><h2>{title}</h2><button aria-label="닫기" disabled={busy} onClick={close}>×</button></header>{children}</dialog>;
}
export function pageRange(page: number, total: number) {
  const pages = Math.max(1, Math.ceil(total/10));
  const current = Math.min(Math.max(1, page), pages);
  const start = Math.floor((current-1)/10)*10+1;
  return {current, pages, numbers: Array.from({length: Math.min(10, pages-start+1)}, (_, i) => start+i)};
}
export function Pagination({page, total, change}: {page: number; total: number; change: (n: number) => void}) {
  const {current, pages, numbers} = pageRange(page, total);
  return <nav className="pagination" aria-label="목록 페이지">{current > 1 && <><button onClick={() => change(1)}>처음</button><button onClick={() => change(current-1)}>이전</button></>}{numbers.map(n => <button key={n} aria-current={n === current ? 'page' : undefined} onClick={() => change(n)}>{n}</button>)}{current < pages && <><button onClick={() => change(current+1)}>다음</button><button onClick={() => change(pages)}>마지막</button></>}</nav>;
}
export function PageCount({page, total}: {page: number; total: number}) {const {current, pages} = pageRange(page, total); return <small className="page-count">{current} / {pages} · 총 {total}개</small>;}
export function deltaText(current:number | null, previous:number | null | undefined, delta:number | null, hasPrevious:boolean):string {
  if(current===null || (hasPrevious && previous==null)) return '비교 불가';
  if(!hasPrevious) return '-';
  return delta===null ? '비교 불가' : `${delta>0?'+':''}${delta}`;
}
export type Confirm = (title: string, content: ReactNode, action: () => Promise<void> | void, dismissOnStart?: boolean) => void;
