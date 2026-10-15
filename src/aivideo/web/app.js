"use strict";
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="editor-token"]').content;
const state = {plan:null, variant:null, revision:null, selected:null, catalog:[], sources:[], variants:[], past:[], future:[],
  dirty:false, saving:false, conflict:false, generation:0, errors:[], preview:null, previewSerial:0, proxyPending:false,
  boundary:null, transitionJob:null, transitionGeneration:-1};
let saveTimer, photoTimer, photoOffset=0, audioContext, videoGain, audioGain;
const proxyStates=new Map();
const thumbnailsPending=new Set();
const thumbnailObserver=new IntersectionObserver(entries=>{for(const observation of entries){if(observation.isIntersecting){thumbnailObserver.unobserve(observation.target);requestThumbnail(observation.target.dataset.entry).catch(fail);}}},{root:document.querySelector('.catalog-panel')});
const video=$('preview-video'), sound=$('preview-audio');
const frameControls=new Map();
function node(tag, text, cls) {const n=document.createElement(tag); if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function button(text, handler, cls) {const b=node('button',text,cls);b.type='button';b.addEventListener('click',handler);return b;}
function titleDeleteButton(handler){
  const b=button(undefined,handler,'title-delete');b.title='Удалить титр';b.setAttribute('aria-label','Удалить титр');
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  for(const [key,value] of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.8','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true',focusable:'false'}))svg.setAttribute(key,value);
  for(const d of ['M3 6h18','M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2','M5 6l1 14a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1l1-14','M10 10v7','M14 10v7']){const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',d);svg.append(path);}
  b.append(svg);return b;
}
function variantName(id){return state.variants.find(v=>v.id===id)?.name||'Другой вариант';}
function showGlobal(tab){
  const open=tab!==null;$('global-content').hidden=!open;$('collapse-global').hidden=!open;$('collapse-global').setAttribute('aria-expanded',String(open));
  for(const name of ['titles','jobs']){const active=name===tab;$(name+'-tab').setAttribute('aria-selected',String(active));$(name+'-tab').tabIndex=active||!open&&name==='titles'?0:-1;$('global-'+name).hidden=!active;}
}
for(const [index,name] of ['titles','jobs'].entries()){
  $(name+'-tab').onclick=()=>showGlobal(name);
  $(name+'-tab').addEventListener('keydown',e=>{let target;if(e.key==='ArrowLeft'||e.key==='ArrowRight')target=['titles','jobs'][1-index];else if(e.key==='Home')target='titles';else if(e.key==='End')target='jobs';if(target){e.preventDefault();showGlobal(target);$(target+'-tab').focus();}});
}
$('collapse-global').onclick=()=>{const active=document.querySelector('.global-tabs [aria-selected="true"]');showGlobal(null);(active||$('titles-tab')).focus();};
function fail(error) {$('error').textContent=String(error.message||error);$('error').hidden=false;}
function clearError() {$('error').hidden=true;}
async function api(url, body) {
  const response=await fetch(url,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Editor-Token':token},body:JSON.stringify(body)});
  const value=await response.json();
  if(!response.ok){const error=new Error(value.error||response.statusText);error.status=response.status;throw error;}
  return value;
}
function time(value) {value=Math.max(0,Number(value)||0);const m=Math.floor(value/60),s=(value%60).toFixed(2).padStart(5,'0');return `${m}:${s}`;}
function clone(value){return structuredClone(value);}
function source(id){return state.sources.find(s=>s.id===id);}
function shotDuration(s){return source(s.source_id)?.kind==='image'?s.duration_s:s.end_s-s.start_s;}
const transitionNames={cut:'Обычная склейка',crossfade:'Плавное смешивание',fade_black:'Через чёрный'};
function edge(a,b){return (state.plan.transitions||[]).find(t=>t.from_shot_id===a&&t.to_shot_id===b);}
function timeline(){let cursor=0;const fps=state.plan.settings?.fps||30;return state.plan.shots.map((s,i)=>{const start=cursor;cursor+=Math.round(shotDuration(s)*fps);const end=cursor;cursor-=Math.round((edge(s.id,state.plan.shots[i+1]?.id)?.duration_s||0)*fps);return {id:s.id,start_s:start/fps,end_s:end/fps};});}
function duration(){return state.plan?timeline().at(-1)?.end_s||0:0;}
function selected(){return state.plan?.shots.find(s=>s.id===state.selected);}
function frameBounds(id){
  const fps=state.plan?.settings?.fps||30,shot=selected();
  if(id==='transition-duration'){const info=boundaryInfo();return {min:1,max:info?boundaryMaxFrames(info,fps):0};}
  if(!shot)return {min:0,max:0};
  if(id!=='photo-duration'&&source(shot.source_id).kind==='image')return {min:0,max:0};
  if(id==='shot-start')return {min:0,max:Math.round(shot.end_s*fps)-1};
  if(id==='shot-end')return {min:Math.round(shot.start_s*fps)+1,max:Math.floor(source(shot.source_id).duration_s*fps+1e-6)};
  return {min:1,max:Infinity};
}
function syncFrameControl(id){
  const control=frameControls.get(id);if(!control)return;const input=$(id),fps=state.plan?.settings?.fps||30,{min,max}=frameBounds(id),frames=Math.round(Number(input.value)*fps);
  input.step='any';input.min=String(Number((min/fps).toFixed(6)));if(Number.isFinite(max))input.max=String(Number((max/fps).toFixed(6)));else input.removeAttribute('max');
  control.minus.disabled=!state.plan||input.disabled||frames<=min;control.plus.disabled=!state.plan||input.disabled||frames>=max;
  control.minus.title='−0,5 с';control.plus.title='+0,5 с';
}
function nudgeFrame(id,delta){
  const input=$(id);if(!state.plan||input.disabled)return;const fps=state.plan.settings?.fps||30,{min,max}=frameBounds(id),value=Number(input.value);
  if(!Number.isFinite(value)||min>max)return;
  const frames=Math.max(min,Math.min(max,Math.round(value*fps)+delta*Math.round(0.5*fps)));
  if(frames===Math.round(value*fps)&&Math.abs(value-frames/fps)<1e-6)return;
  input.value=String(frames/fps);input.dispatchEvent(new Event('change',{bubbles:true}));
}
function bindFrameControl(id,label){
  const input=$(id),wrap=node('div',undefined,'frame-control'),minus=button('−',()=>nudgeFrame(id,-1)),plus=button('+',()=>nudgeFrame(id,1));
  minus.setAttribute('aria-label',`Уменьшить ${label} на 0,5 с`);plus.setAttribute('aria-label',`Увеличить ${label} на 0,5 с`);
  input.before(wrap);wrap.append(minus,input,plus);input.classList.add('frame-input');frameControls.set(id,{minus,plus});
  input.addEventListener('keydown',event=>{if(event.key==='ArrowUp'||event.key==='ArrowDown'){event.preventDefault();nudgeFrame(id,event.key==='ArrowUp'?1:-1);}});
  syncFrameControl(id);
}
function uid(prefix){return prefix+'_'+crypto.randomUUID().replaceAll('-','').slice(0,16);}
function markConflict(){state.conflict=true;$('conflict').hidden=false;$('save-status').textContent='Конфликт сохранения';$('render').disabled=true;}
function setMovie(movie){
  const url=movie?.url||'';
  if($('movie').getAttribute('src')!==url){if(url)$('movie').src=url;else $('movie').removeAttribute('src');}
  $('download').hidden=!url;$('download').href=url;
  $('movie-state').textContent=!movie?'Для этого варианта ещё нет сборки.':(movie.stale||state.dirty?'Фильм требует пересборки':'Фильм соответствует сохранённому плану');
}
function status(){
  $('duration').textContent=`${state.plan?.shots.length||0} сцен · ${time(duration())}`;
  $('render').disabled=!state.plan||state.conflict||state.errors.length>0||state.saving;
  if(state.conflict)return;
  $('save-status').textContent=state.saving?'Сохранение…':state.dirty?'Есть несохранённые правки':state.errors.length?'План сохранён; исправьте ошибки перед сборкой':'Все правки сохранены';
  if(state.dirty&&$('movie').getAttribute('src'))$('movie-state').textContent='Фильм требует пересборки';
}
function changed(edit){
  state.past.push(clone(state.plan));if(state.past.length>80)state.past.shift();state.future=[];
  edit(state.plan);pruneTransitions();state.generation++;state.dirty=true;clearError();staleTransition();refresh();
  clearTimeout(saveTimer);saveTimer=setTimeout(()=>save().catch(handleSaveError),600);
}
function handleSaveError(error){if(error.status===409)markConflict();else fail(error);status();}
async function save(){
  clearTimeout(saveTimer);
  if(state.saving){while(state.saving)await new Promise(r=>setTimeout(r,30));}
  if(state.conflict)throw new Error('Сначала разрешите конфликт сохранения');
  while(state.dirty){
    state.saving=true;status();const generation=state.generation;
    try {
      const value=await api('/api/plan',{variant:state.variant,revision:state.revision,plan:clone(state.plan)});
      state.revision=value.revision;state.errors=value.errors;
      if(state.generation===generation){state.plan=value.plan;state.dirty=false;}
      setMovie(value.movie);
      if(value.errors.length)fail(value.errors.join('\n'));
    } finally {state.saving=false;status();}
  }
}
async function variants(){
  const project=await api('/api/project');$('variants').replaceChildren();
  state.variants=project.variants;
  for(const v of project.variants){const o=node('option',v.name);o.value=v.id;$('variants').append(o);}
  return project;
}
async function loadVariant(id){
  stopTransition();state.boundary=null;state.transitionJob=null;$('transition-tab').hidden=true;
  clearTimeout(saveTimer);const value=await api('/api/plan?variant='+encodeURIComponent(id));
  state.variant=id;state.plan=value.plan;state.revision=value.revision;state.errors=value.errors;
  state.dirty=false;state.conflict=false;state.past=[];state.future=[];state.selected=state.plan.shots[0]?.id;
  $('conflict').hidden=true;$('variants').value=id;clearError();
  if(value.errors.length)fail(value.errors.join('\n'));
  history.replaceState(null,'','/?variant='+encodeURIComponent(id));setMovie(value.movie);refresh();await choose(state.selected);
}
function matchingEntry(shot){return state.catalog.find(e=>e.source_id===shot.source_id&&(e.kind==='image'||e.start_s<=shot.start_s&&e.end_s>shot.start_s));}
function renderCatalog(){
  thumbnailObserver.disconnect();
  const q=$('search').value.toLocaleLowerCase(),kind=$('kind').value,src=$('source').value;
  const entries=state.catalog.filter(e=>(!kind||e.kind===kind)&&(!src||e.source_id===src)&&(`${e.description||''} ${e.source_id}`).toLocaleLowerCase().includes(q));
  $('catalog-count').textContent=`Найдено: ${entries.length}`;$('catalog').replaceChildren();
  for(const e of entries){
    const card=node('article',undefined,'catalog-card');card.dataset.entry=e.id;
    if(e.thumbnail){const img=node('img');img.src=e.thumbnail;img.loading='lazy';img.alt='Миниатюра';card.append(img);}
    card.append(node('div',e.source_id,'source-name'),node('small',e.kind==='image'?'Фотография':`${time(e.start_s)}–${time(e.end_s)}`),node('p',e.description||'Без описания'));
    const actions=node('div',undefined,'toolbar');actions.append(button('Просмотр',()=>previewCatalog(e)),button('Добавить',()=>addEntry(e)));card.append(actions);$('catalog').append(card);
    if(!e.thumbnail&&e.kind==='video')thumbnailObserver.observe(card);
  }
}
async function requestThumbnail(id){
  const entry=state.catalog.find(e=>e.id===id);if(!entry||entry.thumbnail)return;
  const result=await api('/api/thumbnail',{entry_id:id});
  if(result.url){entry.thumbnail=result.url;thumbnailsPending.delete(id);renderCatalog();renderScenes();}
  else thumbnailsPending.add(id);
}
function addEntry(entry){
  const fps=state.plan.settings?.fps||30,m=source(entry.source_id);
  const shot={id:uid('scene'),source_id:entry.source_id,purpose:entry.description||'',audio:{mode:m.has_audio?'original':'silence'}};
  if(m.kind==='image')shot.duration_s=3;
  else {shot.start_s=Math.ceil(entry.start_s*fps-1e-6)/fps;shot.end_s=Math.floor(Math.min(entry.end_s,m.duration_s)*fps+1e-6)/fps;}
  if(m.kind==='video'&&shot.end_s<=shot.start_s)return fail('Интервал короче одного кадра монтажа');
  changed(plan=>{const index=plan.shots.findIndex(s=>s.id===state.selected);plan.shots.splice(index+1,0,shot);state.selected=shot.id;});choose(shot.id);
}
function move(id,delta){changed(plan=>{const i=plan.shots.findIndex(s=>s.id===id),j=i+delta;if(j>=0&&j<plan.shots.length)[plan.shots[i],plan.shots[j]]=[plan.shots[j],plan.shots[i]];});}
function renderScenes(){
  $('scenes').replaceChildren();$('timeline').replaceChildren();$('title-track').replaceChildren();const times=timeline(),total=duration();
  for(const [index,shot] of state.plan.shots.entries()){
    const t=times[index],card=node('article',undefined,'scene-card'+(shot.id===state.selected?' selected':''));card.dataset.shot=shot.id;card.draggable=true;
    const entry=matchingEntry(shot);if(entry?.thumbnail){const img=node('img');img.src=entry.thumbnail;img.alt='';img.loading='lazy';card.append(img);}
    card.append(node('strong',`${String(index+1).padStart(2,'0')} · ${time(t.start_s)}–${time(t.end_s)}`),node('div',shot.source_id,'source-name'),node('small',source(shot.source_id)?.kind==='image'?`${shot.duration_s.toFixed(2)} с · фото`:`Исходник ${time(shot.start_s)}–${time(shot.end_s)}`),node('p',shot.purpose||'Без описания'));
    const actions=node('div',undefined,'toolbar');
    const up=button('↑',e=>{e.stopPropagation();move(shot.id,-1)}),down=button('↓',e=>{e.stopPropagation();move(shot.id,1)});up.disabled=index===0;down.disabled=index===state.plan.shots.length-1;
    actions.append(up,down,button('Копия',e=>{e.stopPropagation();changed(plan=>{const s=clone(shot);s.id=uid('scene');plan.shots.splice(index+1,0,s);state.selected=s.id;});choose(state.selected);}),button('Удалить',e=>{e.stopPropagation();if(state.plan.shots.length===1)return fail('Оставьте хотя бы одну сцену');changed(plan=>{plan.shots.splice(index,1);if(state.selected===shot.id)state.selected=plan.shots[Math.min(index,plan.shots.length-1)].id;});choose(state.selected);}));
    card.append(actions);card.addEventListener('click',()=>choose(shot.id));
    card.addEventListener('dragstart',e=>{e.dataTransfer.setData('text/plain',shot.id);card.classList.add('dragging')});card.addEventListener('dragend',()=>card.classList.remove('dragging'));card.addEventListener('dragover',e=>e.preventDefault());
    card.addEventListener('drop',e=>{e.preventDefault();const id=e.dataTransfer.getData('text/plain');if(id!==shot.id&&state.plan.shots.some(s=>s.id===id))changed(plan=>{const i=plan.shots.findIndex(s=>s.id===id),item=plan.shots.splice(i,1)[0],j=plan.shots.findIndex(s=>s.id===shot.id);plan.shots.splice(j,0,item);});});
    $('scenes').append(card);const b=button(String(index+1),()=>choose(shot.id),shot.id===state.selected?'selected':'');b.classList.add('timeline-scene');b.style.left=`${100*t.start_s/total}%`;b.style.width=`${100*shotDuration(shot)/total}%`;b.title=shot.purpose||shot.source_id;$('timeline').append(b);
    const following=state.plan.shots[index+1];if(following){const e=edge(shot.id,following.id),label=transitionNames[e?.type||'cut']+(e?` · ${e.duration_s.toFixed(3)} с`:'');const selected=state.boundary?.[0]===shot.id&&state.boundary?.[1]===following.id;
      const join=button(label,()=>chooseBoundary(shot.id,following.id),'transition-card'+(selected?' selected':''));join.dataset.from=shot.id;join.dataset.to=following.id;join.setAttribute('aria-label',`Переход ${index+1} → ${index+2}: ${label}`);$('scenes').append(join);
      const marker=button('◆',()=>chooseBoundary(shot.id,following.id),'transition-marker');marker.style.left=`${100*(t.end_s-(e?.duration_s||0)/2)/total}%`;marker.title=label;marker.setAttribute('aria-label',join.getAttribute('aria-label'));$('timeline').append(marker);
    }
  }
  for(const title of state.plan.titles||[]){const bar=node('span',title.text);bar.style.left=`${100*title.start_s/total}%`;bar.style.width=`${100*(title.end_s-title.start_s)/total}%`;bar.title=`${time(title.start_s)}–${time(title.end_s)} · ${title.text}`;$('title-track').append(bar);}
}
function renderFields(){
  const shot=selected();if(!shot)return;const m=source(shot.source_id);
  $('shot-fields').hidden=false;$('start-label').hidden=$('end-label').hidden=m.kind==='image';$('photo-label').hidden=m.kind!=='image';
  $('shot-start').value=Number((shot.start_s??0).toFixed(6));$('shot-end').value=Number((shot.end_s??0).toFixed(6));$('photo-duration').value=Number((shot.duration_s??3).toFixed(6));$('shot-purpose').value=shot.purpose||'';
  const audio=shot.audio||{mode:m.has_audio?'original':'silence'};$('audio-mode').value=audio.mode;$('audio-mode').querySelector('[value="original"]').disabled=!m.has_audio;
  $('audio-gain').value=Math.round((audio.gain??1)*100);$('audio-start').value=audio.start_s||0;
  $('audio-source-fields').hidden=audio.mode!=='source';$('audio-source').value=audio.source_id||state.sources.find(s=>s.has_audio)?.id||'';
  for(const id of ['shot-start','shot-end','photo-duration'])syncFrameControl(id);
}
function renderTitles(){
  $('titles-variant').textContent='Вариант: '+variantName(state.variant);
  $('titles').replaceChildren();
  for(const title of state.plan.titles||[]){
    const card=node('div',undefined,'title-card'+(title.end_s>duration()?' invalid':''));card.dataset.title=title.id;
    const input=node('textarea');input.value=title.text;input.rows=2;input.maxLength=200;input.setAttribute('aria-label','Текст титра');
    input.addEventListener('change',()=>changed(p=>p.titles.find(t=>t.id===title.id).text=input.value));card.append(input);
    const fields=node('div',undefined,'fields');
    for(const [key,label] of [['start_s','Начало, с'],['end_s','Конец, с']]){const wrap=node('label',label),field=node('input');field.type='number';field.min=0;field.step='0.01';field.value=title[key];field.dataset.field=key;field.addEventListener('change',()=>{const value=Number(field.value);changed(p=>p.titles.find(t=>t.id===title.id)[key]=value);});wrap.append(field);fields.append(wrap);}
    fields.append(titleDeleteButton(()=>changed(p=>{p.titles=p.titles.filter(t=>t.id!==title.id)})));card.append(fields);$('titles').append(card);
  }
}
function refresh(){if(!selected())state.selected=state.plan.shots[0]?.id;if(state.boundary&&!boundaryInfo()){state.boundary=null;stopTransition();$('transition-tab').hidden=true;if(!$('transition-view').hidden)showPreviewView('scene');}renderScenes();renderTitles();renderFields();renderTransitionFields();status();$('undo').disabled=!state.past.length;$('redo').disabled=!state.future.length;updateOverlay();}
async function choose(id){state.boundary=null;stopTransition();$('transition-tab').hidden=true;state.selected=id;renderScenes();renderFields();const shot=selected();if(shot)await preview(shot,false);}
function pruneTransitions(){const pairs=new Set(state.plan.shots.slice(1).map((s,i)=>state.plan.shots[i].id+'|'+s.id));state.plan.transitions=(state.plan.transitions||[]).filter(t=>pairs.has(t.from_shot_id+'|'+t.to_shot_id));}
function boundaryInfo(){if(!state.boundary)return null;const i=state.plan.shots.findIndex(s=>s.id===state.boundary[0]);if(i<0||state.plan.shots[i+1]?.id!==state.boundary[1])return null;return {index:i,a:state.plan.shots[i],b:state.plan.shots[i+1],transition:edge(...state.boundary)};}
function stopTransition(){$('transition-video').pause();}
function staleTransition(){if(state.transitionJob){stopTransition();$('transition-status').textContent='Просмотр устарел. Нажмите «Посмотреть стык» для обновления.';}}
function showPreviewView(name){for(const v of ['scene','transition','film']){$(v+'-view').hidden=v!==name;$(v+'-tab').classList.toggle('active',v===name);}if(name!=='transition')stopTransition();if(name!=='scene')stopPreview();if(name!=='film')$('movie').pause();}
function chooseBoundary(a,b){++state.previewSerial;stopPreview();state.proxyPending=false;const same=state.boundary?.[0]===a&&state.boundary?.[1]===b;state.boundary=[a,b];if(!same){state.transitionJob=null;$('transition-video').hidden=true;$('transition-video').removeAttribute('src');$('transition-status').textContent='Нажмите «Посмотреть стык».';}$('transition-tab').hidden=false;showPreviewView('transition');renderScenes();renderTransitionFields();}
function renderTransitionFields(){const info=boundaryInfo();if(!info)return;const {index,a,b,transition}=info,fps=state.plan.settings?.fps||30;
  const maxFrames=boundaryMaxFrames(info,fps);
  $('transition-pair').textContent=`${index+1}. ${a.source_id} → ${index+2}. ${b.source_id}`;$('transition-type').value=transition?.type||'cut';$('transition-duration').value=Number((transition?.duration_s??0).toFixed(6));$('transition-duration').disabled=!transition;syncFrameControl('transition-duration');
  $('transition-limit').textContent=`Фактически: ${(transition?.duration_s||0).toFixed(3)} с (${Math.round((transition?.duration_s||0)*fps)} кадров). Допустимо до ${(Math.max(0,maxFrames)/fps).toFixed(3)} с (${Math.max(0,maxFrames)} кадров).`;
}
function boundaryMaxFrames({index,a,b},fps){const previous=state.plan.shots[index-1],next=state.plan.shots[index+2];return Math.max(0,Math.min(Math.round(shotDuration(a)*fps)-Math.round((edge(previous?.id,a.id)?.duration_s||0)*fps)-1,Math.round(shotDuration(b)*fps)-Math.round((edge(b.id,next?.id)?.duration_s||0)*fps)-1));}
function transitionInput(changeType){const info=boundaryInfo();if(!info)return;const fps=state.plan.settings?.fps||30,type=$('transition-type').value;const value=changeType?(info.transition?.duration_s||.5):Number($('transition-duration').value);const frames=Math.max(1,Math.round(value*fps));
  changed(p=>{p.transitions=p.transitions.filter(t=>t.from_shot_id!==info.a.id||t.to_shot_id!==info.b.id);if(type!=='cut')p.transitions.push({from_shot_id:info.a.id,to_shot_id:info.b.id,type,duration_s:frames/fps});});
}
$('transition-type').onchange=()=>transitionInput(true);$('transition-duration').onchange=()=>transitionInput(false);
function acceptTransitionJob(job){if(!job||job.id!==state.transitionJob||state.transitionGeneration!==state.generation||!boundaryInfo()||job.from_shot_id!==state.boundary[0]||job.to_shot_id!==state.boundary[1])return;
  if(job.status==='complete'&&job.url){if($('transition-video').getAttribute('src')!==job.url)$('transition-video').src=job.url;$('transition-video').hidden=false;$('transition-status').textContent='Просмотр готов: переход, звук и титры до итогового сведения.';}
  else $('transition-status').textContent=job.error||(['paused','interrupted','failed'].includes(job.status)?'Просмотр остановлен. Нажмите «Продолжить» на вкладке «Обработка проекта».':'Готовится просмотр стыка. Состояние — на вкладке «Обработка проекта».');
}
$('play-transition').onclick=async()=>{try{await save();if(state.errors.length)throw new Error(state.errors.join('\n'));if(!boundaryInfo())return;const pair=[...state.boundary],generation=state.generation,variant=state.variant;$('transition-status').textContent='Подготовка просмотра…';const job=await api('/api/transition-preview',{variant,revision:state.revision,from_shot_id:pair[0],to_shot_id:pair[1]});if(generation!==state.generation||variant!==state.variant||pair.join('|')!==state.boundary?.join('|'))return;state.transitionJob=job.id;state.transitionGeneration=generation;acceptTransitionJob(job);await pollJobs();}catch(e){handleSaveError(e);}};
$('transition-tab').onclick=()=>{if(boundaryInfo())showPreviewView('transition');};
function previewCatalog(e){const shot={source_id:e.source_id,start_s:e.start_s,end_s:e.end_s,duration_s:3,purpose:e.description,audio:{mode:source(e.source_id).has_audio?'original':'silence'}};preview(shot,true).catch(fail);}
function audioInit(){
  if(!audioContext){audioContext=new AudioContext();videoGain=audioContext.createGain();audioGain=audioContext.createGain();audioContext.createMediaElementSource(video).connect(videoGain).connect(audioContext.destination);audioContext.createMediaElementSource(sound).connect(audioGain).connect(audioContext.destination);}
  audioContext.resume();
}
function stopPreview(){clearInterval(photoTimer);photoTimer=null;video.pause();sound.pause();}
async function proxyUrl(id){const job=await api('/api/proxy',{source_id:id});proxyStates.set(id,job.status);return job.status==='complete'?job.url:null;}
function proxyMessage(id){return ['paused','interrupted','failed'].includes(proxyStates.get(id))?'Подготовка копии остановлена. Нажмите «Продолжить» на вкладке «Обработка проекта».':'Готовится облегчённая копия. Прогресс и продолжение — на вкладке «Обработка проекта».';}
async function preview(shot,fromCatalog){
  const serial=++state.previewSerial;stopPreview();photoOffset=0;state.preview={shot:clone(shot),fromCatalog};state.proxyPending=false;
  showPreviewView('scene');
  $('shot-fields').hidden=fromCatalog;$('scene-name').textContent=fromCatalog?'Просмотр из каталога':`Сцена ${state.plan.shots.findIndex(s=>s.id===shot.id)+1}`;
  $('preview-status').textContent='Подготовка просмотра…';$('title-overlay').hidden=true;
  const m=source(shot.source_id),entry=matchingEntry(shot),isPhoto=m.kind==='image';
  video.hidden=isPhoto;$('preview-image').hidden=!isPhoto;
  if(isPhoto){$('preview-image').src=entry?.thumbnail||'';}else{
    const url=await proxyUrl(shot.source_id);if(serial!==state.previewSerial)return;
    if(!url){state.proxyPending=true;$('preview-status').textContent=proxyMessage(shot.source_id);video.removeAttribute('src');return;}
    if(video.getAttribute('src')!==url){video.src=url;video.addEventListener('loadedmetadata',()=>{if(serial===state.previewSerial)video.currentTime=shot.start_s},{once:true});}
    else video.currentTime=shot.start_s;
  }
  const mode=shot.audio?.mode||(m.has_audio?'original':'silence');video.muted=mode!=='original';sound.removeAttribute('src');
  if(mode==='source'){
    const url=await proxyUrl(shot.audio.source_id);if(serial!==state.previewSerial)return;
    if(!url){state.proxyPending=true;$('preview-status').textContent=proxyMessage(shot.audio.source_id);return;}
    sound.src=url;sound.addEventListener('loadedmetadata',()=>{sound.currentTime=shot.audio.start_s||0},{once:true});
  }
  $('preview-status').textContent=fromCatalog?'Материал из каталога. Нажмите «Добавить», чтобы включить его в монтаж.':`${shot.source_id} · ${time(shotDuration(shot))}`;
  updateOverlay();
}
function offset(){const p=state.preview;if(!p)return 0;return source(p.shot.source_id).kind==='image'?photoOffset:Math.max(0,video.currentTime-p.shot.start_s);}
function updateOverlay(){
  const p=state.preview;if(!p)return;const t=timeline().find(t=>t.id===p.shot.id),global=(t?.start_s||0)+offset();
  const title=!p.fromCatalog&&(state.plan.titles||[]).find(t=>t.start_s<=global&&global<t.end_s);
  $('title-overlay').hidden=!title;$('title-overlay').textContent=title?.text||'';
  const filmHeight=state.plan.settings?.height||1080;
  $('title-overlay').style.fontSize=`${Math.max(16,Math.round(filmHeight*.042))*$('preview-stage').clientHeight/filmHeight}px`;$('title-overlay').style.padding=`${$('preview-stage').clientHeight*.015}px`;
  $('play-time').textContent=`${time(offset())} / ${time(shotDuration(p.shot))}${p.fromCatalog?'':` · фильм ${time(global)}`}`;
  if(videoGain)videoGain.gain.value=p.shot.audio?.gain??1;if(audioGain)audioGain.gain.value=p.shot.audio?.gain??1;
}
async function syncSound(){
  const p=state.preview;if(!p||p.shot.audio?.mode!=='source'||!sound.getAttribute('src'))return;
  const wanted=(p.shot.audio.start_s||0)+offset();if(Math.abs(sound.currentTime-wanted)>.12)sound.currentTime=wanted;
  sound.playbackRate=video.playbackRate||1;if(!video.paused||photoTimer)await sound.play().catch(()=>{});
}
video.addEventListener('timeupdate',()=>{const p=state.preview;if(p&&video.currentTime>=p.shot.end_s-.008){stopPreview();video.currentTime=p.shot.end_s;}updateOverlay();syncSound();});
video.addEventListener('play',()=>{const p=state.preview;if(!p)return;audioInit();if(video.currentTime<p.shot.start_s||video.currentTime>=p.shot.end_s)video.currentTime=p.shot.start_s;syncSound();});
video.addEventListener('pause',()=>sound.pause());video.addEventListener('seeked',()=>{syncSound();updateOverlay()});
$('play-scene').addEventListener('click',async()=>{if(!state.preview||state.proxyPending)return;audioInit();stopPreview();photoOffset=0;const shot=state.preview.shot;if(source(shot.source_id).kind==='video'){video.currentTime=shot.start_s;await video.play().catch(fail);}else{const began=performance.now();photoTimer=setInterval(()=>{photoOffset=Math.min((performance.now()-began)/1000,shot.duration_s);updateOverlay();syncSound();if(photoOffset>=shot.duration_s)stopPreview();},40);syncSound();}});
function shotInput(){
  const shot=selected();if(!shot)return;const fps=state.plan.settings?.fps||30,m=source(shot.source_id),mode=$('audio-mode').value;
  changed(plan=>{const s=plan.shots.find(s=>s.id===shot.id);if(m.kind==='image')s.duration_s=Math.max(1,Math.round(Number($('photo-duration').value)*fps))/fps;
    else{s.start_s=Math.max(0,Math.round(Number($('shot-start').value)*fps))/fps;s.end_s=Math.min(Math.floor(m.duration_s*fps),Math.round(Number($('shot-end').value)*fps))/fps;}
    s.purpose=$('shot-purpose').value;s.audio={mode,gain:Number($('audio-gain').value)/100};if(mode==='source'){s.audio.source_id=$('audio-source').value;s.audio.start_s=Number($('audio-start').value);}
    for(const key of ['fade_in_s','fade_out_s'])if(s[key]>shotDuration(s))s[key]=Math.max(0,shotDuration(s));
  });preview(selected(),false).catch(fail);
}
for(const id of ['shot-start','shot-end','photo-duration','shot-purpose','audio-mode','audio-source','audio-start','audio-gain'])$(id).addEventListener('change',shotInput);
for(const [id,label] of [['shot-start','начало сцены'],['shot-end','конец сцены'],['photo-duration','длительность фото'],['transition-duration','длительность перехода']])bindFrameControl(id,label);
for(const id of ['search','kind','source'])$(id).addEventListener('input',renderCatalog);
$('scene-tab').onclick=()=>showPreviewView('scene');
$('film-tab').onclick=()=>showPreviewView('film');
$('variants').onchange=async()=>{try{await save();await loadVariant($('variants').value);}catch(e){handleSaveError(e);$('variants').value=state.variant;}};
function variantDialog(){$('variant-name').value='';$('variant-dialog').showModal();$('variant-name').focus();}
$('new-variant').onclick=variantDialog;$('keep-copy').onclick=variantDialog;
$('create-variant').onclick=async()=>{try{clearTimeout(saveTimer);while(state.saving)await new Promise(r=>setTimeout(r,30));const value=await api('/api/variants',{variant:state.variant,name:$('variant-name').value,plan:state.plan});$('variant-dialog').close();await variants();await loadVariant(value.variant);}catch(e){fail(e);}};
$('reload').onclick=()=>loadVariant(state.variant).catch(fail);
$('add-title').onclick=()=>{const times=timeline(),current=times.find(t=>t.id===state.selected);let start=current?.start_s||0;const ordered=[...(state.plan.titles||[])].sort((a,b)=>a.start_s-b.start_s);for(const t of ordered)if(start>=t.start_s&&start<t.end_s)start=t.end_s;const next=ordered.find(t=>t.start_s>=start);const end=Math.min(duration(),start+3,next?.start_s??Infinity);if(end<=start)return fail('На выбранном месте нет свободного времени для титра');changed(p=>{p.titles.push({id:uid('title'),text:'Новый титр',start_s:start,end_s:end});});};
function restoredEdit(){state.generation++;state.dirty=true;staleTransition();refresh();if(selected()&&!state.boundary)preview(selected(),false).catch(fail);save().catch(handleSaveError);}
$('undo').onclick=()=>{if(!state.past.length)return;state.future.push(clone(state.plan));state.plan=state.past.pop();restoredEdit();};
$('redo').onclick=()=>{if(!state.future.length)return;state.past.push(clone(state.plan));state.plan=state.future.pop();restoredEdit();};
$('render').onclick=async()=>{try{await save();if(state.errors.length)return;await api('/api/render',{variant:state.variant,revision:state.revision});await pollJobs();}catch(e){handleSaveError(e);}};
$('history').onclick=async()=>{try{await save();const entries=await api('/api/history?variant='+state.variant);$('history-list').replaceChildren();for(const e of entries){const row=node('div',undefined,'history-entry');row.append(node('span',`${e.saved_at} · ${e.revision.slice(0,8)}`),button('Восстановить',async()=>{try{await api('/api/restore',{variant:state.variant,revision:state.revision,history_revision:e.revision});$('history-dialog').close();await loadVariant(state.variant);}catch(error){handleSaveError(error);}}));$('history-list').append(row);}$('history-dialog').showModal();}catch(e){handleSaveError(e);}};
async function pollJobs(){
  const jobs=await api('/api/jobs');$('jobs').replaceChildren();
  const active=jobs.filter(j=>['queued','running'].includes(j.status)).length;
  $('active-job-count').textContent=String(active);$('active-job-count').setAttribute('aria-label',`Активных заданий: ${active}`);$('active-job-count').classList.toggle('has-active',active>0);
  const recent=jobs.filter(j=>j.status!=='complete').concat(jobs.filter(j=>j.status==='complete').slice(0,4));
  if(!recent.length)$('jobs').append(node('p','Заданий пока нет.','muted'));
  const labels={queued:'В очереди',running:'Выполняется',complete:'Готово',paused:'На паузе',interrupted:'Ожидает продолжения',failed:'Ошибка'};
  for(const job of recent){const card=node('div',undefined,'job');card.dataset.job=job.id;card.append(node('strong',`${job.kind==='render'?'Сборка фильма':job.kind==='transition_preview'?'Просмотр стыка':job.kind==='proxy'?'Копия для просмотра':'Миниатюра'} · ${labels[job.status]}`));
    if(job.variant)card.append(node('p',variantName(job.variant),'job-variant'));
    if(job.kind==='transition_preview')card.append(node('p',`${job.from_shot_id} → ${job.to_shot_id}`));
    if(job.progress){const p=job.progress;card.append(node('p',`${p.stage}: ${p.done}/${p.total}; из кеша ${p.cached}`));const bar=node('progress');bar.max=p.total;bar.value=p.done;card.append(bar);}if(job.source_id)card.append(node('p',job.source_id,'source-name'));if(job.wall_time_s!==undefined)card.append(node('small',`${job.wall_time_s.toFixed(1)} с`));if(job.error)card.append(node('p',job.error));
    if(['running','queued'].includes(job.status))card.append(button('Пауза',async()=>{await api('/api/pause',{id:job.id});pollJobs().catch(fail);}));
    if(['paused','interrupted','failed'].includes(job.status))card.append(button('Продолжить',async()=>{await api('/api/resume',{id:job.id});pollJobs().catch(fail);}));$('jobs').append(card);
  }
  acceptTransitionJob(jobs.find(j=>j.id===state.transitionJob));
  if(state.proxyPending&&state.preview){const p=state.preview;await preview(p.fromCatalog?p.shot:selected()||p.shot,p.fromCatalog);}
  for(const id of [...thumbnailsPending])await requestThumbnail(id);
}
async function pollPlan(){
  if(!state.variant||state.saving||state.conflict)return;
  const value=await api('/api/plan?variant='+state.variant);
  if(value.revision!==state.revision){if(state.dirty)markConflict();else{state.plan=value.plan;state.revision=value.revision;state.errors=value.errors;state.generation++;staleTransition();refresh();if(selected()&&!state.boundary)await preview(selected(),false);}}
  setMovie(value.movie);
}
window.addEventListener('beforeunload',e=>{if(state.dirty||state.saving){e.preventDefault();e.returnValue='';}});
window.addEventListener('resize',updateOverlay);
async function init(){
  const [project,catalog]=await Promise.all([variants(),api('/api/catalog')]);state.catalog=catalog.entries;state.sources=catalog.sources;
  for(const s of state.sources){const option=node('option',s.id);option.value=s.id;$('source').append(option);if(s.has_audio){const audioOption=node('option',s.id);audioOption.value=s.id;$('audio-source').append(audioOption);}}
  renderCatalog();const requested=new URLSearchParams(location.search).get('variant');await loadVariant(project.variants.some(v=>v.id===requested)?requested:project.default_variant);
  pollJobs().catch(fail);setInterval(()=>pollJobs().catch(fail),1500);setInterval(()=>pollPlan().catch(fail),2500);
}
init().catch(fail);
