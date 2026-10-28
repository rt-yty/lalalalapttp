import {expect,test,type Page} from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import {mkdir} from 'node:fs/promises';
import type {RecommendationFeed} from '../src/types';

// No route mocks: real registration, auth cookies, turn jobs, DB and feed builder.
// Deterministic rules interpret the messages; external search is disabled.
const password='Disposable browser test 2026!';
const composer=(page:Page)=>page.getByRole('textbox',{name:'Сообщение помощнику'});
const choose=(page:Page,name:string)=>page.getByRole('button',{name,exact:true});
let accountSequence=0;
const email=()=>`browser-rec-${Date.now()}-${++accountSequence}@example.com`;
async function signup(page:Page,name='Тест рекомендаций'){
 const address=email();await page.goto('/');await choose(page,'Регистрация').click();
 await page.getByLabel('Имя',{exact:true}).fill(name);
 await page.getByLabel('Электронная почта').fill(address);
 await page.getByLabel('Пароль',{exact:true}).fill(password);
 await choose(page,'Создать аккаунт').click();await expect(composer(page)).toBeEnabled();
 await expect(page.locator('.recommendation-card').first()).toBeVisible();return address;
}
async function logout(page:Page){await choose(page,'Мой аккаунт').click();await choose(page,'Выйти из аккаунта').click();await expect(page.getByRole('heading',{name:'Вход в аккаунт'})).toBeVisible();}
async function login(page:Page,address:string){await page.getByLabel('Электронная почта').fill(address);await page.getByLabel('Пароль',{exact:true}).fill(password);await choose(page,'Войти').click();await expect(composer(page)).toBeEnabled();}
async function send(page:Page,text:string){
 const before=await page.locator('.assistant-message').count();
 await composer(page).fill(text);await choose(page,'Отправить сообщение').click();
 await expect(page.locator('.assistant-message')).toHaveCount(before+1,{timeout:20000});
 await expect(page.locator('.thinking')).toHaveCount(0);
}
async function home(page:Page){await choose(page,'Новая поездка').filter({visible:true}).first().click();await expect(page.locator('.recommendation-card').first()).toBeVisible();}
async function feed(page:Page):Promise<RecommendationFeed>{const response=await page.request.get('/api/recommendations');expect(response.ok()).toBe(true);return response.json();}
async function sessions(page:Page){const response=await page.request.get('/api/sessions');expect(response.ok()).toBe(true);return (await response.json()).sessions;}

test.describe.configure({mode:'serial'});
test('real signup, search, budget refinement, draft, dismissal and account isolation',async({page})=>{
 const errors:string[]=[];page.on('pageerror',error=>errors.push(error.message));
 const address=await signup(page);
 await expect(page.getByRole('heading',{name:'Идеи для первой поездки'})).toBeVisible();
 expect(await sessions(page)).toHaveLength(0);
 await choose(page,'Выходные в новом городе').click();await expect(composer(page)).not.toHaveValue('');
 expect(await sessions(page)).toHaveLength(0); // Card clicks are drafts, no job/session created.
 await send(page,'Хочу в Японию из Москвы. Нас двое взрослых, бюджет 180 тысяч рублей на всех.');
 const first=await feed(page);expect(first.history_used).toBe(true);
 expect(first.personalized.find(item=>item.id==='destination:korea')?.budget_minor).toBe(18000000);
 await send(page,'Общий бюджет теперь 120 тысяч рублей на всех.');
 await home(page);await expect(page.getByRole('heading',{name:'Для вас',exact:true})).toBeVisible();
 const updated=await feed(page),korea=updated.personalized.find(item=>item.id==='destination:korea');
 expect(korea).toBeDefined();expect(korea!.budget_minor).toBe(12000000);
 await expect(page.getByText(korea!.budget_label!,{exact:true}).first()).toBeVisible();
 await choose(page,korea!.title).click();await expect(composer(page)).toHaveValue(korea!.prompt);
 expect(await sessions(page)).toHaveLength(1);
 await mkdir('artifacts/recommendations-real',{recursive:true});
 await page.screenshot({path:'artifacts/recommendations-real/registered-personalized.png',fullPage:true});
 await choose(page,'Скрыть идею: '+korea!.title).click();await expect(choose(page,korea!.title)).toHaveCount(0);
 await page.reload();await expect(composer(page)).toBeEnabled();await expect(choose(page,korea!.title)).toHaveCount(0);
 await logout(page);await login(page,address);await expect(choose(page,korea!.title)).toHaveCount(0);
 expect((await feed(page)).personalized.some(item=>item.id===korea!.id)).toBe(false);
 await logout(page);await signup(page,'Другой пользователь');
 expect((await feed(page)).personalized).toEqual([]);expect(await sessions(page)).toEqual([]);
 await expect(page.locator('body')).not.toContainText('120 000 ₽');
 expect(errors).toEqual([]);
});

