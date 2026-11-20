'use strict';
import {buildLesson, sourceOptions, suggestEvidence} from '/lesson.mjs';
import {replayFrames, recognitionTotal} from '/replay.mjs';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const TYPES = {duplicate_invoice:'Счёт учтён дважды',fx_conversion:'Неверный курс валюты',payment_allocation:'Платёж распределён не полностью',period_cutoff:'Операция не в том месяце'};
const RULES = {
  duplicate_invoice:['Укажите, сколько лишних денег учтено по сравнению с суммой исходного счёта.','Все строки признания AR: исходная и повторные.'],
  fx_conversion:['Укажите разницу между суммой в книге и пересчётом по курсу на дату исходного документа.','Одна основная строка: AR для счёта, CASH для платежа.'],
  payment_allocation:['Укажите нераспределённую часть платежа по суммам, которые сейчас отражены в книге.','Один платёж и счёт, указанный в его назначении.'],
  period_cutoff:['Укажите всю сумму записи, попавшей не в тот месяц. Дату нужно сверять с исходным документом.','Одна основная строка: AR для счёта, CASH для платежа.']
};
const TABLES = {invoices:'Счета',ledger:'Записи в книге',payments:'Платежи',fx:'Курсы валют',policy:'Правила учёта'};
const ACCOUNTS = {AR:'Дебиторская задолженность',REVENUE:'Выручка',CASH:'Денежные средства',UNALLOCATED:'Нераспределённые средства',FX_GAIN:'Курсовой доход',FX_LOSS:'Курсовой расход'};
let session, cases = [], currentCase = 'masked_both', tab = 'ledger', query = '', account = '', draft = [], editIndex = -1, busy = false, selectedProof = new Set(), labels = new Map(), recordIndex = new Map(), toastTimer;
let viewMode = 'start', guideStep = 0, answers = {}, recordsOpen = false, usedHints = false;
let replayIndex = null, replayTimer = null, labFamilies = Object.keys(TYPES), arenaRunning = false;
let repairState = null, repairPending = false, repairProofOpen = false, repairError = '';

