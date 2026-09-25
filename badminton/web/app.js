'use strict';
const tg = window.Telegram?.WebApp;
const app = document.querySelector('#app');
const modal = document.querySelector('#modal');
const content = document.querySelector('#modal-content');
const nav = document.querySelector('#navigation');
const notice = document.querySelector('#notice');
const statuses = {draft:'Черновик', settling:'Сбор оплат', closed:'Закрыта'};
let state, page='trainings', filter='active', tid=0, busy=false, pending=null, noticeTimer;
const esc = value => String(value ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = n => new Intl.NumberFormat('ru-RU',{minimumFractionDigits:0,maximumFractionDigits:2}).format(n/100)+' ฿';
const decimal = n => String(n/100);
const dateLabel = date => new Date(date+'T12:00:00').toLocaleDateString('ru-RU',{day:'numeric',month:'long',year:'numeric'});
const button = (label, action, extra='', cls='') => `<button type="button" class="${cls}" data-action="${action}" ${extra}>${label}</button>`;
const badge = status => `<span class="badge ${status}">${statuses[status]}</span>`;
const field = (label,name,value='',attrs='')=>`<label>${label}<input name="${name}" value="${esc(value)}" ${attrs} required></label>`;
const number = (label,name,value='0')=>field(label,name,value,'inputmode="decimal" autocomplete="off" maxlength="16"');
const nameField = (label='Имя участника')=>field(label,'name','','maxlength="60" autocomplete="off" placeholder="Например, Андрей Н"');
const options = (rows,selected)=>rows.map(r=>`<option value="${r.id}" ${Number(selected)===r.id?'selected':''}>${esc(r.name)}</option>`).join('');
function notify(text) {clearTimeout(noticeTimer);notice.textContent=text;notice.hidden=false;noticeTimer=setTimeout(()=>notice.hidden=true,8000);}
function setBusy(value){busy=value;document.querySelectorAll('button').forEach(b=>{if(value){b.dataset.disabled=b.disabled?'1':'0';b.disabled=true;}else if(b.dataset.disabled!==undefined){b.disabled=b.dataset.disabled==='1';delete b.dataset.disabled;}});app.setAttribute('aria-busy',String(value));}
async function request(path,body){
  let response;
  try {response=await fetch(path,{method:body?'POST':'GET',headers:{'X-Telegram-Init-Data':tg?.initData||'','Content-Type':'application/json'},body:body?JSON.stringify(body):undefined,signal:AbortSignal.timeout(25000)});}
  catch {throw new Error('Нет соединения. Проверьте интернет и повторите действие.');}
  const result=await response.json();
  if(!response.ok){const error=new Error(result.error||'Не удалось загрузить данные.');error.status=response.status;throw error;}
  return result;
}
async function load(id=tid){
  if(busy)return;setBusy(true);
  try {state=await request('/api/state'+(id?'?tid='+id:''));tid=id;pending=null;render();}
  catch(error){notify(error.message);if(!state){app.innerHTML=`<div class="empty"><div class="empty-icon">✳</div><h1>Ваш бадминтон — здесь</h1><p>${esc(error.message)}</p>${button('Попробовать снова','refresh')}</div>`;}}
  finally{setBusy(false);}
}
async function save(action,values={}){
  if(busy)return;setBusy(true);
  const base={action,tid,...values,revision:state.revision};
  // Keep the same key after a lost response; an explicit refresh reconciles uncertain results.
  const signature=JSON.stringify(base);
  if(!pending||pending.signature!==signature)pending={signature,body:{...base,request_id:crypto.randomUUID()}};
  try {state=await request('/api/action',pending.body);pending=null;tid=state.training?.id||0;modal.close();render();notify(action==='publish'?'Публикация поставлена в очередь. Бот сообщит о доставке.':'Сохранено');}
  catch(error){if(error.status&&error.status<500)pending=null;notify(error.message);const e=content.querySelector('[role="alert"]');if(e)e.textContent=error.message;}
  finally{setBusy(false);}
}
function render(){
  nav.hidden=false;nav.querySelectorAll('button').forEach(b=>b.classList.toggle('active',b.dataset.nav===page));
  if(tid){app.innerHTML=trainingView(state.training);tg?.BackButton?.show();}
  else{tg?.BackButton?.hide();app.innerHTML=page==='directory'?directoryView():page==='group'?groupView():homeView();}
  app.setAttribute('aria-busy','false');
}
function homeView(){
  const active=state.trainings.filter(t=>t.status!=='closed');
  const list=state.trainings.filter(t=>filter==='all'||(filter==='active'?t.status!=='closed':t.status==='closed'));
  return `<div class="heading"><div><div class="eyebrow">На корте и после</div><h1>Тренировки</h1><p>Играем вместе. Считаем просто.</p></div>${button('+ Новая','new')}</div>
  <div class="summary"><div><b>${active.length}</b><span>Текущих тренировок</span></div><div><b>${state.people.length}</b><span>Участников в клубе</span></div></div>
  <div class="filters" aria-label="Фильтр тренировок">${[['active','Текущие'],['closed','Завершённые'],['all','Все']].map(([id,label])=>button(label,'filter',`data-value="${id}" aria-pressed="${filter===id}"`,filter===id?'active':'')).join('')}</div>
  ${list.length?list.map(t=>`<button class="training" data-action="open" data-id="${t.id}"><div class="row"><strong>${esc(t.location)}</strong>${badge(t.status)}</div><div class="meta"><span>${dateLabel(t.date)}</span><span>· ${t.participants_count} чел.</span><span class="arrow">↗</span></div></button>`).join(''):`<div class="empty"><div class="empty-icon">✳</div><h2>${filter==='closed'?'Завершённых тренировок пока нет':'Время выйти на корт'}</h2><p>${filter==='closed'?'Здесь появится история закрытых расчётов.':'Создайте тренировку, добавьте игроков — и расходы будут под контролем.'}</p>${button('Создать тренировку','new')}</div>`}`;
}
function directoryView(){return `<div class="eyebrow">Всё под рукой</div><h1>Справочники</h1><p>Сохранённые имена и любимые залы.</p><section class="card"><div class="row"><h2>Участники <small>${state.people.length}</small></h2>${button('+ Добавить','person','','text-button')}</div><label>Поиск по имени<input id="search" type="search" placeholder="Начните вводить имя" autocomplete="off"></label><div id="people-list">${state.people.map(p=>`<div class="person row" data-name="${esc(p.name.toLowerCase())}"><span class="avatar">${esc(p.name[0])}</span><div class="person-info">${esc(p.name)}</div></div>`).join('')}</div><p id="no-results" hidden>Никого не нашли.</p></section><section class="card"><div class="row"><h2>Локации</h2>${button('+ Добавить','location','','text-button')}</div>${state.locations.map(l=>`<div class="person">${esc(l.name)}</div>`).join('')}</section>`;}
function groupView(){return `<div class="eyebrow">Общее пространство</div><h1>Группа</h1><p>Одна история тренировок для всей команды.</p><section class="card"><h2>${state.group?esc(state.group.title):'Группа ещё не подключена'}</h2><p>${state.group?'Участникам группы доступны тренировки, расчёты и отметки оплат. Итоги можно отправить из экрана расчёта.':'Попросите владельца подключить групповой чат.'}</p>${state.is_admin?'<div class="hint">Добавьте бота в группу, назначьте администратором и отправьте <b>/bind</b> в нужной теме. Повторная команда обновит тему для новых публикаций.</div>':'<div class="hint">Подключением группы управляет владелец бота.</div>'}</section><section class="card"><h2>Как считаем</h2><p>Стоимость корта и потраченных воланов делится пропорционально времени игры. Из доли вычитаются оплаченный корт и предоставленные воланы.</p><p>Все суммы в тайских батах. Отметки оплат вносите после получения денег: приложение не выполняет банковские переводы.</p><small>Даты — по времени Таиланда (UTC+7).</small></section>`;}
function trainingView(t){
  const draft=t.status==='draft', calc=t.calculation;
  const paid=t.payers.reduce((a,p)=>a+p.amount,0);
  const remaining=t.transfers.reduce((a,tr)=>a+tr.amount-tr.paid,0);
  const shuttle=t.participants.reduce((a,p)=>a+p.shuttle_count*p.shuttle_price,0);
  let html=`${button('← Тренировки','back','','text-button back')}<div class="heading"><div><div class="eyebrow">Тренировка № ${t.id}</div><h1>${esc(t.location)}</h1><p>${dateLabel(t.date)}</p></div>${badge(t.status)}</div>`;
  html+=`<div class="summary"><div><b>${money(t.court_cost+shuttle)}</b><span>Все расходы</span></div><div><b>${draft?t.participants.length:money(remaining)}</b><span>${draft?'Участников':'Осталось перевести'}</span></div></div>`;
  if(draft){
    html+=`<section class="card"><div class="row"><h2>01 · Тренировка</h2>${button('Изменить','details','','text-button')}</div><div class="row"><span class="muted">Стоимость корта</span><b class="amount">${money(t.court_cost)}</b></div></section>
    <section class="card"><div class="row"><h2>02 · Участники</h2>${button('+ Добавить','add','','text-button')}</div>${!t.participants.length?'<p>Выберите игроков из справочника или добавьте гостя. По умолчанию — 2 часа игры.</p>':''}${t.participants.map(p=>`<div class="person row"><span class="avatar">${esc(p.name[0])}</span><div class="person-info"><b>${esc(p.name)}</b>${!p.saved?'<small>Гость</small>':''}<small>${new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2}).format(p.minutes/60)} ч · ${p.shuttle_count} вол. ${p.shuttle_count?'× '+money(p.shuttle_price):''}</small></div>${button('Изменить','participant',`data-id="${p.person_id}"`,'edit')}</div>`).join('')}</section>
    <section class="card"><div class="row"><h2>03 · Оплата корта</h2>${button('+ Плательщик','payer','','text-button')}</div><p>Кто уже оплатил корт? Плательщик может не участвовать в игре.</p>${t.payers.filter(p=>p.amount>0).map(p=>`<div class="person row"><div class="person-info"><b>${esc(p.name)}</b><small>${money(p.amount)}</small></div>${button('Изменить','payer',`data-id="${p.person_id}"`,'edit')}</div>`).join('')}<div class="row breakdown"><span class="muted">Указано / стоимость</span><b class="amount">${money(paid)} / ${money(t.court_cost)}</b></div>${paid!==t.court_cost?`<div class="hint warning">Осталось распределить ${money(t.court_cost-paid)}</div>`:''}</section>
    ${calc?button('Посмотреть расчёт →','preview','','full'):`<div class="hint warning">${esc(t.calculation_error)}</div><button class="full" disabled>Посмотреть расчёт →</button>`}`;
  }else{
    html+=calculationView(t);
    html+=`<section class="card"><h2>Переводы</h2><p>Отмечайте деньги, которые уже получены.</p>${t.transfers.length?t.transfers.map(tr=>`<div class="transfer"><div class="row"><b>${esc(tr.sender_name)} → ${esc(tr.recipient_name)}</b><b class="amount">${money(tr.amount)}</b></div><p>${tr.paid===tr.amount?'✓ Оплачено полностью':`Получено ${money(tr.paid)} · остаток ${money(tr.amount-tr.paid)}`}</p><div class="progress"><progress value="${tr.paid}" max="${tr.amount}" aria-label="Оплачено"></progress></div>${t.status==='settling'?button(tr.paid===tr.amount?'Посмотреть оплату':'Отметить оплату','transfer',`data-id="${tr.id}"`,'secondary full'):''}</div>`).join(''):'<div class="hint">Все расходы уже распределены. Переводы не нужны.</div>'}</section>`;
    const group=t.publication||state.group;
    html+=`<section class="card"><h2>Итог в группе</h2><p>${group?esc(group.title):'Сначала подключите группу в разделе «Группа».'}</p>${group?button(t.publication?'Обновить итог в группе':'Опубликовать итог','publish','','secondary full'):''}<small>Отметки оплат обновляют опубликованное сообщение автоматически.</small></section>`;
    if(t.status==='closed')html+=button('Открыть тренировку снова','reopen','','secondary full');
    else html+=`<div class="stack">${remaining===0?button('Закрыть тренировку ✓','close','','full'):`<div class="hint">Чтобы закрыть тренировку, отметьте оставшиеся ${money(remaining)}.</div>`}${button('Вернуть к редактированию','unlock','','text-button')}</div>`;
  }
  return html;
}
function calculationView(t){const c=t.calculation;if(!c)return `<div class="hint warning">${esc(t.calculation_error)}</div>`;return `<section class="card"><h2>Стоимость участия</h2><div class="row breakdown"><span class="muted">Корт</span><span>${money(t.court_cost)}</span></div><div class="row breakdown"><span class="muted">Воланы</span><span>${money(c.total-t.court_cost)}</span></div>${Object.entries(c.shares).map(([id,amount])=>`<div class="row breakdown"><span>${esc(t.names[id])}</span><b class="amount">${money(amount)}</b></div>`).join('')}<small>Расходы распределены пропорционально времени игры.</small></section>`;}
function openDialog(title,description,fields,submit,onSubmit){
  content.innerHTML=`<h2>${title}</h2><p>${description}</p><form>${fields}<div role="alert" class="danger"></div><button class="full" type="submit">${submit}</button></form>`;
  content.querySelector('form').onsubmit=e=>{e.preventDefault();onSubmit(Object.fromEntries(new FormData(e.target)));};
  modal.showModal();
}
function locationFields(t={}){return `${field('Дата','date',t.date||state.today,'type="date"')}<label for="location-select">Локация</label><select name="location_id" id="location-select">${options(state.locations,t.location_id)}<option value="new">+ Новая локация</option></select><label id="location-new" hidden>Название локации<input name="location_name" maxlength="60" disabled></label>${number('Полная стоимость корта, ฿','court_cost',t.court_cost!==undefined?decimal(t.court_cost):'')}`;}
function bindLocation(){content.querySelector('#location-select').onchange=e=>{const label=content.querySelector('#location-new');label.hidden=e.target.value!=='new';label.querySelector('input').disabled=label.hidden;label.querySelector('input').required=!label.hidden;};content.querySelector('#location-select').dispatchEvent(new Event('change'));}
function personChoice(rows,selected){return `<label for="person-select">Участник</label><select name="pid" id="person-select" required><option value="">Выберите человека</option>${options(rows,selected)}<option value="new">+ Новое имя</option></select><div id="person-new" hidden>${nameField()}<label class="checkbox"><input type="checkbox" name="saved" checked>Сохранить для следующих тренировок</label></div>`;}
function bindPerson(){const select=content.querySelector('#person-select');const area=content.querySelector('#person-new');const sync=()=>{area.hidden=select.value!=='new';area.querySelectorAll('input').forEach(i=>{i.disabled=area.hidden;});};select.onchange=sync;sync();}
function choosePerson(values){if(values.pid==='new')return {name:values.name,saved:values.saved==='on'};return {pid:values.pid};}
function act(action,id,value){
  const t=state?.training;
  if(action==='refresh')return load();
  if(action==='back'){tid=0;page='trainings';render();return;}
  if(action==='filter'){filter=value;render();return;}
  if(action==='open'){page='trainings';return load(Number(id));}
  if(action==='new'||action==='details'){openDialog(action==='new'?'Новая тренировка':'Параметры тренировки','Укажите общую стоимость корта за всю тренировку.',locationFields(action==='details'?t:{}),action==='new'?'Создать тренировку':'Сохранить',v=>save(action==='new'?'create':'details',v));bindLocation();return;}
  if(action==='person'||action==='location'){openDialog(action==='person'?'Новый участник':'Новая локация',action==='person'?'Для тёзок добавьте фамилию или отличительный признак.':'Локация сохранится для следующих тренировок.',nameField(action==='person'?'Имя':'Название'),'Добавить',v=>save(action,v));return;}
  if(action==='add'){
    const rows=state.people.filter(p=>!t.participants.some(x=>x.person_id===p.id));
    openDialog('Добавить участника','По умолчанию: 2 часа игры, без воланов. Для гостя снимите галочку сохранения.',personChoice(rows),'Добавить',v=>save('add',choosePerson(v)));bindPerson();return;
  }
  if(action==='participant'){
    const p=t.participants.find(p=>p.person_id===Number(id));
    openDialog(esc(p.name),'Укажите время игры и потраченные воланы этого участника.',number('Время игры, ч','hours',String(p.minutes/60))+number('Потрачено воланов, шт.','shuttle_count',p.shuttle_count)+number('Цена одного волана, ฿','shuttle_price',decimal(p.shuttle_price))+button('Убрать из тренировки','remove',`data-id="${p.person_id}"`,'text-button danger'),'Сохранить',v=>save('participant',{...v,pid:p.person_id}));return;
  }
  if(action==='remove'){return confirmDialog('Убрать участника?','Его оплата корта, если есть, сохранится. Её можно изменить отдельно.','Убрать',()=>save('remove',{pid:Number(id)}));}
  if(action==='payer'){
    const p=t.payers.find(p=>p.person_id===Number(id));
    const all=new Map([...state.people,...t.participants.map(p=>({id:p.person_id,name:p.name})),...t.payers.map(p=>({id:p.person_id,name:p.name}))].map(p=>[p.id,p]));
    openDialog('Кто оплатил корт','Новая сумма заменяет прежнюю. Введите 0, чтобы убрать оплату.',personChoice([...all.values()],p?.person_id)+number('Оплачено за корт, ฿','amount',p?decimal(p.amount):''),'Сохранить',v=>save('payer',{...choosePerson(v),saved:true,amount:v.amount}));bindPerson();return;
  }
  if(action==='preview'){
    openDialog('Проверим расчёт','После фиксации можно отмечать переводы.',calculationView(t)+`<h3>Кто кому переводит</h3>${t.calculation.transfers.map(tr=>`<div class="breakdown row"><span>${esc(t.names[tr.sender])} → ${esc(t.names[tr.recipient])}</span><b class="amount">${money(tr.amount)}</b></div>`).join('')||'<p>Переводы не нужны.</p>'}`,'Зафиксировать расчёт',()=>save('lock'));return;
  }
  if(action==='transfer'){
    const tr=t.transfers.find(x=>x.id===Number(id)), rest=tr.amount-tr.paid;
    openDialog(`${esc(tr.sender_name)} → ${esc(tr.recipient_name)}`,`Всего ${money(tr.amount)}. Получено ${money(tr.paid)}.`,(rest?number('Полученная сумма, ฿','amount',decimal(rest)):'<div class="hint">Перевод полностью оплачен.</div>')+(tr.paid?button('Отменить последнюю отметку','undo',`data-id="${tr.id}"`,'text-button danger'):''),rest?'Отметить оплату':'Готово',v=>rest?save('pay',{transfer_id:tr.id,amount:v.amount}):modal.close());return;
  }
  if(action==='undo')return confirmDialog('Отменить последнюю отметку?','Остаток перевода увеличится на сумму последней отмеченной оплаты.','Отменить отметку',()=>save('undo',{transfer_id:Number(id)}));
  if(action==='publish'){const g=t.publication||state.group;return confirmDialog('Опубликовать итог?',`В группе «${esc(g.title)}» появятся имена, стоимость участия и суммы переводов.`,'Опубликовать',()=>save('publish',{chat_id:g.chat_id,thread_id:g.thread_id}));}
  if(action==='unlock')return confirmDialog('Вернуть к редактированию?','Сначала отмените все отметки оплат. Опубликованный расчёт будет помечен как неактуальный.','Вернуть к редактированию',()=>save('unlock'));
  if(action==='close')return confirmDialog('Закрыть тренировку?','Все переводы отмечены. Тренировка останется в истории.','Закрыть тренировку',()=>save('close'));
  if(action==='reopen')return save('reopen');
}
function confirmDialog(title,text,label,callback){if(modal.open)modal.close();openDialog(title,text,'',label,callback);}
document.addEventListener('click',e=>{const target=e.target.closest('[data-action]');if(target&&!busy)act(target.dataset.action,target.dataset.id,target.dataset.value);});
nav.addEventListener('click',e=>{const target=e.target.closest('[data-nav]');if(!target||busy)return;page=target.dataset.nav;tid=0;render();window.scrollTo(0,0);});
document.addEventListener('input',e=>{if(e.target.id!=='search')return;const query=e.target.value.toLowerCase().trim();let count=0;document.querySelectorAll('[data-name]').forEach(el=>{el.hidden=!el.dataset.name.includes(query);if(!el.hidden)count++;});document.querySelector('#no-results').hidden=count>0;});
document.querySelector('#dismiss').onclick=()=>modal.close();
modal.addEventListener('cancel',e=>{if(busy)e.preventDefault();});
document.querySelector('#refresh').onclick=()=>load();
tg?.BackButton?.onClick(()=>{if(modal.open)modal.close();else act('back');});
function theme(){document.documentElement.dataset.theme=tg?.colorScheme||'light';}
theme();tg?.onEvent('themeChanged',theme);tg?.ready();tg?.expand();load();
