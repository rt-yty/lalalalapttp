import {useCallback,useEffect,useRef,useState} from 'react';
import {ArrowUpRight,Compass,MapPin,Music2,RotateCcw,Sparkles,TrendingUp,X} from 'lucide-react';
import {api} from './api';
import {publicText} from './format';
import type {Recommendation,RecommendationFeed} from './types';

// These examples remain usable during an outage, and are never labelled popular.
const offlineIdeas:Recommendation[]=[
 {id:'offline-concert',title:'Поездка на концерт',description:'Любимый артист — повод увидеть новый город.',reason:'Начните с музыки, а даты и города подберём вместе.',prompt:'Хочу поехать на концерт. Помоги сравнить ближайшие даты и города.',kind:'artist',source:'starter',budget_minor:null},
 {id:'offline-paris',title:'Неделя в Париже',description:'Город, прогулки и время для себя.',reason:'Уточним даты и бюджет перед подбором.',prompt:'Хочу провести неделю в Париже. Помоги подобрать дорогу и отель.',kind:'destination',source:'starter',budget_minor:null},
];
const kindLabel={destination:'Новое направление',artist:'Музыка и путешествия',theme:'Идея для поездки'};
function isRecommendation(value:unknown):value is Recommendation{
 if(!value||typeof value!=='object')return false;
 const item=value as Partial<Recommendation>;
 return ['id','title','description','reason','prompt'].every(key=>typeof item[key as keyof Recommendation]==='string')
  &&Boolean(item.id&&item.title&&item.prompt)&&['destination','artist','theme'].includes(item.kind??'')
  &&['personalized','popular','starter'].includes(item.source??'')
  &&(item.budget_minor===null||Number.isSafeInteger(item.budget_minor)&&Number(item.budget_minor)>=0)
  &&(item.budget_label===undefined||item.budget_label===null||typeof item.budget_label==='string');
}
function isFeed(value:unknown):value is RecommendationFeed{
 if(!value||typeof value!=='object')return false;
 const feed=value as Partial<RecommendationFeed>;
 if(!Array.isArray(feed.personalized)||!Array.isArray(feed.popular)||typeof feed.history_used!=='boolean'
  ||!['community','starter'].includes(feed.popular_source??'')
  ||feed.starter!==undefined&&!Array.isArray(feed.starter))return false;
 const items=[...feed.personalized,...feed.popular,...(feed.starter??[])];
 return items.every(isRecommendation)&&new Set(items.map(item=>item.id)).size===items.length
  &&feed.personalized.every(item=>item.source==='personalized')
  &&feed.popular.every(item=>item.source===(feed.popular_source==='community'?'popular':'starter'))
  &&(feed.starter??[]).every(item=>item.source==='starter');
}