async function api(path, data) {
  const response = await fetch(path, data === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'Не удалось выполнить запрос. Повторите действие.');
  return body;
}
function toast(message) { clearTimeout(toastTimer); $('#toast').textContent=message; $('#toast').hidden=false; toastTimer=setTimeout(()=>$('#toast').hidden=true,5000); }
function errorMessage(error) { toast(error.message || 'Не удалось выполнить действие. Повторите попытку.'); }
function saveToken() { try { sessionStorage.setItem('genesis-session',JSON.stringify({id:session.session_id,case:currentCase,draft,proof:[...selectedProof],version:3,viewMode,guideStep,answers,tab,usedHints,replayIndex,labFamilies})); } catch {} }
function counted(n,one,few,many) { const ending=n%100>=11&&n%100<=14?many:n%10===1?one:n%10>=2&&n%10<=4?few:many;return `${n} ${ending}`; }
function currencySymbol(c) { return c==='KZT'?'₸':c==='USD'?'$':c==='EUR'?'€':c; }
function amount(value,c='KZT',decimals=false) { const exp=session?.public.manifest.currency_exponents[c] ?? 2; return new Intl.NumberFormat('ru-RU',{minimumFractionDigits:decimals?exp:0,maximumFractionDigits:exp}).format(value/10**exp)+' '+currencySymbol(c); }
function amountSize(text) { return text.length>16?'amount-xs':text.length>11?'amount-sm':''; }
function date(value,withYear=false) { if(!value)return '—'; return new Intl.DateTimeFormat('ru-RU',{day:'2-digit',month:'short',...(withYear?{year:'numeric'}:{}),timeZone:session?.public.manifest.timezone || 'Asia/Almaty'}).format(new Date(value.includes('T')?value:value+'T12:00:00+05:00')); }
function period() { const m=session.public.manifest; return `${date(m.period_start,true)} — ${date(new Date(new Date(m.period_end)-1).toISOString(),true)}`; }
function label(id) { return labels.get(id) || id; }
function short(id) { return id?String(id).slice(-10):'—'; }
function dialog(id) { const el=$('#'+id); if(!el.open)el.showModal(); }
function currentInfo() { return cases.find(c=>c.id===currentCase) || cases[0]; }
function rememberState(value) { session=value; if(value.result)draft=value.result.submitted_findings; buildIndex();saveToken(); }
function buildIndex() {
  labels=new Map([['policy','Учётная политика']]); recordIndex=new Map();
  const keys={ledger:['row_id','Запись'],invoices:['invoice_id','Счёт'],payments:['payment_id','Платёж'],fx:['record_id','Курс']};
  for(const [table,[key,prefix]] of Object.entries(keys))session.public[table].forEach((row,i)=>{
    const name=`${prefix} ${i+1}`;
    labels.set(row[key],name);recordIndex.set(row[key],{table,row,id:row[key]});
    if(row.doc_id){labels.set(row.doc_id,name+' · документ');recordIndex.set(row.doc_id,{table,row,id:row[key]});}
  });
  recordIndex.set('policy',{table:'policy',row:session.public.policy,id:'policy'});
}
async function startCase(id, mode='start') {
  if(busy)return; stopReplay();replayIndex=null;busy=true;
  repairState=null;repairPending=false;repairProofOpen=false;repairError='';
  $('#case-dialog').close();$('#lab-dialog').close(); $('#app-content').hidden=true;$('#loading').hidden=false;
  $('#loading').innerHTML='<span class="spinner"></span> Открываем учебную книгу…';
  try { const value=await api('/api/sessions',id==='generated'?{case:id,families:labFamilies}:{case:id}); currentCase=id; draft=[];selectedProof.clear();tab='invoices';query='';account='';viewMode=mode;guideStep=0;answers={};recordsOpen=false;usedHints=mode==='guided';rememberState(value);render(); }
  catch(error){$('#loading').innerHTML=`<div><p>${esc(error.message)}</p><button class="button primary" data-command="retry">Повторить подключение</button></div>`;errorMessage(error);}
  finally{busy=false;}
}
function renderCases() {
  $('#case-list').innerHTML=cases.map(c=>`<button class="case-option ${c.id===currentCase?'current':''}" data-case="${esc(c.id)}"><span class="case-tag">${c.id==='masked_both'?'Есть пошаговый разбор':esc(c.level)}</span><h3>${esc(c.title)}</h3><p>${esc(c.subtitle)}</p><span class="case-foot">${c.id===currentCase?'Начать новую попытку':'Открыть кейс'}</span></button>`).join('');
}
function caseContext() {
  return `<div class="case-context"><div><span class="eyebrow">ЭПИЗОД СИМУЛЯЦИИ</span><strong>${esc(currentInfo().title)}</strong><span class="context-company">${esc(session.public.manifest.company_name)}</span></div><button class="button secondary compact" data-command="cases">Сценарии</button></div>`;
}
function render() {
  if($('.all-records'))recordsOpen=$('.all-records').open;
  if($('.repair-proof'))repairProofOpen=$('.repair-proof').open;
  saveToken();
  $('#loading').hidden=true;$('#app-content').hidden=false;
  let content;
  if(viewMode==='start')content=renderIntro();
  else if(session.result)content=renderResult()+(currentCase==='masked_both'?renderRepairLab(true):'')+`<details class="panel all-records" ${recordsOpen?'open':''}><summary>Вернуться к документам кейса</summary>${recordsPanel()}</details>`;
  else if(viewMode==='guided'&&currentCase==='masked_both'&&buildLesson(session.public))content=renderGuided();
  else content=renderManual();
  $('#app-content').innerHTML=(viewMode==='start'?'':caseContext())+content;
  if($('#table-content'))renderTable();
  renderCases();
}
function renderIntro() {
  const p=session.public,reference=session.result?.reference,frames=replayFrames(reference);
  const index=replayIndex===null?Math.max(0,frames.length-1):Math.min(replayIndex,Math.max(0,frames.length-1));
  const complete=frames.length>0&&index===frames.length-1,frame=frames[index];
  const masking=currentCase==='masked_both'?buildLesson(p):null;
  const total=recognitionTotal(p),findings=complete?session.result.review.map((item,index)=>({...item,reviewIndex:index})).filter(item=>item.found):[];
  const totalText=amount(total);
  const heroTitle=currentCase==='masked_both'?'Итог верный.<br><em>Учёт — нет.</em>':'Новая книга.<br><em>Новая проверка.</em>';
  const heroCopy=currentCase==='masked_both'?'<strong>Это тренажёр для ИИ-аудитора.</strong><br>В общем итоге ошибки незаметны: они компенсируют друг друга. Аудитор должен найти обе и показать документы.':'<strong>Создайте компанию — проверьте аудитора.</strong><br>Агент получит документы и попробует найти ошибки. Оценщик засчитает только выводы с правильными суммами и доказательствами.';
  const title=currentCase==='masked_both'?'Две ошибки скрывают друг друга.':currentCase==='generated'?'Новая компания. Проверим её учёт.':currentInfo().title;
  const subtitle=currentCase==='masked_both'?'Одна ошибка завышает выручку, другая занижает её на ту же сумму. Найдёт ли аудитор обе по документам?':'Агент получит счета, платежи и проводки. Оценщик проверит каждую заявленную ошибку и её доказательства.';
  return `<div class="arena-presentation"><section class="arena-hero"><div class="eyebrow"><span class="hero-spark" aria-hidden="true">✳</span> ПОЛИГОН ИИ-АУДИТОРОВ</div><h1>${heroTitle}</h1><p>${heroCopy}</p><div class="hero-actions"><button class="button primary" data-command="${reference?'replay-end':'arena'}" ${arenaRunning?'disabled':''}>${arenaRunning?'Выполняется запуск…':reference?'К результату':'Запустить аудитора'} <span aria-hidden="true">↗</span></button><button class="button text" data-command="about">Что здесь создано <span aria-hidden="true">→</span></button></div><div class="hero-caption"><span class="caption-line"></span><p>Агент читает документы → находит расхождения → подаёт доказательства.</p></div><button class="purpose-note" data-command="repair-lab"><span class="purpose-icon" aria-hidden="true">⌘</span><span><strong>Теперь вы меняете историю</strong><small>Исправьте ошибку и посмотрите на результат</small></span><span class="purpose-arrow" aria-hidden="true">↗</span></button></section>
  <section class="experiment ${complete?'revealed':''}" aria-label="Эксперимент с финансовой книгой"><div class="experiment-heading"><div><span class="experiment-tag">${currentCase==='masked_both'?'ЭКСПЕРИМЕНТ 1 / ПРОВЕРКА АУДИТОРА':currentCase==='generated'?'НОВАЯ УЧЕБНАЯ КОМПАНИЯ':'ПРОВЕРКА АУДИТОРА'} / ${esc(p.manifest.company_name)}</span><h2>${esc(title)}</h2><p>${esc(subtitle)}</p></div><div class="experiment-choice"><button class="button secondary compact" data-command="cases">Сценарии</button><button class="button lab-button" data-command="lab">+ Новая компания</button></div></div>
  <div class="evidence-stage"><svg class="stage-wires" viewBox="0 0 1100 310" preserveAspectRatio="none" aria-hidden="true"><path d="M210 92 C350 92 340 151 480 151"/><path d="M210 226 C350 226 340 151 480 151"/><path d="M620 151 C760 151 710 92 870 92"/><path d="M620 151 C760 151 710 226 870 226"/><circle cx="480" cy="151" r="5"/><circle cx="620" cy="151" r="5"/></svg>
  <div class="stage-sources"><div class="stage-label">ПЕРВИЧНЫЕ ДОКУМЕНТЫ</div>${p.invoices.slice(0,2).map((doc,i)=>`<button class="source-slip ${frame?.table==='invoices'?'is-active':''}" data-record="${esc(doc.invoice_id)}"><span class="slip-icon">▤</span><span>${esc(label(doc.invoice_id))}<strong class="${amountSize(amount(doc.total_minor,doc.currency))}">${esc(amount(doc.total_minor,doc.currency))}</strong><small>${esc(date(doc.issued_at,true))}</small></span><span class="slip-corner">${i+1}</span></button>`).join('')}${p.invoices.length>2?`<span class="more-sources">И ещё ${p.invoices.length-2} счетов · ${p.payments.length} платежей</span>`:''}</div>
  <div class="balance-core ${frame?.table==='ledger'?'is-active':''}"><span class="core-orbit" aria-hidden="true"></span><span class="core-axis axis-one" aria-hidden="true"></span><span class="core-axis axis-two" aria-hidden="true"></span><span class="book-brand" aria-hidden="true">genesis<span>↗</span></span><span class="balance-label">ВЫРУЧКА ПО КНИГЕ</span><strong class="${amountSize(totalText)}">${esc(totalText)}</strong><span class="balance-status">${complete?(reference.evaluation.fn||reference.evaluation.fp?'В заключении есть пропуски или ложные обвинения':findings.length?'Итог не раскрыл все ошибки':'Документы подтвердили учёт'):'Сумма видна. Корректность нужно доказать.'}</span><span class="core-records">${counted(p.ledger.length,'запись','записи','записей')} <span>·</span> ${esc(p.manifest.base_currency)}</span></div>
  <div class="stage-verdict"><div class="stage-label">${complete?(findings.length?'ДОКАЗАННЫЕ РАСХОЖДЕНИЯ':'РЕЗУЛЬТАТ ПРОВЕРКИ'):'ВЕРДИКТ ОЦЕНЩИКА'}</div>${complete?(findings.length?findings.slice(0,4).map((finding,i)=>`<button class="verdict-slip" data-review="${finding.reviewIndex}"><span class="verdict-mark">✓</span><span>${esc(TYPES[finding.type])}<strong class="${amountSize(amount(finding.claimed_amount_minor,finding.currency))}">${esc(amount(finding.claimed_amount_minor,finding.currency))}</strong><small>Открыть доказательства</small></span></button>`).join(''):`<div class="verdict-clear"><span>✓</span><strong>${reference.evaluation.fn?'Нет доказанных находок':'Книга корректна'}</strong><p>Пропущено: ${reference.evaluation.fn} · ложных: ${reference.evaluation.fp}</p></div>`):`<div class="sealed-verdict"><span class="lock-glyph">⌕</span><strong>Что скрыто за итогом?</strong><p>${reference?'Просматриваем фактические действия агента.':'Агенту доступны документы. Правильный ответ остаётся у оценщика.'}</p></div>`}</div></div>
  ${complete&&masking?`<div class="cancellation-reveal"><span><small>Повторное признание</small><strong>+${esc(amount(masking.duplicateAmount))}</strong></span><b>+</b><span><small>Неверный курс</small><strong>−${esc(amount(masking.fxAmount))}</strong></span><b>=</b><span class="unchanged"><small>Изменение общего итога</small><strong>0 ₸</strong></span><p>Ошибки скрыли друг друга. Агент доказал каждую отдельно.</p></div>`:''}
  <div class="experiment-controls"><div class="run-description" role="status"><span class="run-dot ${reference?'finished':''}"></span><div><strong>${arenaRunning?'Аудитор проверяет документы…':reference?complete?'Расследование завершено':'Воспроизведение завершённого запуска':'Готов к испытанию'}</strong><small>${reference?`${reference.evaluation.total_steps} действий · ${reference.elapsed_seconds.toFixed(2)} с фактического выполнения`:'Демо: скриптовый аудитор · без внешних API'}</small></div></div><div class="run-actions">${reference?`<button class="button secondary" data-command="replay-toggle">${replayTimer?'Приостановить':complete?'Повторить запись':'Продолжить запись'}</button>${!complete?'<button class="button primary" data-command="replay-end">Показать вердикт</button>':currentCase==='masked_both'?'<button class="button primary" data-command="repair-lab">Исправить ошибки <span aria-hidden="true">→</span></button>':'<button class="button primary" data-command="report">Детальный разбор</button>'}`:`<button class="button primary run-button" data-command="arena" ${arenaRunning?'disabled':''}>${arenaRunning?'<span class="spinner"></span> Выполняется запуск':'Запустить аудитора'}</button>`}</div></div>
  ${reference?`<div class="replay-track" aria-label="Фактические действия агента">${frames.map((item,i)=>`<button class="replay-frame ${i===index?'active':i<index?'passed':''}" data-frame="${i}" aria-current="${i===index?'step':'false'}"><span>${i===0?'●':item.terminal?'✓':item.step}</span><strong>${esc(item.title)}</strong></button>`).join('')}</div><div class="replay-caption" role="status"><span>ЗАПИСЬ ДЕЙСТВИЙ · ${index+1}/${frames.length}</span><p>${esc(frame.detail)}</p></div>`:''}</section></div>
  ${complete?renderPolicyComparison(reference):`<div class="experiment-question"><span>ЧТО ПРОВЕРЯЕМ</span><p>${currentCase==='masked_both'?'Совпадение общей суммы ещё не означает правильный учёт. Аудитор должен доказать каждую ошибку отдельно.':'Засчитываются только находки с правильной суммой и документами. На чистой книге правильный ответ — отсутствие ошибок.'}</p></div>`}
  ${renderRepairLab(Boolean(session.result)&&(!reference||complete))}
  <section class="process-section"><div class="process-heading"><div><span class="eyebrow">ОТ ДОКУМЕНТА К ДОКАЗАТЕЛЬСТВУ</span><h2>Целый финансовый мир.<br><em>Одна проверяемая история.</em></h2></div><p>Genesis — среда для обучения и оценки агентов. Каждый запуск проходит один и тот же путь.</p></div><div class="system-path" aria-label="Что делает наш проект"><div><span>01 / СОЗДАЁМ <b aria-hidden="true">↗</b></span><h3>Финансовый мир</h3><p>Вымышленная компания. Согласованные счета, платежи и проводки.</p></div><div><span>02 / ИСКАЖАЕМ <b aria-hidden="true">↗</b></span><h3>Контролируемые ошибки</h3><p>Четыре семейства ошибок. Взаимодействия. Чистые контрольные книги.</p></div><div><span>03 / ИСПЫТЫВАЕМ <b aria-hidden="true">↗</b></span><h3>Агент расследует</h3><p>Читает документы, проверяет связи и подаёт выводы с доказательствами.</p></div><div><span>04 / ОЦЕНИВАЕМ <b aria-hidden="true">✓</b></span><h3>Награда за точность</h3><p>Независимый оценщик учитывает находки, пропуски и ложные обвинения.</p></div></div></section>
  <section class="arena-bottom"><div><span class="eyebrow">ВАШ СЛЕДУЮЩИЙ ЭКСПЕРИМЕНТ</span><h2>А если поменять<br><em>правила игры?</em></h2><p>Другие суммы, документы и комбинации ошибок.<br>Создайте компанию — и проверьте агента снова.</p><button class="button primary" data-command="lab">Открыть лабораторию <span aria-hidden="true">↗</span></button></div><div class="human-invite"><span class="human-icon" aria-hidden="true">⌕</span><h3>А вы бы нашли?</h3><p>Откройте документы и проведите собственное расследование. Оценщик одинаково проверяет выводы человека и агента.</p><button class="button text" data-command="manual">Проверить документы самому <span aria-hidden="true">→</span></button>${currentCase==='masked_both'?'<button class="button text" data-command="guided">Учебный разбор с подсказками <span aria-hidden="true">→</span></button>':''}</div></section>`;
}
function renderRepairLab(unlocked) {
  if(currentCase!=='masked_both')return `<section class="repair-invite"><div><span class="eyebrow">ЕЩЁ ОДИН ЭКСПЕРИМЕНТ</span><h2>Почему исправление меняет картину?</h2><p>На примере двух маскирующихся ошибок можно убрать каждую отдельно и сравнить результат.</p></div><button class="button secondary" data-command="masking-demo">Открыть пример <span aria-hidden="true">→</span></button></section>`;
  if(!unlocked)return `<section class="repair-lab repair-locked" id="repair-lab" aria-label="Лаборатория исправлений"><div class="repair-heading"><div><span class="eyebrow">ЭКСПЕРИМЕНТ 2 / ВЫ МЕНЯЕТЕ ПРИЧИНУ</span><h2>Исправьте одну ошибку.<br><em>Посмотрите, что станет видно.</em></h2><p>После проверки сможете убрать дубль счёта, исправить курс или сделать оба действия. Мы пересчитаем книгу и заново проверим ошибки.</p></div><div class="repair-lock"><span aria-hidden="true">⑂</span><strong>Откроется после результата</strong><button class="button primary" data-command="arena" ${arenaRunning?'disabled':''}>${arenaRunning?'Аудитор уже проверяет…':'Сначала запустить аудитора'} <span aria-hidden="true">→</span></button></div></div></section>`;
  const originalTotal=recognitionTotal(session.public),originalCount=session.result.review.length;
  const after=repairState?.after||{revenue_minor:originalTotal,issue_count:originalCount};
  const fixed=repairState?.fixed||{duplicate_invoice:false,fx_conversion:false};
  const expected=repairState?.expected_revenue_minor;
  const clean=Boolean(repairState&&after.issue_count===0);
  const note=!repairState?'Включите любое исправление. Мы изменим записи и заново проверим, какие ошибки остались.':clean?'Теперь совпадает не только итог: независимая проверка не нашла ошибок.':repairState.total_matches_documents?'Итог совпадает с документами, но две ошибки снова маскируют друг друга.':fixed.duplicate_invoice?'Дубль убрали. Выручка уменьшилась — теперь видно, что неверный курс занижает её.':'Курс исправили. Выручка выросла — теперь видно, что дубль завышает её.';
  return `<section class="repair-lab ${clean?'repair-clean':''}" id="repair-lab" aria-label="Лаборатория исправлений"><div class="repair-heading"><div><span class="eyebrow">ЭКСПЕРИМЕНТ 2 / А ЧТО, ЕСЛИ ИСПРАВИТЬ?</span><h2>Исправьте одну ошибку.<br><em>Посмотрите, что станет видно.</em></h2><p>Это та же компания и те же документы. Меняются только выбранные записи в копии книги.</p></div><span class="repair-verified">✓ Проверяем после каждого изменения</span></div><div class="repair-switches" aria-label="Выберите исправления"><button class="repair-switch" data-repair="duplicate" aria-pressed="${fixed.duplicate_invoice}" ${repairPending?'disabled':''}><span class="switch-track" aria-hidden="true"></span><span><strong>Убрать дубль счёта</strong><small>Оставить одно признание вместо двух</small></span></button><button class="repair-switch" data-repair="fx" aria-pressed="${fixed.fx_conversion}" ${repairPending?'disabled':''}><span class="switch-track" aria-hidden="true"></span><span><strong>Исправить валютный курс</strong><small>Пересчитать по курсу на дату счёта</small></span></button></div><div class="repair-scoreboard" aria-live="polite" aria-busy="${repairPending}"><article><span>До исправлений</span><strong>${esc(amount(originalTotal))}</strong><small>${counted(originalCount,'ошибка','ошибки','ошибок')} внутри книги</small></article><span class="repair-arrow" aria-hidden="true">→</span><article class="repair-after"><span>После выбранных исправлений</span><strong>${esc(amount(after.revenue_minor))}</strong><small>${counted(after.issue_count,'ошибка осталась','ошибки остались','ошибок осталось')}</small></article><div class="repair-check"><span class="repair-check-icon" aria-hidden="true">${clean?'✓':after.issue_count}</span><strong>${repairPending?'Пересчитываем книгу…':clean?'Учёт подтверждён':after.issue_count?'В книге есть ошибки':'Готово'}</strong>${expected!==undefined?`<small>По первичным счетам: ${esc(amount(expected))}</small>`:'<small>Результат независимого проверяющего</small>'}</div></div><p class="repair-insight" role="status">${repairPending?'Меняем выбранные записи и проверяем оставшиеся ошибки…':esc(note)}</p>${repairError?`<p class="form-error" role="alert">${esc(repairError)}</p>`:''}${repairState?`<details class="repair-proof" ${repairProofOpen?'open':''}><summary>Какие записи в книге изменились <span>${counted(repairState.changed_rows.length,'запись','записи','записей')}</span></summary>${repairState.changed_rows.length?`<div class="table-scroll"><table class="records-table"><thead><tr><th>Запись</th><th>Было</th><th>Стало</th></tr></thead><tbody>${repairState.changed_rows.map(row=>`<tr><td>${esc(label(row.row_id))}<small class="subcell">${esc(ACCOUNTS[row.account]||row.account)}</small></td><td>${esc(amount(row.before_debit_minor+row.before_credit_minor))}</td><td>${row.operation==='removed'?'Дубль удалён':esc(amount(row.after_debit_minor+row.after_credit_minor))}</td></tr>`).join('')}</tbody></table></div>`:'<p>Исходная книга восстановлена. Ни одного исправления не применено.</p>'}</details>`:''}<div class="repair-footer"><p>Счета и курсы в первичных документах сохраняются. Оценка исходной попытки не меняется.</p><button class="button text" data-repair="reset" ${repairPending?'disabled':''}>Вернуть обе ошибки <span aria-hidden="true">↶</span></button></div></section>`;
}
async function runRepair(kind) {
  if(repairPending||!session?.result||!['duplicate','fx','reset'].includes(kind))return;
  const token=session.session_id,previous=repairState?.fixed||{};
  const flags={fix_duplicate:kind==='reset'?false:kind==='duplicate'?!previous.duplicate_invoice:Boolean(previous.duplicate_invoice),fix_fx:kind==='reset'?false:kind==='fx'?!previous.fx_conversion:Boolean(previous.fx_conversion)};
  repairPending=true;repairError='';render();
  try{const report=await api(`/api/sessions/${token}/counterfactual`,flags);if(session?.session_id===token)repairState=report;}
  catch(error){if(session?.session_id===token)repairError=error.message||'Не удалось пересчитать книгу. Повторите действие.';}
  finally{if(session?.session_id===token){repairPending=false;render();document.querySelector('[data-repair="'+kind+'"]')?.focus({preventScroll:true});}}
}
function rewardSVG(value,kind) {
  const x=200+180*Math.max(-1,Math.min(1,value));
  return `<svg class="reward-axis ${kind}" viewBox="0 0 400 55" role="img" aria-label="Награда ${value.toFixed(3)}"><line class="axis-background" x1="20" x2="380" y1="20" y2="20"/><line class="axis-value" x1="200" x2="${x}" y1="20" y2="20"/><circle cx="${x}" cy="20" r="6"/><line class="axis-zero" x1="200" x2="200" y1="9" y2="31"/><text x="20" y="50">−1</text><text x="200" y="50" text-anchor="middle">0</text><text x="380" y="50" text-anchor="end">+1</text></svg>`;
}
function renderPolicyComparison(reference) {
  const baseline=reference.comparison;
  if(!baseline)return `<div class="comparison-note">Открыт прежний одиночный запуск. Для сравнения политик начните новый эпизод.</div>`;
  const rows=[{title:'Не сообщить об ошибках',policy:'silent',report:baseline,kind:'baseline'}, {title:'Доказать ошибки документами',policy:'simple',report:reference,kind:'supported'}];
  return `<section class="policy-comparison"><div class="comparison-heading"><div><div class="eyebrow">ОДНА КОМПАНИЯ / ДВЕ ПОЛИТИКИ / ОДИН ОЦЕНЩИК</div><h2>За что аудитор получил награду?</h2></div><span>Результат одного эпизода</span></div><div class="policy-cards">${rows.map(({title,policy,report,kind})=>{const e=report.evaluation;return `<article class="policy-card ${kind}"><div><span class="policy-name">${kind==='supported'?'ПРОВЕРКА ДОКУМЕНТОВ':'ПУСТОЙ ОТВЕТ'}</span><h3>${title}</h3></div><div class="policy-reward"><strong>${e.reward>0?'+':''}${e.reward.toFixed(3).replace('.',',')}</strong><span>НАГРАДА ОТ −1 ДО +1</span></div>${rewardSVG(e.reward,kind)}<div class="policy-facts"><span><b>${e.tp}</b> доказано</span><span><b>${e.fn}</b> пропущено</span><span><b>${e.fp}</b> ложных</span><span>F1 <b>${Math.round(e.f1*100)}%</b></span></div></article>`;}).join('')}</div><p class="reward-explanation">${baseline.evaluation.fn?`Пустое заключение пропустило ${counted(baseline.evaluation.fn,'ошибку','ошибки','ошибок')}. Аудитор подтвердил находки документами и получил более высокую награду.`:'В корректной книге ошибок нет: обе политики это подтвердили. Чтение инструментов немного уменьшает награду.'} Такой сигнал можно использовать при обучении модели; текущий показ выполняется скриптами.</p><button class="button text" data-command="report">Открыть подробный разбор <span aria-hidden="true">→</span></button></section>`;
}
function stopReplay() {if(replayTimer!==null){clearInterval(replayTimer);replayTimer=null;}}
function playReplay() {
  stopReplay();const frames=replayFrames(session.result?.reference);if(!frames.length)return;
  if(replayIndex===null||replayIndex>=frames.length-1)replayIndex=0;
  if(matchMedia('(prefers-reduced-motion: reduce)').matches){replayIndex=frames.length-1;render();return;}
  replayTimer=setInterval(()=>{if(viewMode!=='start'){stopReplay();return;}replayIndex++;if(replayIndex>=frames.length-1)stopReplay();render();},1100);render();
}
async function runArena() {
  if(busy)return;
  if(session.result?.reference){replayIndex=0;playReplay();return;}
  if(session.result)await startCase(currentCase);
  busy=true;arenaRunning=true;render();
  if(matchMedia('(max-width:900px)').matches)$('.experiment')?.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth',block:'start'});
  try{const value=await api(`/api/sessions/${session.session_id}/arena`,{});rememberState(value);replayIndex=0;busy=false;arenaRunning=false;playReplay();}
  catch(error){busy=false;arenaRunning=false;errorMessage(error);render();}
}
function openLab() {$$('#lab-form input[name=family]').forEach(input=>input.checked=labFamilies.includes(input.value));dialog('lab-dialog');}
function heading(title, subtitle) {return `<div class="page-heading"><div><h1>${title}</h1><p>${subtitle}</p></div><button class="button text compact" data-command="modes">Выбрать режим</button></div>`;}
function recordsPanel() {
  const p=session.public;
  return `<section class="panel records-panel"><div class="panel-heading"><div><h2>Материалы проверки</h2><p>Нажмите название документа, чтобы увидеть детали и связи.</p></div></div><div class="tabs" role="tablist" aria-label="Материалы проверки">${Object.entries(TABLES).filter(([key])=>key!=='payments'||p.payments.length).map(([key,title])=>`<button class="tab ${tab===key?'active':''}" role="tab" aria-selected="${tab===key}" aria-controls="table-content" id="tab-${key}" data-tab="${key}">${title}${key!=='policy'?`<span class="tab-count">${p[key].length}</span>`:''}</button>`).join('')}</div><div id="table-content" role="tabpanel" aria-labelledby="tab-${tab}"></div></section>`;
}
function activityPanel() {return `<details class="technical-details activity"><summary>Действия и правила оценки <span>${session.remaining_steps} из 100 действий осталось</span></summary><p>Четыре таблицы прочитаны при открытии книги. Открытие первичного документа также учитывается. Награда уменьшается на 0,002 за каждое обращение. На попытку — 30 минут.</p><div class="activity-content">${session.events.map(e=>`<div class="activity-row"><b>${e.step}</b><span>${eventLabel(e)}</span></div>`).join('')}</div></details>`;}
function renderManual() {
  return heading('Проверьте документы и составьте вывод.','Начните со счетов. Затем посмотрите, как каждый счёт отражён в книге.')+`<div class="task-note"><span class="note-icon">i</span><p><strong>Что считать ошибкой?</strong> Счёт учтён дважды, использован неверный курс, выбран другой месяц или платёж распределён не полностью. Для каждого вывода нужны документы.</p></div><div class="workbench-grid"><div>${recordsPanel()}${activityPanel()}</div>${renderFindings()}</div>`;
}
function miniDocument(doc, caption) {
  return `<article class="document-card"><div class="document-card-top"><span class="file-icon">▤</span><span>${caption}</span></div><h3>${esc(label(doc.invoice_id))}</h3><div class="document-amount">${esc(amount(doc.total_minor,doc.currency))}</div><div class="document-date">Выставлен ${esc(date(doc.issued_at,true))}</div><button class="button secondary compact" data-record="${esc(doc.invoice_id)}">Открыть документ</button></article>`;
}
function quizOptions(key, question, options, correct, success) {
  const answered=answers[key];
  return `<div class="quiz"><h3>${question}</h3><div class="quiz-options">${options.map(option=>`<button class="quiz-option ${answered===String(option.value)?answered===String(correct)?'correct':'incorrect':''}" data-quiz="${key}" data-answer="${esc(option.value)}" aria-pressed="${answered===String(option.value)}">${esc(option.label)}</button>`).join('')}</div>${answered!==undefined?`<p class="quiz-feedback ${answered===String(correct)?'success':'retry'}" role="status">${answered===String(correct)?success:'Посмотрите на исходный документ и расчёт выше. Попробуйте ещё раз.'}</p>`:''}</div>`;
}
function renderGuided() {
  const l=buildLesson(session.public),titles=['Исходный счёт','Сверка с книгой','Проверка курса','Ваше заключение'];
  let body;
  if(guideStep===0)body=`<div class="lesson-intro"><span class="lesson-kicker">ШАГ 1 ИЗ 4</span><h2>Начните с исходного счёта.</h2><p>Счёт — это документ о поставке. В нём указано, сколько покупатель должен компании. Именно эту сумму нужно сверить с книгой.</p></div><div class="lesson-example">${miniDocument(l.native,'Исходный документ')}<div class="explainer"><h3>Запомните три факта</h3><ul><li>Сумма: <strong>${esc(amount(l.native.total_minor))}</strong></li><li>Валюта: <strong>тенге</strong></li><li>Дата: <strong>${esc(date(l.native.issued_at,true))}</strong></li></ul><p>Теперь посмотрим, как бухгалтер записал этот счёт.</p></div></div><div class="lesson-actions"><span>Пока только читаем документы</span><button class="button primary" data-next="1">Сравнить с книгой</button></div>`;
  else if(guideStep===1)body=`<div class="lesson-intro"><span class="lesson-kicker">ШАГ 2 ИЗ 4</span><h2>Один счёт. Сколько записей?</h2><p>В книге найдены записи о долге покупателя по тому же счёту. Сравните их общую сумму с документом.</p></div><div class="comparison"><div class="compare-source"><span>В исходном счёте</span><strong>${esc(amount(l.native.total_minor))}</strong><button class="text-button" data-record="${esc(l.native.invoice_id)}">${esc(label(l.native.invoice_id))}</button></div><div class="compare-book"><span>В книге по этому счёту</span><strong>${esc(amount(l.recorded))}</strong><div class="posting-links">${l.rows.map(r=>`<button data-record="${esc(r.row_id)}">${esc(label(r.row_id))} · ${esc(amount(r.debit_minor))}</button>`).join('')}</div></div></div><div class="calculation"><span>Лишняя сумма</span><strong>${esc(amount(l.recorded))} − ${esc(amount(l.native.total_minor))} = <em>${esc(amount(l.duplicateAmount))}</em></strong></div>${quizOptions('duplicate','Какую сумму нужно указать как ошибку?',[{value:l.duplicateAmount,label:amount(l.duplicateAmount)},{value:l.recorded,label:amount(l.recorded)},{value:0,label:'Ошибки нет'}],l.duplicateAmount,'Верно. Первое признание счёта корректно; повторная запись добавила лишнюю сумму.')}<div class="lesson-actions"><button class="button text" data-next="0">К исходному счёту</button><button class="button primary" data-guide-add="duplicate" ${answers.duplicate!==String(l.duplicateAmount)?'disabled':''}>Добавить ошибку и продолжить</button></div>`;
  else if(guideStep===2)body=`<div class="lesson-intro"><span class="lesson-kicker">ШАГ 3 ИЗ 4</span><h2>Теперь проверьте валютный счёт.</h2><p>По правилам компании сумму нужно пересчитать по курсу на дату счёта. Посмотрите на документ и опубликованный курс.</p></div><div class="comparison"><div class="compare-source"><span>${esc(label(l.foreign.invoice_id))} · ${esc(date(l.foreign.issued_at))}</span><strong>${esc(amount(l.foreign.total_minor,l.foreign.currency))}</strong><button class="text-button" data-record="${esc(l.foreign.invoice_id)}">Открыть счёт</button></div><div class="compare-book"><span>Курс на дату счёта</span><strong>${esc(l.rate.rate)} ₸ <small>за 1 ${esc(l.foreign.currency)}</small></strong><button class="text-button" data-record="${esc(l.rate.record_id)}">Открыть источник курса</button></div></div><div class="valuation-lines"><div><span>Должно быть по документам</span><strong>${esc(amount(l.expected))}</strong></div><div><span>Отражено в книге · курс ${esc(l.foreignRow.applied_fx_rate)}</span><strong>${esc(amount(l.foreignRow.debit_minor+l.foreignRow.credit_minor))}</strong></div><div class="difference-line"><span>Разница</span><strong>${esc(amount(l.fxAmount))}</strong></div></div>${quizOptions('fx','Какой курс подходит для этого счёта?',[{value:l.rate.rate,label:l.rate.rate+' ₸'},{value:l.foreignRow.applied_fx_rate,label:l.foreignRow.applied_fx_rate+' ₸'}],l.rate.rate,'Верно. Курс выбирают по дате исходного документа. Оценка в книге занижена на сумму разницы.')}<div class="lesson-actions"><button class="button text" data-next="1">К предыдущему шагу</button><button class="button primary" data-guide-add="fx" ${answers.fx!==String(l.rate.rate)?'disabled':''}>Добавить ошибку в заключение</button></div>`;
  else body=`<div class="lesson-intro"><span class="lesson-kicker">ШАГ 4 ИЗ 4</span><h2>Вы собрали доказательства.</h2><p>Проверьте список найденных ошибок. К каждой ошибке уже приложены исходные документы и связанные записи.</p></div><div class="lesson-recap"><span class="recap-check">✓</span><div><h3>${counted(draft.length,'ошибка добавлена','ошибки добавлены','ошибок добавлено')} в заключение</h3><p>Оценщик проверит тип нарушения, сумму и документы. Результат появится после отправки ответа.</p></div></div><p class="teaching-note">Это разбор с подсказками. Полученный балл отражает учебную попытку.</p><div class="lesson-actions"><button class="button text" data-next="2">Вернуться к расчётам</button><button class="button primary" data-command="submit">Проверить моё заключение</button></div>`;
  return heading('Разберём кейс вместе.','Четыре коротких шага — от документа до доказанного вывода.')+`<ol class="lesson-progress">${titles.map((title,i)=>`<li class="${i===guideStep?'current':i<guideStep?'complete':''}" ${i===guideStep?'aria-current="step"':''}><span>${i<guideStep?'✓':i+1}</span>${title}</li>`).join('')}</ol><div class="workbench-grid guided-grid"><section class="panel lesson-panel" id="lesson-panel">${body}</section>${renderFindings()}</div><details class="panel all-records" ${recordsOpen?'open':''}><summary>Открыть все материалы кейса</summary>${recordsPanel()}</details>`;
}
function eventLabel(e) { if(e.action==='list_records')return `Прочитана таблица «${TABLES[e.table]}» · ${e.records} записей`;if(e.action==='get_document')return `Открыт документ: ${esc(label(e.id))}`;return e.source==='reference'?'Проверен ответ скриптового аудитора':'Заключение передано оценщику'; }
function renderTable() {
  const root=$('#table-content');
  if(tab==='policy'){root.innerHTML=policyHTML();return;}
  const p=session.public;
  const headers={ledger:['Запись / дата','Что учтено','Документ','Дебет, ₸','Кредит, ₸'],invoices:['Счёт / дата','Контрагент','Валюта','Сумма'],payments:['Платёж / дата','Назначение','Валюта','Сумма'],fx:['Дата курса','Пара валют','Курс','Источник']};
  root.innerHTML=`<div class="table-tools"><label class="search-wrap"><span aria-hidden="true">⌕</span><input id="table-search" type="search" placeholder="Найти документ или запись…" aria-label="Поиск по текущей таблице" value="${esc(query)}"></label>${tab==='ledger'?`<select id="account-filter" aria-label="Вид записи"><option value="">Все записи</option>${[...new Set(p.ledger.map(r=>r.account))].map(a=>`<option value="${esc(a)}" ${account===a?'selected':''}>${esc(ACCOUNTS[a]||a)}</option>`).join('')}</select>`:''}</div><div id="rows-area"></div>`;
  renderRows(headers[tab]);
}
function renderRows(headers) {
  headers=headers||{ledger:['Запись / дата','Что учтено','Документ','Дебет, ₸','Кредит, ₸'],invoices:['Счёт / дата','Контрагент','Валюта','Сумма'],payments:['Платёж / дата','Назначение','Валюта','Сумма'],fx:['Дата курса','Пара валют','Курс','Источник']}[tab];
  const p=session.public;
  const rows=p[tab].filter(r=>(tab!=='ledger'||!account||r.account===account)&&(!query||[JSON.stringify(r),label(r.row_id||r.invoice_id||r.payment_id||r.record_id),label(r.invoice_id),label(r.payment_id)].join(' ').toLowerCase().includes(query.toLowerCase())));
  if(!rows.length){$('#rows-area').innerHTML=`<div class="table-empty"><div class="empty-icon">▤</div><h3>${p[tab].length?'Ничего не найдено':'В этой книге нет '+(tab==='payments'?'платежей':'записей')}</h3><p>${p[tab].length?'Измените запрос или сбросьте фильтр.':'Это свойство учебного кейса. Продолжите проверку счетов и проводок.'}</p></div>`;return;}
  $('#rows-area').innerHTML=`<div class="table-scroll"><table class="records-table"><thead><tr>${headers.map((h,i)=>`<th scope="col" class="${(tab==='ledger'&&i>=3)||(tab==='invoices'&&i===3)||(tab==='payments'&&i===3)?'num':''}">${h}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>tableRow(r)).join('')}</tbody></table></div><div class="table-bottom"><span>Показано ${rows.length} из ${p[tab].length} записей</span><span>Нажмите номер для деталей</span></div>`;
}
function recordButton(id,text) {return `<button class="record-button" data-record="${esc(id)}">${esc(text||label(id))}</button>`;}
function tableRow(r) {
  if(tab==='ledger')return `<tr><td>${recordButton(r.row_id)}<span class="subcell">${esc(date(r.timestamp,true))}</span></td><td><span class="account-chip" title="${esc(r.account)}">${esc(ACCOUNTS[r.account]||r.account)}</span></td><td>${recordButton(r.payment_id||r.invoice_id)}${r.source_currency!=='KZT'?`<span class="subcell">${esc(r.source_currency)} · курс ${esc(r.applied_fx_rate)}</span>`:''}</td><td class="num">${r.debit_minor?esc(amount(r.debit_minor,r.currency).replace(' ₸','')):'—'}</td><td class="num">${r.credit_minor?esc(amount(r.credit_minor,r.currency).replace(' ₸','')):'—'}</td></tr>`;
  if(tab==='invoices')return `<tr><td>${recordButton(r.invoice_id)}<span class="subcell">${esc(date(r.issued_at,true))}</span></td><td><span title="${esc(r.entity_name||r.entity_id)}">${esc(entityName(r))}</span></td><td><span class="currency-chip">${esc(r.currency)}</span></td><td class="num">${esc(amount(r.total_minor,r.currency))}</td></tr>`;
  if(tab==='payments')return `<tr><td>${recordButton(r.payment_id)}<span class="subcell">${esc(date(r.timestamp,true))}</span></td><td>${r.invoice_refs?.length?r.invoice_refs.map(id=>recordButton(id)).join(', '):'Не указано'}</td><td><span class="currency-chip">${esc(r.currency)}</span></td><td class="num">${esc(amount(r.amount_minor,r.currency))}</td></tr>`;
  return `<tr><td>${esc(date(r.date,true))}</td><td><span class="currency-chip">${esc(r.source_currency)} / ${esc(r.target_currency)}</span></td><td>${esc(r.rate)}</td><td>${recordButton(r.record_id)}</td></tr>`;
}
function entityName(r) { const entities=[...new Set(session.public.invoices.map(i=>i.entity_id))];return `Контрагент ${String(entities.indexOf(r.entity_id)+1).padStart(2,'0')}`; }
function policyHTML() {return `<article class="policy-view"><h3>Документ важнее совпавшей суммы</h3><p>Признание счёта — по дате выставления, платежа — по дате банковского документа. Убедитесь, что каждая операция попала в нужный период и правильно оценена.</p><dl><dt>Отчётный период</dt><dd>${esc(period())}, начало включено, конец исключён.</dd><dt>Курс валюты</dt><dd>На дату исходного документа в часовом поясе ${esc(session.public.manifest.timezone)}. Округление — к ближайшему чётному (half even).</dd><dt>Распределение</dt><dd>Если платёж однозначно адресован одному счёту, он должен быть распределён полностью. При неизвестном назначении нельзя угадывать счёт по сумме или дате.</dd><dt>Доказательства</dt><dd>До 10 ссылок на находку. Нужны первичные документы, соответствующие проводки, политика и курс, когда он участвует в расчёте.</dd><dt>Сумма нарушения</dt><dd>Всегда в валюте учёта (${esc(session.public.manifest.base_currency)}), без знака. В форме находки показано правило для каждого типа.</dd></dl><details><summary>Полная опубликованная политика</summary><pre class="raw-json">${esc(JSON.stringify(session.public.policy,null,2))}</pre></details></article>`;}
function renderFindings() {
  return `<aside class="panel findings-panel" aria-label="Ваше заключение"><div class="panel-heading"><div><h2>Ваше заключение</h2><p>Подтверждённые вами расхождения</p></div><span class="counter">${draft.length}</span></div><div class="findings-body">${draft.length?draft.map((f,i)=>`<article class="finding-card"><div class="finding-card-header"><span class="finding-number">${i+1}</span><h3>${esc(TYPES[f.type]||f.type)}</h3></div><div class="amount">${esc(amount(f.claimed_amount_minor,f.currency))}</div><p>Доказательств приложено: ${f.evidence_ids.length}</p><div class="finding-card-actions"><button class="text-button" data-edit="${i}">Изменить</button><button class="text-button muted" data-remove="${i}">Удалить</button></div></article>`).join(''):`<div class="findings-empty"><span class="empty-sheet">≡</span><h3>Здесь будут ваши выводы</h3><p>${viewMode==='guided'?'Продолжайте пошаговый разбор. Найденные расхождения появятся здесь.':'Добавьте ошибку, когда сможете подтвердить её документами.'}</p></div>`}<div class="findings-actions"><button class="button secondary wide" data-command="add">Добавить ошибку вручную</button>${viewMode!=='guided'||guideStep===3?'<button class="button primary wide" data-command="submit">Закончить и проверить</button>':''}</div>${viewMode!=='guided'?'<p class="quiet-note">Если всё учтено верно, отправьте заключение без ошибок.</p>':''}</div>${selectedProof.size?`<div class="proof-basket">В подборке ${counted(selectedProof.size,'документ','документа','документов')}. Они появятся в форме ошибки.</div>`:''}</aside>`;
}
function renderResult() {
  const result=session.result,e=result.reference?.evaluation||result.evaluation,perfect=e.f1===1;
  const missed=e.fn,unmatched=new Set(result.evaluation.matched_pairs.map(pair=>pair[0]));
  const falseFindings=result.submitted_findings.map((finding,index)=>({finding,index})).filter(item=>!unmatched.has(item.index));
  const title=e.status!=='scored'?'Ответ нужно оформить заново.':perfect?(result.review.length?'Все ошибки подтверждены.':'Вы верно проверили чистую книгу.'):'Проверка завершена. Посмотрим на расхождения.';
  const description=e.status!=='scored'?'Не удалось принять формат заключения. Начните новую попытку.':perfect?'Документы подтверждают ваше заключение.':`Часть ошибок осталась незамеченной или не получила достаточных доказательств. Разбор ниже поможет разобраться.`;
  const lesson=currentCase==='masked_both'?buildLesson(session.public):null;
  return `<section class="result-section" id="results"><div class="result-heading"><span class="result-check ${perfect?'':'partial'}">${perfect?'✓':'!'}</span><div><div class="eyebrow">${result.reference?'ПРИМЕР РАБОТЫ СКРИПТОВОГО АУДИТОРА':usedHints?'УЧЕБНАЯ ПОПЫТКА С ПОДСКАЗКАМИ':'ВАШ РЕЗУЛЬТАТ'}</div><h1>${title}</h1><p>${description}</p></div></div><div class="outcome-grid"><div class="outcome"><strong>${e.tp}</strong><span>Найдено и доказано</span></div><div class="outcome ${missed?'attention':''}"><strong>${e.fn}</strong><span>Пропущено ошибок</span></div><div class="outcome ${e.fp?'attention':''}"><strong>${e.fp}</strong><span>Не подтвердилось</span></div></div>${lesson?`<div class="masking-proof"><div><h2>Почему общей суммы оказалось мало?</h2><p>Повторный счёт прибавил ${esc(amount(lesson.duplicateAmount))}, неверный курс убрал столько же. Две ошибки скрылись в совпавшем итоге.</p></div><div class="masking-equation"><span class="positive">+${esc(amount(lesson.duplicateAmount))}<small>Лишнее признание</small></span><span class="operator">+</span><span class="negative">−${esc(amount(lesson.fxAmount))}<small>Неверный курс</small></span><span class="operator">=</span><span>0 ₸<small>Разница в итоге</small></span></div></div>`:''}<section class="panel review-panel"><div class="panel-heading"><div><h2>Что произошло в этой книге</h2><p>Откройте объяснение, чтобы сверить расчёт и документы.</p></div></div>${result.review.length?result.review.map((f,i)=>`<article class="review-item"><span class="review-type ${f.found?'':'missed'}">${f.found?'✓':'!'}</span><div><span class="review-status ${f.found?'':'missed'}">${f.found?'Вы нашли эту ошибку':'Эта ошибка пропущена'}</span><h3>${esc(TYPES[f.type])}</h3><p>${f.affected_ids.map(id=>esc(label(id))).join(', ')}</p></div><strong class="review-amount">${esc(amount(f.claimed_amount_minor,f.currency))}</strong><button class="button secondary compact" data-review="${i}">Понять ошибку</button></article>`).join(''):'<div class="clean-review"><strong>Нарушений нет.</strong><p>Счета и записи в книге согласованы. Пустое заключение — правильный ответ.</p></div>'}</section>${falseFindings.length?`<section class="panel unconfirmed"><div class="panel-heading"><div><h2>Что не подтвердилось в вашем ответе</h2><p>Проверьте сумму и полноту документов: одного подозрения недостаточно.</p></div></div>${falseFindings.map(({finding:f,index})=>`<article class="review-item"><span class="review-type missed">!</span><div><h3>${esc(TYPES[f.type]||f.type)}</h3><p>${esc(amount(f.claimed_amount_minor,f.currency))}</p></div><button class="button secondary compact" data-submitted="${index}">Посмотреть ответ</button></article>`).join('')}</section>`:''}<details class="technical-details score-details"><summary>Оценка в цифрах: F1 ${e.f1==null?'—':Math.round(e.f1*100)+'%'} · награда ${e.reward>0?'+':''}${e.reward.toFixed(3).replace('.',',')}</summary><p>F1 учитывает полноту и точность найденных ошибок. Обращений к инструментам: ${e.inspection_steps}. Каждое стоит 0,002 награды.</p>${result.reference?`<p>Реальный запуск скриптовой политики simple в отдельном процессе: ${result.reference.evaluation.total_steps} действий, ${result.reference.elapsed_seconds.toFixed(2)} с. На экране показана оценка этого запуска; оценка ручной попытки сохранена отдельно в JSON.</p>`:usedHints?'<p>Подсказки были включены. Этот результат относится к учебному разбору.</p>':''}</details><div class="result-controls"><div><button class="button primary" data-command="cases">Попробовать другой кейс</button><button class="button secondary" data-command="restart">Повторить этот кейс</button></div><button class="button text" data-command="download">Скачать результат JSON</button></div></section>`;
}
async function openRecord(id) {
  const found=recordIndex.get(id);if(!found)return toast('Запись не найдена.');
  busy=true;try{
    if(['invoices','payments'].includes(found.table)&&!session.result){const value=await api(`/api/sessions/${session.session_id}/document`,{id:found.row.doc_id});rememberState(value);render();}
    showRecord(found);
  }catch(error){errorMessage(error);}finally{busy=false;}
}
function showRecord({table,row:r,id}) {
  if(table==='policy'){$('#detail-content').innerHTML=`<div class="dialog-top"><div><span class="detail-tag">ПОЛИТИКА</span><h2>Учётные правила</h2></div><button class="icon-button" data-close="detail-dialog" aria-label="Закрыть документ">×</button></div>${policyHTML()}${proofButton('policy')}`;dialog('detail-dialog');return;}
  const isLedger=table==='ledger',isFx=table==='fx',isInvoice=table==='invoices',proofId=r.doc_id||r.row_id||r.record_id;
  const money=isFx?`1 ${esc(r.source_currency)} = ${esc(r.rate)} ${esc(r.target_currency)}`:esc(amount(isLedger?r.debit_minor+r.credit_minor:isInvoice?r.total_minor:r.amount_minor,r.currency));
  const entries=isLedger?[["Что учтено",ACCOUNTS[r.account]||r.account],["Дата записи",date(r.timestamp,true)],["Исходная валюта",r.source_currency],["Применённый курс",r.applied_fx_rate],["Дата курса",date(r.applied_fx_date,true)],["Распределено",r.allocated_minor==null?'—':amount(r.allocated_minor,r.source_currency)]]:isFx?[["Дата курса",date(r.date,true)],["Направление",`${r.source_currency} → ${r.target_currency}`]]:isInvoice?[["Выставлен",date(r.issued_at,true)],["Срок оплаты",date(r.due_at,true)],["Контрагент",entityName(r)],["Валюта",r.currency]]:[["Дата платежа",date(r.timestamp,true)],["Валюта",r.currency],["Назначение",r.invoice_refs?.length?r.invoice_refs.map(label).join(', '):'Не указано'],["Тип",r.amount_minor<0?'Возврат':'Поступление']];
  let related=[];
  if(isLedger){if(r.invoice_id)related.push(r.invoice_id);if(r.payment_id)related.push(r.payment_id);related.push(...session.public.ledger.filter(x=>x.journal_id===r.journal_id&&x.row_id!==id).map(x=>x.row_id));}
  else if(!isFx)related=session.public.ledger.filter(x=>x.invoice_id===r.invoice_id&&isInvoice||x.payment_id===r.payment_id&&!isInvoice).map(x=>x.row_id);
  $('#detail-content').innerHTML=`<div class="dialog-top"><div><span class="detail-tag">${TABLES[table].toUpperCase()}</span><h2>${esc(label(id))}</h2></div><button class="icon-button" data-close="detail-dialog" aria-label="Закрыть документ">×</button></div><div class="document-amount">${money}</div><dl class="detail-grid">${entries.map(([k,v])=>`<div><dt>${esc(k)}</dt><dd>${esc(v??'—')}</dd></div>`).join('')}</dl>${related.length?`<div class="detail-related"><h3>Связанные документы и проводки</h3>${[...new Set(related)].map(ref=>`<button class="related-item" data-record="${esc(ref)}">${esc(label(ref))}${recordIndex.get(ref)?.table==='ledger'?' · '+esc(ACCOUNTS[recordIndex.get(ref).row.account]||recordIndex.get(ref).row.account):' · первичный документ'}</button>`).join('')}</div>`:''}<details><summary>Исходная запись и полные идентификаторы</summary><pre class="raw-json">${esc(JSON.stringify(r,null,2))}</pre></details>${proofButton(proofId)}`;
  dialog('detail-dialog');
}
function proofButton(id) {return session.result?'':`<div class="dialog-actions"><button class="button secondary" data-close="detail-dialog">Закрыть</button><button class="button primary" data-proof="${esc(id)}">${selectedProof.has(id)?'Убрать из доказательств':'В доказательства'+(selectedProof.size?' · '+selectedProof.size:'')}</button></div>`;}
function selectionLabel(id) {
  if(id==='policy')return ['Учётная политика','Правила периода, курса и распределения'];
  const item=recordIndex.get(id);if(!item)return [label(id),''];const r=item.row;
  if(item.table==='ledger')return [label(r.payment_id||r.invoice_id)+' · '+label(id),(ACCOUNTS[r.account]||r.account)+' · '+amount(r.debit_minor+r.credit_minor,r.currency)+' · '+date(r.timestamp)];
  if(item.table==='fx')return [label(id)+` · ${r.source_currency}/${r.target_currency}`,`${date(r.date)} · курс ${r.rate}`];
  return [label(id),amount(r.total_minor??r.amount_minor,r.currency)+' · '+date(r.issued_at||r.timestamp)];
}
function checkHTML(id,group,checked) {const [title,sub]=selectionLabel(id);return `<label class="check-option"><input type="checkbox" name="${group}" value="${esc(id)}" ${checked?'checked':''}><span>${esc(title)}<small>${esc(sub)}</small></span></label>`;}
function preferredSource(finding) {
  if(!finding)return null;
  if(finding.type==='duplicate_invoice')return session.public.ledger.find(row=>finding.affected_ids.includes(row.row_id))?.invoice_id;
  if(finding.type==='payment_allocation')return session.public.payments.find(row=>finding.affected_ids.includes(row.payment_id))?.payment_id;
  return finding.affected_ids[0];
}
function findingOptions(existing=null) {
  const type=$('#finding-type').value,available=sourceOptions(session.public,type);
  const preferred=preferredSource(existing);
  $('#finding-source').innerHTML=available.length?available.map(id=>{const text=selectionLabel(id);return `<option value="${esc(id)}" ${id===preferred?'selected':''}>${esc(text[0])} · ${esc(text[1])}</option>`;}).join(''):'<option value="">Нет подходящих операций в этой книге</option>';
  $('#finding-source').disabled=!available.length;
  updateSourceEvidence(existing);
}
function updateSourceEvidence(existing=null) {
  const type=$('#finding-type').value,links=suggestEvidence(session.public,type,$('#finding-source').value);
  const affected=new Set(existing?existing.affected_ids:links.affected);
  const evidence=new Set(existing?existing.evidence_ids:[...links.evidence,...selectedProof]);
  const possible=type==='payment_allocation'?[...session.public.payments.map(row=>row.payment_id),...session.public.invoices.map(row=>row.invoice_id)]:session.public.ledger.filter(row=>type==='duplicate_invoice'?row.account===session.public.policy.accounts.receivable&&row.debit_minor>0:(row.payment_id?row.account===session.public.policy.accounts.cash:row.account===session.public.policy.accounts.receivable)).map(row=>row.row_id);
  $('#finding-rule').textContent=RULES[type][0];$('#affected-help').textContent=RULES[type][1];
  $('#affected-options').innerHTML=possible.map(id=>checkHTML(id,'affected',affected.has(id))).join('')||'<p class="field-help">Нет подходящих записей.</p>';
  const all=['policy',...session.public.invoices.map(row=>row.doc_id),...session.public.payments.map(row=>row.doc_id),...session.public.fx.map(row=>row.record_id),...session.public.ledger.map(row=>row.row_id)];
  $('#evidence-options').innerHTML=all.map(id=>checkHTML(id,'evidence',evidence.has(id))).join('');
  updateProofCount();
}
function openFinding(index=-1) {
  if(session.result)return;
  editIndex=index;const finding=index>=0?draft[index]:null;
  $('#finding-title').textContent=finding?'Изменить ошибку':'Добавить ошибку';
  $('#finding-type').value=finding?.type||'duplicate_invoice';
  $('#finding-amount').value=finding?String(finding.claimed_amount_minor/100).replace('.',','):'';
  $('#finding-explanation').value=finding?.explanation||'';$('#form-error').textContent='';
  $('#finding-dialog .proof-details').open=false;
  findingOptions(finding);dialog('finding-dialog');
}
function updateProofCount() {$('#proof-count').textContent=$$('input[name=evidence]:checked').length+' / 10';}
function saveFinding(event) {
  event.preventDefault();const text=$('#finding-amount').value.replace(/\s/g,'').replace(',','.');
  if(!/^\d+(\.\d{1,2})?$/.test(text))return $('#form-error').textContent='Введите неотрицательную сумму с точностью до двух знаков, например 50000,00.';
  const [whole,fraction='']=text.split('.'),minor=Number(whole)*100+Number(fraction.padEnd(2,'0'));
  if(!Number.isSafeInteger(minor))return $('#form-error').textContent='Слишком большая сумма.';
  const affected=$$('input[name=affected]:checked').map(x=>x.value),evidence=$$('input[name=evidence]:checked').map(x=>x.value);
  if(!affected.length||!evidence.length)return $('#form-error').textContent='Выберите затронутые записи и хотя бы одно доказательство.';
  if(evidence.length>10)return $('#form-error').textContent='В одной находке может быть не больше 10 доказательств.';
  if(editIndex<0&&draft.length>=30)return $('#form-error').textContent='Можно отправить не больше 30 находок.';
  const f={type:$('#finding-type').value,affected_ids:affected,claimed_amount_minor:minor,currency:session.public.manifest.base_currency,evidence_ids:evidence,explanation:$('#finding-explanation').value};
  if(editIndex>=0)draft[editIndex]=f;else draft.push(f);selectedProof.clear();$('#finding-dialog').close();render();toast('Ошибка добавлена в заключение. Документы сохранены вместе с ней.');
}
function showFinding(f,submittedIndex) {
  const explanations={
    duplicate_invoice:'Один и тот же счёт бухгалтер учёл несколько раз. Первое отражение верно. Ошибка — сумма, добавленная повторными записями.',
    fx_conversion:'Для пересчёта нужно взять курс на дату исходного счёта или платежа. Разница между этим пересчётом и суммой в книге — размер ошибки.',
    payment_allocation:'В платёжном документе указан конкретный счёт, но часть денег осталась нераспределённой. Размер ошибки — остаток по записям в книге. Исправление курса может изменить эту сумму.',
    period_cutoff:'Счёт или платёж попал не в тот отчётный период. Сверьте дату исходного документа с датой в книге. Размер ошибки — сумма основной записи.',
  };
  const evaluation=session.result.evaluation,matched=evaluation.matched_pairs.some(pair=>pair[0]===submittedIndex);
  const lesson=currentCase==='masked_both'?buildLesson(session.public):null;
  let calculation='';
  if(lesson&&f.type==='duplicate_invoice')calculation=`<div class="calculation"><span>Повторно учтено</span><strong>${esc(amount(lesson.recorded))} − ${esc(amount(lesson.native.total_minor))} = <em>${esc(amount(lesson.duplicateAmount))}</em></strong></div>`;
  if(lesson&&f.type==='fx_conversion')calculation=`<div class="valuation-lines"><div><span>${esc(amount(lesson.foreign.total_minor,lesson.foreign.currency))} × ${esc(lesson.rate.rate)} ₸</span><strong>${esc(amount(lesson.expected))}</strong></div><div><span>В книге</span><strong>${esc(amount(lesson.foreignRow.debit_minor+lesson.foreignRow.credit_minor))}</strong></div><div class="difference-line"><span>Сумма ошибки</span><strong>${esc(amount(lesson.fxAmount))}</strong></div></div>`;
  $('#detail-content').innerHTML=`<div class="dialog-top"><div><span class="detail-tag">${submittedIndex===undefined?'ОБЪЯСНЕНИЕ ПО ДОКУМЕНТАМ':matched?'ВАШ ВЫВОД ПОДТВЕРЖДЁН':'ВАШ ВЫВОД НЕ ПОДТВЕРДИЛСЯ'}</span><h2>${esc(TYPES[f.type]||f.type)}</h2></div><button class="icon-button" data-close="detail-dialog" aria-label="Закрыть разбор">×</button></div><div class="document-amount">${esc(amount(f.claimed_amount_minor,f.currency))}</div><p class="rule-note">${esc(explanations[f.type]||f.explanation)}</p>${calculation}${f.explanation&&submittedIndex!==undefined&&!session.result.reference?`<p class="dialog-note">Ваше объяснение: ${esc(f.explanation)}</p>`:''}<div class="detail-related"><h3>Какие записи нужно проверить</h3>${f.affected_ids.map(id=>`<button class="related-item" data-record="${esc(id)}">${esc(label(id))}</button>`).join('')}<h3>Документы, на которые опирается вывод</h3>${f.evidence_ids.map(id=>`<button class="related-item" data-record="${esc(id)}">${esc(label(id))}</button>`).join('')}</div><details><summary>Технические данные находки</summary><pre class="raw-json">${esc(JSON.stringify(f,null,2))}</pre></details>`;
  dialog('detail-dialog');
}
async function submit(reference=false) {
  if(busy)return;busy=true;$('#submit-dialog').close();
  const button=$(`[data-command="${reference?'reference':'submit'}"]`);if(button){button.disabled=true;button.innerHTML='<span class="spinner"></span> '+(reference?'Аудитор исследует книгу…':'Проверяем заключение…');}
  try{const value=await api(`/api/sessions/${session.session_id}/${reference?'reference':'submit'}`,reference?{}:{findings:draft});rememberState(value);render();$('#results')?.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth',block:'start'});toast(reference?'Аудитор завершил реальный запуск. Разбор открыт.':'Ответ проверен. Разбор открыт.');}
  catch(error){errorMessage(error);render();}finally{busy=false;}
}
function download() {const content={product:'Genesis Audit Lab',mode:'local_educational',assistance:session.result.reference?'scripted_reference':usedHints?'guided_lesson':'manual',case:currentCase,public:session.public,...session.result,events:session.events};const url=URL.createObjectURL(new Blob([JSON.stringify(content,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=`genesis-audit-${currentCase}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}

