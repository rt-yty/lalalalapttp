import {expect,test,type Page} from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import {mkdir} from 'node:fs/promises';
import type {Recommendation,RecommendationFeed} from '../src/types';
const user={id:'recommendations-user',email:'travel@example.test',name:'Алексей',email_verified:false};
const korea:Recommendation={id:'destination:korea',title:'Южная Корея',description:'Сеул, гастрономия и новые маршруты по Восточной Азии.',reason:'Вы интересовались Японией. Продолжите знакомство с Восточной Азией.',prompt:'Хочу рассмотреть Южную Корею вместо Японии. Общий бюджет до 180000 рублей. Помоги сравнить варианты и проверь актуальные цены.',kind:'destination',source:'personalized',budget_minor:18000000,budget_label:'Ваш бюджет: до 180 000 ₽ на всех'};
const rock:Recommendation={id:'artist:rammstein',title:'Поездка на Rammstein',description:'Другой характер живого рока — и новый город для поездки.',reason:'После интереса к Imagine Dragons — ещё одна идея для музыкального путешествия.',prompt:'Проверь, есть ли будущие концерты Rammstein, и помоги выбрать город для поездки.',kind:'artist',source:'personalized',budget_minor:null};
const weekend:Recommendation={id:'theme:weekend',title:'Выходные в новом городе',description:'Короткая поездка, чтобы сменить обстановку.',reason:'Стартовая идея — подберём направление под ваши пожелания.',prompt:'Хочу на выходные в новый город. Помоги выбрать направление.',kind:'theme',source:'starter',budget_minor:null};
const concert:Recommendation={id:'theme:concert',title:'Поездка на концерт',description:'Музыка, любимый артист и знакомство с новым городом.',reason:'Стартовая идея — сначала проверим афишу.',prompt:'Хочу поехать на концерт. Помоги сравнить ближайшие даты и города.',kind:'artist',source:'starter',budget_minor:null};
const starter:RecommendationFeed={personalized:[],popular:[weekend,concert],popular_source:'starter',history_used:false};
const personalized:RecommendationFeed={personalized:[korea,rock],popular:[{...weekend,source:'popular',reason:'Одно из популярных направлений поиска за последнее время.'},{...concert,source:'popular',reason:'Путешественники часто ищут поездки на концерты.'}],popular_source:'community',history_used:true};
async function setup(page:Page,initial=personalized,{failed=false,failDismiss=false}={}){
 let feed=structuredClone(initial),failure=failed,dismissFailure=failDismiss,reads=0,private401=false;
 const hidden=new Set<string>(),turns:unknown[]=[],dismissals:string[]=[];
 await page.route('**/api/**',async route=>{
  const path=new URL(route.request().url()).pathname;
  if(path==='/api/bootstrap')return route.fulfill({json:{csrf_token:'recommendations-token',user:private401?null:user,data_mode:'live',checkout_mode:'disabled'}});
  if(path==='/api/recommendations'){
   reads++;
   if(private401)return route.fulfill({status:401,json:{detail:'Войдите в аккаунт'}});
   if(failure)return route.fulfill({status:503,json:{detail:'Временно недоступно'}});
   return route.fulfill({json:{...feed,personalized:feed.personalized.filter(item=>!hidden.has(item.id)),popular:feed.popular.filter(item=>!hidden.has(item.id))}});
  }
  if(path.startsWith('/api/recommendations/')&&path.endsWith('/dismiss')){
   if(dismissFailure)return route.fulfill({status:503,json:{detail:'Временно недоступно'}});
   const id=decodeURIComponent(path.split('/')[3]);hidden.add(id);dismissals.push(id);return route.fulfill({json:{ok:true}});
  }
  if(path==='/api/sessions')return route.fulfill({json:{sessions:[]}});
  if(path==='/api/profile')return route.fulfill({json:{origin:'Москва',preferences:[]}});
  if(path==='/api/turns'){turns.push(route.request().postDataJSON());return route.fulfill({json:{job_id:'job'}});}
  if(path==='/api/auth/logout'){private401=true;return route.fulfill({json:{user:null,csrf_token:'signed-out'}});}
  return route.fulfill({json:{session:null,result:null,messages:[],active_job_id:null}});
 });
 await page.goto('/');await expect(page.getByRole('textbox',{name:'Сообщение помощнику'})).toBeEnabled();
 return {turns,dismissals,reads:()=>reads,setFeed:(value:RecommendationFeed)=>{feed=value;},recover:()=>{failure=false;},failDismiss:(value:boolean)=>{dismissFailure=value;},expire:()=>{private401=true;}};
}
const button=(page:Page,name:string)=>page.getByRole('button',{name,exact:true});
const composer=(page:Page)=>page.getByRole('textbox',{name:'Сообщение помощнику'});

