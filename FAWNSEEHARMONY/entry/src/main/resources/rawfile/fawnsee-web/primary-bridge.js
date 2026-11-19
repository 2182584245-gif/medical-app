/* Same primary DOM; communicate only with its immediate parent prototype. */
const sharedImages=[...document.querySelectorAll('[data-shared-image]')];
window.addEventListener('message',event=>{
  if(event.source!==parent||event.data?.type!=='fawnsee-assets')return;
  sharedImages.forEach(image=>{const src=event.data.assets?.[image.dataset.sharedImage];if(src)image.src=src;});
  const y=Math.max(0,Number(event.data.scrollY)||0);const restoreAllowed=Math.abs(window.scrollY)<1;if(restoreAllowed)window.scrollTo(0,y);const actualY=window.scrollY;const ready=Promise.all([document.fonts?.ready||Promise.resolve(),...sharedImages.map(image=>image.decode?.().catch(()=>{})||Promise.resolve())]);ready.finally(()=>requestAnimationFrame(()=>requestAnimationFrame(()=>{if(restoreAllowed&&Math.abs(window.scrollY-actualY)<1)window.scrollTo(0,y);parent.postMessage({type:'fawnsee-primary-ready'},'*')})));
});
parent.postMessage({type:'fawnsee-assets-ready',keys:[...new Set(sharedImages.map(image=>image.dataset.sharedImage))]},'*');
let scrollScheduled=false;
window.addEventListener('scroll',()=>{if(scrollScheduled)return;scrollScheduled=true;requestAnimationFrame(()=>{scrollScheduled=false;parent.postMessage({type:'fawnsee-primary-scroll',scrollY:window.scrollY},'*');});},{passive:true});
document.addEventListener('click',event=>{
  const target=event.target.closest('[data-parent-action],[data-parent-route]');
  if(!target)return;
  event.preventDefault();event.stopPropagation();
  if(target.dataset.parentAction==='auth-visibility'){const input=target.closest('div')?.querySelector('input');if(input)input.type=input.type==='password'?'text':'password';return;}
  if(target.dataset.parentAction==='auth-enter'){
    const form=document.querySelector('[data-primary-auth-form]');if(!form||form.dataset.authPending==='true')return;
    let note=form.querySelector('[data-auth-error]');if(!note){note=document.createElement('p');note.dataset.authError='true';note.style.cssText='color:#a2473b;font-size:14px;line-height:1.6';note.setAttribute('role','alert');form.append(note);}
    const fields=[...form.querySelectorAll('input:not([type=checkbox])')];
    if(!fields[0]?.value.trim()||!fields[1]?.value||fields[1].value.length>256||(form.dataset.primaryAuthForm==='register'&&fields[1].value.length<8)){note.textContent='请填写账号和至少8位密码。';return;}
    if(fields[2]&&fields[2].value!==fields[1].value){note.textContent='两次输入的密码需要一致。';return;}
    const consent=form.querySelector('input[type=checkbox]');if(consent&&!consent.checked){note.textContent='请先阅读并同意使用条款。';return;}
    note.textContent='';
    form.dataset.authPending='true';document.querySelectorAll('[data-parent-action=auth-enter]').forEach(b=>b.disabled=true);parent.postMessage({type:'fawnsee-primary-action',action:'auth-enter',value:form.dataset.primaryAuthForm,credentials:{username:fields[0].value.trim(),password:fields[1].value}},'*');return;
  }
  if(target.dataset.parentAction){parent.postMessage({type:'fawnsee-primary-action',action:target.dataset.parentAction,id:target.dataset.id,day:target.dataset.day,topic:target.dataset.topic,value:target.dataset.value??document.querySelector('[data-primary-chat-input]')?.value,scrollY:window.scrollY},'*');return;}
  const value=target.dataset.parentRoute,route=value.startsWith('#')?value:'#'+value;
  parent.postMessage({type:'fawnsee-primary-route',route,scrollY:window.scrollY},'*');
});
document.addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing&&event.keyCode!==229&&event.target.matches('[data-primary-chat-input]')){event.preventDefault();document.querySelector('[data-parent-action=ai-send]')?.click();}else if((event.key==='Enter'||event.key===' ')&&event.target.matches('[role="link"],[role="button"]')){event.preventDefault();event.target.click();}else if(event.key==='Enter'&&!event.isComposing&&event.target.closest('[data-primary-auth-form]')){event.preventDefault();document.querySelector('[data-parent-action=auth-enter]')?.click();}});
function primaryInput(event){const input=event.target.closest('[data-parent-input]');if(input)parent.postMessage({type:'fawnsee-primary-action',action:input.dataset.parentInput,value:input.type==='checkbox'?input.checked:input.value,scrollY:window.scrollY},'*');}
document.addEventListener('input',primaryInput);
document.addEventListener('change',event=>{if(event.target.type==='checkbox')primaryInput(event);});


window.addEventListener('message',e=>{if(e.source!==parent)return;if(e.data?.type==='fawnsee-auth-result'){const f=document.querySelector('[data-primary-auth-form]');if(f)delete f.dataset.authPending;let n=document.querySelector('[data-auth-error]');if(n)n.textContent=e.data.error||'';document.querySelectorAll('[data-parent-action=auth-enter]').forEach(b=>b.disabled=false);if(!e.data.error)[...f.querySelectorAll('input:not([type=checkbox])')].slice(1).forEach(n=>n.value='');}});
