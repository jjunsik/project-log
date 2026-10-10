import type {Observation} from './data';
import {Text, date} from './ui';

const states: Record<string,{label:string;description:string}> = {
  working:{label:'작업 파일',description:'수집 당시 작업 폴더에 있던 파일입니다. 아직 스테이징하거나 커밋하지 않은 수정사항도 포함될 수 있습니다.'},
  index:{label:'스테이징된 파일',description:'수집 당시 Git Index(스테이징 영역)에 기록되어 있던 파일 내용입니다. 마지막 커밋과 같을 수도 있고, 아직 커밋되지 않은 변경사항이 포함될 수도 있습니다.'},
  head:{label:'커밋된 파일',description:'수집 당시 HEAD 커밋에 기록되어 있던 파일입니다. 이후 작업 폴더에서 수정한 내용은 포함하지 않습니다.'},
  document:{label:'개발 문서',description:'수집 당시 개발 문서로 관측한 자료입니다. 당시 확보한 본문과 미확보 사유를 유지합니다.'},
  untracked:{label:'Git 미추적 파일',description:'수집 당시 Git에서 추적하지 않던 파일입니다. 본문을 확보하지 않은 관측은 메타데이터만 확인할 수 있습니다.'},
  ignored:{label:'제외 자료의 메타데이터',description:'수집 정책에서 본문 수집이 제외된 자료의 메타데이터입니다.'},
};
export function fileState(o: Observation) {return states[o.layer] ?? {label:o.layer,description:`실제 관측 영역: ${o.layer}. 보존된 출처와 본문 확보 여부를 확인하세요.`};}
export function ObservationChoice({observations,selected,change}: {observations:Observation[];selected:Observation;change:(o:Observation)=>void}) {
  return <div className="observation-choice"><p>수집 당시 파일 상태</p><div role="radiogroup" aria-label="수집 당시 파일 상태">{observations.map((o,i)=>{
    const {label,description}=fileState(o), name=label+(o.metadata.stage == null ? '' : ` · stage ${o.metadata.stage}`);
    return <button key={`${o.layer}:${o.id}`} role="radio" aria-label={name} aria-checked={o===selected} tabIndex={o===selected ? 0 : -1} onClick={()=>change(o)} onKeyDown={e=>{
      if(['ArrowRight','ArrowDown','ArrowLeft','ArrowUp'].includes(e.key)){e.preventDefault();const next=(i+(e.key==='ArrowRight'||e.key==='ArrowDown'?1:-1)+observations.length)%observations.length;change(observations[next]);(e.currentTarget.parentElement?.children[next] as HTMLElement)?.focus();}
    }}><Text text={description} display={name} focusable={false}/></button>;
  })}</div></div>;
}
const keys: [string,string][] = [
  ['metadata.path','수집 당시 파일 경로'],['observed_at','자료를 관측한 시각'],
  ['body_observed_at','파일 본문을 관측한 시각'],['body_reason','본문을 확보하지 못한 사유(null은 이 필드에 사유가 없음을 뜻하며 본문 확보를 단독 보장하지 않음)'],
  ['provenance.kind','자료의 확보 방식·출처 유형'],['repaired','기존 관측 자료에 복구 처리가 적용됐는지 여부'],
];
export function observationTime(value:string):string {
  // Preserve the source offset's wall time and supplied fractional precision.
  const iso=value.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/);
  return iso ? `${iso[1]} ${iso[2]}${iso[3] ?? ''}` : value;
}
export function EvidenceInfo({observation}: {observation:Observation}) {
  const lookup=(key:string):unknown=>key.split('.').reduce<unknown>((value,k)=>value && typeof value==='object' ? (value as Record<string,unknown>)[k] : undefined,observation);
  return <details className="evidence-info"><summary>수집 근거·기술 정보</summary><dl>{keys.map(([key,meaning])=>{
    const value=lookup(key), label=value===undefined?'항목 없음':value===null?'null':String(value);
    const display=(key==='observed_at' || key==='body_observed_at') ? observationTime(label) : label;
    return <div key={key}><dt><code>{key}</code> — {meaning}</dt><dd>{<Text text={label} display={display}/>}</dd></div>;
  })}</dl><details className="raw-observation"><summary>원본 JSON 보기</summary><pre>{JSON.stringify(observation,null,2)}</pre></details></details>;
}
export function ObservationNotes({observation:o}: {observation:Observation}) {
  return <><p className="hint">관측: {date(o.observed_at)} · 본문 관측: {date(o.body_observed_at)}</p>{o.repaired && <p className="notice">수집 후 재관측한 자료입니다. 당시 bytes와의 동일성은 UNKNOWN입니다. 최초 본문 미확보 사유: {o.original_body_reason ?? '—'}</p>}</>;
}
