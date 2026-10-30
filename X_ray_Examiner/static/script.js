const csrfToken=document.querySelector('meta[name=csrf-token]')?.content;
document.body.classList.toggle('dark', localStorage.getItem('medpeek-theme') === 'dark');
document.querySelector('#theme-toggle')?.addEventListener('click', () => {document.body.classList.toggle('dark');localStorage.setItem('medpeek-theme',document.body.classList.contains('dark')?'dark':'light');});
// Heart spinner: the word changes every 6.6 s and ends on "Finalizing".
const MP_WORDS=['Thinking','Pondering','Honking','Processing','Compiling'];
const MP_HEART='<svg class="mp-heart" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 21.3 10.6 20C5.4 15.4 2 12.3 2 8.5 2 5.4 4.4 3 7.5 3c1.7 0 3.4.8 4.5 2.1C13.1 3.8 14.8 3 16.5 3 19.6 3 22 5.4 22 8.5c0 3.8-3.4 6.9-8.6 11.5L12 21.3z"/></svg>';
let mpRunStart=null;
function mpWord(){if(!mpRunStart)mpRunStart=Date.now();const i=Math.floor((Date.now()-mpRunStart)/6600);return i<MP_WORDS.length?MP_WORDS[i]:'Finalizing';}
function thinkingNode(note,word){const w=document.createElement('div');w.className='mp-thinking';w.innerHTML=MP_HEART+'<div><div class="mp-word"></div><p class="mp-note"></p></div>';const label=w.querySelector('.mp-word');label.textContent=(word||mpWord())+'…';if(word)label.dataset.fixed='1';w.querySelector('.mp-note').textContent=note||'';return w;}
setInterval(()=>document.querySelectorAll('.mp-word:not([data-fixed])').forEach(n=>{n.textContent=mpWord()+'…';}),300);
const form=document.querySelector('#chat-form');
if(form){
 const files=document.querySelector('#xray_upload'), input=form.querySelector('textarea'), messages=document.querySelector('#messages'), windowEl=document.querySelector('#chat-window'), error=document.querySelector('#form-error');
 let chatId=new URL(location.href).searchParams.get('chat'),busy=false,loading=false,signature='',pendingBefore=new Set(),finalizing=new Map();
 const controls=[document.querySelector('#send'),document.querySelector('#attach'),document.querySelector('#clear-chat')];
 async function api(path,options={}){const r=await fetch(path,{...options,headers:{'X-CSRF-Token':csrfToken},signal:AbortSignal.timeout(90000)});let data;try{data=await r.json();}catch{throw Error('The request could not be completed.');}if(!r.ok)throw Error(data.error||'The conversation could not be loaded.');return data;}
 function bubble(role,text){const el=document.createElement('article');el.className='message '+role;const label=document.createElement('div');label.className='label';label.textContent=role==='user'?'YOU':'MEDPEEK EXAMINER';const content=document.createElement('div');content.className='bubble';content.textContent=text;el.append(label,content);messages.append(el);return {el,content};}
 async function list(){const data=await api('/api/chats');const box=document.querySelector('#history-list');box.replaceChildren();if(!data.chats.length){box.textContent='Your conversations will appear here.';return;}for(const c of data.chats){const link=document.createElement('a');link.href='/results?chat='+encodeURIComponent(c.id);link.textContent=c.title;link.title=new Date(c.updated*1000).toLocaleString();if(c.id===chatId)link.setAttribute('aria-current','page');link.onclick=e=>{e.preventDefault();if(busy)return;pendingBefore=new Set();chatId=c.id;history.replaceState(null,'','/results?chat='+chatId);signature='';load().catch(showError);};box.append(link);}}
 function showError(e){error.textContent=e.message;}
 async function load(){if(!chatId||loading)return;loading=true;try{const c=await api('/api/chats/'+encodeURIComponent(chatId));document.querySelector('#image-count').textContent=c.image_count+'/20 images';const sig=JSON.stringify(c.messages);if(sig!==signature){const nearBottom=windowEl.scrollHeight-windowEl.scrollTop-windowEl.clientHeight<100;signature=sig;messages.replaceChildren();if(c.messages.length)document.querySelector('#welcome')?.remove();const pendingNow=new Set();c.messages.forEach((m,i)=>{const b=bubble(m.role,m.text);const key=chatId+':'+i;if(m.result&&pendingBefore.has(key)&&!finalizing.has(key)){finalizing.set(key,Date.now()+1200);setTimeout(()=>{signature='';load().catch(showError);},1300);}if(m.result&&finalizing.has(key)&&Date.now()<finalizing.get(key)){b.el.classList.add('pending');b.content.replaceChildren(thinkingNode('Putting your report together.','Finalizing'));}else if(m.result)renderAnalysis(b.content,m.result,m.text);if(['sending','queued','running'].includes(m.status)){pendingNow.add(key);b.el.classList.add('pending');b.content.replaceChildren(thinkingNode(m.status==='sending'?'Sending your image…':'Gemma 4 is reading your image. This chat is saved; you can return later.'));}if(m.status==='error')b.el.classList.add('error');});pendingBefore=pendingNow;if(!pendingNow.size&&!busy)mpRunStart=null;if(nearBottom)windowEl.scrollTop=windowEl.scrollHeight;}await list();}finally{loading=false;}}
 async function ensureChat(){if(!chatId){const c=await api('/api/chats',{method:'POST'});chatId=c.id;history.replaceState(null,'','/results?chat='+chatId);}}
 document.querySelectorAll('[data-prompt]').forEach(button=>button.onclick=()=>{input.value=button.dataset.prompt;input.focus();input.dispatchEvent(new Event('input'));});
 document.querySelector('#attach').onclick=()=>files.click();
 files.onchange=()=>document.querySelector('#file-selection-text').textContent=Array.from(files.files).map(f=>f.name).join(' · ');
 input.oninput=()=>{input.style.height='auto';input.style.height=input.scrollHeight+'px';};
 input.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();form.requestSubmit();}};
 document.querySelector('#clear-chat').onclick=()=>{if(!busy)location.href='/results?new=1';};
 form.onsubmit=async e=>{e.preventDefault();if(busy)return;error.textContent='';const selected=Array.from(files.files),text=input.value.trim();if(!selected.length&&!text)return;if(selected.length>5){error.textContent='Choose up to 5 images at a time.';return;}if(selected.some(f=>f.size>25*1024*1024)){error.textContent='Each image must be smaller than 25 MB.';return;}if(selected.length&&!form.elements.body_part.value){error.textContent='Choose the body area.';return;}
 busy=true;controls.forEach(c=>c.disabled=true);let succeeded=false;
 try{await ensureChat();document.querySelector('#welcome')?.remove();bubble('user',[text,...selected.map(f=>'Attached: '+f.name)].filter(Boolean).join('\n'));mpRunStart=Date.now();const pending=bubble('bot','');pending.content.replaceChildren(thinkingNode('Saving and sending…'));pending.el.classList.add('pending');windowEl.scrollTop=windowEl.scrollHeight;
 for(const file of (selected.length?selected:[null])){const data=new FormData();data.append('chat_id',chatId);data.append('user_message',text);data.append('body_part',form.elements.body_part.value);data.append('modality',form.elements.modality.value);if(file)data.append('xray_image',file);await api('/api/submit',{method:'POST',body:data});}
 succeeded=true;input.value='';files.value='';document.querySelector('#file-selection-text').textContent='';
 }catch(e){showError(e);}finally{busy=false;controls.forEach(c=>c.disabled=false);signature='';await load().catch(showError);if(succeeded)input.focus();}};
 setInterval(()=>{if(chatId&&!busy&&!document.hidden)load().catch(showError);},4000);
 (async()=>{if(!chatId&&!new URL(location.href).searchParams.has('new')){const data=await api('/api/chats');if(data.chats.length){chatId=data.chats[0].id;history.replaceState(null,'','/results?chat='+chatId);}}await list();await load();})().catch(showError);
 window.addEventListener('beforeunload',e=>{if(busy){e.preventDefault();e.returnValue='';}});
}


