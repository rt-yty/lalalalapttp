import {test,expect,type Page} from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import {mkdir} from 'node:fs/promises';
const user={id:'audit-account',email:'audit@example.test',name:'Путешественник',email_verified:false};
const request={origin:'Москва',destination:'Казань',departure_date:'2026-11-24',return_date:'2026-11-27',adults:1,children_ages:[],products:['hotel'],budget_minor:4000000};
const hotel={component_id:'audit-hotel',options_version:1,product_type:'hotel',title:'Отель у Кремля',price_minor:1800000,currency:'RUB',eligibility:'satisfied',query:request,conditions:{room_name:'Номер с завтраком',breakfast:true},location:{latitude:55.8,longitude:49.1}};
const saved={session:{session_id:'audit-trip',state_version:4,request},result:{session_id:'audit-trip',state_version:4,status:'options_ready',message:'Я нашёл отель.',hotel_options:[hotel],transport_options:[],selected_components:[]},messages:[{role:'assistant',text:'Я нашёл отель.'}],active_job_id:null};
type State=Record<string,any>;
async function setup(page:Page,{state=saved,voice=false,sessionError=false}: {state?:State;voice?:boolean;sessionError?:boolean}={}){
 const turns:State[]=[];let snapshot=structuredClone(state),fail=sessionError;
 await page.addInitScript(()=>localStorage.setItem('odin.active-session.v1','audit-trip'));
 await page.route('**/api/**',async route=>{
  const path=new URL(route.request().url()).pathname;
  if(path==='/api/bootstrap')return route.fulfill({json:{csrf_token:'audit-token',user,data_mode:'live',checkout_mode:'disabled',voice:{available:voice}}});
  if(path==='/api/sessions')return route.fulfill({json:{sessions:[{session_id:'other-trip',title:'Другая поездка',updated_at:'2026-10-28'}]}});
  if(path==='/api/profile')return route.fulfill({json:{origin:'',preferences:[]}});
  if(path==='/api/voice/transcribe')return route.fulfill({json:{text:'Хочу в Сочи'}});
  if(path==='/api/turns'){const turn=route.request().postDataJSON();turns.push(turn);snapshot={...snapshot,session:{session_id:turn.session_id,state_version:5,request},result:{...snapshot.result,state_version:5},messages:[...snapshot.messages,{role:'user',text:turn.payload.text??'Выбираю отель'},{role:'assistant',text:'Я учёл пожелания.'}]};return route.fulfill({json:{job_id:'audit-job'}});}
  if(path.startsWith('/api/jobs/'))return route.fulfill({json:{status:'completed',result:snapshot.result}});
  if(path==='/api/sessions/other-trip')return route.fulfill({json:{session:{session_id:'other-trip',state_version:1,request:{destination:'Сочи'}},result:null,messages:[],active_job_id:null}});
  if(fail)return route.fulfill({status:503,json:{detail:'Я пока не смог загрузить поездку.'}});
  return route.fulfill({json:snapshot});
 });
 await page.goto('/');await expect(page.getByRole('textbox',{name:'Сообщение помощнику'})).toBeVisible();
 if(!sessionError)await expect(page.getByRole('textbox',{name:'Сообщение помощнику'})).toBeEnabled();
 return {turns,setState:(next:State)=>{snapshot=next;},recover:()=>{fail=false;}};
}
const composer=(page:Page)=>page.getByRole('textbox',{name:'Сообщение помощнику'});
const button=(page:Page,name:string)=>page.getByRole('button',{name,exact:true});
async function fakeMicrophone(page:Page,defer=false){
 await page.addInitScript(deferred=>{
  const state=window as any;state.microphoneStops=0;state.recorderStarts=0;state.permissionRequests=0;
  const stream={getTracks:()=>[{stop:()=>state.microphoneStops++}]};
  Object.defineProperty(navigator,'mediaDevices',{configurable:true,value:{getUserMedia:()=>{state.permissionRequests++;return deferred?new Promise(resolve=>{state.releaseMicrophone=()=>resolve(stream);}):Promise.resolve(stream);}}});
  state.MediaRecorder=class{static isTypeSupported(){return true;}state='inactive';mimeType='audio/webm';stream:any;onstop:(()=>void)|null=null;ondataavailable:((event:any)=>void)|null=null;constructor(source:any){this.stream=source;}start(){this.state='recording';state.recorderStarts++;}stop(){this.state='inactive';this.ondataavailable?.({data:new Blob(['audit audio'])});this.onstop?.();}};
 },defer);
}
async function evidence(page:Page,name:string){await mkdir('artifacts/comprehensive-audit',{recursive:true});await page.screenshot({path:'artifacts/comprehensive-audit/'+name+'.png',fullPage:true});}

