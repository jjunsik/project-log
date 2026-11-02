import {useState} from 'react';
import {createRoot} from 'react-dom/client';
import Experiences from '../Experiences';
import ThemeControl from '../ThemeControl';
import AnalysisFlow from '../AnalysisFlow';
import useAnalysis from '../useAnalysis';
import {isActiveAnalysis, type AnalysisView, type ExecutionStatus} from '../analysis';
import {collections,materials,execution,fixtureResponse,prepare,projectId,scenarios,scenarioView,type Scenario} from './experienceFixtures';
import '../style.css';

function Preview() {
  const [scene,setScene]=useState<Scenario>('idle'),[view,setView]=useState<AnalysisView>(()=>scenarioView('idle')),[visible,setVisible]=useState(true);
  const selectScene=(next:Scenario)=>{setScene(next);setView(scenarioView(next));setVisible(true);};
  const advance=(status:ExecutionStatus)=>setView(old=>({...old,executions:[{...(old.executions[0]??execution(status)),status,phase:status==='running'?'개발 경험 복원 중 · 합성 진행':'합성 실행 종료'}]}));
  return <main className="analysis-preview"><section className="card analysis-preview-toolbar"><header><div><h1>개발 경험 · 합성 UI 미리보기</h1><p className="notice">모든 프로젝트·자료·결과·실행 상태는 합성 데이터입니다. 실제 서비스·DB·Gemini와 연결하지 않습니다.</p></div><ThemeControl/></header>
    <label>화면 사례<select aria-label="합성 화면 사례" value={scene} onChange={e=>selectScene(e.target.value as Scenario)}>{Object.entries(scenarios).map(([key,label])=><option key={key} value={key}>{label}</option>)}</select></label>
    <div className="actions"><button onClick={()=>setVisible(old=>!old)}>{visible?'다른 화면으로 이동 (합성)':'개발 경험으로 돌아오기 (합성)'}</button>
      {view.executions[0]?.status==='queued' && <button onClick={()=>advance('running')}>실행 중으로 (합성)</button>}
      {view.executions[0]?.status==='running' && <button onClick={()=>advance('completed')}>완료로 (합성)</button>}
      {view.executions[0]?.status==='cancel_requested' && <button onClick={()=>advance('cancelled')}>취소 완료로 (합성)</button>}
      <button onClick={()=>selectScene(scene)}>현재 사례 초기화</button></div>
    <p className="hint">화면 이동은 합성 실행 상태를 유지합니다. 새로고침하면 초기 사례로 돌아가며 실제 서버 복구를 검증하는 기능은 아닙니다.</p>
  </section>
  {visible ? <Experiences key={scene} projectId={projectId} collections={scene==='noCollections'?[]:collections} analysis={view} actions={scene==='unavailable'?undefined:{
    prepare:(end,resume)=>setView(old=>({...old,preparation:{state:'ready',data:{...prepare(end),...(resume?{id:'synthetic-resume-preparation',version:'synthetic-new-consent',resume:{requestId:resume,checkpointCount:2,inheritedAttempts:3}}:{})}}})),
    submit:prepared=>setView(old=>({...old,preparation:{state:'idle'},executions:[{...execution('queued',prepared),...(prepared.resume?{id:'synthetic-resumed',resumeFromRequestId:prepared.resume.requestId,reusedCheckpoints:2,inheritedAttempts:3}:{})},...old.executions.map(e=>({...e,resumedByRequestId:prepared.resume?'synthetic-resumed':e.resumedByRequestId}))]})),
    cancel:id=>setView(old=>({...old,executions:old.executions.map(e=>e.id===id && isActiveAnalysis(e.status)?{...e,status:'cancel_requested'}:e)})),
    preview:async()=>({policy:'synthetic-manual',input_digest:'synthetic-input',transfer_digest:'synthetic-transfer-digest',system_instruction:'합성 지시',task_instructions:{discover:'합성 지시'},later_context:'합성 후속 문맥',chunks:[{id:'synthetic-text',sources:[],text:'합성 전체 본문',offset:0,media:null}],offset:0,total:1,all_chunks_included:true,chunk_text_truncated:false,media_bytes_included:false}),
    transferUrl:()=>`data:application/json,${encodeURIComponent(JSON.stringify({synthetic:true,text:'합성 전체 본문'}))}`,
    refresh:()=>{if(scene==='unknown')setView(old=>({...old,preparation:{state:'idle'},executions:[execution('queued')]}));},
  }}/> : <section className="card"><h2>다른 화면 · 합성 사례</h2><p>분석이 화면과 독립적으로 이어지는 상황의 화면 복귀를 검토합니다.</p></section>}
  </main>;
}
export function startPreview() {
  if(new URLSearchParams(location.search).has('adapter')){
    // Browser tests intercept every API request; this entry is excluded from production.
    function AdapterPreview(){const {view,actions}=useAnalysis(projectId,true);return <main className="analysis-preview"><AnalysisFlow projectId={projectId} collections={collections} materials={materials} view={view} actions={actions} refreshMaterials={()=>{}}/></main>;}
    createRoot(document.getElementById('root')!).render(<AdapterPreview/>);return;
  }
  // Never delegate to native fetch: even an unknown path cannot reach a real backend.
  window.fetch=async(input,init)=>{
    const request=input instanceof Request?input:undefined;
    if((init?.method??request?.method??'GET')!=='GET')throw new Error('합성 미리보기에서는 실제 변경 요청을 보내지 않습니다.');
    const url=new URL(request?.url??String(input),location.origin);
    return new Response(JSON.stringify(fixtureResponse(url.pathname+url.search)),{headers:{'Content-Type':'application/json'}});
  };
  // This entry only mounts an isolated page; App and its service actions are not mounted.
  createRoot(document.getElementById('root')!).render(<Preview/>);
}