function renderAnalysis(container, result, fallback) {
 if (!result || typeof result !== 'object') {container.textContent=fallback||'No result returned.';return;}
 container.replaceChildren();container.classList.add('analysis-card');
 const add=(parent,tag,text,cls)=>{const node=document.createElement(tag);if(cls)node.className=cls;node.textContent=text;parent.append(node);return node;};
 const section=(title)=>{const block=document.createElement('section');block.className='analysis-section';add(block,'h3',title);container.append(block);return block;};
 const list=(parent,items)=>{if(!Array.isArray(items)||!items.length)return;const ul=document.createElement('ul');items.forEach(item=>add(ul,'li',String(item)));parent.append(ul);};
 if(result.mock)add(container,'div','Demo result · Simulated answer','analysis-alert');
 if(result.urgency_notice || ['urgent','emergency'].includes(result.urgency))add(container,'div',result.urgency_notice||('Follow-up priority: '+result.urgency),'analysis-alert');
 add(container,'div',[result.body_part_label,result.modality_label].filter(Boolean).join(' · '),'analysis-meta');
 add(container,'h2','Your image, explained');add(container,'p',result.summary||'No summary provided.','analysis-summary');
 if(result.is_medical_image===false || result.image_quality==='unreadable')add(container,'p','This image could not be interpreted as a readable medical scan.','analysis-limit');
 if(Array.isArray(result.findings)&&result.findings.length){const block=section('What the examiner noticed');result.findings.forEach(f=>{const item=document.createElement('div');item.className='finding';add(item,'h4',f.finding||'Finding');if(f.location)add(item,'div',f.location,'analysis-meta');if(f.explanation)add(item,'p',f.explanation);if(f.supported_by_reference_data===false)add(item,'small','Not supported by the available reference data.');block.append(item);});}
 if(result.next_steps?.length)list(section('What to do next'),result.next_steps);
 const referral=result.referral;
 if(referral?.required){const block=section('Who to follow up with');add(block,'h4',referral.specialist);if(referral.timeframe)add(block,'p',referral.timeframe);if(referral.reason)add(block,'p',referral.reason);}
 if(result.safety_notes?.length)list(section('Limitations to keep in mind'),result.safety_notes);
 const details=document.createElement('details');details.className='analysis-details';add(details,'summary','References & questions for your doctor');
 if(result.coverage_note)add(details,'p',result.coverage_note);
 if(result.reference_comparison)add(details,'p',result.reference_comparison);
 if(result.questions_for_doctor?.length){add(details,'h4','Questions to bring to your appointment');list(details,result.questions_for_doctor);}
 container.append(details);
 add(container,'div',result.ai_notice||'MedPeek is AI and can make mistakes.','analysis-alert');
 add(container,'p',result.disclaimer||'Educational information, not a diagnosis. Review findings with a healthcare professional.','analysis-disclaimer');
 // Ask at the bottom of every report whether the patient wants a PDF copy.
 const ask=document.createElement('div');ask.className='pdf-ask';
 add(ask,'p','Would you like a PDF copy of this MedPeek report, with the date and your scan details, to bring to your doctor?');
 const row=document.createElement('div');row.className='row';
 const yes=add(row,'button','Yes, download the PDF','mp-primary');yes.type='button';
 const no=add(row,'button','No, thanks');no.type='button';
 ask.append(row);container.append(ask);
 no.onclick=()=>ask.replaceChildren(Object.assign(document.createElement('p'),{textContent:'No problem. You can use "Report a problem" at the top if anything looks wrong.'}));
 yes.onclick=async()=>{yes.disabled=true;yes.textContent='Making your PDF…';try{const r=await fetch('/api/report-pdf',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify(result)});if(!r.ok){let msg='The PDF could not be made.';try{msg=(await r.json()).error||msg;}catch{}throw Error(msg);}const url=URL.createObjectURL(await r.blob());const a=document.createElement('a');a.href=url;a.download='MedPeek-report-'+(result.id||'report')+'.pdf';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),5000);yes.textContent='PDF downloaded';}catch(e){yes.disabled=false;yes.textContent='Yes, download the PDF';const err=ask.querySelector('.mp-note')||add(ask,'p','','mp-note');err.textContent=e.message;}};
}