test('AUD-UI-01 failed saved trip load cannot submit with an unknown version',async({page})=>{
 const app=await setup(page,{sessionError:true});await expect(button(page,'Подключиться снова')).toBeVisible();await expect(composer(page)).toBeDisabled();expect(app.turns).toHaveLength(0);
 app.recover();await button(page,'Подключиться снова').click();await expect(composer(page)).toBeEnabled();await composer(page).fill('Нужен завтрак');await button(page,'Отправить сообщение').click();await expect.poll(()=>app.turns.length).toBe(1);expect(app.turns[0].expected_state_version).toBe(4);
});
test('AUD-UI-02 preferences dialog has an accessible name',async({page})=>{
 await setup(page);await button(page,'Мои предпочтения').click();await expect(button(page,'Сохранить')).toBeEnabled();await expect(page.getByRole('dialog')).toHaveAccessibleName('Мои предпочтения');await button(page,'Сохранить').evaluate(el=>Promise.all(el.getAnimations().map(animation=>animation.finished)));const audit=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();expect(audit.violations).toEqual([]);await expect(page.getByRole('dialog')).toHaveAccessibleName('Мои предпочтения');await evidence(page,'preferences-desktop');
});
for(const action of ['new','history'])test('AUD-UI-03-'+action+' changing trip cancels active recording without upload',async({page})=>{
 await fakeMicrophone(page);await setup(page,{voice:true});let uploads=0;page.on('request',r=>{if(r.url().includes('/voice/transcribe'))uploads++;});
 await button(page,'Записать голосовое сообщение').click();await expect(button(page,'Остановить запись')).toBeVisible();
 if(action==='new')await button(page,'Новая поездка').first().click();else await page.getByRole('navigation',{name:'Сохранённые поездки'}).getByRole('button',{name:'Другая поездка'}).click();
 await expect(button(page,'Остановить запись')).toHaveCount(0);expect(uploads).toBe(0);expect(await page.evaluate(()=>(window as any).microphoneStops)).toBe(1);await expect(composer(page)).toHaveValue('');
});
test('AUD-UI-04 a late microphone permission cannot start recording in a different trip',async({page})=>{
 await fakeMicrophone(page,true);await setup(page,{voice:true});await button(page,'Записать голосовое сообщение').click();await button(page,'Новая поездка').first().click();await page.evaluate(()=>(window as any).releaseMicrophone());await expect.poll(()=>page.evaluate(()=>(window as any).microphoneStops)).toBe(1);expect(await page.evaluate(()=>(window as any).recorderStarts)).toBe(0);
});
test('AUD-UI-05 a late transcript cannot alter the draft of a different trip',async({page})=>{
 await fakeMicrophone(page);await setup(page,{voice:true});let release=()=>{};const pending=new Promise<void>(resolve=>{release=resolve;});
 await page.route('**/api/voice/transcribe',async route=>{await pending;await route.fulfill({json:{text:'Текст из прежней поездки'}});});
 await button(page,'Записать голосовое сообщение').click();await button(page,'Остановить запись').click();await expect(page.getByText('Распознавание…')).toBeVisible();await button(page,'Новая поездка').first().click();await composer(page).fill('Мой новый черновик');release();await expect(page.getByText('Распознавание…')).toHaveCount(0);await expect(composer(page)).toHaveValue('Мой новый черновик');
});
test('AUD-UI-06 recording can be explicitly cancelled without uploading or changing a draft',async({page})=>{
 await fakeMicrophone(page);const app=await setup(page,{voice:true});let uploads=0;page.on('request',r=>{if(r.url().includes('/voice/transcribe'))uploads++;});
 await composer(page).fill('Мой черновик');await button(page,'Записать голосовое сообщение').click();await button(page,'Отменить запись').click();await expect(button(page,'Записать голосовое сообщение')).toBeVisible();await expect(composer(page)).toHaveValue('Мой черновик');expect(uploads).toBe(0);expect(app.turns).toHaveLength(0);
});
test('AUD-UI-07 unavailable product does not promise nonexistent found options',async({page})=>{
 await setup(page,{state:{...saved,result:{...saved.result,hotel_options:[],component_errors:{hotel:{code:'unavailable'}}}}});await expect(page.locator('.missing-note')).not.toContainText('Можно продолжить с найденными');await expect(page.locator('.missing-note')).toContainText('Повторите запрос');
});
test('AUD-UI-08 Shift Enter adds a newline and Enter sends exactly once',async({page})=>{
 const app=await setup(page);await composer(page).fill('Нужен завтрак');await composer(page).press('Shift+Enter');await composer(page).pressSequentially('И тихий номер');expect(app.turns).toHaveLength(0);await composer(page).press('Enter');await expect.poll(()=>app.turns.length).toBe(1);expect(app.turns[0].payload.text).toBe('Нужен завтрак\nИ тихий номер');
});
test('AUD-UI-09 whitespace cannot be sent',async({page})=>{
 const app=await setup(page);await composer(page).fill('  \n  ');await composer(page).press('Enter');await expect(button(page,'Отправить сообщение')).toBeDisabled();expect(app.turns).toHaveLength(0);
});
test('AUD-UI-10 keyboard composition never submits unfinished input',async({page})=>{
 const app=await setup(page);await composer(page).fill('Токио');await composer(page).dispatchEvent('keydown',{key:'Enter',isComposing:true});expect(app.turns).toHaveLength(0);await expect(composer(page)).toHaveValue('Токио');
});
test('AUD-UI-11 a fast duplicate submission produces one request',async({page})=>{
 const app=await setup(page);let release=()=>{};const pending=new Promise<void>(resolve=>{release=resolve;});let received=0;await page.route('**/api/turns',async route=>{received++;await pending;await route.fallback();});await composer(page).fill('Нужен завтрак');await composer(page).press('Enter');await composer(page).press('Enter');await expect.poll(()=>received).toBe(1);await expect(button(page,'Отправить сообщение')).toBeDisabled();release();await expect.poll(()=>app.turns.length).toBe(1);
});
test('AUD-UI-12 chat response hides travel-action suggestions',async({page})=>{
 await setup(page,{state:{...saved,result:{...saved.result,response_kind:'chat',suggestions:[{id:'wrong',label:'Забронировать всё',text:'Забронировать'}]}}});await expect(button(page,'Забронировать всё')).toHaveCount(0);
});
test('AUD-UI-13 normal voice recognition fills a draft without sending',async({page})=>{
 await fakeMicrophone(page);const app=await setup(page,{voice:true});await composer(page).fill('Привет.');await button(page,'Записать голосовое сообщение').click();await button(page,'Остановить запись').click();await expect(composer(page)).toHaveValue('Привет. Хочу в Сочи');expect(app.turns).toHaveLength(0);
});
test('AUD-UI-14 denied microphone permission leaves normal text usable',async({page})=>{
 await page.addInitScript(()=>Object.defineProperty(navigator,'mediaDevices',{configurable:true,value:{getUserMedia:()=>Promise.reject(new Error('denied'))}}));await setup(page,{voice:true});await button(page,'Записать голосовое сообщение').click();await expect(page.getByRole('alert')).toContainText('Разрешите запись');await composer(page).fill('Поедем в Сочи');await expect(button(page,'Отправить сообщение')).toBeEnabled();
});
test('AUD-UI-15 profile failure preserves unsaved input and never claims success',async({page})=>{
 await setup(page);await button(page,'Мои предпочтения').click();await page.getByLabel('Обычно отправляюсь из').fill('Тверь');await page.route('**/api/profile',route=>route.fulfill({status:503,json:{detail:'Не удалось сохранить пожелания.'}}));await button(page,'Сохранить').click();await expect(page.getByRole('alert')).toHaveText('Не удалось сохранить пожелания.');await expect(page.getByLabel('Обычно отправляюсь из')).toHaveValue('Тверь');await expect(page.getByText('Предпочтения сохранены',{exact:true})).toHaveCount(0);
});
test('AUD-UI-16 an overlong preference is rejected before the request',async({page})=>{
 await setup(page);let writes=0;page.on('request',r=>{if(r.url().endsWith('/api/profile')&&r.method()==='PATCH')writes++;});await button(page,'Мои предпочтения').click();await page.getByLabel('Что важно в поездке').fill('а'.repeat(161));await button(page,'Сохранить').click();await expect(page.getByRole('alert')).toContainText('каждое до 160 символов');expect(writes).toBe(0);
});
test('AUD-UI-17 hotel-only trip never asks to choose a road',async({page})=>{
 await setup(page,{state:{...saved,result:{...saved.result,selected_components:[hotel],current_plan:{total_minor:1800000,complete:true,eligibility:'satisfied',verification_status:'verified',missing_products:[]}}}});await expect(page.locator('.trip-summary h2')).toHaveText('Казань');await expect(page.locator('.trip-summary')).not.toContainText('Москва');await expect(page.getByText('Выбранное проживание',{exact:true})).toBeVisible();await expect(button(page,'Оформить')).toBeEnabled();
});
test('AUD-UI-18 an unverified plan cannot be marked completed',async({page})=>{
 await setup(page,{state:{...saved,result:{...saved.result,selected_components:[hotel],current_plan:{total_minor:1800000,complete:true,eligibility:'satisfied',verification_status:'unverified',missing_products:[]}}}});await expect(button(page,'Оформить')).toBeDisabled();
});
test('AUD-UI-19 history errors never present an empty account as a fact',async({page})=>{
 await setup(page);await page.route('**/api/sessions',route=>route.fulfill({status:503,json:{detail:'unavailable'}}));await button(page,'История поездок').click();await expect(page.getByRole('dialog').getByRole('alert')).toContainText('не смог загрузить историю');await expect(page.getByText('После первого сообщения поездка появится здесь.')).toHaveCount(0);await page.keyboard.press('Escape');await expect(composer(page)).toBeEnabled();
});
test('AUD-UI-20 assistant paragraphs and long words remain readable on small screen',async({page})=>{
 const response='Я учёл пожелания.\n\nМожно выбрать отель и уточнить условия отмены.\n'+('длинноеслово'.repeat(40));await setup(page,{state:{...saved,messages:[{role:'assistant',text:response}]}});await page.setViewportSize({width:320,height:568});await expect(page.locator('.assistant-message .message-text')).toHaveText(response);expect(await page.locator('.messages').evaluate(el=>el.scrollWidth<=el.clientWidth)).toBe(true);await expect(composer(page)).toBeInViewport();await evidence(page,'long-answer-mobile');
});


