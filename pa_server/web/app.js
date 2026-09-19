(() => {
  let token = "";
  let watches = [];
  let selectedWatchId = null;
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
  const toast = (message, bad=false) => {
    const node=$("toast"); node.textContent=message; node.style.display="block";
    node.style.borderColor=bad?"#ff6470":"#2b6570"; setTimeout(()=>node.style.display="none",3500);
  };
  async function api(path, options={}) {
    const headers=Object.assign({},options.headers||{},{Authorization:"Bearer "+token});
    if(options.body && typeof options.body!=="string"){headers["Content-Type"]="application/json";options.body=JSON.stringify(options.body);}
    const response=await fetch(path,Object.assign({},options,{headers}));
    if(response.status===204)return null;
    const data=await response.json().catch(()=>({detail:response.statusText}));
    if(!response.ok)throw new Error(typeof data.detail==="string"?data.detail:JSON.stringify(data.detail));
    return data;
  }
  const formObject=form=>Object.fromEntries(new FormData(form).entries());
  function setStats(health){
    const values=[["監控中",health.active_watches],["等待分析",health.queued_jobs],["分析執行中",health.running_jobs],["待通知",health.pending_notifications],["待交易",health.pending_trade_tasks]];
    $("stats").innerHTML=values.map(item=>'<div class="stat"><b>'+esc(item[1])+'</b><span>'+item[0]+'</span></div>').join("");
  }
  function decisionSummary(watch){
    const decision=watch.latest_result&&watch.latest_result.stage2_decision&&watch.latest_result.stage2_decision.decision;
    return decision?(decision.order_type||"—")+"／信心 "+(decision.trade_confidence??"—"):"尚無結果";
  }
  function renderWatches(){
    $("watch-count").textContent=watches.length+" 組";
    $("watch-list").innerHTML=watches.map(watch=>'<tr><td><b>'+esc(watch.exchange)+':'+esc(watch.symbol)+'</b><br><small>'+esc(watch.okx_instrument||"")+'</small></td><td>'+esc(watch.timeframe)+'<br><small>'+esc(watch.bar_count)+' bars</small></td><td class="'+(watch.state==="active"?"ok":"warn")+'">'+esc(watch.state)+'<br><small>'+esc(watch.error||"")+'</small></td><td>'+esc(decisionSummary(watch))+'<br><small>積壓 '+esc(watch.queued_count||0)+'</small></td><td class="'+(watch.trading_enabled?"ok":"muted")+'">'+(watch.trading_enabled?"已啟用":"關閉")+'</td><td><div class="actions"><button data-action="detail" data-id="'+esc(watch.id)+'" class="secondary">結果</button><button data-action="state" data-id="'+esc(watch.id)+'" data-state="'+(watch.state==="active"?"paused":"active")+'" class="secondary">'+(watch.state==="active"?"暫停":"恢復")+'</button><button data-action="trade" data-id="'+esc(watch.id)+'" class="secondary">'+(watch.trading_enabled?"停用交易":"啟用交易")+'</button><button data-action="delete" data-id="'+esc(watch.id)+'" class="danger-button">刪除</button></div></td></tr>').join("");
  }
  async function loadWatches(){watches=await api("/v1/watches");renderWatches();}
  async function showAnalysis(id){
    selectedWatchId=id;
    const items=await api("/v1/watches/"+id+"/analyses?limit=20");
    $("analysis-title").textContent="最近 "+items.length+" 筆"; $("analysis-detail").textContent=JSON.stringify(items,null,2);
  }
  function fillTrading(settings){
    const form=$("trading-form");
    Object.entries(settings).forEach(([key,value])=>{const input=form.elements[key];if(!input)return;if(input.type==="checkbox")input.checked=!!value;else input.value=value;});
    const configured=settings.credentials_configured||{};
    $("credential-status").textContent="模擬憑證 "+(configured.demo?"已設定":"未設定")+"／實盤憑證 "+(configured.live?"已設定":"未設定");
  }
  function renderTrading(status){
    fillTrading(status.settings); $("trading-error").textContent=status.last_error||"";
    $("thesis-list").innerHTML=(status.active||[]).map(thesis=>'<tr><td>'+esc(thesis.id)+'</td><td>'+esc(thesis.profile)+'／'+esc(thesis.inst_id)+'</td><td>'+esc(thesis.status)+'</td><td>'+esc(thesis.direction)+'</td><td>入 '+esc(thesis.current_entry)+'<br>停 '+esc(thesis.current_stop)+'<br>TP1 '+esc(thesis.current_tp1||thesis.tp1)+'／TP2 '+esc(thesis.current_tp2||thesis.tp2)+'</td><td>'+esc(thesis.filled_size)+' / '+esc(thesis.total_size)+'</td><td><div class="actions"><button data-thesis="'+esc(thesis.id)+'" data-do="cancel" class="secondary">取消掛單</button><button data-thesis="'+esc(thesis.id)+'" data-do="close">平倉</button></div></td></tr>').join("");
    const events=[...(status.events||[]).map(event=>({time:event.created_at,title:event.event,body:event.data,state:event.notify_status})),...(status.tasks||[]).map(task=>({time:task.created_at,title:"交易工作 "+task.status,body:task.result||task.last_error||task.job_id,state:task.status}))].sort((a,b)=>String(b.time).localeCompare(String(a.time)));
    $("event-list").innerHTML=events.map(event=>'<div class="event"><b>'+esc(event.title)+'</b> <small>'+esc(event.time)+'／'+esc(event.state)+'</small><div>'+esc(typeof event.body==="string"?event.body:JSON.stringify(event.body))+'</div></div>').join("")||'<span class="muted">尚無交易事件</span>';
  }
  async function loadTrading(){renderTrading(await api("/v1/trading/status"));}
  async function refreshAll(){
    try{const health=await fetch("/health").then(response=>response.json());setStats(health);await Promise.all([loadWatches(),loadTrading()]);$("health-badge").textContent="服務正常";$("health-badge").className="badge ok";}
    catch(error){$("health-badge").textContent="連線失敗";$("health-badge").className="badge bad";throw error;}
  }
  $("login-form").addEventListener("submit",async event=>{event.preventDefault();token=$("token").value;try{await api("/v1/watches");$("login").classList.add("hidden");$("token").value="";await refreshAll();}catch(error){token="";$("login-error").textContent=error.message;}});
  $("logout").onclick=()=>{token="";$("login").classList.remove("hidden");};
  $("refresh").onclick=()=>refreshAll().catch(error=>toast(error.message,true));
  document.querySelectorAll(".tab").forEach(button=>button.onclick=()=>{document.querySelectorAll(".tab,.panel").forEach(node=>node.classList.remove("active"));button.classList.add("active");$("panel-"+button.dataset.tab).classList.add("active");});
  $("watch-form").addEventListener("submit",async event=>{event.preventDefault();const value=formObject(event.target);value.bar_count=Number(value.bar_count);value.extended_session=false;value.trading_enabled=event.target.elements.trading_enabled.checked;try{await api("/v1/watches",{method:"POST",headers:{"Idempotency-Key":crypto.randomUUID()},body:value});event.target.elements.trading_enabled.checked=false;await refreshAll();toast("監控已建立");}catch(error){toast(error.message,true);}});
  $("watch-list").onclick=async event=>{const button=event.target.closest("button");if(!button)return;const watch=watches.find(item=>item.id===button.dataset.id);if(!watch)return;try{if(button.dataset.action==="detail")await showAnalysis(watch.id);if(button.dataset.action==="state")await api("/v1/watches/"+watch.id,{method:"PATCH",body:{state:button.dataset.state}});if(button.dataset.action==="trade")await api("/v1/watches/"+watch.id,{method:"PATCH",body:{trading_enabled:!watch.trading_enabled,okx_instrument:watch.okx_instrument}});if(button.dataset.action==="delete"){const label=watch.exchange+":"+watch.symbol+"／"+watch.timeframe;if(!confirm("確認刪除監控「"+label+"」？\n歷史分析會保留，尚未開始的分析會取消。"))return;await api("/v1/watches/"+watch.id,{method:"DELETE"});if(selectedWatchId===watch.id){selectedWatchId=null;$("analysis-title").textContent="選擇一組監控查看";$("analysis-detail").textContent="尚未選擇";}toast("監控已刪除");}await refreshAll();}catch(error){toast(error.message,true);}};
  $("trading-form").addEventListener("submit",async event=>{event.preventDefault();const value=formObject(event.target);["leverage","fixed_notional_usdt","fixed_margin_usdt","risk_percent","max_notional_usdt","pending_expiry_bars","poll_interval_seconds"].forEach(key=>value[key]=Number(value[key]));value.enabled=event.target.elements.enabled.checked;try{fillTrading(await api("/v1/trading/settings",{method:"PUT",body:value}));await refreshAll();toast("OKX 設定已儲存");}catch(error){toast(error.message,true);}});
  $("validate-trading").onclick=async()=>{try{const result=await api("/v1/trading/validate",{method:"POST"});toast("設定有效："+result.profile+" / "+result.position_mode);}catch(error){toast(error.message,true);}};
  $("thesis-list").onclick=async event=>{const button=event.target.closest("button");if(!button)return;const verb=button.dataset.do==="close"?"平倉":"取消掛單";if(!confirm("確認"+verb+"交易方案 #"+button.dataset.thesis+"？"))return;try{await api("/v1/trading/theses/"+button.dataset.thesis+"/"+button.dataset.do,{method:"POST",headers:{"Idempotency-Key":crypto.randomUUID()}});await refreshAll();toast(verb+"請求已送出");}catch(error){toast(error.message,true);}};
  $("feishu-test").onclick=async()=>{try{await api("/v1/notifications/feishu/test",{method:"POST"});toast("飛書測試訊息已送出");}catch(error){toast(error.message,true);}};
})();
