import {useEffect, useState} from 'react';
import {api} from './model';
import {message} from './ui';
export function useData<T>(path: string | null, revision: string | number = 0) {
  const [state, setState] = useState<{path: string | null; data?: T; error?: string}>({path});
  useEffect(() => {if (!path) {setState({path}); return;}
    const abort = new AbortController(); setState(old => old.path === path ? old : {path});
    void api<T>(path, 'GET', undefined, abort.signal).then(data => {if (!abort.signal.aborted) setState({path, data});}).catch(e => {if (!abort.signal.aborted) setState({path, error: message(e)});});
    return () => abort.abort();
  }, [path, revision]);
  return state.path === path ? state : {path};
}
export interface Commit {oid: string; metadata: {message?: string | null; message_reason?: string; parents: string[]; author?: {timestamp?: number; timezone?: string}; committer?: {timestamp?: number}}; body_reason?: string}
export interface Observation {id: number; layer: string; metadata: {path: string; path_b64: string; commit?: string; oid?: string; stage?: number; size?: number; mtime_ns?: number; mtime?: number; [key: string]: unknown}; body_reason?: string | null; observed_at: string; body_observed_at?: string; repaired?: boolean; original_body_reason?: string; provenance?: unknown; sha256?: string | null}
export interface FileNode {name: string; path: string; path_b64: string; directory: boolean; observations: Observation[]}
export interface Page<T> {total: number; items: T[]}
export function preferred(node: FileNode) {return node.observations.find(o => ['working','document','untracked'].includes(o.layer)) ?? node.observations[0];}
