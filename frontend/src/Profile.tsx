import {useRef,useState} from 'react';
import {Check,Settings2,X} from 'lucide-react';
import {api} from './api';
import {publicText} from './format';
export function Profile({onApply,onSaved,disabled}:{onApply:(text:string)=>void;onSaved?:()=>void;disabled?:boolean}){
 const dialog=useRef<HTMLDialogElement>(null),generation=useRef(0),[origin,setOrigin]=useState(''),[preferences,setPreferences]=useState(''),[loading,setLoading]=useState(false),[saving,setSaving]=useState(false),[saved,setSaved]=useState(false),[loaded,setLoaded]=useState(false),[error,setError]=useState('');
 const close=()=>{generation.current++;dialog.current?.close();};
 async function open(){
  const current=++generation.current;dialog.current?.showModal();setLoading(true);setSaving(false);setLoaded(false);setOrigin('');setPreferences('');setSaved(false);setError('');
  try{const profile=await api.profile();if(current!==generation.current)return;setOrigin(profile.origin??'');setPreferences((profile.preferences??[]).join('\n'));setLoaded(true);}
  catch(e){if(current===generation.current)setError(publicText((e as Error).message));}
  finally{if(current===generation.current)setLoading(false);}
 }
 async function save(){
  const current=generation.current,values=preferences.split('\n').map(value=>value.trim()).filter(Boolean);setSaved(false);
  if(values.length>10||values.some(value=>value.length>160)){setError('Добавьте не более 10 пожеланий, каждое до 160 символов.');return;}
  setSaving(true);setError('');
  try{await api.saveProfile({origin:origin.trim(),preferences:values});if(current===generation.current){setSaved(true);onSaved?.();}}
  catch(e){if(current===generation.current)setError(publicText((e as Error).message));}
  finally{if(current===generation.current)setSaving(false);}
 }
 return <><button className="icon-button" aria-label="Мои предпочтения" disabled={disabled} onClick={()=>void open()}><Settings2 size={19}/></button><dialog ref={dialog} className="history-dialog profile-dialog" aria-labelledby="profile-title" onCancel={()=>{generation.current++;}} onClick={event=>{if(event.target===event.currentTarget)close();}}><div className="dialog-heading"><h2 id="profile-title">Мои предпочтения</h2><button className="icon-button" onClick={close} aria-label="Закрыть предпочтения"><X size={22}/></button></div><label>Обычно отправляюсь из<input value={origin} onChange={event=>{setOrigin(event.target.value);setSaved(false);}} maxLength={120} placeholder="Например, Москва" disabled={!loaded||loading||saving}/></label><label>Что важно в поездке<textarea value={preferences} onChange={event=>{setPreferences(event.target.value);setSaved(false);}} rows={4} placeholder={'Прямые рейсы\nЗавтрак в отеле\nТихий номер'} disabled={!loaded||loading||saving} maxLength={1600}/></label><p className="fine-print">До 10 пожеланий, каждое с новой строки.</p>{error&&<p role="alert" className="profile-error">{error}</p>}{saved&&<p role="status" className="profile-saved"><Check size={14}/>Предпочтения сохранены</p>}<div className="profile-actions"><button className="primary-button" disabled={!loaded||loading||saving} onClick={()=>void save()}>{saving?'Сохраняю…':'Сохранить'}</button><button className="text-button" disabled={!loaded||loading||saving||(!origin.trim()&&!preferences.trim())} onClick={()=>{onApply([origin.trim()?`Обычно я выезжаю из ${origin.trim()}.`:'',preferences.trim()?`Мои пожелания: ${preferences.split('\n').filter(Boolean).join('; ')}.`:''].filter(Boolean).join(' '));close();}}>Добавить в сообщение</button></div></dialog></>;
}
