(function(root) {
  'use strict';
  const routes = new Map([['#auth/login','phone'],['#auth/password','login'],['#auth/register','register']]);
  const labels = {phone:'手机号登录或注册', login:'账号密码登录', register:'账号密码注册'};
  const pages = new Map();
  let active = null;

  const eye = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" aria-hidden="true"><path d="M2 12s3.5-6.5 10-6.5S22 12 22 12s-3.5 6.5-10 6.5S2 12 2 12Z"/><circle class="pupil" cx="12" cy="12" r="2.8"/><path class="slash" d="m4 3.5 16 17"/></svg>';

  function passwordField(name, label, placeholder, completion) {
    return `<label class="field-label" for="auth-${name}">${label} *</label>
      <div class="field-row secret-row"><input class="field" id="auth-${name}" name="${name}" type="password" maxlength="1024" placeholder="${placeholder}" autocomplete="${completion}">
      <button type="button" class="eye" data-parent-action="auth-visibility" aria-label="显示或隐藏${label}" aria-pressed="false">${eye}</button></div>`;
  }

  function makePage(mode) {
    const phone = mode === 'phone';
    const registration = mode === 'register';
    const submit = phone ? '验证并继续' : registration ? '注册并登录' : '登录';
    const fields = phone ? `<label class="field-label" for="auth-phone">手机号 *</label>
      <input class="field" id="auth-phone" name="account" type="tel" inputmode="numeric" maxlength="11" autocomplete="tel-national" placeholder="请输入手机号">
      <label class="field-label" for="auth-code">短信验证码 *</label>
      <div class="field-row"><input class="field" id="auth-code" name="password" type="text" inputmode="numeric" maxlength="6" autocomplete="one-time-code" placeholder="请输入验证码">
      <button class="code-send" type="button" data-entry-send>发送验证码</button></div>`
      : `<label class="field-label" for="auth-account">账号 *</label>
      <input class="field" id="auth-account" name="account" type="text" maxlength="64" autocomplete="username" placeholder="${registration ? '设置账号名（最多64个字符）' : '请输入账号'}">
      ${passwordField('password','密码',registration ? '设置密码（至少8个字符）' : '请输入密码',registration ? 'new-password' : 'current-password')}
      ${registration ? passwordField('confirm','确认密码','请再次输入密码','new-password') : ''}`;
    const alternatives = [...routes].filter(([,target]) => target !== mode)
      .map(([route,target]) => `<button type="button" data-entry-route="${route}">${labels[target]}  ›</button>`).join('');
    return `<!doctype html><html lang="zh-CN" data-harmony-auth><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
      <title>${labels[mode]}</title><link rel="stylesheet" href="harmony-auth.css"></head><body>
      <img class="wallpaper" src="assets/mobile-design/images/auth-watercolor1612.webp" alt="" aria-hidden="true"><main>
      <img class="brand" src="assets/harmony/records-logo.png" alt="凡思"><div class="badge">●  会员端${registration ? '注册' : '登录'}</div>
      <h1>${registration ? '创建会员账号' : '欢迎回来'}</h1><form data-entry-mode="${mode}" novalidate>${fields}
      <div class="options"><span class="option-group"><label class="check-label"><input type="checkbox" id="auth-terms">我已阅读并同意</label>
      <button type="button" data-entry-native="privacy.document">《隐私政策》</button></span>
      <label class="check-label"><input type="checkbox" data-auth-auto-login id="auth-auto-login">自动登录</label></div>
      <button class="submit" type="submit" data-parent-action="auth-enter" data-purpose="primary-submit-button">${submit}  →</button>
      <div class="route-links">${alternatives}</div></form>
      <footer><button type="button" data-entry-route="#ai">继续体验</button><button type="button" data-entry-native="privacy.settings">管理本机隐私选择</button></footer>
      </main></body></html>`;
  }

  function clear(state) {
    root.clearInterval(state.cooldown);
    state.challenge=null;
    const form=state.frame?.contentDocument?.querySelector('form[data-entry-mode]');
    if(form){
      form.reset();
      form.querySelectorAll('input:not([type=checkbox])').forEach(input=>{input.value='';});
      for(const eye of form.querySelectorAll('[data-parent-action=auth-visibility]')){
        eye.parentElement.querySelector('input').type='password';
        eye.setAttribute('aria-pressed','false');
      }
    }
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
      if(!form.querySelector('#auth-terms').checked){show('请先阅读并同意隐私政策。');return;}
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
      if(!form.querySelector('#auth-terms').checked){show('请先阅读并同意隐私政策。');return;}
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
    doc.addEventListener('click',event=>{
      const button=event.target.closest('button');if(!button||state.busy)return;
      event.preventDefault();
      if(button.dataset.entryRoute){state.go(button.dataset.entryRoute,true);return;}
      if(button.dataset.entryNative){
        root.FAWNSEE_MOBILE.native(button.dataset.entryNative,{}).catch(error=>{if(active===state)show(error.message);});return;
      }
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
    if(!pages.has(mode))pages.set(mode,makePage(mode));
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