function enterMode(mode) {
  stopReplay();viewMode=mode;if(mode==='guided')usedHints=true;tab='invoices';query='';account='';render();
}
function addGuided(kind) {
  const lesson=buildLesson(session.public);
  if(!lesson)return;
  if(kind==='duplicate'&&answers.duplicate!==String(lesson.duplicateAmount))return;
  if(kind==='fx'&&answers.fx!==String(lesson.rate.rate))return;
  const finding=kind==='duplicate'?lesson.duplicate:lesson.fx;
  const index=draft.findIndex(item=>item.type===finding.type&&JSON.stringify([...item.affected_ids].sort())===JSON.stringify([...finding.affected_ids].sort()));
  if(index>=0)draft[index]=finding;else if(draft.length<30)draft.push(finding);else return toast('В заключении уже 30 ошибок. Удалите лишние записи.');
  guideStep=kind==='duplicate'?2:3;render();toast('Расхождение добавлено вместе с документами.');
}
function askSubmit() {
  $('#submit-description').textContent=draft.length?`В заключении ${counted(draft.length,'ошибка','ошибки','ошибок')}. Документы будут проверены вместе с суммами.`:'Ваш вывод: ошибок в книге нет. Оценщик проверит это заключение.';
  dialog('submit-dialog');
}

