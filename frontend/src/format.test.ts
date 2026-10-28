import {describe,it,expect} from 'vitest';
import {clockTime,deadline,duration,eventTitle,localTime,money,nights,publicText,sameComponent,selectedLabel,variantsLabel,seatCategory} from './format';
import type {Component} from './types';
describe('truthful travel presentation',()=>{
 it('formats integer minor units including cents without multiplying the price',()=>{expect(money(6792000)).toContain('67 920');expect(money(716924)).toContain('7 169,24');expect(money(undefined)).toBe('Цена не указана');});
 it('preserves local departure and arrival clock times',()=>{expect(localTime('2026-11-24T16:20:00+04:00')).toBe('16:20');expect(duration('2026-11-24T11:35:00+03:00','2026-11-24T16:20:00+04:00')).toBe('3 ч 45 мин');});
 it('does not label a UTC deadline as destination local time',()=>{expect(deadline('2026-11-21T09:30:00Z')).toContain('12:30 МСК');});
 it.each([[1,'1 ночь'],[2,'2 ночи'],[7,'7 ночей'],[11,'11 ночей'],[21,'21 ночь']])('formats %s nights', (count,expected)=>{expect(nights('2026-11-01',`2026-11-${String(Number(count)+1).padStart(2,'0')}`)).toBe(expected);});
 it('does not invent a duration when times are missing',()=>{expect(duration()).toBe('');expect(nights()).toBeNull();});
 it('recognizes retained components across new option ids',()=>{const base={component_id:'old',provider_refs:{reference_key:'same'}} as Component;expect(sameComponent(base,{...base,component_id:'new'})).toBe(true);expect(sameComponent(base,{component_id:'new'} as Component)).toBe(false);});
 it('does not expose legacy supplier brands',()=>{for(const value of ['Туту','Tutu.ru','Яндекса','Yandex Search API'])expect(publicText(value)).not.toMatch(/туту|tutu|яндекс|yandex/i);});
 it('labels a complete hotel-only or bus-only plan accurately',()=>{expect(selectedLabel([{product_type:'hotel'} as Component],true)).toBe('Выбранное проживание');expect(selectedLabel([{product_type:'bus'} as Component],true)).toBe('Выбранная дорога');expect(selectedLabel([{product_type:'hotel'} as Component],false)).toBe('Выбранная часть поездки');});
 it('uses natural Russian variant counts',()=>{expect(variantsLabel(1)).toBe('1 вариант');expect(variantsLabel(3)).toBe('3 варианта');expect(variantsLabel(6)).toBe('6 вариантов');expect(variantsLabel(11)).toBe('11 вариантов');});
 it('never renders unknown seat category codes',()=>{expect(seatCategory('COMPARTMENT')).toBe('Купе');expect(seatCategory('NEW_CODE')).toBe('Категория места уточняется');});
 it('makes a saved generic event title meaningful using its actual artist',()=>{expect(eventTitle({title:'Концерт',artist:'Алекс Лим'})).toBe('Алекс Лим · концерт');expect(eventTitle({title:'',artist:'Мураками'})).toBe('Мураками · концерт');});
 it('preserves a meaningful event title and never invents a missing artist',()=>{expect(eventTitle({title:'Мураками — новый тур',artist:'Мураками'})).toBe('Мураками — новый тур');expect(eventTitle({title:'Концерт',artist:''})).toBe('Концерт');expect(eventTitle({title:'Концерт артиста',artist:'Артист'})).toBe('Концерт артиста');});
});

it('shows hotel clocks without seconds and does not invent invalid hours',()=>{expect(clockTime('14:00:00')).toBe('14:00');expect(clockTime('09:30')).toBe('09:30');expect(clockTime('25:70:00')).toBeNull();expect(clockTime()).toBeNull();});
