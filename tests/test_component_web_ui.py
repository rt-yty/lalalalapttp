"""Exercise component selection UI without a live provider or browser session."""
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_component_cards_keep_partial_choices_and_restore_with_actions():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the local UI behavior check")
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const nodes=new Map(),all=[];
class Element {
  constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.style={};this.listeners={};this.attributes={};this.className='';this.hidden=false;this.disabled=false;this.value='';this._text='';all.push(this);this.classList={add:(...s)=>s.forEach(x=>this.toggleClass(x,true)),remove:(...s)=>s.forEach(x=>this.toggleClass(x,false)),toggle:(s,v)=>this.toggleClass(s,v)};}
  toggleClass(name,value){const c=new Set(this.className.split(' ').filter(Boolean));const yes=value===undefined?!c.has(name):value;yes?c.add(name):c.delete(name);this.className=[...c].join(' ');return yes;}
  get childNodes(){return this.children;}
  get textContent(){return this._text+this.children.map(c=>c.textContent).join(' ');}
  set textContent(value){this._text=String(value);this.children=[];}
  append(...children){for(const c of children){this.children.push(c);c.parentNode=this;}}
  replaceChildren(...children){this.children.forEach(c=>c.parentNode=null);this.children=[];this._text='';this.append(...children);}
  cloneNode(deep=false){const n=new Element(this.tagName);n._text=this._text;n.className=this.className;if(deep)n.append(...this.children.map(c=>c.cloneNode(true)));return n;}
  setAttribute(k,v){this.attributes[k]=String(v);}
  addEventListener(k,f){(this.listeners[k]??=[]).push(f);}
  remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(c=>c!==this);this.parentNode=null;}
  click(){if(!this.disabled)for(const f of this.listeners.click||[])f({preventDefault(){}});}
  focus(){} scrollIntoView(){} setSelectionRange(){} dispatchEvent(){} reportValidity(){return true;}
}
const html=fs.readFileSync(process.argv[2],'utf8');
for(const [,id] of html.matchAll(/\bid="([^"]+)"/g)){const n=new Element();n.id=id;nodes.set(id,n);}
global.document={getElementById:id=>nodes.get(id)||all.find(n=>n.id===id)||null,createElement:tag=>new Element(tag),createTextNode:text=>{const n=new Element('#text');n.textContent=text;return n;},querySelectorAll:()=>[],querySelector:()=>null,addEventListener(){}};
global.window={addEventListener(){},matchMedia(){return {matches:true};}};global.navigator={};global.requestAnimationFrame=f=>f();
global.localStorage={getItem(){return null;},setItem(){}};global.crypto={randomUUID:()=>"ui-test"};
global.__sent=[];
let source=fs.readFileSync(process.argv[1],'utf8');
const marker='  start();\n})();';assert(source.includes(marker));
source=source.replace(marker,"  globalThis.__ui={state,renderTrip,loadSession}; send=async(kind,payload,label)=>{globalThis.__sent.push({kind,payload,label});};\n})();");
vm.runInThisContext(source);
const ui=global.__ui,root=nodes.get('trip-content');
const walk=n=>[n,...n.children.flatMap(walk)];
const findButton=text=>walk(root).find(n=>n.tagName==='BUTTON'&&n.textContent===text);
const group=kind=>walk(root).find(n=>n.dataset.componentGroup===kind);
const transport=(kind,id,price)=>({product_type:kind,component_id:id,options_version:4,title:kind==='rail'?'Поезд 017':'Рейс A1',price_minor:price,currency:'RUB',eligibility:'satisfied',query:{origin:'Москва',destination:'Казань'},conditions:{direct:true,baggage_kg:kind==='avia'?23:null,seat_category:kind==='rail'?'Купе':null,refundable:true},provider_refs:{reference_key:id},departure_at:'2026-11-20T23:10:00+03:00',arrival_at:'2026-11-21T06:30:00+03:00',return_departure_at:'2026-11-24T12:00:00+03:00',return_arrival_at:'2026-11-24T19:00:00+03:00'});
const avia=transport('avia','a1',3000000),rail=transport('rail','r1',1600000);
const hotel={product_type:'hotel',component_id:'h1',options_version:4,title:'Отель Центральный',price_minor:2400000,currency:'RUB',eligibility:'satisfied',query:{destination:'Казань',departure_date:'2026-11-21',return_date:'2026-11-24'},conditions:{room_name:'Стандарт',breakfast:true,rating:9.1,free_cancellation:true},provider_refs:{reference_key:'h1'}};
const request={origin:'Москва',destination:'Казань',products:['avia','rail','hotel'],adults:1};
const base={state_version:2,status:'options_ready',transport_options:[avia,rail],hotel_options:[hotel],selected_components:[],options:[],message:'Выберите варианты'};
Object.assign(ui.state,{ready:true,busy:false,config:{voice:{available:false},web_search:{configured:true}},session:{request},messages:[],version:2,result:base});
ui.state.result={...base,response_kind:'chat',transport_options:[],hotel_options:[],status:'needs_input',message:'17 × 23 = 391.'};
ui.state.messages=[{role:'assistant',text:ui.state.result.message}];ui.renderTrip();
assert(!root.textContent.includes('Ответьте на вопрос'),'small talk does not display a nonexistent trip clarification');
assert.equal(nodes.get('trip-title').textContent,'Ваша поездка');
ui.state.result=base;ui.state.messages=[];
ui.renderTrip();
assert(group('avia')&&group('rail')&&group('hotel'),'three independent groups');
assert(group('rail').textContent.includes('Купе'));
assert(group('avia').textContent.includes('23:10')&&group('avia').textContent.includes('06:30'),'provider local clocks are retained');
findButton('Выбрать поезд').click();
assert.deepEqual(__sent.pop(),{kind:'select_component',payload:{component_id:'r1',options_version:4},label:'Выбираю поезд 1: Поезд 017'});
const partial={components:[avia],total_minor:avia.price_minor,complete:false,missing_products:['hotel'],eligibility:'unknown',request};
ui.state.result={...base,current_plan:partial,selected_components:[avia],status:'plan_ready'};
ui.renderTrip();
assert(root.textContent.includes('Осталось выбрать отель'));
assert(group('hotel')&&findButton('Выбрать отель'),'partial plan retains hotel choices');
assert(!findButton('Проверить тестовое оформление'),'partial plan cannot check out');
findButton('Убрать из выбора').click();assert.deepEqual(__sent.pop().payload,{product_type:'avia'});
const complete={...partial,components:[avia,hotel],total_minor:5400000,complete:true,missing_products:[],eligibility:'satisfied'};
ui.state.result={...base,current_plan:complete,selected_components:[avia,hotel],status:'plan_ready'};
ui.renderTrip();
assert(findButton('Проверить тестовое оформление'));
assert(group('rail')&&findButton('Выбрать поезд'),'replacement choices remain after a complete plan');
ui.state.config.checkout_mode='disabled';ui.renderTrip();
assert(!findButton('Проверить тестовое оформление'),'disabled checkout has no action even for a complete plan');
ui.state.config.checkout_mode='demo';
ui.state.result={...ui.state.result,current_plan:{...complete,verification_status:'unverified'}};
ui.renderTrip();assert(!findButton('Проверить тестовое оформление'));assert(findButton('Обновить варианты'));
ui.state.session={request:{...request,products:['hotel']}};
ui.state.result={...base,transport_options:[],current_plan:null};ui.renderTrip();
assert(group('hotel')&&!group('avia')&&!group('rail'),'hotel-only never adds transport');
ui.state.sessionId='restore';
const restored={session:{state_version:7,request,transport_options:[rail],hotel_options:[hotel],selected_components:[rail],current_plan:{...partial,components:[rail]},component_errors:{}},messages:[]};
global.fetch=async()=>({ok:true,json:async()=>restored});
(async()=>{await ui.loadSession({render:false});assert.equal(ui.state.result.transport_options[0].component_id,'r1');assert.equal(ui.state.result.hotel_options[0].component_id,'h1');assert.equal(ui.state.result.selected_components[0].component_id,'r1');ui.renderTrip();assert(group('hotel'));assert(!findButton('Проверить тестовое оформление'));console.log('component UI behavior OK');})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    completed = subprocess.run(
        [node, "-e", script, str(ROOT / "src/travel_agent/web_static/app.js"),
         str(ROOT / "src/travel_agent/web_static/index.html")],
        capture_output=True, text=True, timeout=15,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "component UI behavior OK" in completed.stdout
