import pytest
from travel_agent.concert_intake import ConcertIntakeState, update_intake
from travel_agent.interpretation import RuleBasedGateway
from test_concert_intake import recording_service, turn, GENERIC

@pytest.mark.parametrize('text',['Не надо, отмена.','Отмена','Стоп','Передумал.','Не надо'])
async def test_cancel_concert_stops_without_recommending_new_trip(tmp_path,text):
    class Gateway(RuleBasedGateway):
        async def interpret(self,text,request,*args,**kwargs):
            return {'intent':'plan','patch':{},'concert_intake':{'mode':'cancel'}} if text!=GENERIC else {'intent':'plan','patch':{}}
    service,search,travel=recording_service(tmp_path,Gateway())
    try:
        first=await service.run_turn(turn(GENERIC))
        result=await service.run_turn(turn(text,first.state_version))
        assert result.response_kind=='chat'
        assert 'останов' in result.message.lower()
        assert not result.suggestions
        assert (await service.get_session('u','intake')).concert_intake.mode=='inactive'
        search.search.assert_not_called();travel.search.assert_not_called()
    finally:service.close()

@pytest.mark.parametrize('text',[
'Хочу попасть на концерт Coldplay 12 июня 2027 в Лондоне. Сначала проверь, есть ли он.',
'Хочу на концерт Coldplay. Сначала проверь расписание.',
])
async def test_verify_event_first_does_not_demand_trip_budget_or_party(tmp_path,text):
    class Gateway(RuleBasedGateway):
        async def interpret(self,text,request,*args,**kwargs):
            return {'intent':'plan','patch':{},'concert_intake':{'mode':'trip','event_name':'Coldplay'}}
    service,search,travel=recording_service(tmp_path,Gateway())
    try:
        result=await service.run_turn(turn(text))
        assert search.search.await_count==1
        assert result.status=='partial'
        assert (await service.get_session('u','intake')).concert_intake.mode=='information'
        travel.search.assert_not_called()
    finally:service.close()

async def test_concert_cancel_does_not_depend_on_model_availability(tmp_path):
    class Gateway(RuleBasedGateway):
        async def interpret(self,text,request,*args,**kwargs):
            if text!=GENERIC:raise AssertionError('Cancellation must not call the model')
            return {'intent':'plan','patch':{}}
    service,search,travel=recording_service(tmp_path,Gateway())
    try:
        first=await service.run_turn(turn(GENERIC))
        second=await service.run_turn(turn('Не надо, отмена.',first.state_version))
        assert second.response_kind=='chat' and 'остановлю' in second.message
        assert (await service.get_session('u','intake')).concert_intake.mode=='inactive'
    finally:service.close()

def test_punctuation_is_not_a_concert_cancellation():
    from travel_agent.concert_intake import cancels_intake
    assert not cancels_intake('...')
