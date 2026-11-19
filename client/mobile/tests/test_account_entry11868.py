"""Exercise the packaged Android forms in Chromium using an isolated native bridge."""
from pathlib import Path
from base64 import b64encode
import json
import os

from playwright.sync_api import sync_playwright, expect


def test_three_account_entry_forms(tmp_path):
    assets = Path(__file__).resolve().parents[2] / "android/app/src/main/assets/www"
    bundle = (assets / "app.js").read_text(encoding="utf-8")
    head = bundle.index("const MAIN=") + len("const MAIN=")
    template = json.JSONDecoder().raw_decode(bundle[head:])[0]["login"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 393, "height": 852})
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.set_content('<style>*{box-sizing:border-box}body{margin:0}iframe{width:100%;height:100vh;border:0}</style><div id="bottom"></div>')
        page.add_script_tag(path=assets / "account-entry.js")
        logo = (assets / "assets/mobile-design/images/logo-symbol.svg").read_bytes()
        page.evaluate("""([tpl,logo])=>{
          window.renderAccount=route=>FAWNSEE_AUTH_ENTRIES.render(route,()=>tpl,renderAccount);
          window.entries=new Map();window.actions=[];window.failOnce=false;
          window.FAWNSEE_EXPERIENCE={primary(name,source){
            for(const [key,frame] of entries){frame.id='cached-'+key;frame.hidden=key!==name;}
            if(!entries.has(name)){const frame=document.createElement('iframe');frame.srcdoc=source;
              frame.onload=()=>frame.contentDocument.querySelector('[data-shared-image=logo]').src=logo;
              document.body.appendChild(frame);entries.set(name,frame);}
            entries.get(name).id='main-frame';entries.get(name).hidden=false;
          }};
          window.FAWNSEE_MOBILE={
            async native(a,p){actions.push([a,structuredClone(p)]);return {challenge_id:'synthetic-otp',expires_in:120,retry_after:45};},
            async authenticate(a,p){actions.push([a,structuredClone(p)]);
              if(failOnce){failOnce=false;throw Object.assign(Error('retry login'),{code:'registration_login_required'});}
            }
          };renderAccount('#auth/login');
        }""", [template, "data:image/svg+xml;base64," + b64encode(logo).decode()])
        def frame(mode):
            view = page.frame_locator("#main-frame")
            expect(view.locator("form")).to_have_attribute("data-entry-mode", mode)
            return view
        def action():
            return page.evaluate("actions.at(-1)")
        def image(mode):
            out = Path(os.environ.get("FAWNSEE_AUTH_EVIDENCE", tmp_path))
            out.mkdir(parents=True, exist_ok=True)
            page.wait_for_function("document.querySelector('#main-frame').contentDocument.querySelector('img').naturalWidth>0")
            page.screenshot(path=out/f"android-{mode}.png")
        def fill(view, value, secret):
            view.locator('[name=account]').fill(value)
            view.locator('[name=password]').fill(secret)
            view.locator('[data-purpose=terms-consent] input').check()
        phone = frame("phone")
        image("phone")
        phone.locator('[data-entry-send]').click()
        expect(phone.get_by_role("status")).to_contain_text("11位手机号")
        phone.locator('[name=account]').fill("13800000011")
        phone.locator('[data-entry-send]').click()
        expect(phone.get_by_role("status")).to_contain_text("已发送")
        expect(phone.locator('[data-entry-send]')).to_be_disabled()
        fill(phone, "13800000012", "789012")
        phone.locator('[data-parent-action=auth-enter]').click()
        expect(phone.get_by_role("status")).to_contain_text("先获取")
        phone.locator('[data-entry-send]').click()
        phone.locator('[data-parent-action=auth-enter]').click()
        assert action()[0] == "auth.phone.quick-entry"
        assert action()[1]["challenge_id"] == "synthetic-otp" and action()[1]["code"] == "789012"
        phone.locator('[data-entry-route="#auth/register"]').click()
        register = frame("register")
        image("register")
        assert register.locator('[name=confirm]').get_attribute("type") == "password"
        assert not register.locator('[type=tel]').count()
        fill(register, "android_synthetic", "Synthetic.11868")
        register.locator('[name=confirm]').fill("different")
        register.locator('[data-parent-action=auth-enter]').click()
        expect(register.get_by_role("status")).to_contain_text("需要一致")
        register.locator('[name=confirm]').fill("Synthetic.11868")
        register.locator('[data-purpose=terms-consent] input').uncheck()
        register.locator('[data-parent-action=auth-enter]').click()
        expect(register.get_by_role("status")).to_contain_text("同意")
        register.locator('[data-purpose=terms-consent] input').check()
        register.locator('[data-auth-auto-login]').uncheck()
        page.evaluate("window.failOnce=true")
        register.locator('[data-parent-action=auth-enter]').click()
        login = frame("login")
        expect(login.get_by_role("status")).to_contain_text("账号已创建")
        assert action() == ["auth.register", {"username":"android_synthetic","password":"Synthetic.11868","auto_login":False}]
        login.locator('[data-entry-route="#auth/register"]').click()
        register = frame("register")
        expect(register.locator('[name=password]')).to_have_value("")
        fill(register, "second_synthetic", "Synthetic.11868")
        register.locator('[name=confirm]').fill("Synthetic.11868")
        register.locator('[name=confirm]').press("Enter")
        assert action()[0] == "auth.register"
        register.locator('[data-entry-route="#auth/password"]').click()
        login = frame("login")
        image("password")
        fill(login, "second_synthetic", "Synthetic.11868")
        login.locator('[data-parent-action=auth-visibility]').click()
        assert login.locator('[name=password]').get_attribute("type") == "text"
        login.locator('[data-parent-action=auth-enter]').click()
        assert action()[0] == "auth.login"
        login.locator('[data-entry-route="#auth/login"]').click()
        expect(frame("phone").locator('[name=account]')).to_have_value("")
        assert not errors
        browser.close()