test('new users receive honest starter ideas and clicking only fills a draft',async({page})=>{
 const app=await setup(page,starter);await expect(page.getByRole('heading',{name:'Идеи для первой поездки'})).toBeVisible();await expect(page.getByRole('heading',{name:'Для вас',exact:true})).toHaveCount(0);await expect(page.getByRole('heading',{name:'Популярное у путешественников'})).toHaveCount(0);
 await button(page,weekend.title).click();await expect(composer(page)).toHaveValue(weekend.prompt);expect(app.turns).toHaveLength(0);await composer(page).fill('Хочу на выходные на поезде');await expect(composer(page)).toHaveValue('Хочу на выходные на поезде');
});
test('returning users see reasons, prior budget and genuine community section',async({page})=>{
 const app=await setup(page);await expect(page.getByRole('heading',{name:'Для вас',exact:true})).toBeVisible();await expect(page.getByText(korea.reason,{exact:true})).toBeVisible();await expect(page.getByText(korea.budget_label!,{exact:true})).toBeVisible();await expect(page.getByRole('heading',{name:'Популярное у путешественников'})).toBeVisible();await button(page,korea.title).click();await expect(composer(page)).toHaveValue(korea.prompt);expect(app.turns).toHaveLength(0);
 await button(page,rock.title).click();await expect(composer(page)).toHaveValue(rock.prompt);expect(app.turns).toHaveLength(0);await expect(page.locator('.event-card')).toHaveCount(0);
 await mkdir('artifacts/recommendations',{recursive:true});await page.screenshot({path:'artifacts/recommendations/personalized-desktop.png',fullPage:true});
});
test('a dismissed idea stays hidden after refresh and never submits a trip',async({page})=>{
 const app=await setup(page);await button(page,'Скрыть идею: '+korea.title).click();await expect(button(page,korea.title)).toHaveCount(0);expect(app.dismissals).toEqual([korea.id]);await page.reload();await expect(button(page,rock.title)).toBeVisible();await expect(button(page,korea.title)).toHaveCount(0);expect(app.turns).toHaveLength(0);
});
test('failed dismissal preserves the card and can be retried',async({page})=>{
 const app=await setup(page,personalized,{failDismiss:true});await button(page,'Скрыть идею: '+korea.title).click();await expect(page.getByText('Не удалось скрыть идею. Попробуйте ещё раз.')).toBeVisible();await expect(button(page,korea.title)).toBeEnabled();app.failDismiss(false);await button(page,'Скрыть идею: '+korea.title).click();await expect(button(page,korea.title)).toHaveCount(0);
});
test('recommendations refresh when starting another trip',async({page})=>{
 const app=await setup(page,starter);await expect(button(page,weekend.title)).toBeVisible();const before=app.reads();app.setFeed(personalized);await button(page,'Новая поездка').click();await expect(button(page,korea.title)).toBeVisible();expect(app.reads()).toBeGreaterThan(before);
});
test('feed outage leaves honest usable examples and a working retry',async({page})=>{
 const app=await setup(page,personalized,{failed:true});await expect(page.getByRole('heading',{name:'С чего начать'})).toBeVisible();await button(page,'Поездка на концерт').click();await expect(composer(page)).toHaveValue(concert.prompt);expect(app.turns).toHaveLength(0);await expect(page.getByRole('heading',{name:'Популярное у путешественников'})).toHaveCount(0);app.recover();await button(page,'Обновить рекомендации').click();await expect(button(page,korea.title)).toBeVisible();await expect(page.getByText(/Не удалось обновить рекомендации/)).toHaveCount(0);
});
test('expired recommendation session clears account content',async({page})=>{
 const app=await setup(page);await expect(button(page,korea.title)).toBeVisible();app.expire();await button(page,'Новая поездка').click();await expect(page.getByRole('heading',{name:'Вход в аккаунт'})).toBeVisible();await expect(page.getByText(korea.title,{exact:true})).toHaveCount(0);await expect(composer(page)).toHaveCount(0);
});
for(const width of [390,1440])test(`recommendations are accessible without overflow at ${width}px`,async({page})=>{
 await page.setViewportSize({width,height:width===390?844:1000});await setup(page);await expect(button(page,korea.title)).toBeVisible();expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);const audit=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();expect(audit.violations).toEqual([]);await expect(composer(page)).toBeVisible();await mkdir('artifacts/recommendations',{recursive:true});await page.screenshot({path:`artifacts/recommendations/personalized-${width}.png`,fullPage:true});
});

