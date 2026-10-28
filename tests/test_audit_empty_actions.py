from uuid import uuid4
import pytest
from travel_agent.domain.models import TurnInput
from travel_agent.service import AgentService
from travel_agent.response_policy import policy_decision

@pytest.mark.parametrize('intent,extras,text',[
('checkout',{},'Купи билеты и забронируй отель сейчас.'),
('select_component',{'component_type':'hotel','option_index':1},'Выбираю первый отель.'),
('question',{'question_text':'Сравни первый и второй варианты.'},'Сравни первый и второй варианты.'),
])
async def test_empty_action_offers_possible_next_step(tmp_path,intent,extras,text):
    class Gateway:
        async def interpret(self,*args,**kwargs):return {'intent':intent,'patch':{},**extras}
    service=AgentService(db_path=tmp_path/'empty.sqlite',checkout_mode='disabled',gateway=Gateway())
    try:
        result=await service.run_turn(TurnInput(request_id=str(uuid4()),user_id='u',session_id='s',input='message',payload={'text':text},expected_state_version=0,now='2026-10-28T12:00:00+03:00'))
        assert result.status=='needs_input'
        assert 'Куда хотите поехать?' in result.message
        assert result.questions==['Куда хотите поехать?']
        if intent=='checkout':assert 'недоступны' in result.message
        assert not result.current_plan and not result.options and not result.pending_action
    finally:service.close()

def test_price_guarantee_is_answered_without_needing_saved_offers():
    result=policy_decision('Можешь гарантировать, что выбранный номер останется по этой цене завтра?')
    assert result and result['intent']=='chat' and result['patch']=={}
    assert 'Не могу гарантировать' in result['reply_text']

@pytest.mark.parametrize('requirement,question',[
('Обязательно доступный вход без ступенек для человека на коляске.','Подтверждён доступный вход для коляски?'),
('Обязательно тихий номер подальше от лифта.','Подтверждено, что номер тихий и не рядом с лифтом?')])
async def test_unknown_conditions_answer_does_not_claim_options_are_ready(tmp_path,requirement,question):
    from test_component_flow import REQUEST,ScriptedGateway,ComponentAdapter,turn
    text='Нужен только отель в Казани с 15 по 20 ноября 2026, двое взрослых. '+requirement
    gateway=ScriptedGateway(REQUEST,{text:{'intent':'plan','patch':{**REQUEST,'products':['hotel'],'adults':2}},question:{'intent':'question','patch':{},'question_text':question}})
    service=AgentService(db_path=tmp_path/'required.sqlite',gateway=gateway,adapter=ComponentAdapter(),checkout_mode='disabled')
    try:
        first=await service.run_turn(turn(text=text));assert first.status=='partial'
        second=await service.run_turn(turn(first.state_version,question))
        assert second.status=='partial'
        assert not second.options and not second.hotel_options
        assert 'не подтвердил' in second.message
    finally:service.close()