test('real searches from three accounts produce community demand for a fresh observer',async({browser})=>{
 for(let i=0;i<3;i++){
  const context=await browser.newContext();const page=await context.newPage();
  await signup(page,`Путешественник ${i}`);await send(page,'Хочу в Японию из Москвы. Бюджет 90 тысяч рублей на всех.');
  await context.close();
 }
 const context=await browser.newContext();const page=await context.newPage();await signup(page,'Новый путешественник');
 const response=await feed(page);expect(response.personalized).toEqual([]);expect(response.popular_source).toBe('community');
 expect(response.popular.some(item=>item.id==='destination:japan')).toBe(true);
 expect(response.popular.every(item=>item.source==='popular'&&item.budget_minor===null)).toBe(true);
 await expect(page.getByRole('heading',{name:'Популярное у путешественников'})).toBeVisible();
 await expect(choose(page,'Япония')).toBeVisible();
 await expect(page.getByRole('heading',{name:'Ещё идеи для поездки'})).toBeVisible();
 expect(await page.locator('.recommendation-card').count()).toBeGreaterThanOrEqual(3);
 await page.getByRole('button',{name:'Мои предпочтения'}).click();
 await page.getByLabel('Обычно отправляюсь из').fill('Казань');await choose(page,'Сохранить').click();
 await expect(page.getByText('Предпочтения сохранены')).toBeVisible();await choose(page,'Закрыть предпочтения').click();
 await choose(page,'Япония').click();
 await expect(composer(page)).toHaveValue(/Отправляюсь из Казани/);
 await page.screenshot({path:'artifacts/recommendations-real/registered-community.png',fullPage:true});
 await context.close();
});

test('real concert interest, mobile keyboard use and long cards remain usable',async({page})=>{
 await page.setViewportSize({width:320,height:740});await signup(page,'Музыка');
 await send(page,'Хочу поехать на концерт Imagine Dragons.');await home(page);
 const response=await feed(page);expect(response.personalized.some(item=>item.kind==='artist')).toBe(true);
 const music=response.personalized.find(item=>item.kind==='artist')!;
 await choose(page,music.title).focus();await page.keyboard.press('Enter');
 await expect(composer(page)).toHaveValue(music.prompt);await expect(composer(page)).toBeFocused();
 expect(await sessions(page)).toHaveLength(1);expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
 const audit=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();expect(audit.violations).toEqual([]);
 await page.screenshot({path:'artifacts/recommendations-real/registered-mobile-320.png',fullPage:true});
});

test('sending the generated card preserves the whole hotel-only family request',async({page})=>{
 await signup(page,'Семейная поездка');
 await send(page,'Нужен только отель в Нижнем Новгороде. Из Москвы, 2 взрослых, дети 5 и 7 лет. Общий бюджет 60 тысяч рублей на всех.');
 const response=await feed(page),next=response.personalized.find(item=>item.id==='destination:kazan');
 expect(next).toBeDefined();await home(page);await choose(page,next!.title).click();
 await expect(composer(page)).toHaveValue(next!.prompt);await send(page,next!.prompt);
 const all=await sessions(page);expect(all).toHaveLength(2);
 const id=await page.evaluate(()=>localStorage.getItem('odin.active-session.v1'));
 const snapshot=await (await page.request.get('/api/sessions/'+id)).json();
 expect(snapshot.session.request).toMatchObject({origin:'Москва',destination:'Казань',adults:2,children_ages:[5,7],budget_minor:6000000,budget_scope:'trip',products:['hotel']});
 await expect(page.locator('.assistant-message')).toHaveCount(1);
});