// "Report a problem": a mistake in a report, a bug, or any other issue.
(()=>{const modal=document.querySelector('#report-modal'),open=document.querySelector('#report-open');if(!modal||!open)return;
 const status=document.querySelector('#report-status'),send=document.querySelector('#report-send');
 const close=()=>{modal.hidden=true;};
 open.onclick=()=>{status.textContent='';modal.hidden=false;document.querySelector('#report-message').focus();};
 document.querySelector('#report-cancel').onclick=close;
 modal.addEventListener('click',e=>{if(e.target===modal)close();});
 document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!modal.hidden)close();});
 document.querySelector('#report-form').onsubmit=async e=>{e.preventDefault();send.disabled=true;status.textContent='Sending…';
  const reportId=new URL(location.href).searchParams.get('chat')||'';
  try{const r=await fetch('/api/report-issue',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({kind:document.querySelector('#report-kind').value,message:document.querySelector('#report-message').value,contact:document.querySelector('#report-contact').value,report_id:reportId})});
   const data=await r.json().catch(()=>({}));if(!r.ok)throw Error(data.error||'Your report could not be sent.');
   status.textContent='Thanks! Your ticket is '+data.ticket+'.';document.querySelector('#report-message').value='';setTimeout(close,1800);
  }catch(err){status.textContent=err.message;}finally{send.disabled=false;}};
})();
