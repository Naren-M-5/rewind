const $ = s => document.querySelector(s);
const esc = x => String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let selected = null, config = {}, busy = false;
const names = {capture_timeout:'Capture response lost',delivery_loss:'Result delivery lost',true_failure:'Merchant execution failed'};
const scenarioNumbers = {capture_timeout:'01',delivery_loss:'02',true_failure:'03'};
const labels = {TASK_CREATED:'Task intent recorded',ORDER_CREATED:'Provider order created',STATE_RECONCILED:'Payment state reconciled',CAPTURE_REQUESTED:'Agent requested capture',CAPTURE_ACKNOWLEDGED:'Capture acknowledged',RESPONSE_DROPPED:'Capture response deliberately dropped',DUPLICATE_INTENT_BLOCKED:'Duplicate task payment blocked',ARTIFACT_COMMITTED:'Merchant persisted the result',DELIVERY_LOST:'Delivery response lost',DELIVERY_ACKNOWLEDGED:'Result received',MERCHANT_EXECUTION_FAILED:'Merchant execution terminated',AI_DIAGNOSIS:'Evidence interpreted',POLICY_OVERRIDE:'Policy overrode AI recommendation',POLICY_DECISION:'Recovery policy evaluated',RESULT_REDELIVERED:'Original result redelivered',REFUND_COMPLETED:'Provider confirmed refund',REFUND_PENDING:'Refund submitted, awaiting confirmation',VERIFIED_WEBHOOK:'Verified PayPal webhook',AI_REJECTED:'AI output unavailable or rejected',MERCHANT_MESSAGE:'Merchant message added',CAPTURE_UNCERTAIN:'Capture outcome uncertain',REFUND_UNCERTAIN:'Refund outcome uncertain'};
function disclosure(message,ready=true){const box=$('#disclosure');box.classList.toggle('ready',ready);box.innerHTML=`<span class="pulse"></span><span>${esc(message)}</span>`}
function taskProgress(t){
 const kinds=new Set(t.events.map(e=>e.kind));
 const steps=[['01','Intent',true],['02','Payment',Boolean(t.order_id)],['03','Work',Boolean(t.merchant_terminal||kinds.has('ARTIFACT_COMMITTED'))],['04','AI memo',Boolean(t.diagnosis)],['05','Recovery',Boolean(t.decision)]];
 const firstPending=steps.findIndex(s=>!s[2]);
 return steps.map((s,i)=>`<div class="progress-step ${s[2]?'done':i===firstPending?'active':''}"><b>${s[2]?'✓':s[0]}</b><span>${s[1]}</span></div>`).join('');
}
async function api(path, body) {
  const r = await fetch('/api'+path,{method:body === undefined ? 'GET':'POST',headers:{'Content-Type':'application/json','X-Rewind-Token':sessionStorage.getItem('rewind-token')||''},body:body===undefined?undefined:JSON.stringify(body)});
  const data = await r.json(); if(!r.ok) throw new Error(data.detail || 'Request failed'); return data;
}
function toast(message,error=false){const t=$('#toast');t.textContent=message;t.className='toast'+(error?' error':'');t.hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>t.hidden=true,6500)}
async function action(fn){if(busy)return;busy=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);try{await fn()}catch(e){toast(e.message,true)}finally{busy=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);$('#export').disabled=!selected;await refresh().catch(e=>toast(e.message,true))}}
async function refresh(){
 config=await api('/config');$('#mode').textContent=config.read_only?(config.provider==='sandbox'?'Sandbox run · replay':'Emulator · replay'):config.provider==='sandbox'?'PayPal sandbox':'Local emulator';
 $('#nav-mode').textContent=config.read_only?'Judge replay':config.provider==='sandbox'?'Sandbox connected':'Recovery ledger';
 disclosure((config.provider==='sandbox'?'PayPal sandbox adapter active. Real sandbox IDs appear after buyer approval. Merchant and injected failures are local simulations.':'Emulator rehearsal · Payment objects are simulated. No PayPal API calls are made.')+(config.ai==='fixture'?' AI diagnosis is an explicitly selected recorded fixture.':config.ai_configuration?.configured?' Live AI configured; each diagnosis reports whether inference succeeded.':' Live AI requires configuration. No recorded fixture will be substituted.'));
 $('#run').textContent=config.provider==='sandbox'?'Create sandbox task →':'Run scenario →';
 $('#lab-note').textContent=config.provider==='sandbox'?'Create task → create order → approve with sandbox buyer → capture → run merchant → diagnose → recover.':'One-click rehearsal uses the local emulator. All generated IDs begin with SIM-.';
 if(config.read_only){
  disclosure('READ-ONLY REPLAY · '+config.disclosure+' No live PayPal or AI calls are made.');
  document.querySelector('.lab').hidden=true;document.querySelector('.detail-actions').hidden=true;
  document.querySelector('.evidence-input').hidden=true;$('#settings').hidden=true;
 }
 const tasks=await api('/tasks');$('#count').textContent=tasks.length;$('#task-total').textContent=tasks.length;
 $('#delivered').textContent=tasks.filter(t=>t.scenario==='delivery_loss'&&t.delivery==='delivered').length;
 $('#refunded').textContent=tasks.filter(t=>t.payment==='refunded').length;
 $('#tasks').innerHTML=tasks.length?tasks.map(t=>`<button class="task-item ${selected===t.id?'selected':''}" data-id="${esc(t.id)}"><div class="task-item-top"><span class="task-index">CASE ${esc(scenarioNumbers[t.scenario]||'—')} · ${esc(t.id.slice(0,8))}</span><span class="tag ${esc(t.payment)}">${esc(t.payment)}</span></div><h3>${esc(names[t.scenario])}</h3><div class="task-meta"><span>$${esc(t.amount)} USD</span><time>${new Date(t.created).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}</time></div></button>`).join(''):'<div class="empty"><span>INBOX ZERO</span><strong>No cases yet.</strong><small>Open an incident profile to begin.</small></div>';
 $('#tasks').querySelectorAll('button').forEach(b=>b.onclick=()=>action(async()=>{selected=b.dataset.id;await detail()}));
 // Summaries come from persisted events, not increments in the browser.
 let blocked=0;for(const t of tasks){const d=await api('/tasks/'+t.id);blocked+=d.events.filter(e=>e.kind==='DUPLICATE_INTENT_BLOCKED').length}$('#blocked').textContent=blocked;
 if(!selected&&config.read_only&&tasks.length)selected=tasks[0].id;
 if(selected)await detail();
}
async function detail(){
 const t=await api('/tasks/'+selected);$('#detail-empty').hidden=true;$('#detail').hidden=false;$('#export').disabled=false;$('#task-label').textContent='TASK / '+t.id;
 $('#case-progress').innerHTML=taskProgress(t);
 $('#state').innerHTML=[['Payment','$',t.payment,t.last_verified?'Provider state verified':'Awaiting provider'],['Fulfillment','◇',t.fulfillment,t.merchant_terminal?'Merchant terminal state':'Merchant not finished'],['Delivery','✓',t.delivery,t.delivery==='delivered'?'Matching receipt verified':'Result not acknowledged']].map(([name,icon,value,note])=>`<div class="state-card"><div class="state-card-top"><label>${name.toUpperCase()}</label><span class="state-icon">${icon}</span></div><strong>${esc(value.replace('_',' '))}</strong><small>${esc(note)}</small></div>`).join('');
 const duplicate=t.events.some(e=>e.kind==='DUPLICATE_INTENT_BLOCKED');const outcome=$('#outcome');outcome.hidden=!(t.decision||duplicate);outcome.className='outcome'+(t.decision?.action==='review'?' warning':'');
 if(t.decision)outcome.innerHTML=`<strong>${esc({redeliver:'Result recovered. No additional charge.',refund:t.payment==='refunded'?'Refund confirmed by provider.':'Refund awaiting provider confirmation.',none:duplicate?'Original payment recovered. Duplicate blocked.':'Task reconciled.',review:'Recovery paused for review.'}[t.decision.action])}</strong><p>${esc(t.decision.reason)}</p>`;
 else if(duplicate)outcome.innerHTML='<strong>Duplicate task payment intercepted</strong><p>Existing provider capture recovered. No new order or charge created.</p>';
 $('#approve').hidden=!t.approval_url||t.payment!=='created';$('#approve').onclick=()=>{if(t.approval_url?.startsWith('https://www.sandbox.paypal.com/'))window.open(t.approval_url,'_blank','noopener,noreferrer');else toast('No approved sandbox redirect URL',true)};
 $('#artifact').hidden=config.read_only||t.delivery!=='delivered';$('#event-count').textContent=t.events.length+' EVENTS';
 $('#timeline').innerHTML=t.events.map(e=>`<li class="event ${/DROPPED|LOST|FAILED|BLOCKED|UNCERTAIN/.test(e.kind)?'alert':''} ${/OVERRIDE|REDELIVERED|REFUND_COMPLETED/.test(e.kind)?'recovery':''}"><div class="event-title"><span>${esc(labels[e.kind]||e.kind)}</span><time>${new Date(e.time).toLocaleTimeString([],{hour12:false})}</time></div><p>${esc(e.detail.reason||e.detail.message||e.detail.payment||e.detail.action||e.source)}</p><details><summary>Event ${e.seq} · ${esc(e.source)}</summary><pre>${esc(JSON.stringify(e.detail,null,2))}</pre></details></li>`).join('');
 const d=t.diagnosis;$('#diagnosis').innerHTML=d?`<span class="ai-badge">${d.live_ai?(config.read_only?'RECORDED LIVE AI · ':'LIVE AI · ')+esc(d.provider):d.provider==='fixture'?'RECORDED FIXTURE':'AI UNAVAILABLE'}</span><h3>${esc(d.cause||'Diagnosis unavailable')}</h3>${t.contradiction?'<div class="override">Fulfillment conflict · recovery held for review</div>':t.delivery_gap?'<div class="override">Delivery gap · artifact exists, receipt missing</div>':''}<p>${esc(d.explanation)}</p>${d.contradictions?.length?`<p><b>Contradictions</b><br>${d.contradictions.map(esc).join('<br>')}</p>`:''}<p><b>Evidence cited</b> · ${esc(d.evidence_ids?.join(', ')||'—')}</p>${t.decision?.ai_overridden?`<div class="override">Policy override → ${esc(t.decision.action)}<br>Financial actions require verified state.</div>`:''}<div class="recommend"><span>Suggested action</span><b>${esc(d.recommendation)}</b><span>${d.confidence!==undefined?Math.round(d.confidence*100)+'% confidence':'—'}</span></div>`:'<span class="ai-badge">AWAITING EVIDENCE</span><h3>No diagnosis yet.</h3><p>Run evidence interpretation after the merchant has executed.</p><p>AI suggestions remain advisory and cannot authorize a financial operation.</p>';
 $('#objects').innerHTML=[['Provider',t.provider],['Order ID',t.order_id],['Capture ID',t.capture_id],['Refund ID',t.refund_id],['Last reconciled',t.last_verified],['Result SHA-256',t.artifact_hash]].map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v||'—')}</dd>`).join('');
}
async function step(name,body={}){await api('/tasks/'+selected+'/'+name,body);await refresh()}
async function create(){const t=await api('/tasks',{scenario:$('#scenario').value});selected=t.id;await refresh();return t}
$('#run').onclick=()=>action(async()=>{await create();if(config.provider==='sandbox'){toast('Task created. Create an order, then approve with your sandbox buyer.');return}await step('order');await step('capture',{drop_response:$('#scenario').value==='capture_timeout'});if($('#scenario').value==='capture_timeout')await step('capture');await step('merchant');await step('diagnose');await step('recover');toast('Scenario complete. Inspect the recovery timeline.');});
$('#new').onclick=()=>action(create);$('#refresh').onclick=()=>action(refresh);
document.querySelectorAll('[data-action]').forEach(b=>b.onclick=()=>action(async()=>{if(!selected)return;const a=b.dataset.action;await step(a==='drop'?'capture':a,a==='drop'?{drop_response:true}:{});}));
$('#add-evidence').onclick=()=>action(async()=>{const message=$('#message').value.trim();if(!message)return;await step('evidence',{message});$('#message').value='';toast('Evidence added. Run diagnosis to interpret it.');});
function download(data,name,type){const url=URL.createObjectURL(new Blob([data],{type}));const a=document.createElement('a');a.href=url;a.download=name;a.click();URL.revokeObjectURL(url)}
$('#export').onclick=()=>action(async()=>download(JSON.stringify(await api('/tasks/'+selected+'/evidence-package'),null,2),'rewind-evidence-'+selected.slice(0,8)+'.json','application/json'));
$('#artifact').onclick=()=>action(async()=>{const r=await fetch('/api/tasks/'+selected+'/artifact',{headers:{'X-Rewind-Token':sessionStorage.getItem('rewind-token')||''}});if(!r.ok)throw new Error('Result unavailable');download(await r.text(),'recovered-report.txt','text/plain')});
$('#settings').onclick=()=>{$('#token').value=sessionStorage.getItem('rewind-token')||'';$('#settings-dialog').showModal()};$('#save-token').onclick=()=>{sessionStorage.setItem('rewind-token',$('#token').value);setTimeout(()=>action(refresh),0)};
const approved=new URLSearchParams(location.search).get('approved');if(approved&&/^[a-f0-9]{32}$/.test(approved)){selected=approved;toast('Buyer returned from sandbox. Capture after verifying approval.');history.replaceState({},'',location.pathname)}
refresh().catch(e=>{toast(e.message,true);if(/token/.test(e.message))$('#settings-dialog').showModal()});
