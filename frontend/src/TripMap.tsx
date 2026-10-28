import {useEffect,useMemo,useRef,useState} from 'react';
import {ExternalLink,MapPin} from 'lucide-react';
import type {Component,EventChoice,TripRequest} from './types';

export function cityMapQuery(destination?:string,event?:Pick<EventChoice,'city'|'country'>|null){return destination&&destination===event?.city&&event.country?`${destination}, ${event.country}`:destination;}

export function mapLocation(item:Component,destination?:string){
 const location=item.location;
 if(location&&Number.isFinite(location.latitude)&&Number.isFinite(location.longitude)&&Math.abs(location.latitude!)<=90&&Math.abs(location.longitude!)<=180)return `${location.latitude},${location.longitude}`;
 return [item.title,location?.address,destination].filter(Boolean).join(', ');
}
export function hotelPlaceKey(item:Component){return item.provider_refs?.hotel_id!=null?`hotel:${item.provider_refs.hotel_id}`:`place:${item.title}:${item.location?.latitude??''}:${item.location?.longitude??''}:${item.location?.address??''}`;}
export function uniqueHotelPlaces(items:Component[]){const seen=new Set<string>();return items.filter(item=>item.product_type==='hotel').filter(item=>{const key=hotelPlaceKey(item);if(seen.has(key))return false;seen.add(key);return true;});}
export function googleMapUrls(query:string,key?:string|null){
 const search=new URL('https://www.google.com/maps/search/');search.searchParams.set('api','1');search.searchParams.set('query',query);
 const embed=new URL(key?'https://www.google.com/maps/embed/v1/place':'https://www.google.com/maps');
 embed.searchParams.set('q',query);if(key){embed.searchParams.set('key',key);embed.searchParams.set('language','ru');}else{embed.searchParams.set('output','embed');embed.searchParams.set('hl','ru');embed.searchParams.set('z','13');}
 return {search:search.toString(),embed:embed.toString()};
}
export function TripMap({request,selected,hotels=[],apiKey,event,active=true}:{request:TripRequest;selected:Component[];hotels?:Component[];apiKey?:string|null;event?:Pick<EventChoice,'city'|'country'>|null;active?:boolean}){
 const locations=useMemo(()=>uniqueHotelPlaces([...selected,...hotels]),[selected,hotels]);
 const selectedHotel=selected.find(item=>item.product_type==='hotel'),selectedKey=selectedHotel?hotelPlaceKey(selectedHotel):'city';
 const [chosen,setChosen]=useState<string>(selectedKey),[failed,setFailed]=useState(false),[loading,setLoading]=useState(true),[attempt,setAttempt]=useState(0);
 const loadTimer=useRef<ReturnType<typeof setTimeout>|null>(null);
 useEffect(()=>{setChosen(selectedKey);setFailed(false);},[selectedKey]);
 const hotel=locations.find(item=>hotelPlaceKey(item)===chosen),query=hotel?mapLocation(hotel,request.destination):cityMapQuery(request.destination,event);
 const urls=query?googleMapUrls(query,apiKey):null;
 useEffect(()=>{if(!active||!urls)return;setLoading(true);setFailed(false);loadTimer.current=setTimeout(()=>{setLoading(false);setFailed(true);},12000);return()=>{if(loadTimer.current)clearTimeout(loadTimer.current);};},[active,urls?.embed,attempt]);
 const loaded=()=>{if(loadTimer.current)clearTimeout(loadTimer.current);setLoading(false);setFailed(false);};
 const failedLoad=()=>{if(loadTimer.current)clearTimeout(loadTimer.current);setLoading(false);setFailed(true);};
 if(!query||!urls)return <div className="context-empty"><MapPin size={24}/><h3>Выберите направление</h3></div>;
 return <section className="google-map-panel" aria-label="Карта поездки"><div className="map-location-select"><label htmlFor="map-place">Место на карте</label><select id="map-place" value={hotel?chosen:'city'} onChange={event=>{setChosen(event.target.value);setFailed(false);}}><option value="city">{request.destination??'Направление'}</option>{locations.map(item=><option key={hotelPlaceKey(item)} value={hotelPlaceKey(item)}>{item.title}</option>)}</select></div><div className="map-frame">{active&&<iframe key={urls.embed+attempt} title={`Google Maps — ${hotel?.title??request.destination}`} src={urls.embed} referrerPolicy="strict-origin-when-cross-origin" allowFullScreen onLoad={loaded} onError={failedLoad}/>} {active&&loading&&<div className="map-loading" role="status"><span className="spinner"/><span>Загружаю карту…</span></div>}{active&&failed&&<div className="map-loading map-fallback" role="status"><MapPin size={25}/><p>Карта пока не загрузилась.</p><a href={urls.search} target="_blank" rel="noopener noreferrer">Посмотреть место в Google Maps<ExternalLink size={13}/></a><button className="text-button" onClick={()=>setAttempt(value=>value+1)}>Загрузить карту снова</button></div>}</div><div className="map-footer">{hotel?.location?.address&&<span>{hotel.location.address}</span>}<a href={urls.search} target="_blank" rel="noopener noreferrer">Открыть Google Maps<ExternalLink size={13}/></a></div></section>;
}
