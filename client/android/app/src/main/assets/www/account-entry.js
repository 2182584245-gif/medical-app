(function(root) {
  'use strict';
  const routes = new Map([['#auth/login','phone'],['#auth/password','login'],['#auth/register','register']]);
  const labels = {phone:'手机号登录或注册', login:'账号密码登录', register:'账号密码注册'};
  const pages = new Map();
  let active = null;

  function makePage(mode, source) {
    const doc = new DOMParser().parseFromString(source, 'text/html');
    doc.querySelectorAll('script').forEach(node => node.remove());
    const form = doc.querySelector('form');
    form.dataset.entryMode = mode;
    doc.title = labels[mode];
    doc.querySelector('h1').textContent = mode === 'phone' ? '手机号登录或注册' : mode === 'register' ? '创建你的账号' : '欢迎回来';
    const badge = [...doc.querySelectorAll('header span')].find(node => node.textContent.trim() === '会员端登录');
    if (badge && mode === 'register') badge.textContent = '会员端注册';
    const account = form.querySelector('[name=account]');
    const secret = form.querySelector('[name=password]');
    account.maxLength = 64;
    secret.maxLength = 1024;
    account.autocomplete = 'username';
    secret.autocomplete = mode === 'register' ? 'new-password' : 'current-password';
    for (const option of form.querySelectorAll('input[type=checkbox]')) option.defaultChecked = true;
    const primary = form.querySelector('[data-purpose=primary-submit-button]');
    primary.textContent = mode === 'register' ? '注册并登录' : mode === 'phone' ? '验证并继续' : '登录';
    if (mode === 'register') {
      secret.placeholder = '请设置至少8位密码';
      const copy = secret.closest('[data-purpose=password-input-group]').cloneNode(true);
      copy.removeAttribute('data-purpose');
      copy.querySelectorAll('[id]').forEach(node => node.removeAttribute('id'));
      copy.querySelector('label').htmlFor = 'confirm-password';
      copy.querySelector('label').firstChild.textContent = '确认密码 ';
      const confirm = copy.querySelector('input');
      confirm.id = 'confirm-password';
      confirm.name = 'confirm';
      confirm.placeholder = '请再次输入密码';
      secret.closest('[data-purpose=password-input-group]').after(copy);
    } else if (mode === 'phone') {
      form.querySelector('label[for=account-id]').firstChild.textContent = '手机号 ';
      account.type='tel'; account.inputMode='numeric'; account.maxLength=11;
      account.autocomplete='tel-national';account.placeholder='请输入手机号';
      form.querySelector('label[for=password-id]').firstChild.textContent = '短信验证码 ';
      secret.type='text'; secret.inputMode='numeric'; secret.minLength=6; secret.maxLength=6;
      secret.autocomplete='one-time-code'; secret.placeholder='请输入验证码';
      secret.style.paddingRight='7em';
      const send=form.querySelector('[data-parent-action=auth-visibility]');
      send.removeAttribute('data-parent-action');
      send.setAttribute('aria-label','发送验证码');send.dataset.entrySend='';send.textContent='发送验证码';
      send.style.cssText='position:absolute;right:8px;width:auto;padding:5px 6px;color:#216657;font-size:13px';
    }
    const template = form.querySelector('[data-parent-action=auth-other-method]');
    const alternatives = doc.createElement('div');
    alternatives.style.cssText='display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:6px';
    for (const [route, target] of routes) {
      if (target === mode) continue;
      const link = template.cloneNode(false);
      link.removeAttribute('data-parent-action');link.dataset.entryRoute=route;
      link.textContent=labels[target]+' ›';
      link.style.cssText='width:auto;margin:0';
      alternatives.appendChild(link);
    }
    template.remove();
    primary.after(alternatives);
    return '<!doctype html>'+doc.documentElement.outerHTML;
  }

  function clear(state) {
    root.clearInterval(state.cooldown);
    state.challenge=null;
    const form=state.frame?.contentDocument?.querySelector('form[data-entry-mode]');
    if(form){form.reset();form.querySelectorAll('input:not([type=checkbox])').forEach(input=>{input.value='';});}
  }
  function bind(state) {
    const doc=state.frame.contentDocument;
    const form=doc?.querySelector('form[data-entry-mode]');
    if (!form || form.dataset.entryMode!==state.mode || active!==state) return;
    const show=(text, success=false)=>{
      let note=form.querySelector('[data-auth-error]');
      if(!note){note=doc.createElement('p');note.dataset.authError='';note.setAttribute('role','status');form.querySelector('[data-purpose=primary-submit-button]').before(note);}
      note.hidden=!text;note.style.cssText='font-size:14px;line-height:1.5;color:'+(success?'#216657':'#943e31');
      note.textContent=text;
    };
    const refreshSend=()=>{
      const button=form.querySelector('[data-entry-send]');
      if(!button)return;
      const remaining=Math.max(0,Math.ceil(((state.challenge?.retryAt||0)-Date.now())/1000));
      button.disabled=state.busy||remaining>0;
      button.textContent=remaining?remaining+'秒':'发送验证码';
      if(!remaining)root.clearInterval(state.cooldown);
    };
    const busy=(value)=>{state.busy=value;form.querySelectorAll('button,input').forEach(node=>{node.disabled=value;});refreshSend();};
    busy(false);show('');
    // Each visit binds fresh state to a cached document. Remove the old visit's listeners.
    state.listeners=new AbortController();
    const opt={signal:state.listeners.signal};
    const account=form.querySelector('[name=account]'), secret=form.querySelector('[name=password]');
    async function send() {
      const number=account.value.trim();
      if(!/^1[3-9]\d{9}$/.test(number)){show('请输入有效的11位手机号。');return;}
      busy(true);
      try {
        const reply=await root.FAWNSEE_MOBILE.native('auth.phone.send',{phone:number,purpose:'login'});
        if(active!==state)return;
        if(!reply?.challenge_id || !(reply.expires_in>0))throw Error('未能确认验证码，请重新发送。');
        const retry=Math.max(0,Number(reply.retry_after)||0);
        state.challenge={id:reply.challenge_id,number,expires:Date.now()+reply.expires_in*1000,retryAt:Date.now()+retry*1000};
        root.clearInterval(state.cooldown);
        if(retry)state.cooldown=root.setInterval(refreshSend,500);
        show('验证码已发送。',true);
      } catch(error){if(active===state)show(error.message);}
      finally{if(active===state)busy(false);}
    }
    async function submit(){
      if(state.busy)return;
      const name=account.value.trim(), password=secret.value;
      if(!name){show(state.mode==='phone'?'请输入手机号。':'请输入账号。');return;}
      if(!form.querySelector('input[type=checkbox]').checked){show('请先阅读并同意使用条款。');return;}
      const auto=form.querySelector('[data-auth-auto-login]').checked;
      let action,payload;
      if(state.mode==='phone'){
        if(!/^1[3-9]\d{9}$/.test(name)){show('请输入有效的11位手机号。');return;}
        const c=state.challenge;
        if(!c||c.number!==name||c.expires<=Date.now()){show('请先获取这个手机号的验证码。');return;}
        if(!/^\d{6}$/.test(password)){show('请输入6位短信验证码。');return;}
        action='auth.phone.quick-entry';payload={phone:name,challenge_id:c.id,code:password,auto_login:auto};
      }else{
        if(!password || (state.mode==='register'&&Array.from(password).length<8)){show(state.mode==='register'?'请设置至少8位密码。':'请输入密码。');return;}
        if(state.mode==='register'&&form.querySelector('[name=confirm]').value!==password){show('两次输入的密码需要一致。');return;}
        action=state.mode==='register'?'auth.register':'auth.login';
        payload={username:name,password,auto_login:auto};
      }
      busy(true);show('');
      try {await root.FAWNSEE_MOBILE.authenticate(action,payload);}
      catch(error){
        if(active!==state)return;
        if(error.code==='registration_login_required'){
          root.FAWNSEE_AUTH_ENTRIES.notice='账号已创建，请用刚设置的密码登录。';
          state.go('#auth/password',true);
        }else show(error.message||'未能完成请求，请重试。');
      }finally{payload.password='';payload.code='';if(active===state)busy(false);}
    }
    form.addEventListener('input',event=>{if(event.target===account){state.challenge=null;refreshSend();}show('');},opt);
    form.addEventListener('submit',event=>{event.preventDefault();submit();},opt);
    form.addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.isComposing&&event.target.matches('input:not([type=checkbox])')){event.preventDefault();submit();}},opt);
    form.addEventListener('click',event=>{
      const button=event.target.closest('button');if(!button||state.busy)return;
      event.preventDefault();
      if(button.dataset.entryRoute){state.go(button.dataset.entryRoute,true);return;}
      if(button.hasAttribute('data-entry-send')){send();return;}
      if(button.dataset.parentAction==='auth-visibility'){
        const input=button.parentElement.querySelector('input');
        input.type=input.type==='password'?'text':'password';
        button.setAttribute('aria-pressed',String(input.type==='text'));return;
      }
      if(button.dataset.parentAction==='auth-enter')submit();
    },opt);
    if(root.FAWNSEE_AUTH_ENTRIES.notice){show(root.FAWNSEE_AUTH_ENTRIES.notice,true);root.FAWNSEE_AUTH_ENTRIES.notice='';}
  }

  function render(route, template, go) {
    if(active?.route!==route&&active){active.listeners?.abort();clear(active);active=null;}
    const mode=routes.get(route);
    if(!mode)return false;
    root.document.querySelector('#bottom').style.display='none';
    if(active?.route===route)return true;
    if(!pages.has(mode))pages.set(mode,makePage(mode,template()));
    root.FAWNSEE_EXPERIENCE.primary('auth-entry-'+mode,pages.get(mode));
    const frame=root.document.querySelector('#main-frame');
    if(!frame)return false;
    frame.classList.add('f-auth-frame');
    const state={route,mode,frame,go,busy:false,challenge:null,listeners:null};active=state;
    frame.setAttribute('title',labels[mode]);
    if(frame.contentDocument?.querySelector('form[data-entry-mode]'))bind(state);
    else frame.addEventListener('load',()=>bind(state),{once:true});
    return true;
  }
  root.FAWNSEE_AUTH_ENTRIES={render,notice:''};
})(globalThis);
