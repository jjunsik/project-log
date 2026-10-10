import {createContext, useCallback, useContext, useState, type Dispatch, type SetStateAction} from 'react';
import type {FileNode} from './data';

export type Screen = 'projects' | 'dashboard' | 'collections' | 'commits' | 'documents' | 'materials' | 'files' | 'settings' | 'add' | 'commit' | 'document';
export interface Visit {
  screen: Screen; origin: Screen; project: string; selected: string; oid: string; document: FileNode | null;
  collectionPage: number; pages: Record<string,number>; scroll: Record<string,number>;
}
const screens: Screen[] = ['projects','dashboard','collections','commits','documents','materials','files','settings','add','commit','document'];
export function readVisit(value: unknown): {index:number; view:Visit} | null {
  const state=value as {projectLog?:number;index?:number;view?:Visit} | null;
  const v=state?.view;
  if(state?.projectLog!==1 || !Number.isInteger(state.index) || !v || !screens.includes(v.screen) || !screens.includes(v.origin)
    || !['project','selected','oid'].every(key=>typeof v[key as 'project' | 'selected' | 'oid']==='string')
    || !Number.isInteger(v.collectionPage) || v.collectionPage<1 || !v.pages || !v.scroll
    || !Object.values(v.pages).every(p=>Number.isInteger(p) && p>0)
    || !Object.values(v.scroll).every(y=>Number.isFinite(y) && y>=0)
    || (v.document!==null && (!v.document || typeof v.document.path_b64!=='string' || !Array.isArray(v.document.observations)))) return null;
  return {index:state.index!,view:v};
}
export function visitState(index:number,view:Visit) {return {projectLog:1,index,view};}
export function sameLocation(a:Visit,b:Visit) {
  return a.screen===b.screen && a.origin===b.origin && a.project===b.project && a.selected===b.selected
    && a.oid===b.oid && a.document?.path_b64===b.document?.path_b64;
}
export const HistoryPages = createContext<{pages:Record<string,number>;setPages:Dispatch<SetStateAction<Record<string,number>>>} | null>(null);
export function useHistoryPage(key:string): [number,Dispatch<SetStateAction<number>>] {
  const context=useContext(HistoryPages),[local,setLocal]=useState(1),setPages=context?.setPages;
  const change=useCallback<Dispatch<SetStateAction<number>>>(next=>{
    if(setPages)setPages(old=>({...old,[key]:typeof next==='function'?next(old[key] ?? 1):next}));
    else setLocal(next);
  },[setPages,key]);
  return [context?.pages[key] ?? local,change];
}
