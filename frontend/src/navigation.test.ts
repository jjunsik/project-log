import {expect,it} from 'vitest';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {HistoryPages, readVisit, sameLocation, useHistoryPage, visitState, type Visit} from './navigation';
const view:Visit={screen:'commits',origin:'dashboard',project:'A',selected:'old-uuid',oid:'',document:null,collectionPage:2,pages:{'old-uuid/commits/all':2},scroll:{commits:120}};
it('preserves visit identity and UI state without treating it as a fresh Project entry',()=>{
  expect(readVisit(JSON.parse(JSON.stringify(visitState(3,view))))).toEqual({index:3,view});
  expect(view.selected).toBe('old-uuid');
});
it('only distinct user destinations create another navigation entry',()=>{
  expect(sameLocation(view,{...view,pages:{},scroll:{},collectionPage:1})).toBe(true);
  for(const change of [{screen:'dashboard' as const},{project:'B'},{selected:'new-uuid'},{screen:'commit' as const,oid:'full-hash'}])expect(sameLocation(view,{...view,...change})).toBe(false);
});
it('ignores foreign and malformed browser states',()=>{
  for(const value of [null,{}, {projectLog:2,index:0,view},visitState(0,{...view,screen:'unknown'} as unknown as Visit),visitState(0,{...view,pages:{bad:0}}),visitState(0,{...view,scroll:{bad:-1}})])expect(readVisit(value)).toBeNull();
});
it('restores each list page by its Collection or Project scope',()=>{
  function Probe(){const [a]=useHistoryPage('old-uuid/commits/all'),[b]=useHistoryPage('new-uuid/commits/all');return createElement('span',null,`${a}/${b}`);}
  expect(renderToStaticMarkup(createElement(HistoryPages,{value:{pages:view.pages,setPages:()=>{}}},createElement(Probe)))).toBe('<span>2/1</span>');
});
