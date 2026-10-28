import pytest
from travel_agent.domain.models import SessionSnapshot
from travel_agent.graph.suggestions import build_suggestions, suggest_next_messages

@pytest.mark.parametrize('destination,forbidden', [('Казань','Казани'),('Москва','Москвы'),('Санкт-Петербург','Петербург')])
def test_origin_suggestion_does_not_propose_destination(destination,forbidden):
    session=SessionSnapshot(user_id='audit',session_id='route',data_mode='live',checkout_mode='disabled',request={'destination':destination})
    result={'status':'needs_input','message':'Из какого города поедете?','questions':['Из какого города поедете?']}
    suggestions=build_suggestions(session,result)
    assert suggestions
    assert all(forbidden.casefold() not in item['text'].casefold() for item in suggestions)

@pytest.mark.parametrize('kind,trip_request,text',[
('origin',{'destination':'Казань'},'Поеду из Казани.'),
('origin',{'destination':'Москва'},'Поеду из Москвы.'),
('origin',{'destination':'Санкт-Петербург'},'Поеду из Санкт-Петербурга.'),
('destination',{'origin':'Казань'},'Хочу поехать в Казань.'),
])
async def test_model_same_city_suggestion_is_rejected(kind,trip_request,text):
    class Gateway:
        async def suggest(self,context):
            return [{'id':'same-city','label':'Этот город','text':text}]
    session=SessionSnapshot(user_id='audit',session_id='route',data_mode='live',checkout_mode='disabled',request=trip_request)
    question='Из какого города поедете?' if kind=='origin' else 'Куда хотите поехать?'
    result=await suggest_next_messages({'session':session.model_dump(mode='json'),'turn':{'input':'message','payload':{'text':'Хочу поехать'},'now':'2026-10-28T12:00:00+03:00'},'result':{'status':'needs_input','message':question,'questions':[question]}},gateway=Gateway())
    assert all(item['id']!='same-city' for item in result['result']['suggestions'])

async def test_already_selected_hotel_is_not_suggested_as_a_new_choice():
    from test_suggestion_components import card, state, generated
    first=card('hotel',1,title='Отель у озера')
    second=card('hotel',2,title='Отель у парка')
    saved=state(['hotel'],hotels=[first,second],selected=[first])
    assert await generated(saved,['Выбираю отель 1.','Выбираю отель 2.']) == ['Выбираю отель 2.']

@pytest.mark.parametrize('label',['Назвать даты','Выбрать дату вылета','Указать дату','Уточнить даты поездки'])
async def test_generic_date_label_cannot_hide_a_specific_calendar_choice(label):
    class Gateway:
        async def suggest(self,context):
            return [{'id':'hidden-date','label':label,'text':'Вылетаю 15 ноября 2026 года.'}]
    snapshot=SessionSnapshot(user_id='audit',session_id='date',data_mode='live',checkout_mode='disabled',request={'origin':'Москва','destination':'Казань'})
    result=await suggest_next_messages({'session':snapshot.model_dump(mode='json'),'turn':{'input':'message','payload':{'text':'Хочу в Казань'},'now':'2026-10-28T12:00:00+03:00'},'result':{'status':'needs_input','message':'Когда хотите вылететь?','questions':['Когда хотите вылететь?']}},gateway=Gateway())
    assert all(item['id']!='hidden-date' for item in result['result']['suggestions'])

