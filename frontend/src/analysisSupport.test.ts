import {describe, expect, it, vi} from 'vitest';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {analysisReason, mediaScope, uploadAnalysisNotice} from './analysisSupport';
import Experiences from './Experiences';

const coverage={collections:['before','after'],input_entries:2,discovery_chunks:2,
  excluded:[{reference:'old',reason:'docx_tracked_changes_requires_review'}],
  limitations:[{reference:'new',reasons:['docx_visual_unprocessed:docx_external_content_render_deferred']}],
  chunks:[{id:'pdf-part',references:['new'],offset:0,media:{name:'result.pdf',mime_type:'application/pdf',sha256:'synthetic',provenance:{transform:'docx-to-pdf',page_start:32,page_end:63,document_pages:70}}}]};
vi.mock('./data',()=>({useData:(path:string)=>({data:path.endsWith('/experiences')
  ?{runs:[{id:'run',created_at:'2026-10-14T00:00:00Z',mode:'isolated',candidate_count:1,coverage}]}
  :{id:'run',created_at:'2026-10-14T00:00:00Z',mode:'isolated',result:{coverage,candidates:[{id:'candidate',title:'Synthetic correction',category:'potential',rationale:'Synthetic',claims:[{section:'implementation',statement:'Unverified interpretation',status:'INFERRED',citations:[{evidence_id:'pdf-part',quote:null,locator:'table',page:34}]}],limitations:[],grounding_notes:[],review_status:'unreviewed'}]}},error:null})}));

describe('preservation and analysis support notices',()=>{
  it('keeps upload retention distinct from analysis and identifies partial support',()=>{
    expect(uploadAnalysisNotice).toContain('원본 사본 보존');
    expect(uploadAnalysisNotice).toContain('AI 분석 지원·완료를 뜻하지 않습니다');
    expect(uploadAnalysisNotice).toContain('변경 추적·첨부·외부 연결·특수 기능');
    expect(analysisReason('docx_visual_unprocessed:docx_external_content_render_deferred')).toContain('외부 연결 자료를 불러오지 않기 위해');
    expect(analysisReason('docx_unsupported_media:word/media/graph.emf')).toContain('word/media/graph.emf');
    expect(analysisReason('docx_renderer_unavailable')).toContain('Runtime 또는 서비스 구성');
    expect(analysisReason('docx_embedded_objects_render_deferred')).toContain('첨부·OLE 객체');
    expect(analysisReason('future_reason')).toContain('future_reason');
  });
  it('renders exclusions, global derived pages and missing-experience warning',()=>{
    const html=renderToStaticMarkup(createElement(Experiences,{projectId:'synthetic'}));
    expect(html).toContain('중요한 개발 경험이 누락될 수 있습니다');
    expect(html).toContain('문서 전체 분석을 보류');
    expect(html).toContain('DOCX에서 변환한 PDF · 32–63 / 전체 70페이지');
    expect(html).toContain('DOCX 변환 PDF');
    expect(html).toContain('34');
    expect(html).toContain('실제 Gemini 분석 품질을 확인한 결과가 아닙니다');
    expect(html).toContain('disabled');
  });
  it('does not describe a preserved original as AI input',()=>{
    expect(mediaScope({name:'original.docx',mime_type:'docx',sha256:'synthetic',use_for_analysis:false,provenance:{transform:'original-docx'}})).toContain('직접 AI 입력에는 사용하지 않음');
  });
});
