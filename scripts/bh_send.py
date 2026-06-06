# exec'd inside browser-harness; helpers (goto_url, js, click_at_xy, type_text,
# press_key, page_info, wait_for_load) are available as globals.
import time as _t, json as _j
def send(A, MSG, UNIQ):
    goto_url("https://x.com/"+A); wait_for_load(); _t.sleep(2.5)
    prof = js(r"""
    (() => {
      const nameEl=document.querySelector('[data-testid="UserName"]');
      const bioEl=document.querySelector('[data-testid="UserDescription"]');
      let followers=null;
      document.querySelectorAll('a[href$="/verified_followers"],a[href$="/followers"]').forEach(a=>{followers=a.innerText.replace(/\n/g,' ');});
      const tweets=[...document.querySelectorAll('[data-testid="tweetText"]')].slice(0,3).map(t=>t.innerText.slice(0,120).replace(/\n/g,' '));
      const b=document.querySelector('[data-testid="sendDMFromProfile"]');
      let msgRect=null; if(b){const r=b.getBoundingClientRect(); msgRect={x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};}
      const suspended=/This account doesn|Account suspended|Hmm.*went wrong|Caution/i.test(document.body.innerText.slice(0,300));
      return {name:nameEl?nameEl.innerText.replace(/\n/g,' | '):null, bio:bioEl?bioEl.innerText.replace(/\n/g,' '):null, followers, tweets, hasMsg:!!b, msgRect, suspended};
    })()
    """)
    prof['tweets']=(prof.get('tweets') or [])[:1]
    prof['bio']=(prof.get('bio') or '')[:140]
    out={"prof":prof}
    if prof.get('suspended') or not prof.get('hasMsg'):
        out["status"]="no_dm"; print(_j.dumps(out,ensure_ascii=True)[:2400]); return
    click_at_xy(prof['msgRect']['x'],prof['msgRect']['y']); _t.sleep(3)
    rect=js(r"""(()=>{const t=document.querySelector('[data-testid="dm-composer-textarea"]'); if(!t)return null; const r=t.getBoundingClientRect(); return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};})()""")
    if not rect:
        out["status"]="no_composer"; out["url"]=page_info().get('url'); print(_j.dumps(out,ensure_ascii=True)[:2400]); return
    click_at_xy(rect['x'],rect['y']); _t.sleep(0.6)
    type_text(MSG); _t.sleep(0.9)
    val=js(r"""(()=>{const t=document.querySelector('[data-testid="dm-composer-textarea"]'); return t?t.value:null;})()""")
    if (val or "").strip()!=MSG:
        out["status"]="type_mismatch"; out["len"]=len(val or ""); out["url"]=page_info().get('url'); print(_j.dumps(out,ensure_ascii=True)[:2400]); return
    press_key("Enter"); _t.sleep(2.6)
    chk=js(r"""(()=>{const t=document.querySelector('[data-testid="dm-composer-textarea"]'); const body=document.body.innerText; return {cleared:t?t.value.trim()==='':null, url:location.href, bodyHasHandle:body.includes('@__A__')};})()""".replace('__A__',A))
    full=js(r"""(()=>document.body.innerText)()""") or ""
    chk["hasPhrase"]= UNIQ in full
    out["status"]="sent" if (chk.get('cleared') and chk.get('hasPhrase')) else "send_unverified"
    out["url"]=chk.get('url'); out["verify"]=chk
    print(_j.dumps(out,ensure_ascii=True)[:2400])
