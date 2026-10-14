const reasons:Record<string,string>={
  media_size_limit:'현재 AI 입력 준비의 원본 용량 제한을 넘었습니다. 업로드된 사본은 보존됩니다.',
  docx_tracked_changes_requires_review:'변경 추적이 있어 문서 전체 분석을 보류했습니다.',
  docx_text_only_layout_not_preserved:'추출한 XML 텍스트에는 배치 정보가 없습니다. 시각 보완 여부는 PDF 페이지 범위를 확인하세요.',
  docx_generated_numbering_unprocessed:'XML 텍스트의 자동 번호는 복원하지 않았습니다. 변환 PDF도 동일 재현을 보장하지 않습니다.',
  docx_image_layout_and_text_association_not_preserved:'개별 이미지와 XML 본문의 배치 관계는 보존되지 않습니다. 파생 PDF는 시각적 보완 근거입니다.',
  docx_rendering_fidelity_unverified:'DOCX 변환 PDF는 원본 Word의 배치·특수 기능과 동일한지 검증되지 않았습니다.',
  docx_embedded_attachments_unprocessed:'DOCX 첨부 객체 내부 내용은 분석하지 않았습니다.',
  docx_altchunk_unprocessed:'DOCX altChunk로 연결된 내용은 분석하지 않았습니다.',
  docx_images_or_attachments_unprocessed:'DOCX 이미지 또는 첨부를 처리하지 못했습니다.',
  docx_tracked_changes_present:'변경 추적이 있는 문서입니다.',
  docx_external_hyperlinks_not_loaded:'외부 hyperlink는 불러오지 않고 표시 텍스트만 보존했습니다.',
  docx_renderer_unavailable:'승인된 로컬 DOCX 변환 Runtime 또는 서비스 구성을 준비할 수 없습니다.',
  docx_renderer_platform_unsupported:'이 환경에는 검증된 DOCX 변환 Runtime이 없습니다.',
  docx_embedded_objects_render_deferred:'첨부·OLE 객체가 있어 문서의 시각 변환을 보류했습니다.',
  docx_altchunk_render_deferred:'altChunk가 있어 문서의 시각 변환을 보류했습니다.',
  docx_word_format_render_deferred:'시각 변환을 검증하지 않은 Word XML 형식입니다.',
  docx_external_content_render_deferred:'외부 연결 자료를 불러오지 않기 위해 시각 변환을 보류했습니다.',
  docx_active_content_requires_review:'매크로·활성 콘텐츠 의심 요소가 있어 시각 변환을 보류했습니다.',
  docx_unsafe_package:'안전하게 변환할 수 없는 DOCX 내부 구조입니다.',
  docx_media_expansion_limit:'DOCX 내부 이미지의 압축 해제 용량 제한을 넘었습니다.',
  docx_render_expansion_limit:'DOCX 변환의 압축 해제 용량 제한을 넘었습니다.',
  docx_render_xml_limit:'DOCX 시각 변환의 XML 용량 제한을 넘었습니다.',
  docx_conversion_failed:'DOCX의 PDF 변환 결과를 확보하지 못했습니다.',
  invalid_docx:'DOCX의 안전한 텍스트 추출에 실패했습니다. 문서 손상으로만 단정할 수는 없습니다.',
  pdf_parse_failed:'PDF 구조를 확인하지 못해 페이지 분석에 사용하지 않았습니다.',
  pdf_encrypted_requires_review:'암호화된 PDF는 분석하지 않았습니다.',
  pdf_page_count_limit:'PDF 페이지 수가 로컬 처리 범위를 넘었습니다.',
  pdf_single_page_size_limit:'PDF 한 페이지가 분할 입력 용량 제한을 넘었습니다.',
  pdf_derivative_size_limit:'PDF 파생본의 처리·보존 용량 제한을 넘었습니다.',
  pdf_embedded_attachments_unprocessed:'PDF 첨부 파일 내부 내용은 분석하지 않았습니다.',
  pdf_article_threads_not_preserved:'PDF 분할본에는 페이지 간 article thread 구조가 없습니다. 원본 사본은 보존됩니다.',
  media_sandbox_unavailable:'안전한 로컬 문서 처리 격리를 적용할 수 없습니다.',
  media_resource_isolation_unavailable:'문서 처리 자원 제한·감시를 적용할 수 없습니다.',
  media_processing_failed:'격리된 문서 처리 실행에 실패했습니다.',
  media_processing_timeout:'문서 처리 시간 제한을 넘었습니다.',
  media_memory_limit:'문서 처리 메모리 제한을 넘었습니다.',
  media_scratch_limit:'문서 처리 임시 공간 제한을 넘었습니다.',
  native_media_requires_visual_privacy_review:'시각적 개인정보·기밀성 및 전송 권한은 별도 확인이 필요합니다.',
  native_media_content_not_locally_verified:'원본 이미지·PDF의 내용을 로컬에서 사실 검증한 것은 아닙니다.',
  suspected_secret:'Secret 의심 내용이 있어 분석에서 제외했습니다.',
  sensitive_path:'민감한 파일명·경로 정책에 따라 분석에서 제외했습니다.',
  commit_personal_headers_excluded:'커밋 작성자 이름·email 등 개인 header는 AI 입력에서 제외했습니다.',
};
export function analysisReason(code:string):string {
  const prefix='docx_visual_unprocessed:';
  if(code.startsWith(prefix))return `DOCX 시각적 내용 미처리: ${analysisReason(code.slice(prefix.length))}`;
  if(code.startsWith('docx_unsupported_media:'))return `개별 이미지로 분석하지 못한 DOCX 내부 자료: ${code.slice('docx_unsupported_media:'.length)}. 파생 PDF에서도 동일 재현을 보장하지 않습니다.`;
  return reasons[code] ?? `처리 한계 또는 제외 사유: ${code}`;
}
export interface MediaInfo {name:string;mime_type:string;sha256:string;use_for_analysis?:boolean;provenance?:{transform?:string;page_start?:number;page_end?:number;document_pages?:number;original_sha256?:string;renderer_version?:string}}
export function mediaScope(media:MediaInfo):string {
  if(media.use_for_analysis===false)return media.provenance?.transform==='original-docx'?'원본 DOCX 사본 · 직접 AI 입력에는 사용하지 않음':'원본 PDF 사본 · 직접 AI 입력에는 사용하지 않음';
  const p=media.provenance;
  if(p?.page_start!==undefined)return `${p.transform==='docx-to-pdf'?'DOCX에서 변환한 PDF':'PDF'} · ${p.page_start}–${p.page_end} / 전체 ${p.document_pages}페이지 · 분석 입력`;
  return '개별 이미지 · 분석 입력';
}
export const uploadAnalysisNotice='파일 추가는 원본 사본 보존입니다. AI 분석 지원·완료를 뜻하지 않습니다. DOCX는 텍스트·내부 이미지와 로컬 PDF 변환으로 분석 입력을 준비하며, 변환 실행 실패·변경 추적·첨부·외부 연결·특수 기능으로 일부 또는 전체 분석이 보류될 수 있습니다. PDF·이미지도 구조·페이지·용량·보안 제한으로 처리하지 못한 내용이 있을 수 있으며 결과에서 범위와 사유를 확인할 수 있습니다.';
