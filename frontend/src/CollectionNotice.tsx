export default function CollectionNotice() {
  return <div className="notice"><p>수집 전에 개인정보·Secret·기밀정보·제3자 자료와 제공 권한을 직접 점검하세요. 수집에는 기존의 제한적인 Secret 검사·민감 경로 제외가 적용되지만 모든 민감정보를 탐지하지는 못합니다. AI 분석 단계의 별도 자동 탐지·마스킹은 제공하지 않습니다.</p>
    <details><summary>수집 범위와 임시 수정·복구 시 주의</summary><p>Git History·Diff·문서·미커밋 자료가 포함될 수 있으며 수집 당시 자료가 보존됩니다. 필요하면 민감정보를 직접 임시 수정하고 수집 완료 후 직접 복구할 수 있습니다. 현재 파일을 수정해도 과거 Commit·Diff·기존 Collection에는 소급 적용되지 않습니다. Project Log는 원본 파일을 자동 수정하거나 복구하지 않습니다.</p></details>
  </div>;
}