@pytest.mark.parametrize('question,label,text,visible',[
('Когда хотите вылететь?','В ноябре','Вылетаю 1 ноября 2026 года.','1 ноября'),
('На какие даты нужен отель?','На неделю','Заезд 1 ноября 2026, выезд 8 ноября 2026.','8 ноября'),
('Из какого города поедете?','Из другого города','Из Екатеринбурга.','Екатеринбурга'),
])
async def test_concrete_values_are_visible_on_the_suggestion(question,label,text,visible):
    class Gateway:
        async def suggest(self,context):return [{'id':'specific','label':label,'text':text}]
    snapshot=SessionSnapshot(user_id='audit',session_id='clear',data_mode='live',checkout_mode='disabled',request={'destination':'Казань'})
    result=await suggest_next_messages({'session':snapshot.model_dump(mode='json'),'turn':{'input':'message','payload':{'text':'Планируем'},'now':'2026-10-28T12:00:00+03:00'},'result':{'status':'needs_input','message':question,'questions':[question]}},gateway=Gateway())
    hints=result['result']['suggestions']
    assert any(hint['id']=='specific' and visible in hint['label'] for hint in hints)

@pytest.mark.parametrize('text',['Снимем требование тихого номера.','Подбери отель без доступного входа.','Мне подойдёт номер рядом с лифтом.'])
async def test_missing_confirmation_does_not_suggest_removing_mandatory_room_requirements(text):
    from test_suggestion_components import state,generated
    saved=state(['hotel']);saved['session']['request']['constraints']={'wheelchair_accessible':True,'quiet_room':True,'away_from_elevator':True}
    assert text not in await generated(saved,[text])

async def test_generic_choose_hotel_chip_discloses_the_number_it_selects():
    from test_suggestion_components import card,state
    class Gateway:
        async def suggest(self,context):return [{'id':'hotel','label':'Выбрать проживание','text':'Выбираю отель 1: Отель у озера.'}]
    saved=state(['hotel'],hotels=[card('hotel',1,title='Отель у озера')])
    output=await suggest_next_messages(saved,gateway=Gateway())
    assert 'отель 1' in output['result']['suggestions'][0]['label'].lower()

def test_destination_question_is_not_confused_by_origin_in_explanation():
    from travel_agent.graph.suggestions import _question_kind
    assert _question_kind('Город отправления и назначения совпадают. В какой другой город хотите поехать?')=='destination'

async def test_transport_alternative_question_does_not_suggest_cities():
    from test_suggestion_components import state,generated
    saved=state(['bus'],question='Можно ли рассмотреть другой транспорт?')
    assert await generated(saved,['Поедем в Сочи.','Рассмотрим самолёт.'])==['Рассмотрим самолёт.']

async def test_multicity_clarification_does_not_suggest_the_explicit_origin():
    class Gateway:
        async def suggest(self,context):return [{'id':'origin','label':'Москва','text':'Хочу поехать в Москву.'},{'id':'kazan','label':'Казань','text':'Хочу поехать в Казань.'}]
    snapshot=SessionSnapshot(user_id='audit',session_id='multi',data_mode='live',checkout_mode='disabled')
    result=await suggest_next_messages({'session':snapshot.model_dump(mode='json'),'turn':{'input':'message','payload':{'text':'Хочу Москва — Казань — Сочи — Москва, по три дня в каждом городе.'}},'result':{'status':'needs_input','message':'Пока поддерживается один город назначения. Какой город выбрать?'}},gateway=Gateway())
    assert [x['id'] for x in result['result']['suggestions']]==['kazan']

def test_workaround_chip_discloses_date_even_without_a_date_question():
    from travel_agent.domain.models import Suggestion
    from travel_agent.graph.suggestions import _transparent_label
    chip=Suggestion(id='later',label='Вернуться позже',text='Вернусь 17 ноября 2026 года.')
    assert '17 ноября' in _transparent_label(chip,None).label

@pytest.mark.parametrize("title", ["Автобус · Амельченко А.С.", "Автобус · Поехали!", "Автобус · Регион"])
def test_choice_draft_does_not_duplicate_title_punctuation(title):
    from test_suggestion_components import card,state
    saved=state(["bus"],transport=[card("bus",title=title)])
    hints=build_suggestions(SessionSnapshot.model_validate(saved["session"]),saved["result"])
    draft=hints[0]["text"]
    assert ".." not in draft and "!." not in draft
    assert title in draft
