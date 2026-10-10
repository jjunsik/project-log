import {describe, expect, it} from 'vitest';
import {pageRange, shortHash, limitText, deltaText} from './ui';
import {numberedCollections, type Collection} from './model';
import {EvidenceInfo, fileState, observationTime} from './Observation';
import {renderToStaticMarkup} from 'react-dom/server';
import {createElement} from 'react';
import type {Observation} from './data';
describe('ten-item pages and groups of ten',()=>{
  it('advances one page from group 1–10 to 11–15',()=>{expect(pageRange(10,150)).toEqual({current:10,pages:15,numbers:[1,2,3,4,5,6,7,8,9,10]}); expect(pageRange(11,150)).toEqual({current:11,pages:15,numbers:[11,12,13,14,15]});});
  it('clamps deleted pages and handles an empty list',()=>{expect(pageRange(11,100).current).toBe(10); expect(pageRange(1,0)).toEqual({current:1,pages:1,numbers:[1]});});
});
describe('overview delta display',()=>{
  it('distinguishes no predecessor from unavailable counts',()=>{
    expect(deltaText(3,undefined,null,false)).toBe('-');expect(deltaText(null,undefined,null,false)).toBe('비교 불가');expect(deltaText(3,null,null,true)).toBe('비교 불가');expect(deltaText(3,2,null,true)).toBe('비교 불가');
  });
  it('formats measured increases, decreases and equal counts',()=>{expect(deltaText(6,3,3,true)).toBe('+3');expect(deltaText(3,6,-3,true)).toBe('-3');expect(deltaText(3,3,0,true)).toBe('0');});
});
describe('observation summary timestamps',()=>{
  it.each([
    ['2026-10-10T15:16:47.867276+09:00','2026-10-10 15:16:47.867276'],
    ['2026-10-10T03:16:47.012-03:30','2026-10-10 03:16:47.012'],
    ['2026-10-10T06:16:47Z','2026-10-10 06:16:47'],
    ['2026-10-10T06:16:47.0+00:00','2026-10-10 06:16:47.0'],
    ['미확보','미확보'],
  ])('preserves source wall time and existing precision: %s',(original,display)=>expect(observationTime(original)).toBe(display));
  it('only formats the two summary times and retains null, missing and raw JSON values',()=>{
    const original={metadata:{path:'2026-10-10T15:16:47.867276+09:00'},observed_at:'2026-10-10T15:16:47.867276+09:00',body_observed_at:null} as unknown as Observation;
    const json=JSON.stringify(original),html=renderToStaticMarkup(createElement(EvidenceInfo,{observation:original}));
    expect(html).toContain('2026-10-10 15:16:47.867276');expect(html).toContain('2026-10-10T15:16:47.867276+09:00');expect(html).toContain('null');expect(html).toContain('항목 없음');expect(JSON.stringify(original)).toBe(json);
  });
});
describe('Acceptance display policies',()=>{
  it('numbers the complete history with deterministic ties and recalculates after deletion',()=>{
    const c=(id:string,time:string,state='completed')=>({id,created_at:time,state,snapshot:{}} as Collection);
    const records=[c('c','2026-10-11'),c('b','2026-10-10'),c('a','2026-10-10'),c('d','2026-10-12','running')];
    expect(numberedCollections(records).map(c=>c.number)).toEqual([3,2,1,undefined]);
    expect(numberedCollections(records.filter(c=>c.id!=='b')).map(c=>c.number)).toEqual([2,1,undefined]);
    expect(records.every(c=>c.number===undefined)).toBe(true);
  });
  it('expands colliding hashes while retaining original identifiers',()=>{
    const a='1234567a'+'1'.repeat(32),b='1234567b'+'2'.repeat(32);
    expect(shortHash(a,[a,b])).toBe('1234567a');expect(shortHash(b,[a,b])).toBe('1234567b');
    expect(shortHash(a,[a,a])).toBe('1234567');expect(a).toHaveLength(40);
  });
  it('limits Unicode code points rather than UTF-16 units',()=>{
    expect(limitText('😀'.repeat(51),50)).toBe('😀'.repeat(50));expect(Array.from(limitText('가'.repeat(201),200))).toHaveLength(200);
  });
  it('keeps HEAD, index, working, document and untracked meanings separate',()=>{
    const layer=(layer:string)=>fileState({layer} as Observation).label;
    expect(['working','index','head','document','untracked'].map(layer)).toEqual(['작업 파일','스테이징된 파일','커밋된 파일','개발 문서','Git 미추적 파일']);
    expect(layer('head')).not.toBe('Git 객체에서 확보한 파일');
  });
});