document.addEventListener('click',event=>{
  const el=event.target.closest('button');if(!el)return;
  if(el.dataset.repair!==undefined){runRepair(el.dataset.repair);return;}
  if(el.dataset.close){$('#'+el.dataset.close).close();return;}
  if(busy)return;
  if(el.dataset.case){if(el.dataset.case==='generated')openLab();else startCase(el.dataset.case);return;}
  if(el.dataset.frame!==undefined){stopReplay();replayIndex=Number(el.dataset.frame);render();return;}
  if(el.dataset.tab){tab=el.dataset.tab;query='';account='';render();$(`#tab-${tab}`)?.focus();return;}
  if(el.dataset.record){openRecord(el.dataset.record);return;}
  if(el.dataset.next!==undefined){guideStep=Number(el.dataset.next);render();return;}
  if(el.dataset.quiz){answers[el.dataset.quiz]=el.dataset.answer;render();return;}
  if(el.dataset.guideAdd){addGuided(el.dataset.guideAdd);return;}
  if(el.dataset.proof){const id=el.dataset.proof;if(selectedProof.has(id))selectedProof.delete(id);else if(selectedProof.size>=9)return toast('Не больше 10 доказательств вместе с правилами учёта.');else selectedProof.add(id);saveToken();$('#detail-dialog').close();render();toast('Документ сохранён в подборку. Его можно приложить к ошибке.');return;}
  if(el.dataset.edit!==undefined){openFinding(Number(el.dataset.edit));return;}
  if(el.dataset.remove!==undefined){draft.splice(Number(el.dataset.remove),1);render();toast('Ошибка удалена из черновика.');return;}
  if(el.dataset.review!==undefined){showFinding(session.result.review[Number(el.dataset.review)]);return;}
  if(el.dataset.submitted!==undefined){const i=Number(el.dataset.submitted);showFinding(draft[i],i);return;}
  const command=el.dataset.command;
  if(command==='arena-home'){stopReplay();if(currentCase==='masked_both')enterMode('start');else startCase('masked_both');}
  else if(command==='arena')runArena();
  else if(command==='lab')openLab();
  else if(command==='masking-demo')startCase('masked_both');
  else if(command==='repair-lab')$('#repair-lab')?.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth',block:'start'});
  else if(command==='about')dialog('about-dialog');
  else if(command==='report'){stopReplay();viewMode='report';render();}
  else if(command==='replay-end'){stopReplay();replayIndex=replayFrames(session.result.reference).length-1;render();$('.experiment')?.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth',block:'start'});}
  else if(command==='replay-toggle'){if(replayTimer){stopReplay();render();}else playReplay();}
  else if(command==='cases'){renderCases();dialog('case-dialog');}
  else if(command==='help')dialog('help-dialog');
  else if(command==='restart'||command==='retry')startCase(currentCase);
  else if(command==='guided')enterMode('guided');
  else if(command==='manual')enterMode('manual');
  else if(command==='first-lesson')startCase('masked_both','guided');
  else if(command==='modes')enterMode('start');
  else if(command==='add')openFinding();
  else if(command==='reference')submit(true);
  else if(command==='submit')askSubmit();
  else if(command==='download')download();
});
document.addEventListener('input',event=>{if(event.target.id==='table-search'){query=event.target.value;renderRows();}});
document.addEventListener('change',event=>{
  if(event.target.id==='account-filter'){account=event.target.value;renderRows();}
  if(event.target.id==='finding-type')findingOptions();
  if(event.target.id==='finding-source')updateSourceEvidence();
  if(event.target.name==='evidence')updateProofCount();
});
$('#lab-form').addEventListener('submit',event=>{event.preventDefault();labFamilies=$$('#lab-form input[name=family]:checked').map(input=>input.value);startCase('generated');});
$('#finding-form').addEventListener('submit',saveFinding);$('#confirm-submit').addEventListener('click',()=>submit());
$$('dialog').forEach(d=>d.addEventListener('click',event=>{if(event.target===d){const rect=d.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)d.close();}}));
async function init(){
  try{
    cases=(await api('/api/cases')).cases;
    let saved;try{saved=JSON.parse(sessionStorage.getItem('genesis-session'));}catch{}
    if(saved?.id&&cases.some(c=>c.id===saved.case)){
      try{
        const value=await api(`/api/sessions/${saved.id}/state`,{});
        currentCase=saved.case;draft=Array.isArray(saved.draft)?saved.draft:[];
        selectedProof=new Set(Array.isArray(saved.proof)?saved.proof:[]);
        if(saved.version===2||saved.version===3){viewMode=['start','guided','manual','report'].includes(saved.viewMode)?saved.viewMode:'start';guideStep=Number.isInteger(saved.guideStep)?Math.max(0,Math.min(3,saved.guideStep)):0;answers=saved.answers&&typeof saved.answers==='object'?saved.answers:{};}
        else viewMode=draft.length?'manual':'start';
        if(saved.tab&&TABLES[saved.tab])tab=saved.tab;
        usedHints=!!saved.usedHints||saved.viewMode==='guided';
        replayIndex=Number.isInteger(saved.replayIndex)?saved.replayIndex:null;
        if(Array.isArray(saved.labFamilies))labFamilies=saved.labFamilies.filter(value=>Object.hasOwn(TYPES,value));
        rememberState(value);render();return;
      }catch{}
    }
    await startCase('masked_both');
  }catch(error){$('#loading').innerHTML=`<div><p>${esc(error.message)}</p><button class="button primary" data-command="retry">Повторить подключение</button></div>`;}
}
init();