for(const malformed of [
 {...personalized,personalized:[null]},
 {...personalized,popular:[{...weekend,source:'starter'}]},
 {...personalized,personalized:[korea,korea]},
])test(`invalid recommendation response remains recoverable (${JSON.stringify(malformed.personalized).slice(0,16)}-${malformed.popular[0]?.source})`,async({page})=>{
 const errors:string[]=[];page.on('pageerror',error=>errors.push(error.message));
 await setup(page,personalized,{failed:true});
 await expect(page.getByRole('heading',{name:'С чего начать'})).toBeVisible();
 await page.route('**/api/recommendations',route=>route.fulfill({json:malformed}));
 await button(page,'Обновить рекомендации').click();
 await expect(page.getByRole('heading',{name:'С чего начать'})).toBeVisible();
 await expect(composer(page)).toBeEnabled();expect(errors).toEqual([]);
 await page.route('**/api/recommendations',route=>route.fulfill({json:personalized}));
 await button(page,'Обновить рекомендации').click();
 await expect(button(page,korea.title)).toBeVisible();
});
test('late dismissal after starting a new trip refreshes the visible home',async({page})=>{
 const app=await setup(page);await expect(button(page,korea.title)).toBeVisible();
 let release=()=>{};const waiting=new Promise<void>(resolve=>{release=resolve;});
 await page.route('**/api/recommendations/*/dismiss',async route=>{await waiting;await route.fallback();});
 await button(page,'Скрыть идею: '+korea.title).click();
 await expect(button(page,'Скрыть идею: '+korea.title)).toBeDisabled();
 const before=app.reads();await button(page,'Новая поездка').click();
 await expect.poll(app.reads).toBeGreaterThan(before);await expect(button(page,korea.title)).toBeVisible();
 release();await expect(button(page,korea.title)).toHaveCount(0);
 expect(app.dismissals).toEqual([korea.id]);
});
test('sparse true popularity is supplemented by separately labeled starter cards',async({page})=>{
 await setup(page,{personalized:[],popular:[{...korea,source:'popular',budget_minor:null,budget_label:null}],starter:[weekend,concert],popular_source:'community',history_used:false});
 await expect(page.getByRole('heading',{name:'Популярное у путешественников'})).toBeVisible();
 await expect(page.getByRole('heading',{name:'Ещё идеи для поездки'})).toBeVisible();
 await expect(page.getByRole('region',{name:'Популярное у путешественников'}).getByRole('button',{name:weekend.title,exact:true})).toHaveCount(0);
 await button(page,'Скрыть идею: '+weekend.title).click();await expect(button(page,weekend.title)).toHaveCount(0);
});

test('long recommendation copy and budget remain within a 320px screen',async({page})=>{
 await page.setViewportSize({width:320,height:740});
 const long={...korea,title:'Путешествие в Южную Корею с большой семьёй',description:'Несколько городов, гастрономия, музеи, прогулки и отдых в комфортном для вашей семьи темпе. '.repeat(3),reason:'Вы интересовались поездкой в Японию: предлагаем познакомиться с другим направлением Восточной Азии. '.repeat(2),budget_label:'Ваш бюджет: до 1 250 000,50 ₽ на всех, включая дорогу и проживание'};
 await setup(page,{personalized:[long],popular:[],popular_source:'starter',history_used:true});
 await expect(button(page,long.title)).toBeVisible();
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
 await button(page,long.title).click();await expect(composer(page)).toHaveValue(long.prompt);
 await expect(composer(page)).toBeInViewport();
 const audit=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();expect(audit.violations).toEqual([]);
});
