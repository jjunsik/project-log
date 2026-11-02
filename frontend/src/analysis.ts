import {numberedCollections, type Collection} from './model';

// View models projected from the server analysis contract.
export interface MaterialInfo {id:string;filename:string;size_bytes:number;added_at:string}
export interface PreparedItem {id:string;name:string;outcome:'ready'|'partial'|'excluded';description:string}
export interface RelationTransition {policy:string;digest:string;source_contract:string;target_contract:string;previous_attempts:number;max_attempts:number;max_regenerations:number;scope:string;old_response_or_feedback_reused:false}
export interface PreparedAnalysis {
  id:string;version:string;endCollectionId:string;collectionIds:string[];materialIds:string[];
  items:PreparedItem[];provider:string;model:string;policy:string;transferScope:string;
  transferDigest:string;
  failurePolicy?:string;planningPolicy?:string;durabilityPolicy?:string;
  diagnostic?:{wireDigest:string;wireBytes:number};
  relationRecovery?:{maxAttempts:number;maxRegenerations:number;digest:string};
  resume?:{requestId:string;checkpointCount:number;inheritedAttempts:number;compatibilityPolicy?:string;reusedStage?:string;transition?:RelationTransition};
}
export type ExecutionStatus='checking'|'rejected'|'queued'|'running'|'cancel_requested'|'completed'|'failed'|'cancelled';
export interface AnalysisExecution {
  id:string;status:ExecutionStatus;createdAt:string;endCollectionId:string;endCollectionLabel:string;
  collectionCount:number;materialCount:number;phase?:string;calls?:number;message?:string;
  eventCount?:number;failureDetail?:Record<string,unknown>;partial?:boolean;preparationId?:string;diagnostic?:boolean;diagnosticAvailable?:boolean;
  checkpointCount?:number;reusedCheckpoints?:number;inheritedAttempts?:number;
  resumeFromRequestId?:string;resumedByRequestId?:string;resumeBlockReason?:string;resumeAfter?:string;
}
export interface AnalysisView {
  executionAvailable?:boolean;
  // Execution/history belong to the keyed Project; capacity is global across Projects.
  preparation:{state:'unavailable'|'idle'|'preparing'|'ready'|'empty'|'stale'|'unknown';data?:PreparedAnalysis;message?:string};
  capacity:{state:'unknown'|'available'|'busy';projectName?:string};
  historyAvailable:boolean;executions:AnalysisExecution[];
}
export interface AnalysisActions {
  prepare?:(endCollectionId:string,resumeFromRequestId?:string,diagnosticRetry?:boolean)=>void;
  submit?:(preparation:PreparedAnalysis)=>void;
  cancel?:(executionId:string)=>void;
  refresh?:()=>void;
  retry?:()=>void;
  preview?:(id:string,offset?:number)=>Promise<TransferPreview>;
  transferUrl?:(id:string,index?:number)=>string;
}
export interface TransferChunk {id:string;sources:Record<string,unknown>[];text:string|null;offset:number;media:null|{name:string;mime_type:string;sha256:string;size_bytes:number;provenance:Record<string,unknown>}}
export interface DiagnosticRequest {endpoint:string;model:string;wire_digest:string;wire_bytes:number;max_new_calls:number;automatic_retries:number;continue_analysis:boolean;request:Record<string,unknown>}
export interface TransferPreview {planning_policy?:string;durability_policy?:string;failure_policy?:string;relation_transition?:RelationTransition;relation_recovery_digest?:string;relation_recovery?:Record<string,unknown>;diagnostic_request?:DiagnosticRequest;policy:string;input_digest:string;transfer_digest:string;system_instruction:string;task_instructions:Record<string,string>;response_schemas?:Record<string,unknown>;generation?:Record<string,unknown>;consolidation?:Record<string,unknown>;later_context:string;chunks:TransferChunk[];offset:number;total:number;all_chunks_included:boolean;chunk_text_truncated:false;media_bytes_included:boolean}
export const unavailableAnalysis:AnalysisView={preparation:{state:'unavailable'},capacity:{state:'unknown'},historyAvailable:false,executions:[]};
export const executionLabels:Record<ExecutionStatus,string>={checking:'접수 확인 중',rejected:'접수 거절',queued:'분석 대기',running:'분석 중',cancel_requested:'취소 정리 중',completed:'완료',failed:'실패',cancelled:'취소 완료'};
export const isActiveAnalysis=(status:ExecutionStatus)=>['queued','running','cancel_requested'].includes(status);

export function completedAnalysisCollections(collections:Collection[]):Collection[] {
  return numberedCollections(collections).filter(c=>c.state==='completed')
    .sort((a,b)=>a.created_at.localeCompare(b.created_at)||a.id.localeCompare(b.id));
}
export function analysisPrefix(collections:Collection[],end:string):Collection[] {
  const ordered=completedAnalysisCollections(collections),index=ordered.findIndex(c=>c.id===end);
  return index<0?[]:ordered.slice(0,index+1);
}
export function matchesPreparedScope(prepared:PreparedAnalysis,collections:Collection[],end:string,materials:MaterialInfo[]|undefined):boolean {
  const ids=analysisPrefix(collections,end).map(c=>c.id);
  return !!ids.length && end===prepared.endCollectionId && ids.join()===prepared.collectionIds.join()
    && materials!==undefined && [...materials.map(m=>m.id)].sort().join()===[...prepared.materialIds].sort().join();
}