test('AUD-UI-21 a delayed profile load cannot overwrite a reopened dialog or its draft',async({page})=>{
 await setup(page);let release=()=>{};const waiting=new Promise<void>(resolve=>{release=resolve;});let reads=0;
 await page.route('**/api/profile',async route=>{reads++;if(reads===1){await waiting;await route.fulfill({json:{origin:'Москва',preferences:['Старые пожелания']}});}else await route.fulfill({json:{origin:'Казань',preferences:['Новые пожелания']}});});
 await button(page,'Мои предпочтения').click();await button(page,'Закрыть предпочтения').click();await button(page,'Мои предпочтения').click();await expect(page.getByLabel('Обычно отправляюсь из')).toHaveValue('Казань');await page.getByLabel('Обычно отправляюсь из').fill('Тверь');
 const response=page.waitForResponse(r=>r.url().endsWith('/api/profile'));release();await (await response).finished();await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));await expect.poll(()=>reads).toBe(2);await expect(page.getByLabel('Обычно отправляюсь из')).toHaveValue('Тверь');await expect(page.getByLabel('Что важно в поездке')).toHaveValue('Новые пожелания');
});
test('AUD-UI-22 requesting microphone permission twice cannot create duplicate recordings',async({page})=>{
 await fakeMicrophone(page,true);await setup(page,{voice:true});await button(page,'Записать голосовое сообщение').click();await expect(button(page,'Записать голосовое сообщение')).toBeDisabled();await expect(button(page,'Отправить сообщение')).toBeDisabled();expect(await page.evaluate(()=>(window as any).permissionRequests)).toBe(1);await button(page,'Отменить запись').click();await page.evaluate(()=>(window as any).releaseMicrophone());await expect.poll(()=>page.evaluate(()=>(window as any).microphoneStops)).toBe(1);expect(await page.evaluate(()=>(window as any).recorderStarts)).toBe(0);
});
test('AUD-UI-23 a failed session load can be escaped by starting a fresh trip',async({page})=>{
 const app=await setup(page,{sessionError:true});await expect(button(page,'Подключиться снова')).toBeVisible();await button(page,'Новая поездка').first().click();await expect(composer(page)).toBeEnabled();await composer(page).fill('Начнём заново');await button(page,'Отправить сообщение').click();await expect.poll(()=>app.turns.length).toBe(1);expect(app.turns[0].session_id).not.toBe('audit-trip');expect(app.turns[0].expected_state_version).toBe(0);
});
test('AUD-UI-24 hotel details explain unknown payment and cancellation without inventing them',async({page})=>{
 await setup(page);await page.getByText('Условия номера',{exact:true}).click();await expect(page.getByText('Условия отмены не указаны.',{exact:true})).toBeVisible();await expect(page.getByText('Оплата онлайн: не подтверждена.',{exact:true})).toBeVisible();await expect(page.getByText('Оплата при заселении: не подтверждена.',{exact:true})).toBeVisible();
});
test('AUD-UI-25 asking about reviews fills an editable draft without accepting an offer',async({page})=>{
 const app=await setup(page);await button(page,'Об отзывах').click();await expect(composer(page)).toHaveValue('Что известно об отзывах об отеле «Отель у Кремля»? Насколько можно им доверять?');expect(app.turns).toHaveLength(0);await expect(page.locator('.offer-card.chosen')).toHaveCount(0);
});
test('AUD-UI-26 map without a destination shows the next step and no invented location',async({page})=>{
 await setup(page,{state:{session:null,result:null,messages:[{role:'assistant',text:'Куда хотите поехать?'}],active_job_id:null}});await button(page,'Карта').click();await expect(page.getByRole('heading',{name:'Выберите направление'})).toBeVisible();await expect(page.locator('iframe')).toHaveCount(0);
});
test('AUD-UI-27 loading a new session blocks a stale event selection',async({page})=>{
 const event={event_id:'concert',artist:'Мураками',title:'Концерт',city:'Казань',date:'2026-11-24'};
 const app=await setup(page,{state:{...saved,result:{...saved.result,event_choices:[event],event_choices_version:2}}});let release=()=>{};const waiting=new Promise<void>(resolve=>{release=resolve;});await page.route('**/api/sessions/other-trip',async route=>{await waiting;await route.fallback();});await page.getByRole('navigation',{name:'Сохранённые поездки'}).getByRole('button',{name:'Другая поездка'}).click();await expect(button(page,'Выбрать концерт')).toBeDisabled();expect(app.turns).toHaveLength(0);release();await expect(composer(page)).toBeEnabled();
});