export function HomeRecommendations({onChoose,onInvalidate,disabled=false}:{onChoose:(text:string)=>void;onInvalidate?:()=>void;disabled?:boolean}){
 const [feed,setFeed]=useState<RecommendationFeed|null>(null),[loading,setLoading]=useState(true),[error,setError]=useState(''),[hiding,setHiding]=useState<string[]>([]),[notice,setNotice]=useState('');
 const generation=useRef(0),mounted=useRef(true),hidingRef=useRef(new Set<string>());
 const load=useCallback(async()=>{
  const current=++generation.current;setLoading(true);setError('');
  try{const value=await api.recommendations();if(!isFeed(value))throw new Error('invalid feed');if(mounted.current&&current===generation.current)setFeed(value);}
  catch{if(mounted.current&&current===generation.current)setError('Не удалось обновить рекомендации. Можно начать с любой идеи или написать свой запрос.');}
  finally{if(mounted.current&&current===generation.current)setLoading(false);}
 },[]);
 useEffect(()=>{mounted.current=true;void load();return()=>{mounted.current=false;generation.current++;};},[load]);
 async function dismiss(item:Recommendation){
  if(hidingRef.current.has(item.id))return;
  hidingRef.current.add(item.id);setHiding([...hidingRef.current]);setNotice('');
  try{await api.dismissRecommendation(item.id);if(!mounted.current){onInvalidate?.();return;}setFeed(previous=>previous?{...previous,personalized:previous.personalized.filter(card=>card.id!==item.id),popular:previous.popular.filter(card=>card.id!==item.id),starter:previous.starter?.filter(card=>card.id!==item.id)}:previous);setNotice('Идея скрыта. Учтём это в следующих рекомендациях.');}
  catch{if(mounted.current)setNotice('Не удалось скрыть идею. Попробуйте ещё раз.');}
  finally{hidingRef.current.delete(item.id);if(mounted.current)setHiding([...hidingRef.current]);}
 }
 function cards(items:Recommendation[],offline=false){return <div className="recommendation-grid">{items.map(item=>{
  const Icon=item.kind==='artist'?Music2:item.kind==='destination'?MapPin:Compass;
  return <article key={item.id} className={'recommendation-card recommendation-'+item.kind}>
   {!offline&&<button type="button" className="recommendation-dismiss" aria-label={'Скрыть идею: '+publicText(item.title)} title="Не интересно" disabled={disabled||hiding.includes(item.id)} onClick={()=>void dismiss(item)}><X size={15} aria-hidden="true"/></button>}
   <button type="button" className="recommendation-choice" aria-label={publicText(item.title)} disabled={disabled||hiding.includes(item.id)} onClick={()=>onChoose(item.prompt)}>
    <span className="recommendation-topline"><span className="recommendation-icon"><Icon size={19} aria-hidden="true"/></span><span>{kindLabel[item.kind]}</span></span>
    <strong>{publicText(item.title)}</strong>
    <span className="recommendation-description">{publicText(item.description)}</span>
    {item.budget_label&&<span className="recommendation-budget">{publicText(item.budget_label)}</span>}
    <span className="recommendation-reason">{publicText(item.reason)}</span>
    <span className="recommendation-action">Обсудить поездку<ArrowUpRight size={15} aria-hidden="true"/></span>
   </button>
  </article>;
 })}</div>;}
 const personal=feed?.personalized??[],popular=feed?.popular??[],starter=feed?.starter??[];
 return <div className="home-recommendations">
  {loading&&!feed?<div className="recommendation-loading" role="status"><span>Подбираем идеи для вас…</span><div className="recommendation-grid" aria-hidden="true">{[0,1,2,3].map(id=><div key={id} className="recommendation-skeleton"><i/><i/><i/></div>)}</div></div>:<>
   {personal.length>0&&<section aria-labelledby="for-you-heading" className="recommendation-section"><div className="recommendation-heading"><Sparkles size={18} aria-hidden="true"/><h3 id="for-you-heading">Для вас</h3><span>По вашим интересам</span></div><p className="recommendation-subtitle">Новые впечатления с учётом того, что вы искали раньше.</p>{cards(personal)}</section>}
   {popular.length>0&&<section aria-labelledby="popular-heading" className="recommendation-section"><div className="recommendation-heading">{feed?.popular_source==='community'?<TrendingUp size={18} aria-hidden="true"/>:<Compass size={18} aria-hidden="true"/>}<h3 id="popular-heading">{feed?.popular_source==='community'?'Популярное у путешественников':feed?.history_used?'Ещё идеи для поездки':'Идеи для первой поездки'}</h3></div><p className="recommendation-subtitle">{feed?.popular_source==='community'?'Направления и впечатления, которые сейчас ищут другие.':'Несколько отправных точек. Ваши поиски помогут сделать подборку точнее.'}</p>{cards(popular)}</section>}
   {starter.length>0&&<section aria-labelledby="more-ideas-heading" className="recommendation-section"><div className="recommendation-heading"><Compass size={18} aria-hidden="true"/><h3 id="more-ideas-heading">Ещё идеи для поездки</h3></div><p className="recommendation-subtitle">Несколько отправных точек, пока общая подборка растёт.</p>{cards(starter)}</section>}
   {!feed&&error&&<section aria-labelledby="starter-heading" className="recommendation-section"><div className="recommendation-heading"><Compass size={18} aria-hidden="true"/><h3 id="starter-heading">С чего начать</h3></div>{cards(offlineIdeas,true)}</section>}
   {feed&&!personal.length&&!popular.length&&!starter.length&&<p className="recommendation-empty">Пока все идеи скрыты. Расскажите, куда или за какими впечатлениями хочется отправиться.</p>}
   {(personal.length>0||popular.length>0||starter.length>0)&&<p className="recommendation-footnote">Нажмите на идею — появится черновик запроса. Даты, доступность и стоимость проверим при подборе.</p>}
  </>}
  {error&&<div className="recommendation-error" role="status"><span>{error}</span><button className="text-button" disabled={loading} onClick={()=>void load()}><RotateCcw size={13} aria-hidden="true"/>{loading?'Обновляем…':'Обновить рекомендации'}</button></div>}
  {notice&&<p className="recommendation-notice" role="status">{notice}</p>}
 </div>;
}
