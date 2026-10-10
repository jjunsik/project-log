export type ProjectStatus = 'new' | 'ongoing' | 'completed';
export type CollectionState = 'capturing' | 'queued' | 'running' | 'completed' | 'cancel_pending';
export interface Settings {name: string; status: ProjectStatus; base_branch: string; path?: string; acknowledged_warnings?: string | null; expected_updated_at?: string}
export interface Summary {
  commits?: number; changes?: number; head_files?: number; working_entries?: number;
  preserved_working_bodies?: number; preserved_diffs?: number; document_candidates?: number;
  preserved_untracked_bodies?: number; preserved_tracked_working_bodies?: number; errors?: number;
  preserved_document_bodies?: number;
  body_exclusions?: {reason: string; count: number}[];
  body_errors?: {reason: string; count: number}[];
  comparison?: {available: boolean; reason?: string; baseline_id: string | null;
    new?: number; changed?: number; unchanged?: number; deleted?: number; unknown?: number};
}
export interface Collection {
  number?: number;
  title?: string | null; description?: string | null;
  id: string; state: CollectionState; created_at: string; finished_at: string | null;
  retry_of: string | null; summary: Summary;
  kind?: 'initial' | 'manual' | 'retry';
  baseline_id?: string | null;
  snapshot: {head?: string | null; branch?: string | null; path?: string; started_at?: string; finished_at?: string};
  issues?: {id: number; phase: string; code: string; message: string; context?: {path?: string | {path: string}; commit?: string}}[];
}
export function numberedCollections(collections: Collection[]): Collection[] {
  const completed = collections.filter(c => c.state === 'completed').sort((a,b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id));
  const numbers = new Map(completed.map((c,i) => [c.id,i+1]));
  return collections.map(c => ({...c,number:numbers.get(c.id)}));
}
export function collectionCaption(c: Collection): string {
  return `${c.number ? `#${c.number}` : '진행 중'} · ${c.title || '제목 없음'} · ${new Date(c.snapshot.started_at ?? c.created_at).toLocaleString('ko-KR')} · ${c.snapshot.branch ?? '브랜치 미확보'}`;
}
export interface Project extends Omit<Settings, 'base_branch'> {
  base_branch: string | null;
  id: string; path: string; collection_id: string | null; collection_state: CollectionState | null; collection_busy?: boolean;
  created_at: string; updated_at: string;
  collection_summary: Summary | null; collections?: Collection[];
  collection_created_at?: string | null; collection_observed_at?: string | null;
}
export const statusLabels: Record<ProjectStatus, string> = {new: '새 프로젝트', ongoing: '개발 중', completed: '개발 완료'};
export const collectionLabels: Record<CollectionState, string> = {
  capturing: '현재 자료 확보 중', queued: '수집 대기', running: 'History 수집 중',
  completed: '수집 완료', cancel_pending: '취소 정리 중',
};
export function hasActiveCollection(project: Project | null): boolean {
  return !!project?.collection_busy || !!project?.collections?.some(c => c.state !== 'completed');
}
export function suggestedName(path: string): string {
  return path.trim().replace(/\/+$/, '').split('/').at(-1) ?? '';
}
export class ApiError extends Error {
  constructor(message: string, public status: number) {super(message);}
}
export async function api<T>(path: string, method = 'GET', body?: unknown, signal?: AbortSignal, headers: Record<string,string> = {}): Promise<T> {
  const response = await fetch(`/api/${path}`, {method, signal,
    headers: method === 'GET' ? headers : {'Content-Type': 'application/json',...headers},
    body: method === 'GET' ? undefined : JSON.stringify(body ?? {})});
  const data = await response.json();
  if (!response.ok) throw new ApiError(typeof data.detail === 'string' ? data.detail : '입력 내용을 확인하세요.', response.status);
  return data as T;
}

export function collectionOption(c: Collection): string {
  return c.title || `${new Date(c.created_at).toLocaleString('ko-KR')} · ${c.snapshot?.branch ?? '브랜치 미확보'} · ${collectionLabels[c.state]}`;
}
export function collectionSelection(collections: Collection[], selected: string): string {
  if (selected && collections.some(c => c.id === selected)) return selected;
  return collections.filter(c => c.state === 'completed')
    .sort((a,b) => b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id))[0]?.id ?? '';
}
