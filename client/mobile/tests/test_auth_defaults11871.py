"""DOM regression for the packaged account, registration and SMS forms."""
import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def test_defaults_and_user_choices():
    source = Path(__file__).resolve().parents[2] / "android/app/src/main/assets/www"
    assets = Path(os.environ.get("FAWNSEE_TEST_WEB_ROOT", source))
    bundle = (assets / "app.js").read_text(encoding="utf-8")
    template = json.JSONDecoder().raw_decode(bundle.split("const MAIN=", 1)[1])[0]["login"]
    with sync_playwright() as driver:
        browser = driver.chromium.launch(channel="chrome")
        for mode, route in [("login", "#auth/password"), ("register", "#auth/register"), ("phone", "#auth/login")]:
            page = browser.new_page()
            page.route("http*://**", lambda request: request.abort())
            page.set_content('<div id="bottom"></div><iframe id="main-frame"></iframe>')
            page.add_script_tag(path=assets / "account-entry.js")
            page.evaluate("""([route,template])=>{
                window.actions=[];
                window.FAWNSEE_EXPERIENCE={primary(name,html){document.querySelector('iframe').srcdoc=html;}};
                window.FAWNSEE_MOBILE={
                    native:async(a,p)=>({challenge_id:'test',retry_after:0,expires_in:120}),
                    authenticate:async(a,p)=>actions.push({action:a,auto:p.auto_login})
                };
                FAWNSEE_AUTH_ENTRIES.render(route,()=>template,()=>{});
            }""", [route, template])
            form = page.frame_locator("#main-frame").locator("form")
            expect(form).to_have_attribute("data-entry-mode", mode)
            terms = form.locator('input[type=checkbox]:not([data-auth-auto-login])')
            auto = form.locator("[data-auth-auto-login]")
            assert terms.is_checked() and auto.is_checked(), (mode, terms.is_checked(), auto.is_checked())
            form.locator("[name=account]").fill("13800000011" if mode == "phone" else "test-member")
            form.locator("[name=password]").fill("654321" if mode == "phone" else "sample-password")
            if mode == "register":
                form.locator("[name=confirm]").fill("sample-password")
            terms.uncheck()
            auto.uncheck()
            submit = form.locator("[data-parent-action=auth-enter]")
            submit.click()
            expect(form.get_by_role("status")).to_contain_text("同意")
            assert not page.evaluate("actions.length")
            assert not auto.is_checked()
            terms.check()
            if mode == "phone":
                form.locator("[data-entry-send]").click()
                expect(form.get_by_role("status")).to_contain_text("已发送")
            submit.click()
            assert page.evaluate("actions.map(row=>row.auto)") == [False]
            page.close()
        browser.close()
