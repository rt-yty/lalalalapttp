'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const SESSION_KEY = 'odin_trip_session';
  const state = {config:null, sessionId:null, version:0, session:null, result:null, messages:[], busy:false, ready:false, restoring:false, searchSaving:false, voiceActive:false, retry:null, lastSequence:0};
  let voice=null;
  let replyReader=null;
  const emptyTrip = $('trip-content').cloneNode(true);
  const money = (minor) => new Intl.NumberFormat('ru-RU', {style:'currency', currency:'RUB', maximumFractionDigits:minor % 100 ? 2 : 0}).format(minor / 100);
  const uuid = () => crypto.randomUUID();
  const dateLabel = (value, options = {day:'numeric',month:'short'}) => {
    if (!value) return 'даты уточняются';
    const date = new Date(value.length === 10 ? value + 'T12:00:00' : value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString('ru-RU', options);
  };
  const el = (tag, cls, text) => {const node=document.createElement(tag); if(cls)node.className=cls; if(text!==undefined)node.textContent=String(text); return node;};
  const button = (text, cls, action) => {const node=el('button', 'button '+(cls||''), text);node.type='button';node.dataset.actionButton='true';node.disabled=state.busy||state.voiceActive||!state.ready;node.addEventListener('click',action);return node;};
  const sleep = (ms) => new Promise(resolve=>setTimeout(resolve,ms));

  async function api(path, options={}) {
    let response;
    try {response=await fetch(path, {cache:'no-store',...options});}
    catch (_) {throw new Error('Не удалось связаться с приложением. Проверьте, что окно запуска остаётся открытым.');}
    let body;
    try {body=await response.json();} catch (_) {throw new Error('Приложение вернуло неожиданный ответ. Попробуйте ещё раз.');}
    if(!response.ok){const detail=typeof body.detail==='string'?body.detail:typeof body.error==='string'?body.error:null;const error=new Error(detail||'Не удалось выполнить запрос. Попробуйте ещё раз.');error.httpStatus=response.status;throw error;}
    return body;
  }
  function showError(message, retry){$('error-text').textContent=message;$('connection-error').hidden=false;state.retry=retry;$('retry').hidden=!retry;}
  function clearError(){$('connection-error').hidden=true;state.retry=null;}
  function updateControls(){
    const disabled=state.busy||state.voiceActive||!state.ready;
    $('new-trip').disabled=disabled;$('try-example').disabled=disabled;$('message-input').disabled=disabled;
    $('send-button').disabled=disabled||!$('message-input').value.trim();
    document.querySelectorAll('[data-action-button]').forEach(node=>{node.disabled=disabled||node.dataset.blocked==='true';});
    $('progress').hidden=!state.busy;
    $('agent-status').replaceChildren(el('span','status-dot'),document.createTextNode(!state.ready?'Подключаемся…':state.busy?'Работаю над вашей поездкой':'Готов помочь'));
    renderSuggestions();
    updateSearchControls();
    $('retry').disabled=state.voiceActive;
    voice?.update();
    replyReader?.update();
  }
  function setBusy(value){state.busy=value;updateControls();if(value)scrollChat();}
  function scrollChat(){requestAnimationFrame(()=>{$('chat-scroll').scrollTop=$('chat-scroll').scrollHeight;});}
  function renderMessages(){
    $('welcome').hidden=state.messages.length>0;
    $('messages').replaceChildren();
    state.messages.forEach((message,index)=>{
      const article=el('article','message message-'+(message.role==='user'?'user':'assistant'));
      article.append(el('div','message-label',message.role==='user'?'Вы':'О!дин запрос'),el('p','message-content',message.text));
      if(message.role==='assistant'){
        if(index===state.messages.length-1)article.dataset.latestAssistant='true';
      }
      $('messages').append(article);
    });
    renderSuggestions();
    renderSearchConnection();
    scrollChat();
  }
  function renderSuggestions(){
    document.getElementById('suggestions')?.remove();
    const result=state.result;
    const latest=state.messages[state.messages.length-1];
    if(!state.ready||state.busy||state.voiceActive||state.restoring||!result||result.state_version!==state.version||latest?.role!=='assistant'||latest.text!==result.message)return;
    const host=document.querySelector('[data-latest-assistant]');
    if(!host||!Array.isArray(result.suggestions))return;
    const seen=new Set();
    const suggestions=result.suggestions.filter(item=>{
      if(!item||typeof item.id!=='string'||!item.id.trim()||seen.has(item.id)||typeof item.label!=='string'||!item.label.trim()||typeof item.text!=='string'||!item.text.trim()||item.text.length>12000)return false;
      seen.add(item.id);return true;
    }).slice(0,3);
    if(!suggestions.length)return;
    const group=el('div','suggestions message-suggestions');group.id='suggestions';group.setAttribute('role','group');group.setAttribute('aria-label','Что можно написать дальше');
    const hint=el('p','suggestions-hint',result.status==='needs_input'?'Варианты ответа':'Можно продолжить так');hint.id='suggestions-hint';group.append(hint);
    for(const suggestion of suggestions){
      const chip=el('button','suggestion-chip',suggestion.label);chip.type='button';chip.dataset.suggestionId=suggestion.id;chip.setAttribute('aria-describedby','suggestions-hint');chip.title='Подставить сообщение в поле ввода';
      chip.addEventListener('click',()=>{
        if(state.busy||state.voiceActive||state.restoring||!state.ready||state.result!==result||state.version!==result.state_version)return;
        const input=$('message-input');input.value=suggestion.text;input.focus();const placeholder=input.value.indexOf('…');input.setSelectionRange(placeholder<0?input.value.length:placeholder,placeholder<0?input.value.length:placeholder+1);input.dispatchEvent(new Event('input',{bubbles:true}));
      });
      group.append(chip);
    }
    host.append(group);
  }
  function updateSearchControls(){
    const disabled=state.busy||state.voiceActive||!state.ready||state.searchSaving;
    for(const id of ['search-settings-toggle','search-config-submit','search-config-cancel','search-api-key','search-folder-id','search-repeat'])$(id).disabled=disabled;
    $('search-config-submit').textContent=state.searchSaving?'Подключаем…':'Подключить';
  }
  function renderSearchConnection(){
    const configured=state.config?.web_search?.configured===true;
    $('search-connection-status').textContent=configured?'Ключ добавлен. Доступ проверяется при поиске.':'Яндекс не настроен';
    $('search-connection-status').classList.remove('is-connected');
    $('search-settings-toggle').textContent=configured?'Настройки':'Подключить Яндекс';
    $('search-repeat').hidden=!configured||!state.result?.web_search?.error;
    updateSearchControls();
  }
  function openSearchSettings(open){
    if(state.busy||state.voiceActive||!state.ready||state.searchSaving)return;
    $('search-settings').hidden=!open;$('search-settings-toggle').setAttribute('aria-expanded',String(open));$('search-config-error').hidden=true;
    if(open){$('search-settings').scrollIntoView({behavior:'smooth',block:'nearest'});$('search-api-key').focus();}
    else{$('search-api-key').value='';$('search-folder-id').value='';$('search-settings-toggle').focus();}
  }
  async function configureSearch(event){
    event.preventDefault();if(state.busy||state.voiceActive||!state.ready||state.searchSaving)return;
    if(!$('search-settings').reportValidity())return;
    state.searchSaving=true;updateSearchControls();$('search-config-error').hidden=true;
    try{
      const payload={api_key:$('search-api-key').value.trim(),folder_id:$('search-folder-id').value.trim()};
      const submit=()=>api('/api/search/config',{method:'POST',headers:{'Content-Type':'application/json','X-Travel-Token':state.config.csrf_token},body:JSON.stringify(payload)});
      let result;
      try{result=await submit();}catch(error){if(error.httpStatus!==403)throw error;state.config=await api('/api/bootstrap');result=await submit();}
      if(result.configured!==true)throw new Error('Search configuration was not accepted');
      state.config.web_search={provider:'yandex',configured:true};
      $('search-api-key').value='';$('search-folder-id').value='';$('search-settings').hidden=true;$('search-settings-toggle').setAttribute('aria-expanded','false');
      renderMessages();
    }catch(_){$('search-config-error').textContent='Не удалось подключить Яндекс. Проверьте ключ API и ID каталога, затем повторите.';$('search-config-error').hidden=false;}
    finally{state.searchSaving=false;updateSearchControls();if($('search-settings').hidden)$('search-settings-toggle').focus();}
  }
  function createVoiceController(){
    let current=null;
    let feedback=null;
    const supported=!!(navigator.mediaDevices?.getUserMedia&&window.MediaRecorder);
    const allowed=()=>state.config?.voice?.available===true&&supported;
    const maxSeconds=()=>Math.min(60,Math.max(1,Number(state.config?.voice?.max_seconds)||60));
    const maxBytes=()=>Math.min(8388608,Math.max(1,Number(state.config?.voice?.max_bytes)||8388608));
    const isCurrent=operation=>current===operation&&operation.sessionId===state.sessionId;
    const stopTracks=stream=>stream?.getTracks().forEach(track=>track.stop());
    function clearTimers(operation){clearInterval(operation.ticker);clearTimeout(operation.stopTimer);clearTimeout(operation.requestTimer);}
    function cleanup(operation){
      clearTimers(operation);operation.controller?.abort();
      if(operation.recorder){operation.recorder.ondataavailable=null;operation.recorder.onstop=null;operation.recorder.onerror=null;if(operation.recorder.state!=='inactive'){try{operation.recorder.stop();}catch(_){}}}
      stopTracks(operation.stream);operation.chunks=[];
    }
    function updateTimer(){
      if(!current||current.phase!=='recording')return;
      const seconds=Math.min(maxSeconds(),Math.floor((Date.now()-current.startedAt)/1000));
      $('voice-timer').textContent=Math.floor(seconds/60)+':'+String(seconds%60).padStart(2,'0')+' / '+Math.floor(maxSeconds()/60)+':'+String(maxSeconds()%60).padStart(2,'0');
    }
    function update(){
      const phase=current?.phase;
      const recording=phase==='recording';
      const busy=!!current;
      const disabled=phase==='requesting'||phase==='transcribing'||(!recording&&(!state.ready||state.busy||state.searchSaving||state.restoring||!allowed()));
      $('voice-button').disabled=disabled;
      $('voice-button').classList.toggle('is-recording',recording);
      $('voice-button').setAttribute('aria-pressed',String(recording));
      const label=recording?'Остановить запись и распознать речь':'Записать голосовое сообщение';
      $('voice-button').setAttribute('aria-label',label);$('voice-button').title=label;
      $('voice-mic-icon').hidden=recording;$('voice-stop-icon').hidden=!recording;
      $('voice-cancel').hidden=!busy;$('voice-cancel').disabled=false;
      $('voice-timer').hidden=!recording;updateTimer();
      const message=phase==='requesting'?'Разрешите доступ к микрофону в браузере.':recording?'Слушаю. Нажмите ещё раз, когда закончите.':phase==='transcribing'?'Распознаю речь на этом компьютере…':feedback?.text;
      $('voice-status').hidden=!message;
      $('voice-status').classList.toggle('is-recording',recording);
      $('voice-status').classList.toggle('is-error',feedback?.kind==='error'&&!busy);
      $('voice-status-text').textContent=message||'';
      $('voice-hint').textContent=!state.config?'Проверяем доступность голосового ввода…':!supported?'Этот браузер не поддерживает запись. Можно продолжить текстом.':state.config.voice?.available!==true?(state.config.voice?.message||'Голосовой ввод пока недоступен. Можно продолжить текстом.'):'Голос распознаётся на этом компьютере · до '+maxSeconds()+' с';
    }
    function finish(operation,kind,text){
      if(current!==operation)return;
      current=null;state.voiceActive=false;cleanup(operation);feedback=text?{kind,text}:null;updateControls();
    }
    function cancel(){
      if(!current)return;
      const operation=current;finish(operation,'info','Запись отменена. Черновик сохранён.');$('message-input').focus();
    }
    function stopRecording(operation=current){
      if(!operation||!isCurrent(operation)||operation.phase!=='recording')return;
      operation.phase='transcribing';clearInterval(operation.ticker);clearTimeout(operation.stopTimer);update();
      try{operation.recorder.stop();stopTracks(operation.stream);}catch(_){finish(operation,'error','Не удалось завершить запись. Попробуйте ещё раз.');}
    }
    async function postAudio(operation,blob){
      const response=await fetch('/api/voice/transcribe',{method:'POST',cache:'no-store',headers:{'Content-Type':blob.type,'X-Travel-Token':state.config.csrf_token},body:blob,signal:operation.controller.signal});
      let body;try{body=await response.json();}catch(_){throw new Error('Не удалось прочитать результат распознавания. Попробуйте ещё раз.');}
      if(!response.ok){const error=new Error(typeof body.detail==='string'?body.detail.slice(0,500):'Не удалось распознать запись. Попробуйте ещё раз.');error.httpStatus=response.status;throw error;}
      return body;
    }
    async function transcribe(operation){
      if(!isCurrent(operation))return;
      operation.phase='transcribing';clearInterval(operation.ticker);clearTimeout(operation.stopTimer);stopTracks(operation.stream);update();
      const mime=operation.recorder.mimeType||operation.chunks.find(chunk=>chunk.type)?.type||'';
      const blob=new Blob(operation.chunks,{type:mime});operation.chunks=[];
      if(!blob.size){finish(operation,'error','Запись пустая. Попробуйте ещё раз и произнесите несколько слов.');return;}
      if(blob.size>maxBytes()){finish(operation,'error','Запись слишком большая. Попробуйте более короткое сообщение.');return;}
      if(!blob.type){finish(operation,'error','Не удалось определить формат записи. Попробуйте другой браузер.');return;}
      operation.controller=new AbortController();
      operation.requestTimer=setTimeout(()=>{operation.timedOut=true;operation.controller.abort();},120000);
      try{
        let result;
        try{result=await postAudio(operation,blob);}catch(error){
          if(error.httpStatus!==403||!isCurrent(operation))throw error;
          state.config=await api('/api/bootstrap',{signal:operation.controller.signal});
          if(!isCurrent(operation))return;
          result=await postAudio(operation,blob);
        }
        if(!isCurrent(operation))return;
        const text=typeof result.text==='string'?result.text.trim():'';
        if(!text)throw new Error('Не удалось разобрать речь. Попробуйте говорить чуть ближе к микрофону.');
        const input=$('message-input');
        const draft=input.value+(input.value&&!/\s$/.test(input.value)?'\n':'')+text;
        if(draft.length>(input.maxLength>0?input.maxLength:12000))throw new Error('В сообщении не осталось места для записи. Сократите черновик и попробуйте ещё раз.');
        input.value=draft;
        finish(operation,'success','Текст добавлен. Проверьте его и отправьте.');
        input.dispatchEvent(new Event('input',{bubbles:true}));input.focus();input.setSelectionRange(input.value.length,input.value.length);
      }catch(error){
        if(!isCurrent(operation))return;
        finish(operation,'error',operation.timedOut?'Распознавание заняло слишком много времени. Черновик сохранён; попробуйте более короткую запись.':error instanceof TypeError?'Не удалось связаться с приложением. Черновик сохранён.':error.message||'Не удалось распознать запись. Черновик сохранён.');
      }
    }
    async function startRecording(){
      if(current||state.busy||!state.ready||state.searchSaving||state.restoring||!allowed())return;
      replyReader?.cancel();
      const operation={sessionId:state.sessionId,phase:'requesting',chunks:[],bytes:0,stream:null,recorder:null,controller:null};
      current=operation;feedback=null;state.voiceActive=true;updateControls();
      try{
        const stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true},video:false});
        if(!isCurrent(operation)){stopTracks(stream);return;}
        operation.stream=stream;
        const candidates=['audio/webm;codecs=opus','audio/webm','audio/mp4;codecs=mp4a.40.2','audio/mp4','audio/ogg;codecs=opus'];
        const mime=candidates.find(value=>typeof MediaRecorder.isTypeSupported==='function'&&MediaRecorder.isTypeSupported(value));
        const recorder=new MediaRecorder(stream,mime?{mimeType:mime}:undefined);operation.recorder=recorder;
        recorder.ondataavailable=event=>{
          if(!isCurrent(operation)||!event.data?.size)return;
          operation.chunks.push(event.data);operation.bytes+=event.data.size;
          if(operation.bytes>maxBytes())finish(operation,'error','Запись слишком большая. Попробуйте более короткое сообщение.');
        };
        recorder.onerror=()=>{if(isCurrent(operation))finish(operation,'error','Запись прервалась. Проверьте микрофон и попробуйте ещё раз.');};
        recorder.onstop=()=>transcribe(operation);
        stream.getTracks().forEach(track=>track.addEventListener('ended',()=>stopRecording(operation)));
        recorder.start(250);operation.startedAt=Date.now();operation.phase='recording';
        operation.ticker=setInterval(updateTimer,250);operation.stopTimer=setTimeout(()=>stopRecording(operation),maxSeconds()*1000);
        update();$('voice-button').focus();
      }catch(error){
        if(!isCurrent(operation))return;
        const text=error.name==='NotAllowedError'?'Микрофон недоступен. Разрешите доступ в настройках браузера или продолжите текстом.':error.name==='NotFoundError'?'Микрофон не найден. Подключите его или продолжите текстом.':error.name==='NotReadableError'?'Не удалось включить микрофон. Проверьте, не занят ли он другим приложением.':'Не удалось начать запись. Попробуйте ещё раз или продолжите текстом.';
        finish(operation,'error',text);
      }
    }
    $('voice-button').addEventListener('click',()=>{if(current?.phase==='recording')stopRecording();else startRecording();});
    $('voice-cancel').addEventListener('click',cancel);
    document.addEventListener('keydown',event=>{if(event.key==='Escape'&&current){event.preventDefault();cancel();}});
    window.addEventListener('pagehide',()=>{if(current){const operation=current;current=null;state.voiceActive=false;cleanup(operation);}});
    return {update,reset(){if(current)cancel();feedback=null;update();}};
  }
  function createReplyReader(){
    const synth=window.speechSynthesis;
    const supported=!!(synth&&typeof synth.getVoices==='function'&&typeof synth.speak==='function'&&window.SpeechSynthesisUtterance);
    const spoken=new Set();
    let localVoice=null;
    const cancel=()=>{if(supported)synth.cancel();};
    function update(){
      localVoice=supported?synth.getVoices().find(item=>item.localService===true&&/^ru(?:[-_]|$)/i.test(item.lang)):null;
      $('read-aloud-toggle').disabled=!localVoice;
      if(!localVoice&&$('read-aloud-toggle').checked){$('read-aloud-toggle').checked=false;cancel();}
      $('read-aloud-note').textContent=localVoice?'Локальный русский голос':supported?'Нет локального русского голоса':'Озвучивание недоступно в этом браузере';
    }
    function speak(text,id){
      update();
      if(!$('read-aloud-toggle').checked||!localVoice||state.voiceActive||spoken.has(id)||typeof text!=='string'||!text.trim())return;
      spoken.add(id);cancel();
      const utterance=new SpeechSynthesisUtterance(text);utterance.lang=localVoice.lang;utterance.voice=localVoice;utterance.rate=1;
      utterance.onerror=event=>{if(!['canceled','interrupted'].includes(event.error))$('read-aloud-note').textContent='Не удалось озвучить ответ. Текст доступен в чате.';};
      try{synth.speak(utterance);}catch(_){$('read-aloud-note').textContent='Не удалось озвучить ответ. Текст доступен в чате.';}
    }
    $('read-aloud-toggle').addEventListener('change',()=>{if(!$('read-aloud-toggle').checked)cancel();});
    if(supported)synth.addEventListener?.('voiceschanged',update);
    window.addEventListener('pagehide',cancel);
    update();
    return {update,speak,cancel};
  }
  const suitableOptions = (result) => (result?.options || []).filter(option => option.eligibility === 'satisfied');
  function componentState(result=state.result){
    const items=(key)=>Array.isArray(result?.[key])?result[key]:Array.isArray(state.session?.[key])?state.session[key]:[];
    return {transport:items('transport_options'),hotels:items('hotel_options'),selected:items('selected_components')};
  }
  const completePlan=(plan)=>!!plan&&plan.complete!==false&&!(plan.missing_products||[]).length;
  function updateSteps(){
    const parts=componentState();
    const current=state.result?.demo_order||completePlan(state.result?.current_plan)?3:parts.transport.length||parts.hotels.length||parts.selected.length||suitableOptions(state.result).length?2:1;
    document.querySelectorAll('[data-step]').forEach(node=>{const step=Number(node.dataset.step);node.classList.toggle('is-current',step===current);node.classList.toggle('is-done',step<current);});
  }
  function buildMeta(request){
    const meta=el('div','session-meta');
    if(request.origin&&request.destination)meta.append(el('span','',request.origin+' → '+request.destination));
    else if(request.destination)meta.append(el('span','',request.destination));
    if(request.departure_date&&request.return_date)meta.append(el('span','',dateLabel(request.departure_date)+' — '+dateLabel(request.return_date)));
    if(request.adults)meta.append(el('span','',request.adults+' взр.'+(request.children_ages?.length?' · детей: '+request.children_ages.length:'')));
    return meta;
  }
  function flightLocalTime(value){
    // Preserve the airport-local clock supplied with its offset, rather than
    // converting both ends of the journey to the browser's time zone.
    const match=typeof value==='string'&&value.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
    if(!match)return null;
    const date=new Date(Date.UTC(Number(match[1]),Number(match[2])-1,Number(match[3])));
    return date.toLocaleDateString('ru-RU',{day:'numeric',month:'short',timeZone:'UTC'})+', '+match[4]+':'+match[5];
  }
  function flightSchedule(label,departure,arrival){
    const from=flightLocalTime(departure),to=flightLocalTime(arrival);
    if(!from&&!to)return null;
    return label+': '+(from||'отправление уточняется')+' → '+(to||'прибытие уточняется');
  }
  const productLabels={avia:'Перелёт',rail:'Поезд',hotel:'Отель'};
  function sameComponent(left,right){
    if(left.product_type!==right.product_type)return false;
    if(left.component_id&&right.component_id&&left.component_id===right.component_id)return true;
    const reference=left.provider_refs?.reference_key;
    return !!reference&&reference===right.provider_refs?.reference_key;
  }
  function durationLabel(departure,arrival){
    const minutes=Math.round((Date.parse(arrival)-Date.parse(departure))/60000);
    if(!Number.isFinite(minutes)||minutes<=0)return null;
    return Math.floor(minutes/60)+' ч'+(minutes%60?' '+minutes%60+' мин':'');
  }
  function componentCard(item,index,{selected=false,removable=false,unverified=false}={}){
    const kind=item.product_type,hotel=kind==='hotel',conditions=item.conditions||{},query=item.query||{};
    const card=el('article','offer-card component-card'+(selected?' is-selected':''));
    card.dataset.productType=kind;
    if(item.component_id)card.dataset.componentId=item.component_id;
    const top=el('div','offer-top');
    top.append(el('span','offer-kicker',selected?'ВАШ ВЫБОР':(productLabels[kind]||'Вариант').toUpperCase()+' '+index));
    if(selected)top.append(el('span','eligibility '+(unverified?'unknown':'satisfied'),unverified?'Нужно обновить':'Выбрано ✓'));
    card.append(top,el('h3','',item.title||productLabels[kind]||'Вариант'));
    if(hotel){
      if(conditions.room_name)card.append(el('p','hotel-subtitle',conditions.room_name));
      if(query.departure_date&&query.return_date)card.append(el('p','hotel-subtitle','Заезд '+dateLabel(query.departure_date)+' · выезд '+dateLabel(query.return_date)));
    }else{
      const route=[query.origin,query.destination].filter(Boolean).join(' → ');
      if(route)card.append(el('p','hotel-subtitle',route+' · туда и обратно'));
      const schedule=el('div','component-schedule');
      for(const [label,departure,arrival] of [['Туда',item.departure_at,item.arrival_at],['Обратно',item.return_departure_at,item.return_arrival_at]]){
        const timing=flightSchedule(label,departure,arrival);
        if(timing)schedule.append(el('p','flight-schedule',timing));
        const duration=durationLabel(departure,arrival);
        if(duration)schedule.append(el('span','flight-time-note','В пути '+duration));
      }
      if(schedule.children.length){schedule.append(el('p','flight-time-note',kind==='rail'?'Время отправления и прибытия из расписания':'Местное время аэропортов'));card.append(schedule);}
    }
    const features=el('div','offer-features');
    const feature=(text)=>features.append(el('span','feature-pill',text));
    if(hotel){
      if(conditions.stars)feature('★'.repeat(Math.min(5,Math.max(0,Number(conditions.stars)))));
      if(conditions.rating!==null&&conditions.rating!==undefined)feature('Оценка '+conditions.rating);
      if(conditions.breakfast===true)feature('Завтрак включён');
      else if(conditions.breakfast===false)feature('Без завтрака');
      if(conditions.free_cancellation===true)feature('Бесплатная отмена по тарифу');
    }else{
      if(kind==='avia'&&conditions.baggage_kg!==null&&conditions.baggage_kg!==undefined)feature(conditions.baggage_kg?'Багаж '+conditions.baggage_kg+' кг':'Без багажа');
      if(kind==='avia'&&conditions.cabin_baggage_kg!==null&&conditions.cabin_baggage_kg!==undefined)feature('Ручная кладь '+conditions.cabin_baggage_kg+' кг');
      if(conditions.direct===true)feature(kind==='rail'?'Без пересадок':'Прямые рейсы');
      if(conditions.seat_category)feature(conditions.seat_category);
      else if(Array.isArray(conditions.seat_categories))feature(conditions.seat_categories.join(' / '));
      if(conditions.refundable===true)feature('Возвратный тариф');
    }
    if(features.children.length)card.append(features);
    const priceRow=el('div','price-row'),price=el('div');
    price.append(el('div','price',money(item.price_minor)),el('span','price-label',(hotel?'За всё проживание':'Туда и обратно')+' · на всех'));
    priceRow.append(price);
    if(!selected){
      const choose=button('Выбрать '+(hotel?'отель':kind==='rail'?'поезд':'перелёт'),'',()=>send('select_component',{component_id:item.component_id,options_version:item.options_version},'Выбираю '+(hotel?'отель':kind==='rail'?'поезд':'перелёт')+' '+index+': '+item.title));
      if(!item.component_id||!Number.isInteger(item.options_version)||item.eligibility!=='satisfied'){choose.disabled=true;choose.dataset.blocked='true';}
      priceRow.append(choose);
    }else if(removable){
      priceRow.append(button('Убрать из выбора','button-quiet button-small',()=>send('remove_component',{product_type:kind},'Убираю выбранный '+(hotel?'отель':kind==='rail'?'поезд':'перелёт'))));
    }
    card.append(priceRow);
    const details=el('details','offer-details');details.append(el('summary','','Условия тарифа'));const list=el('ul');
    if(hotel){
      if(conditions.check_in_time)list.append(el('li','','Заезд с '+conditions.check_in_time));
      if(conditions.check_out_time)list.append(el('li','','Выезд до '+conditions.check_out_time));
      if(conditions.free_cancellation===true&&conditions.free_cancellation_until){
        const deadline=flightLocalTime(conditions.free_cancellation_until);
        list.append(el('li','','Бесплатная отмена до '+(deadline||'срока, указанного в тарифе')+' по времени тарифа. После этого возможен штраф.'));
      }else list.append(el('li','','Бесплатная отмена: '+(conditions.free_cancellation===false?'не предусмотрена':conditions.free_cancellation===true?'предусмотрена по тарифу':'сведения не указаны')));
    }else{
      list.append(el('li','','Возврат билета: '+(conditions.refundable===true?'доступен, возможны штрафы':conditions.refundable===false?'не предусмотрен':'условия не указаны')));
      (item.provider_refs?.segments||[]).forEach(segment=>{const timing=flightSchedule(segment.voyage_no||'Участок пути',segment.departure_at,segment.arrival_at);if(timing)list.append(el('li','',timing));});
    }
    const seen=new Set();
    [...(item.reasons||[]),...(item.warnings||[])].forEach(text=>{if(typeof text==='string'&&text.trim()&&!seen.has(text)){seen.add(text);list.append(el('li','',text));}});
    details.append(list);card.append(details);return card;
  }
  function renderComponentTrip(content,r,request,parts){
    const plan=r.current_plan,selected=parts.selected.length?parts.selected:plan?.components||[];
    const unverified=plan?.verification_status==='unverified'||(r.status==='partial'&&r.error?.code==='stale_offer');
    const complete=completePlan(plan),missing=plan?.missing_products||[];
    $('trip-title').textContent=r.demo_order?'Поездка собрана':selected.length?'Ваш выбор и варианты':'Варианты для вас';
    const count=parts.transport.length+parts.hotels.length;
    $('trip-count').hidden=!count;$('trip-count').textContent=String(count);
    if(r.demo_order){
      const receipt=el('section','receipt');receipt.append(el('span','receipt-icon','✓'),el('h3','','Поездка собрана'),el('p','','Демонстрационный заказ сохранён. Реального бронирования и списания денег нет.'),el('span','receipt-number',r.demo_order.local_id));content.append(receipt);
    }
    if(selected.length){
      const choice=el('section','selected-components');choice.setAttribute('aria-label','Выбранные части поездки');
      choice.append(el('h3','component-section-title','Выбрано'));
      selected.forEach(item=>choice.append(componentCard(item,0,{selected:true,removable:!r.demo_order,unverified})));
      const total=plan?.total_minor??selected.reduce((sum,item)=>sum+item.price_minor,0);
      choice.append(el('p','selected-total',(complete?'Итого за выбранное: ':'Уже выбрано на ')+money(total)));
      if(missing.length)choice.append(el('p','selection-next','Осталось выбрать '+missing.map(kind=>kind==='hotel'?'отель':'транспорт').join(' и ')+'. Варианты ниже.'));
      if(r.plan_diff?.length)choice.append(el('p','diff-summary','Выбор обновлён. Проверьте стоимость и условия.'));
      content.append(choice);
      if(!r.demo_order&&complete&&plan.eligibility==='satisfied'&&!unverified&&state.config.checkout_mode!=='disabled'){
        if(r.pending_action){
          const confirm=el('section','confirm-box');confirm.append(el('h3','','Подтвердить тестовый заказ?'),el('p','','Сохраним выбранные части поездки. Реального бронирования и списания денег не будет.'),el('strong','confirm-total',money(total)));
          const actions=el('div','button-group');actions.append(button('Подтвердить заказ','',()=>send('confirm_action',Object.fromEntries(['action_id','plan_id','plan_version','payload_hash'].map(key=>[key,r.pending_action[key]])),'Подтверждаю демонстрационный заказ')),button('Вернуться к выбору','button-secondary',()=>send('reject_action',{},'Вернуться к выбору без оформления')));confirm.append(actions);content.append(confirm);
        }else{
          const actions=el('section','plan-actions');actions.append(el('p','','Выбранные части поездки сочетаются. Можно заменить любой вариант ниже или проверить демонстрационное оформление.'),button('Проверить тестовое оформление','',()=>send('message',{text:'Оформить демонстрационный заказ'},'Оформить демонстрационный заказ')));content.append(actions);
        }
      }else if(!r.demo_order&&unverified){
        const actions=el('section','plan-actions');actions.append(el('p','','Выбранные условия нужно обновить перед оформлением.'),button('Обновить варианты','',()=>send('message',{text:'Подбери новые варианты по тем же пожеланиям.'},'Подбери новые варианты по тем же пожеланиям.')));content.append(actions);
      }
    }
    if(r.demo_order)return;
    const groups=[['avia','Перелёты',parts.transport.filter(item=>item.product_type==='avia')],['rail','Поезда',parts.transport.filter(item=>item.product_type==='rail')],['hotel','Отели',parts.hotels]];
    const requested=request.products||['avia','hotel'];
    for(const [kind,title,cards] of groups){
      if(!requested.includes(kind))continue;
      const failure=(r.component_errors||state.session?.component_errors||{})[kind];
      if(!cards.length&&!failure)continue;
      const group=el('section','component-group');group.dataset.componentGroup=kind;group.setAttribute('aria-label',title);
      const header=el('div','component-group-heading');header.append(el('h3','component-section-title',title));if(cards.length)header.append(el('span','count-badge',cards.length));group.append(header);
      if(cards.length){
        group.append(el('p','results-summary',kind==='hotel'?'Стоимость за всё проживание. Отель можно выбрать независимо от транспорта.':'Стоимость за дорогу туда и обратно. Выберите один вариант транспорта.'));
        const list=el('div','options-list');cards.forEach((item,index)=>list.append(componentCard(item,index+1,{selected:selected.some(chosen=>sameComponent(chosen,item)),unverified})));group.append(list);
      }
      if(failure&&requested.includes(kind))group.append(el('p','component-search-note',cards.length?'Часть вариантов пока не удалось проверить. Можно продолжить с найденными.':(kind==='hotel'?'Подбор отелей':kind==='rail'?'Подбор поездов':'Подбор перелётов')+' не завершился. Остальной выбор сохранён.'));
      content.append(group);
    }
  }
  function offerCard(option, index, selected=false){
    const hotel=option.components.find(c=>c.product_type==='hotel')||{};
    const flight=option.components.find(c=>c.product_type==='avia')||{};
    const hc=hotel.conditions||{},fc=flight.conditions||{};
    const card=el('article','offer-card'+(selected?' is-selected':'')+(option.eligibility!=='satisfied'?' is-unavailable':''));
    const top=el('div','offer-top');
    top.append(el('span','offer-kicker',selected?'ВЫБРАННАЯ ПОЕЗДКА':'ВАРИАНТ '+index));
    const eligibility=option.eligibility||'unknown';
    top.append(el('span','eligibility '+eligibility,option.verification_status==='unverified'?'Требует проверки':{satisfied:'Соответствует запросу',unknown:'Нужна проверка',violated:'Есть несоответствия'}[eligibility]));
    card.append(top,el('h3','',hotel.title||option.title),el('p','hotel-subtitle',hc.room_name||'Номер уточняется'));
    if(hotel.query?.departure_date&&hotel.query?.return_date)card.append(el('p','hotel-subtitle','Заезд '+dateLabel(hotel.query.departure_date)+' · выезд '+dateLabel(hotel.query.return_date)));
    const features=el('div','offer-features');
    if(hc.stars)features.append(el('span','feature-pill','★'.repeat(Math.min(5,Number(hc.stars)))));
    if(hc.rating)features.append(el('span','feature-pill','Оценка '+hc.rating));
    features.append(el('span','feature-pill',hc.breakfast===true?'Завтрак включён':hc.breakfast===false?'Без завтрака':'Завтрак не уточнён'));
    if(fc.baggage_kg!==null&&fc.baggage_kg!==undefined)features.append(el('span','feature-pill',fc.baggage_kg?'Багаж '+fc.baggage_kg+' кг':'Без багажа'));
    card.append(features);
    const flightRow=el('div','flight-row');const flightDetails=el('div','flight-details');
    flightDetails.append(el('strong','',flight.title||'Перелёт'));
    const query=flight.query||{};const route=[query.origin,query.destination].filter(Boolean).join(' → ');
    flightDetails.append(el('span','',route+(fc.direct===true?' · прямые рейсы':'')+' · туда и обратно'));
    const outbound=flightSchedule('Туда',flight.departure_at,flight.arrival_at);
    const inbound=flightSchedule('Обратно',flight.return_departure_at,flight.return_arrival_at);
    if(outbound)flightDetails.append(el('span','flight-schedule',outbound));
    if(inbound)flightDetails.append(el('span','flight-schedule',inbound));
    if(outbound||inbound)flightDetails.append(el('span','flight-time-note','Местное время аэропортов'));
    flightRow.append(el('span','flight-glyph','✈'),flightDetails);card.append(flightRow);
    const issues=(option.checks||[]).filter(c=>c.hard&&c.status!=='satisfied');
    issues.slice(0,2).forEach(issue=>card.append(el('p','violation-note',issue.explanation)));
    const priceRow=el('div','price-row');const priceBlock=el('div');priceBlock.append(el('div','price',money(option.total_minor)),el('span','price-label','Перелёт + отель · на всех'));
    priceRow.append(priceBlock);
    if(!selected){const choose=button(eligibility==='satisfied'?'Выбрать вариант':'Не подходит','',()=>send('select_option',{option_id:option.option_id,options_version:option.options_version},'Выбираю вариант '+index+': '+(hotel.title||option.title)));if(eligibility!=='satisfied'){choose.disabled=true;choose.dataset.blocked='true';}priceRow.append(choose);}
    else priceRow.append(el('span','eligibility','Ваш выбор ✓'));
    card.append(priceRow);
    const details=el('details','offer-details');details.append(el('summary','','Стоимость и условия'));const ul=el('ul');
    (option.reasons||[]).forEach(text=>ul.append(el('li','',text)));
    (flight.provider_refs?.segments||[]).forEach(segment=>{
      const timing=flightSchedule(segment.voyage_no||'Рейс',segment.departure_at,segment.arrival_at);
      ul.append(el('li','',[(segment.from||'')+' → '+(segment.to||''),timing].filter(Boolean).join(' · ')));
    });
    if(hc.free_cancellation===true&&hc.free_cancellation_until)ul.append(el('li','','Бесплатная отмена отеля до '+new Date(hc.free_cancellation_until).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'numeric',month:'long',hour:'2-digit',minute:'2-digit'})+' МСК; позже — штрафы по тарифу.'));
    else ul.append(el('li','','Бесплатная отмена отеля: '+(hc.free_cancellation===false?'не предусмотрена':'не подтверждена')));
    ul.append(el('li','','Возвратный авиабилет: '+(fc.refundable===true?'да, возможны штрафы':fc.refundable===false?'нет':'не подтверждено')));
    (option.unpriced||[]).forEach(text=>ul.append(el('li','','Не оценено: '+text)));
    (option.warnings||[]).filter(text=>!issues.some(i=>i.explanation===text)).forEach(text=>ul.append(el('li','',text)));
    details.append(ul);card.append(details);return card;
  }
  function renderTrip(){
    const content=$('trip-content');const r=state.result;const options=suitableOptions(r);const parts=componentState(r);const hasComponents=parts.transport.length||parts.hotels.length||parts.selected.length;const searchCompleted=!r?.web_search&&['options_ready','partial'].includes(r?.status);updateSteps();
    $('view-options').hidden=!(hasComponents||options.length||r?.current_plan||r?.demo_order);
    $('view-options').textContent=r?.demo_order?'Посмотреть результат ↓':r?.pending_action?'Проверить и подтвердить ↓':r?.current_plan?'Открыть план поездки ↓':'Посмотреть варианты ↓';
    $('saved-note').hidden=!state.session;$('trip-count').hidden=true;
    if(!r||(!hasComponents&&!options.length&&!r.current_plan&&!r.demo_order&&((!state.messages.length&&!searchCompleted)||r.response_kind==='chat'))){content.replaceChildren(...[...emptyTrip.childNodes].map(n=>n.cloneNode(true)));$('trip-title').textContent='Ваша поездка';return;}
    content.replaceChildren();
    const request=state.session?.request||r.current_plan?.request||{};
    const meta=buildMeta(request);if(meta.children.length)content.append(meta);
    if(hasComponents){renderComponentTrip(content,r,request,parts);updateControls();return;}
    if(r.demo_order){
      $('trip-title').textContent='Поездка собрана';
      const receipt=el('section','receipt');receipt.append(el('span','receipt-icon','✓'),el('h3','','Всё получилось!'),el('p','','Демонстрационный заказ создан. Реальные билеты и номер не забронированы, деньги не списаны.'),el('span','receipt-number',r.demo_order.local_id));content.append(receipt);
      if(r.current_plan)content.append(offerCard(r.current_plan,1,true));
      return;
    }
    if(r.current_plan){
      const unverified=r.current_plan.verification_status==='unverified'||(r.status==='partial'&&r.error?.code==='stale_offer');
      $('trip-title').textContent=r.pending_action?'Проверка перед оформлением':'Ваш план поездки';
      const intro=el('div','plan-intro');const introCopy=el('div');introCopy.append(el('h3','',(request.origin||'Поездка')+' → '+(request.destination||'ваша цель')),el('p','',unverified?'Цена и наличие требуют проверки':'Перелёт и проживание подобраны'));intro.append(introCopy,el('span','','↗'));content.append(intro,offerCard(r.current_plan,1,true));
      if(r.plan_diff?.length){const diff=el('div','diff-summary','План обновлён. Проверьте новую стоимость и условия перед подтверждением.');content.append(diff);}
      if(r.pending_action&&state.config.checkout_mode!=='disabled'){
        const confirm=el('section','confirm-box');confirm.append(el('h3','','Подтвердить тестовый заказ?'),el('p','','Будут сохранены выбранные перелёт и отель. Это проверка сценария — без реального бронирования и списания денег.'),el('strong','confirm-total',money(r.current_plan.total_minor)),el('p','confirm-caption','Трансферы, дополнительное питание и личные расходы не оценены.'));
        const buttons=el('div','button-group');buttons.append(button('Подтвердить заказ','',()=>send('confirm_action',Object.fromEntries(['action_id','plan_id','plan_version','payload_hash'].map(k=>[k,r.pending_action[k]])),'Подтверждаю демонстрационный заказ')),button('Вернуться к плану','button-secondary',()=>send('reject_action',{},'Вернуться к плану без оформления')));confirm.append(buttons);content.append(confirm);
      } else if(unverified){
        const actions=el('section','plan-actions');
        actions.append(el('p','','Выбранный тариф не удалось подтвердить. Обновите варианты перед оформлением.'),button('Обновить варианты','',()=>send('message',{text:'Подбери новые варианты по тем же пожеланиям.'},'Подбери новые варианты по тем же пожеланиям.')));
        content.append(actions);
      } else if(state.config.checkout_mode!=='disabled') {
        const actions=el('section','plan-actions');actions.append(el('p','','Всё подходит? Перейдите к проверке состава перед демонстрационным оформлением. Изменить пожелания можно в чате.'));
        const buttons=el('div','button-group');buttons.append(button('Перейти к оформлению','',()=>send('message',{text:'Оформить демонстрационный заказ'},'Оформить демонстрационный заказ')),button('Изменить пожелания','button-secondary',()=>{$('message-input').focus();$('message-input').placeholder='Что хотите изменить в поездке?';}));actions.append(buttons);
        if(state.session?.plan_history?.length)actions.append(button('Вернуть предыдущий план','button-quiet',()=>send('undo',{},'Вернуть предыдущий выбранный план')));
        content.append(actions);
      }
    } else if(options.length){
      $('trip-title').textContent='Варианты для вас';$('trip-count').textContent=String(options.length);$('trip-count').hidden=false;
      content.append(el('p','results-summary','Сравните условия и выберите вариант. Стоимость указана за перелёт туда-обратно и всё проживание.'));
      const list=el('div','options-list');options.forEach((option,i)=>list.append(offerCard(option,i+1)));content.append(list);
    } else {
      const empty=el('section','empty-result');
      if(r.web_search||r.status==='research_ready'){
        $('trip-title').textContent='Поиск в интернете';
        const search=r.web_search;
        if(search?.error){
          const accessDenied=search.error.code==='search_auth_error';
          empty.append(el('h3','',accessDenied?'Яндекс не разрешил поиск':'Не удалось выполнить поиск'),el('p','',accessDenied?'Проверьте ключ и доступ к Search API.':r.message||'Попробуйте повторить поиск позже.'));
          if(accessDenied||!state.config?.web_search?.configured)empty.append(button('Настройки Яндекса','button-secondary button-small',()=>openSearchSettings(true)));
        } else if(search?.results?.length){
          empty.append(el('h3','','Поиск завершён'),el('p','','Результат — в ответе помощника. Продолжите разговор, чтобы подобрать поездку.'));
        } else {
          empty.append(el('h3','','Нет результатов веб-поиска'),el('p','','По этому запросу ничего не нашлось. Уточните запрос в чате или повторите поиск.'));
        }
      } else if(searchCompleted){
        $('trip-title').textContent='Результат подбора';
        const reason=(r.search_diagnostics||state.session?.search_diagnostics)?.code;
        const heading={missing_flights:'Не найден перелёт',missing_trains:'Не найден поезд',missing_transport:'Не найден транспорт',missing_hotels:'Не найден отель',provider_error:'Поиск не завершён'}[reason]||'Подходящих вариантов не нашлось';
        empty.append(el('h3','',heading),el('p','',r.message||'Среди проверенных предложений нет вариантов, соответствующих вашему запросу. Пожелания можно изменить в чате.'));
      } else {
        $('trip-title').textContent='Уточняем вашу поездку';
        empty.append(el('h3','','Ещё немного деталей'),el('p','','Ответьте на вопрос помощника в чате. Когда для поиска будет достаточно данных, здесь появятся варианты.'));
      }
      content.append(empty);
    }
    updateControls();
  }
  async function loadSession({preserveMessages=false,render=true}={}){
    state.restoring=true;renderSuggestions();
    try{
      const loaded=await api('/api/sessions/'+encodeURIComponent(state.sessionId));
      state.session=loaded.session||null;state.version=loaded.session?.state_version||0;
      if(loaded.result)state.result={...loaded.result,...Object.fromEntries(['transport_options','hotel_options','selected_components','component_errors'].filter(key=>loaded.result[key]===undefined&&loaded.session?.[key]!==undefined).map(key=>[key,loaded.session[key]]))};
      else if(loaded.session)state.result={state_version:loaded.session.state_version,options:loaded.session.options||[],transport_options:loaded.session.transport_options||[],hotel_options:loaded.session.hotel_options||[],selected_components:loaded.session.selected_components||[],component_errors:loaded.session.component_errors||{},current_plan:loaded.session.current_plan,pending_action:loaded.session.pending_action,demo_order:loaded.session.demo_order};
      if(Array.isArray(loaded.messages)&&(!preserveMessages||loaded.messages.length))state.messages=loaded.messages;
      if(render){renderMessages();renderTrip();}return loaded;
    }finally{state.restoring=false;renderSuggestions();}
  }
  async function pollJob(jobId){
    state.lastSequence=0;let misses=0;
    while(true){
      let job;
      try{job=await api('/api/jobs/'+encodeURIComponent(jobId));misses=0;}
      catch(error){if(misses++<2){await sleep(1100);continue;}setBusy(false);showError('Связь прервалась. Запрос может ещё выполняться. Продолжите проверку результата, чтобы не отправлять его заново.',()=>{clearError();setBusy(true);return pollJob(jobId);});return;}
      for(const event of job.events||[]){
        if(event.sequence<=state.lastSequence)continue;
        state.lastSequence=event.sequence;
        const stages={interpret:'Понимаю ваш запрос…',search:'Подбираю варианты поездки…',search_hotels_for_arrival:'Подбираю отели с даты прибытия…',suggestions:'Подбираю полезные продолжения…',web_search:'Ищу в Яндексе…',web_read:'Читаю афишу…',web_search_refine:'Уточняю даты…'};
        let label=null;
        if(event.type==='stage_changed')label=stages[event.payload?.stage];
        if(event.type==='tool_started')label=stages[event.payload?.operation]||(event.payload?.operation==='refresh'?'Перепроверяю выбранные условия…':'Сравниваю предложения…');
        if(event.type==='options_updated')label='Готовлю варианты для вас…';
        if(event.type==='confirmation_required')label='Готовлю подтверждение…';
        if(label)$('progress-text').textContent=label;
      }
      if(job.status==='completed'&&job.result){
        state.result=job.result;state.version=job.result.state_version;
        state.messages.push({role:'assistant',text:job.result.message,web_search:job.result.web_search});
        try{await loadSession({preserveMessages:true});}catch(_){renderMessages();renderTrip();}
        setBusy(false);clearError();$('message-input').focus();replyReader?.speak(job.result.message,jobId);return;
      }
      if(job.status==='failed'){
        setBusy(false);showError(typeof job.error==='string'?job.error:'Не удалось завершить запрос. Попробуйте ещё раз.',async()=>{clearError();await loadSession();$('message-input').focus();});return;
      }
      await sleep(700);
    }
  }
  async function submitBody(body, renewToken=true){
    clearError();setBusy(true);$('progress-text').textContent='Понимаю ваш запрос…';
    try{
      const job=await api('/api/turns',{method:'POST',headers:{'Content-Type':'application/json','X-Travel-Token':state.config.csrf_token},body:JSON.stringify(body)});
      await pollJob(job.job_id);
    }catch(error){
      if(error.httpStatus===403&&renewToken){
        try{state.config=await api('/api/bootstrap');return await submitBody(body,false);}
        catch(refreshError){setBusy(false);showError(refreshError.message,()=>submitBody(body));return;}
      }
      setBusy(false);showError(error.message,()=>submitBody(body));
    }
  }
  async function send(kind,payload,label){
    if(state.busy||state.voiceActive||!state.ready)return;
    const body={session_id:state.sessionId,request_id:uuid(),expected_state_version:state.version,input:kind,payload};
    state.messages.push({role:'user',text:label||payload.text||'Выбор поездки'});renderMessages();
    $('message-input').value='';$('message-input').style.height='';await submitBody(body);
  }
  async function start(){
    clearError();state.ready=false;updateControls();
    try{
      state.config=await api('/api/bootstrap');
      $('model-label').textContent=state.config.model_backend==='codex'?'Работает с Codex':state.config.model_backend==='rules'?'Тестовый разбор':'Помощник с ИИ';
      $('data-label').textContent=state.config.data_mode==='replay'?'архивные данные':'поиск предложений';
      $('test-note').hidden=state.config.data_mode!=='replay';
      renderSearchConnection();
      try{state.sessionId=localStorage.getItem(SESSION_KEY)||uuid();localStorage.setItem(SESSION_KEY,state.sessionId);}catch(_){state.sessionId=uuid();}
      let loaded=await loadSession({render:false});
      if(loaded.session&&loaded.session.data_mode!==state.config.data_mode){
        const continued=await api('/api/sessions/'+encodeURIComponent(state.sessionId)+'/continue',{method:'POST',headers:{'Content-Type':'application/json','X-Travel-Token':state.config.csrf_token},body:'{}'});
        state.sessionId=continued.session_id;state.session=null;state.result=null;state.messages=[];state.version=0;
        try{localStorage.setItem(SESSION_KEY,state.sessionId);}catch(_){}
        loaded=await loadSession({render:false});
        $('mode-notice').textContent=state.config.data_mode==='live'?'Включён живой поиск Туту. Пожелания сохранены; предложения проверим заново.':'Режим поиска обновлён. Пожелания сохранены; предложения проверим заново.';
        $('mode-notice').hidden=false;
      }
      renderMessages();renderTrip();state.ready=true;updateControls();
      if(loaded.active_job_id){setBusy(true);await pollJob(loaded.active_job_id);}
    }catch(error){showError(error.message,start);updateControls();}
  }
  $('chat-form').addEventListener('submit',event=>{event.preventDefault();const text=$('message-input').value.trim();if(text)send('message',{text},text);});
  $('message-input').addEventListener('input',()=>{const area=$('message-input');area.style.height='auto';area.style.height=Math.min(area.scrollHeight,135)+'px';updateControls();});
  $('message-input').addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();$('chat-form').requestSubmit();}});
  $('try-example').addEventListener('click',()=>send('message',{text:state.config.demo_prompt},state.config.demo_prompt));
  $('search-settings-toggle').addEventListener('click',()=>openSearchSettings($('search-settings').hidden));
  $('search-config-cancel').addEventListener('click',()=>openSearchSettings(false));
  $('search-settings').addEventListener('submit',configureSearch);
  $('search-settings').addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();openSearchSettings(false);}});
  $('search-repeat').addEventListener('click',()=>send('message',{text:'Повтори поиск в интернете'},'Повтори поиск в интернете'));
  $('view-options').addEventListener('click',()=>{$('trip-title').scrollIntoView({behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'start'});});
  $('retry').addEventListener('click',()=>{if(state.voiceActive)return;const action=state.retry;if(action)Promise.resolve(action()).catch(error=>showError(error.message,action));});
  $('new-trip').addEventListener('click',()=>{if(state.busy||state.voiceActive)return;voice?.reset();replyReader?.cancel();$('mode-notice').hidden=true;state.sessionId=uuid();state.version=0;state.session=null;state.result=null;state.messages=[];try{localStorage.setItem(SESSION_KEY,state.sessionId);}catch(_){}clearError();renderMessages();renderTrip();updateControls();$('message-input').value='';$('message-input').placeholder='Например: хочу на неделю в Ереван, с завтраком…';$('message-input').focus();});
  voice=createVoiceController();
  replyReader=createReplyReader();
  start();
})();