test('AUD-UI-28 hotel details omit zero stars, seconds and duplicated free-cancellation fee',async({page})=>{
 const until='2026-11-21T09:30:00Z';
 await setup(page,{state:{...saved,result:{...saved.result,hotel_options:[{...hotel,conditions:{...hotel.conditions,stars:0,check_in_time:'14:00:00',check_out_time:'12:00:00',free_cancellation:true,free_cancellation_until:until,cancellation_policy:[{end:'2026-11-21T12:30:00+03:00',penalty_minor:0},{start:until,penalty_minor:1800000}]}}]}}});
 await page.getByText('Условия номера',{exact:true}).click();await expect(page.getByText('Заезд с 14:00, выезд до 12:00.',{exact:true})).toBeVisible();await expect(page.locator('.stars')).toHaveCount(0);await expect(page.locator('.conditions')).not.toContainText('Штраф 0 ₽');await expect(page.locator('.conditions')).toContainText('Штраф 18 000 ₽');await expect(page.locator('.conditions')).toContainText('Бесплатная отмена до 21 нояб. 2026, 12:30 МСК');await evidence(page,'hotel-terms-desktop');
});
test('AUD-UI-29 a zero-fee interval with a different deadline is preserved',async({page})=>{
 await setup(page,{state:{...saved,result:{...saved.result,hotel_options:[{...hotel,conditions:{...hotel.conditions,free_cancellation:true,free_cancellation_until:'2026-11-21T09:30:00Z',cancellation_policy:[{end:'2026-11-20T09:30:00Z',penalty_minor:0}]}}]}}});await page.getByText('Условия номера',{exact:true}).click();await expect(page.locator('.conditions')).toContainText('Штраф 0 ₽');await expect(page.locator('.conditions')).toContainText('20 нояб.');
});