def test_packaged_guest_navigation(tmp_path):
    assets = Path(__file__).resolve().parents[2] / "android/app/src/main/assets/www"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 393, "height": 852})
        page.add_init_script("""if(window === top){
          window.AndroidBridge={postMessage(raw){
            const {id,action,params}=JSON.parse(raw);
            const guest={enabled:true,id:'12345678-abcd-abcd-abcd-123456789abc',session_epoch:'android-test'};
            let result={};
            if(action==='app.bootstrap')result={user:null,guest};
            if(action==='guest.request')result={rows:[],items:[],conversations:[],messages:[],guest};
            setTimeout(()=>FAWNSEE_NATIVE.receive({id,ok:true,result}),0);
          }};
        }""")
        page.goto((assets/"index.html").as_uri())
        page.wait_for_function("window.FAWNSEE_MOBILE?.sessionReady===true")
        page.wait_for_function("!document.querySelector('#fawnsee-startup')")
        page.evaluate("FAWNSEE_GUEST_UI.openLogin()")
        def check(mode):
            form = page.frame_locator("#main-frame").locator("form")
            expect(form).to_have_attribute("data-entry-mode",mode)
            expect(page.locator("#bottom")).to_be_hidden()
            page.wait_for_function("document.querySelector('#main-frame').contentDocument.querySelector('img')?.naturalWidth>0")
            page.wait_for_function("!document.querySelector('#fawnsee-startup')")
            out = Path(os.environ.get("FAWNSEE_AUTH_EVIDENCE", tmp_path))
            out.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=out/f"app-{mode}.png")
            return form
        check("phone").locator('[data-entry-route="#auth/register"]').click()
        check("register").locator('[data-entry-route="#auth/password"]').click()
        check("login").locator('[data-entry-route="#auth/login"]').click()
        check("phone")
        browser.close()
