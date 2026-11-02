import {useEffect, useRef, useState} from 'react';
import {api} from './model';
import {message} from './ui';
import {analysisReason, analysisExecutionReason as reason} from './analysisSupport';
import {unavailableAnalysis, type AnalysisView, type AnalysisExecution, type AnalysisActions, type PreparedAnalysis, type ExecutionStatus, type TransferPreview, type RelationTransition} from './analysis';

type Submission={request_id:string;preparation_id:string;input_digest:string;consent_digest:string;confirmed:true;relation_recovery_digest?:string;relation_transition_digest?:string};
type Pending={preparation?:{preparation_id:string;end_collection_id:string;resume_from_request_id?:string;diagnostic_retry?:boolean};submission?:Submission};
interface Preparation {id:string;state:string;input_digest:string;consent_digest:string;error_code?:string;descriptor:{end_collection_id:string;collection_ids:string[];materials:{id:string}[];transfer_digest:string;relation_recovery_digest?:string;diagnostic?:{wire_digest:string;wire_bytes:number};resume?:{request_id:string;checkpoint_count:number;inherited_attempts:number;transition?:RelationTransition;compatibility?:{policy:string;reused_stage:string;source_contract:string;target_contract:string}};items:{reference:string;outcome:'ready'|'partial'|'excluded';reason:string|null;limitations:string[]}[];profile:{planning_policy?:string;durability_policy?:string;failure_policy?:string;provider:string;model:string;data_policy:string;transfer_scope:string;relation_recovery?:{max_attempts:number;max_regenerations:number}}}}
interface Execution {event_count?:number;failure_detail?:Record<string,unknown>;id:string;preparation_id?:string;state:ExecutionStatus;created_at:string;error_code:string|null;phase:string;call_seq:number;result_run_id:string|null;checkpoint_count?:number;reused_checkpoints?:number;inherited_attempts?:number;resume_from_request_id?:string;resumed_by_request_id?:string;execution_budget_policy?:string;resume_block_reason?:string;resume_after?:string;diagnostic?:boolean;diagnostic_available?:boolean;resume_contract_changed?:boolean;scope:{end_collection_id:string;collection_ids:string[];materials:{id:string}[];collection_labels?:{id:string;title:string|null;created_at:string}[]}}
interface Status {available:boolean;execution_available?:boolean;message:string|null;capacity:AnalysisView['capacity'];executions:Execution[]}
export function preparedView(p:Preparation):PreparedAnalysis {
  const d=p.descriptor;
  if(d.profile.relation_recovery&&!d.relation_recovery_digest)throw new Error("복구 정책 확인값이 없습니다. 새로 준비하세요.");
  if(d.resume?.transition&&(!d.resume.transition.digest||!d.resume.transition.source_contract||!d.resume.transition.target_contract||d.resume.transition.source_contract===d.resume.transition.target_contract||d.resume.transition.policy!=='validated-prefix-to-grounding-contract-fix-v1'||d.resume.transition.previous_attempts!==2||d.resume.transition.max_attempts!==2||d.resume.transition.max_regenerations!==1||d.resume.transition.old_response_or_feedback_reused!==false))throw new Error("계약 전환 확인값이 없습니다. 새로 준비하세요.");
  return {id:p.id,version:p.consent_digest,endCollectionId:d.end_collection_id,collectionIds:d.collection_ids,materialIds:d.materials.map(m=>m.id),provider:d.profile.provider,model:d.profile.model,policy:d.profile.data_policy,transferScope:d.profile.transfer_scope,
    planningPolicy:d.profile.planning_policy,durabilityPolicy:d.profile.durability_policy,failurePolicy:d.profile.failure_policy,transferDigest:d.transfer_digest,diagnostic:d.diagnostic?{wireDigest:d.diagnostic.wire_digest,wireBytes:d.diagnostic.wire_bytes}:undefined,
    relationRecovery:d.profile.relation_recovery?{maxAttempts:d.profile.relation_recovery.max_attempts,maxRegenerations:d.profile.relation_recovery.max_regenerations,digest:d.relation_recovery_digest!}:undefined,
    resume:d.resume?{requestId:d.resume.request_id,checkpointCount:d.resume.checkpoint_count,inheritedAttempts:d.resume.inherited_attempts,...(d.resume.transition?{transition:d.resume.transition}:{}),...(d.resume.compatibility?{compatibilityPolicy:d.resume.compatibility.policy,reusedStage:d.resume.compatibility.reused_stage}:{})}:undefined,
    items:d.items.map(e=>({id:e.reference,name:e.reference,outcome:e.outcome,description:[e.reason?analysisReason(e.reason):'고정 입력에 포함',...e.limitations.map(analysisReason)].join(' · ')}))};
}
function executionView(e:Execution):AnalysisExecution {
  const scope=e.scope,label=scope.collection_labels?.find(c=>c.id===scope.end_collection_id);
  return {eventCount:e.event_count,failureDetail:e.failure_detail,id:e.id,status:e.state,partial:e.state==='failed'&&!!e.result_run_id,createdAt:e.created_at,preparationId:e.preparation_id,diagnostic:e.diagnostic,diagnosticAvailable:e.diagnostic_available,endCollectionId:scope.end_collection_id??'',endCollectionLabel:label?`${label.title??'완료 수집'} · ${label.created_at}`:scope.end_collection_id??'접수 전',collectionCount:scope.collection_ids?.length??0,materialCount:scope.materials?.length??0,phase:e.phase,calls:e.call_seq,message:reason(e.error_code==='analysis_budget_exceeded' && !e.execution_budget_policy?'analysis_legacy_budget_stop':e.error_code,e.diagnostic,e.resume_contract_changed),checkpointCount:e.checkpoint_count,reusedCheckpoints:e.reused_checkpoints,inheritedAttempts:e.inherited_attempts,resumeFromRequestId:e.resume_from_request_id,resumedByRequestId:e.resumed_by_request_id,resumeBlockReason:e.resume_block_reason,resumeAfter:e.resume_after};
}
// Only identifiers and consent/policy digests persist. Never store source bodies or credentials.
export default function useAnalysis(project:string, enabled:boolean) {
  const [view,setView]=useState<AnalysisView>(unavailableAnalysis),[completion,setCompletion]=useState('');
  const refreshRef=useRef<()=>Promise<void>>(async()=>{}), actionsRef=useRef<AnalysisActions>({});
  useEffect(()=>{
    if(!enabled)return;
    let alive=true,busy=false,polling=false,epoch=0,prepared:Preparation|undefined;
    const key=`project-log.analysis.${project}`,path=`projects/${project}/analysis`;
    let pending:Pending={};
    try {pending=JSON.parse(localStorage.getItem(key)??'{}') as Pending;}catch{pending={};}
    const save=()=>localStorage.setItem(key,JSON.stringify(pending));
    const update=(fn:(v:AnalysisView)=>AnalysisView)=>{if(alive)setView(fn);};
    const unknown=(e:unknown)=>update(v=>({...v,preparation:{...v.preparation,state:'unknown',message:message(e)}}));
    const applyPreparation=(p:Preparation)=>{
      prepared=p;
      const state=p.state==='ready'?'ready':p.state==='empty'?'empty':p.state==='preparing'?'preparing':p.state==='consumed'?'idle':'stale';
      update(v=>({...v,preparation:{state,data:p.descriptor?.items?preparedView(p):undefined,message:reason(p.error_code)}}));
    };
    const observeRequest=(e:Execution)=>{
      if(e.state!=='checking'){
        if(e.state==='rejected') update(v=>({...v,preparation:{state:'stale',message:reason(e.error_code==='analysis_budget_exceeded' && !e.execution_budget_policy?'analysis_legacy_budget_stop':e.error_code)}}));
        else update(v=>({...v,preparation:{state:'idle'}}));
        pending={};save();prepared=undefined;
      }else update(v=>({...v,preparation:{state:'unknown',message:'동일 요청의 접수 판정을 확인 중입니다.'}}));
    };
    const refresh=async()=>{
      if(polling||busy||!alive)return;polling=true;const version=epoch;
      try{
        const status=await api<Status>(path);
        if(!alive||version!==epoch)return;
        update(v=>({...v,executionAvailable:status.execution_available??status.available,capacity:status.capacity,historyAvailable:true,executions:status.executions.map(executionView),preparation:!status.available?{state:'unavailable',message:status.message??undefined}:(v.preparation.state==='unavailable'||(v.preparation.state==='unknown'&&!pending.submission&&!pending.preparation))?{state:'idle'}:v.preparation}));
        setCompletion(status.executions.filter(e=>!!e.result_run_id).map(e=>e.result_run_id).join(','));
        if(pending.submission){
          const id=pending.submission.request_id;
          try{const row=await api<Execution>(`${path}/requests/${id}`);if(alive&&version===epoch&&pending.submission?.request_id===id)observeRequest(row);}
          catch(e){if(version===epoch)unknown(e);}
        }else if(pending.preparation && status.available){
          const id=pending.preparation.preparation_id;
          try{const row=await api<Preparation>(`${path}/preparations/${id}`);if(alive&&version===epoch&&pending.preparation?.preparation_id===id)applyPreparation(row);}
          catch(e){
            if(version===epoch)unknown(e);
          }
        }
      }catch(e){if(version===epoch)update(v=>({...v,capacity:{state:'unknown'},historyAvailable:false,preparation:{state:'unknown',message:message(e)}}));}
      finally{polling=false;}
    };
    const postPreparation=async()=>{
      if(!pending.preparation)return;
      applyPreparation(await api<Preparation>(`${path}/preparations`,'POST',pending.preparation));
    };
    const postSubmission=async()=>{
      if(!pending.submission)return;
      observeRequest(await api<Execution>(`${path}/requests`,'POST',pending.submission));
    };
    const perform=async(action:()=>Promise<void>)=>{
      if(busy)return;busy=true;epoch++;
      try{await action();}catch(e){unknown(e);}finally{busy=false;void refresh();}
    };
    actionsRef.current={
      prepare:(end,resume,diagnostic)=>{if(busy||pending.submission)return;void perform(async()=>{
        pending={preparation:{preparation_id:crypto.randomUUID(),end_collection_id:end,...(resume?{resume_from_request_id:resume}:{}),...(diagnostic?{diagnostic_retry:true}:{})}};save();prepared=undefined;
        update(v=>({...v,preparation:{state:'preparing'}}));await postPreparation();
      });},
      submit:p=>{if(busy||!prepared||p.id!==prepared.id||p.version!==prepared.consent_digest)return;void perform(async()=>{
        pending.submission??={request_id:crypto.randomUUID(),preparation_id:p.id,input_digest:prepared!.input_digest,consent_digest:prepared!.consent_digest,confirmed:true,...(p.relationRecovery?{relation_recovery_digest:p.relationRecovery.digest}:{}),...(p.resume?.transition?{relation_transition_digest:p.resume.transition.digest}:{})};
        save();update(v=>({...v,preparation:{state:'unknown'}}));await postSubmission();
      });},
      cancel:id=>{void perform(async()=>{await api(`${path}/requests/${id}/cancel`,'POST');});},
      preview:(id,offset=0)=>api<TransferPreview>(`${path}/preparations/${id}/transfer?offset=${offset}`),
      transferUrl:(id,index)=>`/api/${path}/preparations/${id}/transfer${index===undefined?'?download=true':`/media/${index}`}`,
      refresh:()=>void refresh(),
      // An explicit same-ID resend, never an automatic replacement request after a lost response.
      retry:()=>void perform(async()=>{if(pending.submission)await postSubmission();else if(pending.preparation)await postPreparation();}),
    };
    refreshRef.current=refresh;void refresh();const timer=setInterval(()=>void refresh(),1200);
    return()=>{alive=false;clearInterval(timer);actionsRef.current={};};
  },[project,enabled]);
  return {view,completion,actions:{prepare:(id:string,resume?:string,diagnostic?:boolean)=>actionsRef.current.prepare?.(id,resume,diagnostic),submit:(p:PreparedAnalysis)=>actionsRef.current.submit?.(p),cancel:(id:string)=>actionsRef.current.cancel?.(id),refresh:()=>void refreshRef.current(),retry:()=>actionsRef.current.retry?.(),
    preview:(id:string,offset?:number)=>actionsRef.current.preview!(id,offset),transferUrl:(id:string,index?:number)=>actionsRef.current.transferUrl!(id,index)}};
}