test('AUD-UI-30 map opens lazily with a clear loading state',async({page})=>{
 await setup(page);let release=()=>{};const pending=new Promise<void>(resolve=>{release=resolve;});await page.route('https://www.google.com/**',async route=>{await pending;await route.fulfill({contentType:'text/html',body:'<p>Карта загружена</p>'});});
 await expect(page.locator('iframe')).toHaveCount(0);await button(page,'Карта').click();await expect(page.getByText('Загружаю карту…')).toBeVisible();release();await expect(page.getByText('Загружаю карту…')).toHaveCount(0);await expect(page.locator('.map-fallback')).toHaveCount(0);
});
test('AUD-UI-31 stalled map offers a real location link and retry; a late load recovers',async({page})=>{
 await setup(page);await page.clock.install();let release=()=>{};const pending=new Promise<void>(resolve=>{release=resolve;});await page.route('https://www.google.com/**',async route=>{await pending;await route.fulfill({contentType:'text/html',body:'<p>Карта загружена</p>'});});
 await button(page,'Карта').click();await expect(page.getByText('Загружаю карту…')).toBeVisible();await page.clock.fastForward(12001);await expect(page.getByText('Карта пока не загрузилась.')).toBeVisible();await expect(page.getByRole('link',{name:'Посмотреть место в Google Maps'})).toHaveAttribute('href',/query=/);await expect(button(page,'Загрузить карту снова')).toBeVisible();await evidence(page,'map-timeout-fallback');release();await expect(page.locator('.map-fallback')).toHaveCount(0);
});
test('AUD-UI-32 retrying a stalled map creates a fresh load and keeps the chosen place',async({page})=>{
 await setup(page);await page.clock.install();let release=()=>{};const pending=new Promise<void>(resolve=>{release=resolve;});let calls=0;await page.route('https://www.google.com/**',async route=>{calls++;if(calls===1)await pending;await route.fulfill({contentType:'text/html',body:'<p>Карта загружена</p>'});});
 await button(page,'Карта').click();await page.clock.fastForward(12001);await button(page,'Загрузить карту снова').click();await expect.poll(()=>calls).toBe(2);await expect(page.getByText('Карта пока не загрузилась.')).toHaveCount(0);await expect(page.getByLabel('Место на карте')).toHaveValue('city');release();
});

