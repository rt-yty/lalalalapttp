import type {AccountUser,AuthResponse,Bootstrap,Job,RecommendationFeed,SessionResponse,SessionSummary,Turn} from './types';
export class ApiError extends Error {constructor(message:string,public status:number){super(message);}}
let token='',authEpoch=0,authOperation=0,bootstrapSequence=0;
let authState:{user:AccountUser|null;csrf_token:string}|null=null;
function applyAuth(state:{user:AccountUser|null;csrf_token:string}){if(token!==state.csrf_token||authState?.user?.id!==state.user?.id)authEpoch++;token=state.csrf_token;authState=state;}
const unauthorizedListeners=new Set<()=>void>();
export function onUnauthorized(listener:()=>void){unauthorizedListeners.add(listener);return()=>{unauthorizedListeners.delete(listener);};}
async function request<T>(path:string,init?:RequestInit):Promise<T>{
 const requestToken=token,requestEpoch=authEpoch;
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),init?.body instanceof Blob?95000:20000);
 try{
  let response:Response;
  try{response=await fetch(path,{...init,signal:controller.signal,credentials:'same-origin',headers:{...(init?.body instanceof Blob?{}:{'Content-Type':'application/json'}),'X-Travel-Token':requestToken,...init?.headers}});}catch{throw new ApiError(controller.signal.aborted?'Я не дождался ответа. Попробуйте ещё раз.':'Я не смог связаться с приложением. Проверьте соединение и повторите запрос.',0);}
  if(response.status===401&&requestEpoch===authEpoch&&requestToken===token&&path!=='/api/bootstrap'&&!path.startsWith('/api/auth/'))for(const listener of unauthorizedListeners)listener();
  let data;try{data=await response.json();}catch{throw new ApiError('Я не смог прочитать ответ. Попробуйте ещё раз.',response.status);}
  if(!response.ok)throw new ApiError(typeof data.detail==='string'?data.detail:response.status===422?'Проверьте сообщение: оно должно содержать от 1 до 12 000 символов.':'Я не смог выполнить запрос. Попробуйте ещё раз.',response.status);
  return data;
 }finally{clearTimeout(timer);}
}
async function authenticate(path:string,data:Record<string,string>){const operation=++authOperation;authEpoch++;const response=await request<AuthResponse>(path,{method:"POST",body:JSON.stringify(data)});if(operation!==authOperation)throw new ApiError('Состояние входа изменилось. Повторите действие.',409);applyAuth(response);return response;}
async function bootstrap(){const operation=authOperation,sequence=++bootstrapSequence;const value=await request<Bootstrap>('/api/bootstrap');if(operation===authOperation&&sequence===bootstrapSequence)applyAuth({user:value.user??null,csrf_token:value.csrf_token});return authState?{...value,...authState}:value;}
export const api={recommendations:()=>request<RecommendationFeed>('/api/recommendations'),dismissRecommendation:(id:string)=>request<{ok:boolean}>('/api/recommendations/'+encodeURIComponent(id)+'/dismiss',{method:'POST',body:'{}'}),login:(data:{email:string;password:string})=>authenticate("/api/auth/login",data),signup:(data:{email:string;password:string;name:string})=>authenticate("/api/auth/signup",data),logout:()=>authenticate("/api/auth/logout",{}),bootstrap,continueSession:(id:string)=>request<{session_id:string}>("/api/sessions/"+encodeURIComponent(id)+"/continue",{method:"POST",body:"{}"}),session:(id:string)=>request<SessionResponse>('/api/sessions/'+encodeURIComponent(id)),sessions:()=>request<{sessions:SessionSummary[]}>('/api/sessions'),profile:()=>request<{origin:string;preferences:string[]}>('/api/profile'),saveProfile:(profile:{origin:string;preferences:string[]})=>request<{origin:string;preferences:string[]}>('/api/profile',{method:'PATCH',body:JSON.stringify(profile)}),submit:(turn:Turn)=>request<{job_id:string}>('/api/turns',{method:'POST',body:JSON.stringify(turn)}),job:(id:string)=>request<Job>('/api/jobs/'+encodeURIComponent(id)),transcribe:(blob:Blob)=>request<{text:string}>('/api/voice/transcribe',{method:'POST',body:blob,headers:{'Content-Type':blob.type||'audio/webm'}})};
