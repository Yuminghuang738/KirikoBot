const $=id=>document.getElementById(id);
const $$=(sel,ctx)=>Array.from((ctx||document).querySelectorAll(sel));
let allLines=[], msgTotal=0, errTotal=0, thinkTotal=0, activeLevel='all', startTime=Date.now();
let sseRetry=0, sseStarted=false;

// ── Toast ──
function toast(msg,type='ok'){
  const t=document.createElement('div');
  t.className='toast '+type;
  t.innerHTML='<span class="toast-msg"></span><span class="toast-bar"></span>';
  t.querySelector('.toast-msg').textContent=msg;
  document.body.appendChild(t);
  setTimeout(()=>{t.classList.add('out');setTimeout(()=>t.remove(),280)},2200);
}

// ── Page metadata (drives the topbar title) ──
const PAGE_META={
  overview:['总览','机器人运行概况'],
  logs:['实时日志','SSE 实时推送的运行日志'],
  chat:['对话记录','机器人与群友的对话历史'],
  messages:['群消息','群里采集到的原始消息'],
  profiles:['用户画像','AI 分析出的群友特征'],
  affection:['好感度','互动评分与排行榜'],
  learning:['自学习','从反馈中积累的经验'],
  reminders:['提醒','定时提醒管理'],
  tarot:['塔罗','塔罗抽牌记录'],
  stickers:['表情包','表情包库与整理'],
  features:['功能清单','群友提出的功能请求'],
  versions:['版本日志','版本与变更记录'],
  groups:['群管理','已接入的群'],
  activity:['群活跃','单日发言统计与时段分布'],
  history:['聊天回看','按天回看群聊完整记录'],
  ai:['AI 用量','调用量、延迟、token 与成本'],
  push:['群推送','按群订阅定时推送'],
  settings:['群设置','按群 / 用户精细开关功能'],
  llbot:['LLBot 连接','登录状态、运行指标与配置'],
  webqq:['WebQQ','LLBot 完整 WebUI（内嵌）'],
};

// ── Pointer spotlight on cards (single delegated listener) ──
document.addEventListener('pointermove',e=>{
  const el=e.target.closest('.tile,.panel,.card,.profile-card,.sticker-card');
  if(!el)return;
  const r=el.getBoundingClientRect();
  el.style.setProperty('--mx',(e.clientX-r.left)+'px');
  el.style.setProperty('--my',(e.clientY-r.top)+'px');
},{passive:true});

// ── Navigation ──
$$('#sidenav a').forEach(a=>{a.addEventListener('click',()=>{
  $$('#sidenav a').forEach(x=>x.classList.remove('active'));a.classList.add('active');
  loadPage(a.dataset.page)})});

function setPageTitle(name){
  const m=PAGE_META[name]||['KirikoBot'];
  const t=$('tbTitle');
  if(t)t.textContent=m[0];
}

// Slide the active pill behind the current nav item.
function moveNavPill(){
  const pill=$('navPill'), nav=$('sidenav');
  if(!pill||!nav)return;
  const a=nav.querySelector('a.active');
  if(!a){pill.style.opacity='0';return}
  pill.style.opacity='1';
  pill.style.height=a.offsetHeight+'px';
  pill.style.transform=`translateY(${a.offsetTop}px)`;
}
window.addEventListener('resize',()=>moveNavPill());

async function loadPage(name){
  const mc=$('mainContent');
  setPageTitle(name);
  if(name!=='llbot')stopLLBotLogs();
  switch(name){
    case 'overview': mc.innerHTML=await overviewHTML(); bindLogsIf('overview-logs'); animateCounters(); break;
    case 'logs': mc.innerHTML=logsPageHTML(); bindLogsIf('logs-view'); startLogSSE(); break;
    case 'reminders': mc.innerHTML=await remindersHTML(); break;
    case 'tarot': mc.innerHTML=await tarotHTML(); break;
    case 'chat': mc.innerHTML=await chatHTML(); break;
    case 'profiles': mc.innerHTML=await profilesHTML(); break;
    case 'learning': mc.innerHTML=await learningHTML(); break;
    case 'features': mc.innerHTML=await featuresHTML(); bindFeatures(); break;
    case 'versions': mc.innerHTML=await versionsHTML(); bindVersions(); break;
    case 'stickers': mc.innerHTML=await stickersHTML(); break;
    case 'groups': mc.innerHTML=await groupsHTML(); break;
    case 'activity': mc.innerHTML=await activityHTML(); bindActivity(); break;
    case 'history': mc.innerHTML=await historyHTML(); bindHistory(); break;
    case 'ai': mc.innerHTML=aiUsageHTML(); bindAiUsage(); break;
    case 'push': mc.innerHTML=await pushHTML(); bindPush(); break;
    case 'settings': mc.innerHTML=await settingsHTML(); bindSettings(); break;
    case 'affection': mc.innerHTML=await affectionHTML(); bindAffection(); break;
    case 'messages': mc.innerHTML=await messagesHTML(); break;
    case 'llbot': mc.innerHTML=await llbotHTML(); bindLLBot(); break;
    case 'webqq': mc.innerHTML=webqqHTML(); break;
  }
  mc.classList.remove('page-anim');void mc.offsetWidth;mc.classList.add('page-anim');
  moveNavPill();
}

// ── Helper ──
function formatSize(bytes){if(bytes<1024)return bytes+'B';if(bytes<1048576)return (bytes/1024).toFixed(1)+'KB';return (bytes/1048576).toFixed(1)+'MB'}
function fmtUptime(s){const m=Math.floor(s/60),h=Math.floor(m/60),d=Math.floor(h/24);if(d)return d+'天 '+h%24+'时';if(h)return h+'时 '+m%60+'分';return m+'分'}
function esc(v){return String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function fmtBytes(b){if(!b&&b!==0)return '—';return formatSize(b)}

// ── Status bar updater ──
async function updateStatus(){
  try{
    const s=await fetch('/status').then(r=>r.json());
    const dot=$('statusDot'); if(dot)dot.style.background=s.ok?'var(--green)':'var(--red)';
    const bar=$('statusBar');
    if(bar)bar.innerHTML=
      `<div class="row"><span class="s-dot" style="background:${s.ok?'var(--green)':'var(--red)'}"></span>${s.ok?'运行中':'离线'} · ${fmtUptime(s.uptime||0)}</div>`+
      `<div>工具 ${s.tools||'?'} · 表情 ${s.stickers||'?'} · 群 ${s.groups||'?'}</div>`+
      `<div>模型 ${esc(s.model||'?')} · 调度器 ${s.scheduler?'运行':'停止'}</div>`+
      `<div><a href="#" onclick="queryBalance(event)">查询余额</a> <span id="balanceText"></span></div>`;
    const tr=$('tbRight');
    if(tr)tr.innerHTML=
      `<span class="pill ${s.ok?'ok':'err'}"><span class="s-dot" style="background:currentColor"></span>${s.ok?'运行中':'离线'}</span>`+
      `<span class="pill info">${esc(s.model||'?')}</span>`+
      `<span class="pill">🛠 ${s.tools||'?'}</span>`+
      `<span class="pill off" id="tbLLBot">🔌 LLBot …</span>`;
    refreshLLBotPill();
  }catch(_){}
}
updateStatus();

// ── Manual balance query ──
async function queryBalance(e){
  if(e)e.preventDefault();
  const el=$('balanceText');
  if(!el)return;
  el.textContent='查询中...';
  try{
    const r=await fetch('/api/balance').then(r=>r.json());
    if(r.ok&&r.data){
      const infos=r.data.balance_infos||[];
      if(infos.length){const i=infos[0];el.textContent=i.total_balance+' '+i.currency;el.style.color='var(--green)'}
      else el.textContent='暂无数据'
    }else el.textContent=r.error||'查询失败'
  }catch(_){el.textContent='查询失败'}
}

// ── Overview (bento dashboard) ──
async function overviewHTML(){
  let s={totals:{group_messages:0,chat_turns:0,tarot_draws:0,user_profiles:0,tarot_cards:0},tools:[]};
  let r={reminders:[]}, st={ok:true,sticks:0}, sch={};
  try{s=await fetch('/api/stats').then(r=>r.json())}catch(_){}
  try{r=await fetch('/api/reminders').then(r=>r.json())}catch(_){}
  try{st=await fetch('/status').then(r=>r.json())}catch(_){}
  try{sch=await fetch('/api/scheduler').then(r=>r.json())}catch(_){}

  const t=s.totals;
  const pending=r.reminders.filter(x=>!x.fired);
  const tiles=[
    ['c1','💬',t.group_messages,'群消息记录'],
    ['c2','🤖',t.chat_turns,'AI 对话轮次'],
    ['c3','🃏',t.tarot_draws,'塔罗抽牌'],
    ['c4','👤',t.user_profiles,'用户画像'],
    ['c5','🎴',t.tarot_cards,'塔罗牌库'],
    ['c6','⏰',pending.length,'待触发提醒'],
    ['c3','🎨',st.stickers||0,'表情包'],
    ['c2','👥',st.groups||0,'活跃群组'],
  ];

  let h=`<div class="page-head">
    <div>
      <div class="ph-title">系统 <span class="em">总览</span></div>
      <div class="ph-sub">KirikoBot 运行概况 · 数据实时统计</div>
    </div>
    <div class="ph-right">
      <button class="btn" onclick="loadPage('logs')">📡 实时日志</button>
      <button class="btn primary" onclick="refreshOverview()">🔄 刷新</button>
    </div>
  </div>

  <div class="hero reveal" style="--i:0">
    <div class="av">🌸</div>
    <div class="h-main">
      <div class="h-name">KirikoBot
        <span class="pill ${st.ok?'ok':'err'}"><span class="s-dot" style="background:currentColor"></span>${st.ok?'运行中':'离线'}</span>
      </div>
      <div class="h-meta">运行 ${fmtUptime(st.uptime||0)} · 模型 ${esc(st.model||'?')} · 工具 ${st.tools||0} 个 · 调度器 ${st.scheduler?'运行中':'已停止'}</div>
    </div>
    <div class="h-actions">
      <button class="btn accent" onclick="schedAction()">🌅 手动早安</button>
      <button class="btn" onclick="loadPage('settings')">⚙️ 群设置</button>
    </div>
  </div>

  <div class="bento">
    ${tiles.map((x,i)=>`<div class="tile ${x[0]} b-3 reveal" style="--i:${i+1}">
      <div class="tico">${x[1]}</div>
      <div class="tbody"><div class="num">${x[2]}</div><div class="lbl">${x[3]}</div></div>
    </div>`).join('')}

    <div class="panel b-8 reveal" style="--i:9">
      <div class="panel-header"><span class="hicon">📈</span>工具调用排行
        <span class="ph-right"><span class="tag u">${s.tools.length} 个已启用</span></span>
      </div>
      <div class="panel-body scroll" style="max-height:320px">`;

  if(s.tools.length){
    const max=Math.max(1,...s.tools.map(x=>x.count));
    s.tools.forEach(x=>h+=`<div class="bar"><span class="name">${esc(x.name)}</span>
      <div class="track"><div class="fill" style="width:${Math.max(3,(x.count/max)*100)}%">${x.count}</div></div></div>`);
  } else {
    h+=`<div class="empty"><span class="em-ico">📊</span>还没有工具调用记录</div>`;
  }

  h+=`</div></div>

    <div class="panel b-4 reveal" style="--i:10">
      <div class="panel-header"><span class="hicon">⏱️</span>定时任务
        <span class="ph-right"><span class="tag ${sch.running?'ok':'err'}">${sch.running?'运行中':'已停'}</span></span>
      </div>
      <div class="panel-body"><div class="sched-info">
        <div class="si-item"><div class="si-label">检查间隔</div><div class="si-value">${sch.check_interval??'—'} 秒</div></div>
        <div class="si-item"><div class="si-label">下次早安</div><div class="si-value">${esc(sch.next_morning||'—')}</div></div>
        <div class="si-item"><div class="si-label">活跃群数</div><div class="si-value">${(sch.active_groups||[]).length}</div></div>
        <div class="si-item"><div class="si-label">待触发提醒</div><div class="si-value">${pending.length}</div></div>
      </div></div>
    </div>

    <div class="panel b-4 reveal" style="--i:11">
      <div class="panel-header"><span class="hicon">⏰</span>最近提醒
        <span class="ph-right"><button class="btn sm" onclick="loadPage('reminders')">全部 →</button></span>
      </div>
      <div class="panel-body tight">`;

  if(pending.length){
    h+=`<div class="list">`;
    pending.slice(0,6).forEach(x=>h+=`<div class="item">
      <div class="iava">⏰</div>
      <div class="imain"><div class="ititle">${esc(x.content)}</div>
        <div class="isub">${esc(x.remind_time)} · @${esc(x.user_name||'—')}</div></div>
    </div>`);
    h+=`</div>`;
  } else {
    h+=`<div class="empty"><span class="em-ico">🌙</span>暂无待触发提醒</div>`;
  }

  h+=`</div></div>

    <div class="panel b-8 reveal" style="--i:12">
      <div class="panel-header"><span class="hicon">📡</span>最近日志
        <span class="ph-right"><button class="btn sm" onclick="loadPage('logs')">打开日志页 →</button></span>
      </div>
      <div class="panel-body"><div class="logs" id="overview-logs" style="max-height:280px">${
        allLines.slice(-40).map(logLineHTML).join('')||'<div class="empty">等待日志…</div>'
      }</div></div>
    </div>
  </div>`;

  return h;
}
function refreshOverview(){loadPage('overview')}

// ── Scheduler actions ──
async function schedAction(){
  try{
    const r=await fetch('/api/scheduler/morning',{method:'POST'});
    const d=await r.json();
    toast(d.ok?'早安问候已触发！':'触发失败: '+d.error, d.ok?'ok':'err');
  }catch(_){toast('请求失败','err')}
}

// ── Logs ──
function logsPageHTML(){
  return `<div class="page-head">
    <div><div class="ph-title">实时 <span class="em">日志</span></div>
      <div class="ph-sub">SSE 实时推送 · 支持级别筛选与关键字搜索</div></div>
    <div class="ph-right"><span class="pill ok"><span class="s-dot" style="background:currentColor"></span>实时连接</span>
      <button class="btn danger" onclick="clearLogs()">🗑️ 清屏</button></div>
  </div>
  <div class="bento">
    <div class="tile c1 b-4 reveal" style="--i:0"><div class="tico">📜</div><div class="tbody"><div class="num" id="logTotal">0</div><div class="lbl">日志总行数</div></div></div>
    <div class="tile c6 b-4 reveal" style="--i:1"><div class="tico">⛔</div><div class="tbody"><div class="num" id="logErrors">0</div><div class="lbl">错误</div></div></div>
    <div class="tile c5 b-4 reveal" style="--i:2"><div class="tico">🧠</div><div class="tbody"><div class="num" id="logThinks">0</div><div class="lbl">思维链</div></div></div>
  </div>
  <div class="panel reveal" style="--i:3;margin-top:16px">
    <div class="panel-header"><span class="hicon">📡</span>日志流
      <span class="ph-right"><span class="tag u" id="logCount">0 条</span></span>
    </div>
    <div class="toolbar" style="padding:14px 18px;margin:0;border-bottom:1px solid var(--border)">
      <button class="chip on" data-lvl="all">全部</button>
      <button class="chip" data-lvl="INFO">INFO</button>
      <button class="chip" data-lvl="WARNING">WARN</button>
      <button class="chip" data-lvl="ERROR">ERROR</button>
      <button class="chip" data-lvl="THINK">🧠 思维链</button>
      <input placeholder="搜索日志…" id="logFilter" style="margin-left:6px">
      <label style="display:flex;align-items:center;gap:5px"><input type="checkbox" id="autoScroll" checked> 自动滚动</label>
    </div>
    <div class="panel-body"><div class="logs" id="logs-view" style="height:calc(100vh - 440px);min-height:260px"></div></div>
  </div>`;
}
function bindLogsIf(id){const lv=$(id);if(!lv)return;lv.innerHTML=allLines.slice(-50).map(logLineHTML).join('');lv.dataset.count=String(allLines.length);const tb=lv.parentElement.parentElement;if(tb)$$('button[data-lvl]',tb).forEach(b=>b.addEventListener('click',()=>{$$('button[data-lvl]',tb).forEach(x=>x.classList.remove('on'));b.classList.add('on');activeLevel=b.dataset.lvl;renderLogs(lv)}))}
function entryClass(l){if(l.level==='ERROR')return'E';if(l.level==='WARNING')return'W';if(l.name==='think')return'T';return'I'}
// `display` is pre-escaped server-side (see log_stream.SSELogHandler). The
// fallback path must escape too — otherwise a QQ nickname becomes stored XSS.
function logLineHTML(l){
  const inner=l.display||('<span class="ts">'+esc(l.time)+'</span>'+esc(l.msg));
  return '<div class="log-entry '+entryClass(l)+'">'+inner+'</div>';
}
function renderLogs(lv){
  if(!lv)lv=$('logs-view');if(!lv)return;
  const q=($('logFilter')||{value:''}).value.toLowerCase();
  const unfiltered=activeLevel==='all'&&!q;
  const prev=parseInt(lv.dataset.count||'0',10);
  lv.innerHTML='';let n=0;
  allLines.forEach((l,idx)=>{
    // Filter by level
    if(activeLevel==='THINK'){if(l.name!=='think')return}
    else if(activeLevel!=='all'&&l.level!==activeLevel)return;
    if(q&&!(l.time+' '+l.msg).toLowerCase().includes(q))return;
    const d=document.createElement('div');
    d.className='log-entry '+entryClass(l)+(unfiltered&&idx>=prev?' log-new':'');
    d.innerHTML=l.display||('<span class="ts">'+esc(l.time)+'</span>'+esc(l.msg));lv.appendChild(d);n++
  });
  lv.dataset.count=String(allLines.length);
  if(!n)lv.innerHTML='<div class="empty">无匹配日志</div>';
  if($('autoScroll')&&$('autoScroll').checked)lv.scrollTop=lv.scrollHeight;
  const lc=$('logCount');if(lc)lc.textContent=n+' 条';
  const lt=$('logTotal');if(lt)lt.textContent=allLines.length;
  const le=$('logErrors');if(le)le.textContent=allLines.filter(l=>l.level==='ERROR').length;
  const lk=$('logThinks');if(lk)lk.textContent=allLines.filter(l=>l.name==='think').length;
}
function clearLogs(){allLines=[];msgTotal=0;errTotal=0;thinkTotal=0;renderLogs($('logs-view'));renderLogs($('overview-logs'));toast('日志已清空')}

function startLogSSE(){
  if(sseStarted)return;sseStarted=true;
  function connect(){
    const es=new EventSource('/stream');
    es.onmessage=e=>{
      sseRetry=0;
      try{
        const entries=JSON.parse(e.data);
        entries.forEach(x=>{allLines.push(x);msgTotal++;if(x.level==='ERROR')errTotal++;if(x.name==='think')thinkTotal++});
        if(allLines.length>2000)allLines=allLines.slice(-1000);
        renderLogs($('logs-view'))
      }catch(_){}
    };
    es.onerror=()=>{
      es.close();sseStarted=false;
      sseRetry=Math.min(sseRetry+1,10);
      const delay=Math.min(1000*Math.pow(2,sseRetry),30000);
      setTimeout(()=>{if(document.querySelector('[data-page="logs"].active')||document.querySelector('[data-page="overview"].active'))startLogSSE()},delay)
    }
  }
  connect()
}

// ── Reminders ──
async function remindersHTML(){
  let d={reminders:[]};try{d=await fetch('/api/reminders').then(r=>r.json())}catch(_){}
  const def=new Date(Date.now()+10*60*1000);
  const pad=n=>String(n).padStart(2,'0');
  const defTime=`${def.getFullYear()}-${pad(def.getMonth()+1)}-${pad(def.getDate())}T${pad(def.getHours())}:${pad(def.getMinutes())}`;
  const pending=d.reminders.filter(x=>!x.fired);
  const daily=d.reminders.filter(x=>x.repeat_daily);

  let h=`<div class="page-head">
    <div><div class="ph-title">定时 <span class="em">提醒</span></div>
      <div class="ph-sub">到点由机器人 @ 提醒，可设每日重复</div></div>
    <div class="ph-right"><button class="btn primary" onclick="loadPage('reminders')">🔄 刷新</button></div>
  </div>
  <div class="bento">
    <div class="tile c1 b-4 reveal" style="--i:0"><div class="tico">⏰</div><div class="tbody"><div class="num">${d.reminders.length}</div><div class="lbl">提醒总数</div></div></div>
    <div class="tile c4 b-4 reveal" style="--i:1"><div class="tico">⏳</div><div class="tbody"><div class="num">${pending.length}</div><div class="lbl">待触发</div></div></div>
    <div class="tile c2 b-4 reveal" style="--i:2"><div class="tico">🔁</div><div class="tbody"><div class="num">${daily.length}</div><div class="lbl">每日重复</div></div></div>
  </div>

  <div class="panel reveal" style="--i:3;margin-top:16px">
    <div class="panel-header"><span class="hicon">➕</span>新建提醒</div>
    <div class="add-form">
      <input type="datetime-local" id="remTime" value="${defTime}" style="flex:0 0 auto;width:210px">
      <input type="text" id="remContent" placeholder="提醒内容…">
      <label style="white-space:nowrap;display:flex;align-items:center;gap:5px"><input type="checkbox" id="remDaily"> 每日</label>
      <button onclick="addReminder()">➕ 添加提醒</button>
    </div>
  </div>

  <div class="panel reveal" style="--i:4">
    <div class="panel-header"><span class="hicon">⏰</span>提醒记录
      <span class="ph-right"><span class="tag u">${d.reminders.length} 条</span></span>
    </div>
    <div class="panel-body tight"><div class="list">`;

  if(!d.reminders.length){
    h+=`<div class="empty"><span class="em-ico">⏰</span>暂无提醒记录</div>`;
  } else {
    d.reminders.forEach(x=>h+=`<div class="item${x.fired?' ':''}" id="rem-${x.id}" style="${x.fired?'opacity:.45':''}">
      <div class="iava">${x.fired?'✅':'⏳'}</div>
      <div class="imain">
        <div class="ititle">${esc(x.content)}</div>
        <div class="isub" style="font-family:var(--mono)">${esc(x.remind_time)} · @${esc(x.user_name||'—')}</div>
      </div>
      <div class="imeta">
        <span class="tag ${x.fired?'ok':'warn'}">${x.fired?'已触发':'待触发'}</span>
        ${x.repeat_daily?'<span class="tag t">每日</span>':''}
      </div>
      <div class="iact"><button class="btn sm danger" onclick="delReminder(${x.id})" title="删除提醒">🗑️</button></div>
    </div>`);
  }
  return h+`</div></div></div>`;
}
async function addReminder(){
  const timeVal=($('remTime')||{}).value, content=($('remContent')||{}).value?.trim();
  const daily=($('remDaily')||{}).checked||false;
  if(!content){toast('请输入提醒内容','err');return}
  if(!timeVal){toast('请选择提醒时间','err');return}
  const remind_time=timeVal.replace('T',' ')+':00';
  try{
    const r=await fetch('/api/reminders',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content,remind_time,repeat_daily:daily})});
    const d=await r.json();
    if(d.ok){toast('提醒已创建');loadPage('reminders')}else toast('创建失败: '+(d.error||'未知'),'err')
  }catch(_){toast('请求失败','err')}
}
async function delReminder(id){
  if(!confirm('确定删除此提醒？'))return;
  try{const r=await fetch('/api/reminders/'+id,{method:'DELETE'});const d=await r.json();if(d.ok){const el=document.getElementById('rem-'+id);if(el)el.remove();toast('提醒已删除')}else toast('删除失败: '+(d.error||'未知错误'),'err')}catch(_){toast('请求失败','err')}
}

// ── Tarot ──
async function tarotHTML(){
  let d={records:[]};try{d=await fetch('/api/tarot').then(r=>r.json())}catch(_){}
  let h=`<div class="page-head">
    <div><div class="ph-title">塔罗 <span class="em">记录</span></div>
      <div class="ph-sub">群友的抽牌历史</div></div>
    <div class="ph-right"><span class="tag u">${d.records.length} 次抽牌</span>
      <button class="btn primary" onclick="loadPage('tarot')">🔄 刷新</button></div>
  </div>`;
  if(!d.records.length){
    return h+`<div class="panel reveal"><div class="empty"><span class="em-ico">🃏</span>还没有抽牌记录</div></div>`;
  }
  h+=`<div class="bento">`;
  d.records.forEach((x,i)=>{
    h+=`<div class="card b-4 reveal lift" style="--i:${Math.min(i,12)}">
      <div class="panel-header"><span class="hicon">🃏</span>${esc(x.card||'未知牌')}</div>
      <div class="panel-body">
        <div class="isub">抽牌人 <b style="color:var(--text)">${esc(x.user_id||'—')}</b></div>
        <div class="isub" style="font-family:var(--mono)">${esc(x.time||'—')}</div>
      </div>
    </div>`;
  });
  return h+`</div>`;
}

// Per-turn record: which tools this reply used, and what the model was thinking.
function chainBlock(x){
  if(x.role!=='assistant')return '';
  let chain=[];
  if(x.tool_calls){
    try{chain=JSON.parse(x.tool_calls)||[]}catch(_){chain=[]}
  }
  const reasoning=String(x.reasoning||'').trim();
  if(!chain.length && !reasoning)return '';

  const rows=chain.map(c=>{
    let args=String(c.arguments||'').trim();
    if(args==='{}')args='';
    return `<div class="chain-row"><span class="tag t">🔧 ${esc(c.name||'?')}</span>
      ${args?`<code>${esc(args.slice(0,160))}</code>`:''}</div>`;
  }).join('');

  return `<details class="chain">
    <summary>🔗 调用链${chain.length?` · ${chain.length} 个工具`:''}${reasoning?' · 思维链':''}</summary>
    ${rows}
    ${reasoning?`<div class="chain-think">${esc(reasoning.slice(0,1500))}</div>`:''}
  </details>`;
}

// ── Chat ──
async function chatHTML(){
  let d={records:[]};try{d=await fetch('/api/history').then(r=>r.json())}catch(_){}
  const roleMeta={user:['👤','u','用户'],assistant:['🤖','a','Kiriko'],system:['⚙️','t','系统'],tool:['🔧','t','工具']};
  let h=`<div class="page-head">
    <div><div class="ph-title">对话 <span class="em">记录</span></div>
      <div class="ph-sub">机器人与群友的历史对话</div></div>
    <div class="ph-right"><span class="tag u">${d.records.length} 条</span>
      <button class="btn primary" onclick="loadPage('chat')">🔄 刷新</button></div>
  </div>
  <div class="panel reveal" style="--i:0"><div class="panel-body tight"><div class="list">`;
  if(!d.records.length){
    h+=`<div class="empty"><span class="em-ico">💬</span>暂无对话记录</div>`;
  } else {
    d.records.forEach(x=>{
      const m=roleMeta[x.role]||['💬','u',x.role||'?'];
      h+=`<div class="item">
        <div class="iava">${m[0]}</div>
        <div class="imain">
          <div class="ititle"><span class="tag ${m[1]}">${esc(m[2])}</span>${x.has_tools?'<span class="tag t">🔧 调用工具</span>':''}</div>
          <div class="isub">${esc(String(x.content||'').slice(0,200))}</div>
          ${chainBlock(x)}
        </div>
        <div class="imeta">
          <span class="tag">${esc(x.time||'')}</span>
          <span class="ph-sub">${esc(x.user_id||'')}</span>
        </div>
      </div>`;
    });
  }
  return h+`</div></div></div>`;
}

// ── Profiles ──
async function profilesHTML(){
  let d={profiles:[]};try{d=await fetch('/api/profiles').then(r=>r.json())}catch(_){}
  let h=`<div class="page-head">
    <div><div class="ph-title">用户 <span class="em">画像</span></div>
      <div class="ph-sub">AI 根据聊天记录分析出的群友特征</div></div>
    <div class="ph-right"><span class="tag u">${d.profiles.length} 位</span>
      <button class="btn primary" onclick="loadPage('profiles')">🔄 刷新</button></div>
  </div>`;
  if(!d.profiles.length){
    return h+`<div class="panel reveal"><div class="empty"><span class="em-ico">👤</span>暂无画像，群友发满 20 条消息后会自动生成</div></div>`;
  }
  h+=`<div class="profile-grid">`;
  d.profiles.forEach((x,i)=>{
    const p=x.profile||{};
    h+=`<div class="profile-card reveal" style="--i:${Math.min(i,12)}">
      <div class="pc-top">
        <div class="pc-ava">${esc((x.user_name||'?').slice(0,1))}</div>
        <div style="min-width:0">
          <h4>${esc(x.user_name||'未知用户')}</h4>
          <div class="meta">${x.msg_count||0} 条消息 · ${esc(x.updated||'')}</div>
        </div>
      </div>
      <div class="pc-body">`;
    if(p.personality)h+=`<div class="row"><span class="k">性格</span><span>${esc(p.personality)}</span></div>`;
    if(p.speaking_style)h+=`<div class="row"><span class="k">风格</span><span>${esc(p.speaking_style)}</span></div>`;
    if(p.mood)h+=`<div class="row"><span class="k">情绪</span><span>${esc(p.mood)}</span></div>`;
    if(p.relationship)h+=`<div class="row"><span class="k">关系</span><span>${esc(p.relationship)}</span></div>`;
    if(p.interests&&p.interests.length)h+=`<div class="row"><span class="k">兴趣</span><div class="tags">${p.interests.map(i=>`<span class="t">${esc(i)}</span>`).join('')}</div></div>`;
    if(p.topics&&p.topics.length)h+=`<div class="row"><span class="k">话题</span><span>${p.topics.map(esc).join(' / ')}</span></div>`;
    if(p.note)h+=`<div class="row"><span class="k">备注</span><span>${esc(p.note)}</span></div>`;
    h+=`</div></div>`;
  });
  return h+`</div>`;
}

// ── Learning ──
async function learningHTML(){
  let d={notes:[]};try{d=await fetch('/api/learning').then(r=>r.json())}catch(_){}
  let h=`<div class="page-head">
    <div><div class="ph-title">自 <span class="em">学习</span></div>
      <div class="ph-sub">机器人从互动反馈中积累的经验</div></div>
    <div class="ph-right"><span class="tag u">${d.notes.length} 条</span>
      <button class="btn primary" onclick="loadPage('learning')">🔄 刷新</button></div>
  </div>
  <div class="panel reveal" style="--i:0">
    <div class="panel-header"><span class="hicon">➕</span>手动添加笔记</div>
    <div class="add-form">
      <input id="learnNote" placeholder="笔记内容…">
      <input id="learnUserId" placeholder="用户ID" style="width:130px;flex:0 0 auto" value="dashboard">
      <input id="learnTool" placeholder="工具名（可选）" style="width:130px;flex:0 0 auto">
      <button onclick="addLearningNote()">➕ 添加</button>
    </div>
  </div>`;
  if(!d.notes.length){
    h+=`<div class="panel reveal" style="--i:1"><div class="empty"><span class="em-ico">🧠</span>暂无学习记录，机器人会在互动中自动积累经验 ✨</div></div>`;
  } else {
    h+=`<div class="panel reveal" style="--i:1">
      <div class="panel-header"><span class="hicon">🧠</span>经验时间线</div>
      <div class="panel-body scroll" style="max-height:66vh"><div class="timeline">`;
    d.notes.forEach(x=>{
      h+=`<div class="tl-item" id="learn-${x.id}">
        <div class="tl-time">${esc(x.time||'')}</div>
        <div class="tl-head">
          ${x.tool_name?`<span class="tag t">${esc(x.tool_name)}</span>`:`<span class="tag">直接回复</span>`}
          <span class="ph-sub">${esc(x.user_id||'')}</span>
          <button class="btn sm danger" style="margin-left:auto" onclick="delLearningNote(${x.id})" title="删除此笔记">🗑️</button>
        </div>
        <div class="tl-body">💡 ${esc(x.note||'')}</div>
        ${x.user_msg?`<div class="isub" style="margin-top:5px">👤 ${esc(String(x.user_msg).slice(0,70))}</div>`:''}
        ${x.ai_text?`<div class="isub">🤖 ${esc(String(x.ai_text).slice(0,70))}</div>`:''}
      </div>`;
    });
    h+=`</div></div></div>`;
  }
  return h;
}

async function addLearningNote(){
  const note=($('learnNote')||{}).value?.trim();
  if(!note){toast('请输入笔记内容','err');return}
  const userId=($('learnUserId')||{}).value?.trim()||'dashboard';
  const toolName=($('learnTool')||{}).value?.trim()||'';
  try{
    const r=await fetch('/api/learning',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({note,user_id:userId,tool_name:toolName})});
    const d=await r.json();
    if(d.ok){toast('笔记已添加');loadPage('learning')}else toast('添加失败: '+(d.error||'未知'),'err')
  }catch(_){toast('请求失败','err')}
}

async function delLearningNote(id){
  if(!confirm('确定删除此学习笔记？'))return;
  try{const r=await fetch('/api/learning/'+id,{method:'DELETE'});const d=await r.json();if(d.ok){const el=document.getElementById('learn-'+id);if(el)el.remove();toast('笔记已删除')}else toast('删除失败: '+(d.error||'未知'),'err')}catch(_){toast('请求失败','err')}
}

// ── Features ──
let featureFilter={status:'all',priority:'all',category:'all'};
async function featuresHTML(){
  let d={features:[]};try{d=await fetch('/api/features').then(r=>r.json())}catch(_){}
  if(!d.features.length)return `<div class="panel"><div class="panel-header">📋 功能需求清单</div><div class="panel-body"><div class="empty">暂无功能需求，群友可通过 @bot 提出建议 (◕‿◕✿)</div>
    <div class="add-form" style="margin-top:12px"><input id="featInput" placeholder="输入新功能需求..."><select id="featCat"><option value="未分类">未分类</option><option value="新闻">新闻</option><option value="游戏">游戏</option><option value="AI对话">AI对话</option><option value="工具">工具</option><option value="通知">通知</option><option value="界面">界面</option><option value="其他">其他</option></select><select id="featPrio"><option value="medium">中优先级</option><option value="high">高优先级</option><option value="low">低优先级</option></select><button onclick="addFeature()">➕ 添加</button></div></div>`;
  const prio={high:'🔥 高',medium:'◆ 中',low:' 低'};
  const stat={pending:'⏳待处理',done:'✅已完成',rejected:'❌已拒绝'};
  const catColor={新闻:'#58a6ff',游戏:'#3fb950',AI对话:'#f78166',工具:'#d2991d',通知:'#bc8cff',界面:'#ff7b72',其他:'#8b949e'};
  // Filter counts
  let allPending=0,allHigh=0;
  d.features.forEach(f=>{if(f.status==='pending')allPending++;if(f.priority==='high')allHigh++});
  // Categories from data
  const cats=new Set(d.features.map(f=>f.category).filter(Boolean));
  let h=`<div class="page-head">
    <div><div class="ph-title">功能 <span class="em">清单</span></div>
      <div class="ph-sub">群友提出的功能需求 · 处理进度一览</div></div>
    <div class="ph-right"><button class="btn primary" onclick="loadPage('features')">🔄 刷新</button></div>
  </div>
  <div class="bento" id="featStats">
    <div class="tile c2 b-4 reveal" style="--i:0"><div class="tico">📋</div><div class="tbody"><div class="num">${d.features.length}</div><div class="lbl">总需求</div></div></div>
    <div class="tile c3 b-4 reveal" style="--i:1"><div class="tico">⏳</div><div class="tbody"><div class="num" id="featPending">${allPending}</div><div class="lbl">待处理</div></div></div>
    <div class="tile c4 b-4 reveal" style="--i:2"><div class="tico">🔥</div><div class="tbody"><div class="num" id="featHigh">${allHigh}</div><div class="lbl">高优先级</div></div></div>
  </div>
  <div class="panel reveal" style="--i:3;margin-top:16px"><div class="panel-header"><span class="hicon">📋</span>需求列表
    <span class="ph-right"><span class="tag u" id="featVisible">共 ${d.features.length} 条</span></span>
  </div>
  <div class="toolbar" style="padding:14px 18px;margin:0;border-bottom:1px solid var(--border)">
    <span class="ph-sub">状态</span>
    <button class="chip on" data-fs="all">全部</button><button class="chip" data-fs="pending">⏳ 待处理</button><button class="chip" data-fs="done">✅ 已完成</button><button class="chip" data-fs="rejected">❌ 已拒绝</button>
    <span class="ph-sub" style="margin-left:10px">优先级</span>
    <button class="chip on" data-fp="all">全部</button><button class="chip" data-fp="high">🔥 高</button><button class="chip" data-fp="medium">◆ 中</button><button class="chip" data-fp="low">低</button>
    <span class="ph-sub" style="margin-left:10px">分类</span>
    <button class="chip on" data-fc="all">全部</button>
    ${Array.from(cats).map(c=>`<button class="chip" data-fc="${c}">${c}</button>`).join('')}
  </div>
  <div class="add-form" style="padding:8px 16px;border-bottom:1px solid var(--border)">
    <input id="featInput" placeholder="输入新功能需求...">
    <select id="featCat">
      <option value="未分类">未分类</option><option value="新闻">新闻</option><option value="游戏">游戏</option><option value="AI对话">AI对话</option><option value="工具">工具</option><option value="通知">通知</option><option value="界面">界面</option><option value="其他">其他</option>
    </select>
    <select id="featPrio"><option value="medium">中优先级</option><option value="high">高优先级</option><option value="low">低优先级</option></select>
    <button onclick="addFeature()">➕ 添加</button>
  </div>
  <div class="panel-body" style="max-height:50vh;overflow-y:auto" id="featList">`;
  d.features.forEach(x=>{
    const fcat=x.category||'未分类';
    h+=`<div class="feature-item" data-fid="${x.id}" data-fstatus="${esc(x.status)}" data-fpriority="${esc(x.priority)}" data-fcategory="${esc(fcat)}">
      <div class="f-top">
        <b style="color:var(--accent)">${esc(x.summary||String(x.request||'').substr(0,20))}</b>
        <span class="tag" style="background:${catColor[fcat]||'#8b949e'}22;color:${catColor[fcat]||'#8b949e'}">${esc(fcat)}</span>
        <span class="tag" style="color:var(--yellow)">${esc(prio[x.priority]||x.priority)}</span>
        <span class="tag ${x.status==='pending'?'warn':x.status==='done'?'ok':'err'}">${esc(stat[x.status]||x.status)}</span>
        <span style="font-size:.68rem;color:var(--muted)">${esc(x.user_name)} · ${esc(x.time)}</span>
        <span class="f-actions">
          ${x.status!=='done'?`<button class="btn-done" title="标记完成" onclick="updateFeature(${x.id},'done')">✅</button>`:''}
          ${x.status!=='rejected'?`<button class="btn-reject" title="拒绝" onclick="updateFeature(${x.id},'rejected')">❌</button>`:''}
          ${x.status!=='pending'?`<button class="btn-done" title="重新打开" onclick="updateFeature(${x.id},'pending')">🔄</button>`:''}
          <button class="btn-del" title="删除" onclick="delFeature(${x.id})">🗑️</button>
        </span>
      </div>
      <div class="f-request">${esc(x.request)}</div>
    </div>`;
  });
  return h+`</div></div>`;
}
function bindFeatures(){
  // Filter buttons
  document.querySelectorAll('button[data-fs]').forEach(b=>b.addEventListener('click',()=>{
    document.querySelectorAll('button[data-fs]').forEach(x=>x.classList.remove('on'));b.classList.add('on');
    featureFilter.status=b.dataset.fs;applyFeatFilter()
  }));
  document.querySelectorAll('button[data-fp]').forEach(b=>b.addEventListener('click',()=>{
    document.querySelectorAll('button[data-fp]').forEach(x=>x.classList.remove('on'));b.classList.add('on');
    featureFilter.priority=b.dataset.fp;applyFeatFilter()
  }));
  document.querySelectorAll('button[data-fc]').forEach(b=>b.addEventListener('click',()=>{
    document.querySelectorAll('button[data-fc]').forEach(x=>x.classList.remove('on'));b.classList.add('on');
    featureFilter.category=b.dataset.fc;applyFeatFilter()
  }));
}
function applyFeatFilter(){
  const items=document.querySelectorAll('.feature-item');
  let visible=0;
  items.forEach(el=>{
    const s=el.dataset.fstatus, p=el.dataset.fpriority, c=el.dataset.fcategory||'未分类';
    const match=(featureFilter.status==='all'||s===featureFilter.status)&&(featureFilter.priority==='all'||p===featureFilter.priority)&&(featureFilter.category==='all'||c===featureFilter.category);
    el.style.display=match?'':'none';if(match)visible++
  });
  document.getElementById('featPending')&&(document.getElementById('featPending').textContent=visible)
  const fv=document.getElementById('featVisible');if(fv)fv.textContent='共 '+visible+' 条'
}
async function updateFeature(id,status){
  try{const r=await fetch('/api/features/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({status})});const d=await r.json();if(d.ok){toast('状态已更新: '+status);loadPage('features')}else toast('更新失败: '+(d.error||'未知'),'err')}catch(_){toast('请求失败','err')}
}
async function delFeature(id){
  if(!confirm('确定删除此功能需求？'))return;
  try{const r=await fetch('/api/features/'+id,{method:'DELETE'});const d=await r.json();if(d.ok){toast('已删除');loadPage('features')}else toast('删除失败: '+(d.error||'未知'),'err')}catch(_){toast('请求失败','err')}
}
async function addFeature(){
  const inp=$('featInput');if(!inp||!inp.value.trim()){toast('请输入功能需求','err');return}
  const cat=$('featCat'),prio=$('featPrio');
  try{const r=await fetch('/api/features',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request:inp.value.trim(),category:cat?cat.value:'未分类',priority:prio?prio.value:'medium'})});const d=await r.json();if(d.ok){toast('已添加功能需求');inp.value='';loadPage('features')}else toast('添加失败: '+(d.error||'未知'),'err')}catch(_){toast('请求失败','err')}
}

// ── Versions & Changelog ──
let versionFilter={type:'all'};
async function versionsHTML(){
  let v={ok:true,versions:[]},cur={ok:false};
  try{v=await fetch('/api/versions').then(r=>r.json())}catch(_){}
  try{cur=await fetch('/api/versions/current').then(r=>r.json())}catch(_){}
  const versions=v.versions||[];
  const current=cur.ok?cur.version:null;
  const typeEmoji={feature:'🎉 新功能',fix:'🔧 修复',improve:'💡 改进',breaking:'⚠️ 重大变更'};
  const typeColor={feature:'var(--green)',fix:'var(--blue)',improve:'var(--yellow)',breaking:'var(--red)'};

  // Stats row
  let totalEntries=0;
  versions.forEach(ver=>totalEntries+=(ver.changelog_count||0));
  let h=`<div class="page-head">
    <div><div class="ph-title">版本 <span class="em">日志</span></div>
      <div class="ph-sub">版本号、变更记录与群聊推送</div></div>
    <div class="ph-right"><button class="btn primary" onclick="loadPage('versions')">🔄 刷新</button></div>
  </div>
  <div class="bento">
    <div class="tile c2 b-4 reveal" style="--i:0"><div class="tico">📦</div><div class="tbody"><div class="num">${versions.length}</div><div class="lbl">版本总数</div></div></div>
    <div class="tile c3 b-4 reveal" style="--i:1"><div class="tico">🚀</div><div class="tbody"><div class="num">${current?current.version:'—'}</div><div class="lbl">当前版本</div></div></div>
    <div class="tile c4 b-4 reveal" style="--i:2"><div class="tico">📝</div><div class="tbody"><div class="num">${totalEntries}</div><div class="lbl">变更记录</div></div></div>
  </div>`;

  // Current version card
  if(current){
    h+=`<div class="panel"><div class="panel-header">🟢 当前版本：v${current.version}
      <span style="font-size:.7rem;color:var(--muted);margin-left:8px">${esc(current.release_date)} · ${esc(current.author)}</span>
      <span class="ph-right"><button class="btn primary" onclick="scrollToVersion(${current.id})">查看详情</button></span>
    </div>`;
    if(current.description){
      h+=`<div class="panel-body"><div style="font-size:.78rem;color:var(--text);line-height:1.6">${esc(current.description)}</div></div>`;
    }
    // Recent changelogs for current version
    const logs=current.changelogs||[];
    if(logs.length){
      h+=`<div class="panel-body" style="border-top:1px solid var(--border)">`;
      logs.forEach(e=>{
        h+=`<div class="cl-row">
          <span class="tag" style="color:${typeColor[e.entry_type]||'var(--muted)'};background:color-mix(in srgb, ${typeColor[e.entry_type]||'var(--muted)'} 16%, transparent)">${typeEmoji[e.entry_type]||e.entry_type}</span>
          <span class="cl-main"><b>${esc(e.title)}</b>${e.description?` <span class="cl-desc">— ${esc(e.description)}</span>`:''}</span>
          <span class="cl-meta">${esc(e.created_at||'')}</span>
        </div>`;
      });
      h+=`</div>`;
    }
    h+=`</div>`;
  }

  // Version history
  h+=`<div class="panel"><div class="panel-header">📋 版本历史
    <span class="ph-right"><button class="btn accent" onclick="showAddVersion()">➕ 新建版本</button></span>
  </div>
  <div class="panel-body" style="max-height:60vh;overflow-y:auto">`;

  // Add version form (hidden by default)
  h+=`<div id="addVersionForm" style="display:none;padding:12px;background:var(--bg);border-radius:8px;margin-bottom:12px;border:1px solid var(--border)">
    <div style="font-size:.78rem;font-weight:600;margin-bottom:8px">➕ 创建新版本</div>
    <div class="add-form">
      <input id="newVersion" placeholder="版本号 (如 1.1.0)" style="width:130px">
      <input id="newVerDesc" placeholder="版本说明..." style="flex:1;min-width:200px">
      <select id="newVerAuthor"><option value="dashboard">dashboard</option><option value="developer">developer</option></select>
      <label style="font-size:.72rem;color:var(--muted);white-space:nowrap"><input type="checkbox" id="newVerNotify" checked>群聊通知</label>
      <button onclick="addVersion()">➕ 创建版本</button>
      <button onclick="hideAddVersion()" style="border-color:var(--border);color:var(--muted)">取消</button>
      <button onclick="bumpAndCreate('patch')" class="btn" style="color:var(--green);border-color:rgba(63,185,80,.3);font-size:.65rem">patch++</button>
      <button onclick="bumpAndCreate('minor')" class="btn" style="color:var(--yellow);border-color:rgba(210,153,29,.3);font-size:.65rem">minor++</button>
      <button onclick="bumpAndCreate('major')" class="btn" style="color:var(--red);border-color:rgba(248,81,73,.3);font-size:.65rem">major++</button>
    </div>
  </div>`;

  // Add changelog form (hidden by default)
  h+=`<div id="addChangelogForm" style="display:none;padding:12px;background:var(--bg);border-radius:8px;margin-bottom:12px;border:1px solid var(--border)">
    <div style="font-size:.78rem;font-weight:600;margin-bottom:8px">📝 添加变更日志 <span id="clTargetVer" style="color:var(--accent)"></span></div>
    <div class="add-form">
      <select id="clType"><option value="feature">🎉 新功能</option><option value="fix">🔧 修复</option><option value="improve">💡 改进</option><option value="breaking">⚠️ 重大变更</option></select>
      <input id="clTitle" placeholder="变更标题..." style="flex:1;min-width:160px">
      <input id="clDesc" placeholder="变更描述（可选）..." style="flex:1;min-width:160px">
      <input type="hidden" id="clVersionId" value="0">
      <button onclick="addChangelog()">➕ 添加</button>
      <button onclick="hideAddChangelog()" style="border-color:var(--border);color:var(--muted)">取消</button>
    </div>
  </div>`;

  if(!versions.length){
    h+=`<div class="empty">暂无版本记录</div>`;
  } else {
    // Filter toolbar for changelog types
    h+=`<div class="toolbar" style="margin-bottom:10px">
      <span style="font-size:.7rem;color:var(--muted)">筛选:</span>
      <button class="on" data-vt="all">全部</button>
      <button data-vt="feature">🎉 新功能</button>
      <button data-vt="fix">🔧 修复</button>
      <button data-vt="improve">💡 改进</button>
      <button data-vt="breaking">⚠️ 重大变更</button>
    </div>`;

    versions.forEach((ver,vi)=>{
      const logCount=ver.changelog_count||0;
      h+=`<div class="version-block reveal" style="--i:${Math.min(vi+3,14)}" id="ver-${ver.id}">
        <div class="vb-head" onclick="toggleVersion(${ver.id})">
          <span class="vb-ver">v${esc(ver.version)}</span>
          <span class="ph-sub">${esc(ver.release_date||'')}</span>
          <span class="tag u">${logCount} 条变更</span>
          <span class="vb-desc">${esc(ver.description||'')}</span>
          <span class="vb-author">${esc(ver.author||'')}</span>
          <button class="btn sm" onclick="event.stopPropagation();showAddChangelog(${ver.id},'${ver.version}')" title="添加变更日志">➕ 日志</button>
          <button class="btn sm accent" onclick="event.stopPropagation();pushVersion(${ver.id})" title="手动推送版本更新到群聊">📢 推送</button>
          <span id="verExpand-${ver.id}" class="vb-arrow">▶</span>
        </div>
        <div id="verLogs-${ver.id}" style="display:none;border-top:1px solid var(--border);padding:12px 18px">
          <div style="color:var(--muted);font-size:.7rem;text-align:center">加载中…</div>
        </div>
      </div>`;
    });
  }

  return h+`</div></div>`;
}

function bindVersions(){
  // Type filter buttons
  document.querySelectorAll('button[data-vt]').forEach(b=>b.addEventListener('click',()=>{
    document.querySelectorAll('button[data-vt]').forEach(x=>x.classList.remove('on'));b.classList.add('on');
    versionFilter.type=b.dataset.vt;
  }));
}

async function toggleVersion(versionId){
  const logsDiv=$('verLogs-'+versionId);
  const expandIcon=$('verExpand-'+versionId);
  if(!logsDiv||!expandIcon)return;
  // Toggle collapse
  if(logsDiv.style.display!=='none'){
    logsDiv.style.display='none';expandIcon.textContent='▶';return;
  }
  logsDiv.style.display='block';expandIcon.textContent='▼';
  // Fetch changelogs for this version
  try{
    const r=await fetch('/api/changelog?version_id='+versionId+'&limit=50').then(r=>r.json());
    const logs=r.changelogs||[];
    const typeEmoji={feature:'🎉 新功能',fix:'🔧 修复',improve:'💡 改进',breaking:'⚠️ 重大变更'};
    const typeColor={feature:'var(--green)',fix:'var(--blue)',improve:'var(--yellow)',breaking:'var(--red)'};
    if(!logs.length){
      logsDiv.innerHTML='<div style="color:var(--muted);font-size:.7rem;text-align:center;padding:12px">暂无变更日志</div>';
    }else{
      logsDiv.innerHTML=logs.map(e=>`
        <div class="cl-row">
          <span class="tag" style="color:${typeColor[e.entry_type]||'var(--muted)'};background:color-mix(in srgb, ${typeColor[e.entry_type]||'var(--muted)'} 16%, transparent);flex-shrink:0">${typeEmoji[e.entry_type]||e.entry_type}</span>
          <div class="cl-main"><b>${esc(e.title)}</b>${e.description?`<br><span class="cl-desc">${esc(e.description)}</span>`:''}</div>
          <div class="cl-meta">${esc(e.created_at||'')}<br>${esc(e.author||'')}</div>
          <button class="btn sm accent" style="flex-shrink:0" onclick="event.stopPropagation();pushChangelog(${e.id})" title="手动推送到群聊">📢</button>
        </div>`).join('');
    }
  }catch(_){logsDiv.innerHTML='<div style="color:var(--red);font-size:.7rem;text-align:center;padding:12px">加载失败</div>'}
}

function scrollToVersion(versionId){
  loadPage('versions').then(()=>{
    setTimeout(()=>{
      const el=document.getElementById('ver-'+versionId);
      if(el)el.scrollIntoView({behavior:'smooth',block:'center'});
      toggleVersion(versionId);
    },200);
  });
}

function showAddVersion(){
  const f=$('addVersionForm');if(f)f.style.display='block';
}

function hideAddVersion(){
  const f=$('addVersionForm');if(f)f.style.display='none';
}

function showAddChangelog(versionId,versionStr){
  const f=$('addChangelogForm');const inp=$('clVersionId');const lbl=$('clTargetVer');
  if(f)f.style.display='block';if(inp)inp.value=versionId;if(lbl)lbl.textContent='→ v'+versionStr;
  // Scroll form into view
  const verEl=$('ver-'+versionId);if(verEl)verEl.scrollIntoView({behavior:'smooth',block:'center'});
}

function hideAddChangelog(){
  const f=$('addChangelogForm');if(f)f.style.display='none';
}

async function addVersion(){
  const version=($('newVersion')||{}).value?.trim();
  const description=($('newVerDesc')||{}).value?.trim();
  const author=($('newVerAuthor')||{}).value||'dashboard';
  const notify=($('newVerNotify')||{}).checked||false;
  if(!version){toast('请输入版本号','err');return}
  if(!/^\d+\.\d+\.\d+$/.test(version)){toast('版本号格式：X.Y.Z（如 1.0.0）','err');return}
  try{
    const r=await fetch('/api/versions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version,description,author,notify})});
    const d=await r.json();
    if(d.ok){toast('版本 '+version+' 已创建！'+(notify?' 已发送群聊通知':''));hideAddVersion();loadPage('versions')}
    else toast('创建失败: '+(d.error||'未知'),'err')
  }catch(_){toast('请求失败','err')}
}

async function addChangelog(){
  const versionId=parseInt(($('clVersionId')||{}).value)||0;
  const entryType=($('clType')||{}).value||'feature';
  const title=($('clTitle')||{}).value?.trim();
  const description=($('clDesc')||{}).value?.trim();
  if(!versionId||!title){toast('请填写版本ID和变更标题','err');return}
  try{
    const r=await fetch('/api/changelog',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version_id:versionId,entry_type:entryType,title,description})});
    const d=await r.json();
    if(d.ok){toast('变更日志已添加');hideAddChangelog();loadPage('versions')}
    else toast('添加失败: '+(d.error||'未知'),'err')
  }catch(_){toast('请求失败','err')}
}

async function bumpAndCreate(bumpType){
  try{
    const cur=await fetch('/api/versions/current').then(r=>r.json());
    let currentVer='1.0.0';
    if(cur.ok&&cur.version)currentVer=cur.version.version||'1.0.0';
    const parts=currentVer.split('.').map(Number);
    if(bumpType==='major'){parts[0]++;parts[1]=0;parts[2]=0}
    else if(bumpType==='minor'){parts[1]++;parts[2]=0}
    else parts[2]++;
    const newVer=parts.join('.');
    const inp=$('newVersion');if(inp)inp.value=newVer;
    showAddVersion();
  }catch(_){toast('获取当前版本失败','err')}
}

async function pushVersion(versionId){
  if(!confirm('确定要推送此版本更新到所有群聊？'))return;
  toast('正在推送...');
  try{
    const r=await fetch('/api/versions/'+versionId+'/push',{method:'POST'});
    const d=await r.json();
    if(d.ok){toast('版本 v'+d.pushed+' 已推送到群聊！')}
    else toast('推送失败: '+(d.error||'未知'),'err')
  }catch(_){toast('推送请求失败','err')}
}

async function pushChangelog(entryId){
  if(!confirm('确定要推送此变更日志到所有群聊？'))return;
  toast('正在推送...');
  try{
    const r=await fetch('/api/changelog/'+entryId+'/push',{method:'POST'});
    const d=await r.json();
    if(d.ok){toast('已推送到群聊: '+d.pushed)}
    else toast('推送失败: '+(d.error||'未知'),'err')
  }catch(_){toast('推送请求失败','err')}
}

// ── Stickers ──
async function stickersHTML(){
  let [d, cats] = await Promise.all([
    fetch('/api/stickers').then(r=>r.json()).catch(()=>({stickers:[], total:0})),
    fetch('/api/stickers/categories').then(r=>r.json()).catch(()=>({categories:[]})),
  ]);
  if(!d.stickers.length)return `<div class="panel"><div class="panel-header">🎨 表情包</div><div class="panel-body"><div class="empty">暂未收集到表情包，群聊中发送的图片会自动收集 ✨</div></div></div>`;
  let h=`<div class="page-head">
    <div><div class="ph-title">表情包 <span class="em">画廊</span></div>
      <div class="ph-sub">群聊中发送的图片会自动收集并分类</div></div>
    <div class="ph-right">
      <button class="btn green" onclick="organizeStickers(event)">📊 批量分类</button>
      <button class="btn accent" onclick="scanDuplicates(event)">🔍 查找重复</button>
      <button class="btn danger" onclick="cleanupDuplicates(event)">🗑️ 清理重复</button>
      <button class="btn danger" onclick="cleanupOrphans(event)">🧹 清理孤立</button>
      <button class="btn primary" onclick="loadPage('stickers')">🔄 刷新</button>
    </div>
  </div>
  <div class="toolbar">
    <span class="ph-sub" id="dedupProgress"></span>
    <span class="ph-sub" id="organizeProgress"></span>
  </div>`;
  // Category filter toolbar
  if((cats.categories||[]).length>0){
    h+=`<div class="toolbar">
      <label>分类</label>
      <select id="stickerCategoryFilter" onchange="filterStickers()">
        <option value="">全部 (${d.total})</option>`;
    (cats.categories||[]).forEach(c=>{
      h+=`<option value="${esc(c.name)}">${esc(c.name)} (${c.count})</option>`;
    });
    h+=`</select></div>`;
  }
  h+=`<div class="panel reveal" style="--i:0"><div class="panel-header"><span class="hicon">🖼️</span>所有表情包
    <span class="ph-right"><span class="tag u">${d.total} 个</span></span></div>
  <div class="panel-body"><div class="sticker-grid">`;
  d.stickers.forEach(s=>{
    let cat = s.category || '未分类';
    let catTag = cat !== '未分类'
      ? `<span class="tag u" style="font-size:.58rem">${esc(cat)}</span>`
      : `<span class="tag" style="font-size:.58rem">未分类</span>`;
    let fname = s.filename || s.name || '';
    let fsize = s.file_size || s.size || 0;
    h+=`<div class="sticker-card" data-category="${esc(cat)}" id="sticker-${fname.replace(/[^a-zA-Z0-9_.-]/g,'')}">
      <div style="position:relative">
        <img src="/stickers/${encodeURIComponent(fname)}" alt="${esc(fname)}" loading="lazy" onerror="this.style.display='none'">
        <button class="sticker-del-btn" onclick="event.stopPropagation();delSticker('${fname}')" title="删除此表情包">🗑️</button>
      </div>
      <div class="sinfo">
        <span title="${esc(fname)}" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(fname.substring(0,14))}</span>
        <span style="display:flex;align-items:center;gap:5px;flex-shrink:0">${catTag}<span>${formatSize(fsize)}</span></span>
      </div>
    </div>`;
  });
  return h+`</div></div></div>`;
}

function filterStickers(){
  const cat = document.getElementById('stickerCategoryFilter')?.value || '';
  document.querySelectorAll('.sticker-card').forEach(card => {
    card.style.display = !cat || card.dataset.category === cat ? '' : 'none';
  });
}

async function delSticker(filename){
  if(!confirm(`确定删除表情包「${filename}」？此操作不可恢复。`))return;
  try{
    const r=await fetch('/api/stickers/'+encodeURIComponent(filename),{method:'DELETE'});
    const d=await r.json();
    if(d.ok){
      const el=document.getElementById('sticker-'+filename.replace(/[^a-zA-Z0-9_.-]/g,''));
      if(el)el.remove();
      toast('已删除: '+filename);
    }else toast('删除失败: '+(d.error||'未知'),'err')
  }catch(_){toast('请求失败','err')}
}

async function cleanupOrphans(ev){
  const btn=ev.target;
  btn.disabled=true;
  btn.textContent='⏳ 清理中...';
  try{
    const r=await fetch('/api/stickers/orphans/cleanup',{method:'POST'});
    const d=await r.json();
    if(d.ok){
      toast(`已清理 ${d.cleaned} 条孤立数据库记录`);
      loadPage('stickers');
    }else toast('清理失败: '+(d.error||'未知'),'err')
  }catch(_){toast('请求失败','err')}
  btn.disabled=false;
  btn.textContent='🧹 清理孤立';
}

async function organizeStickers(ev){
  const btn = ev.target;
  btn.disabled = true;
  btn.textContent = '⏳ 启动中...';
  try {
    let r = await fetch('/api/stickers/organize', {method:'POST'}).then(r=>r.json());
    if(!r.ok) { alert(r.error||'启动失败'); btn.disabled=false; btn.textContent='📊 批量分类'; return; }
    const progEl = document.getElementById('organizeProgress');
    const iv = setInterval(async()=>{
      let p = await fetch('/api/stickers/organize/progress').then(r=>r.json());
      if(!p.running){
        clearInterval(iv);
        progEl.textContent = `✅ 完成：${p.completed||0}个`;
        btn.disabled=false;
        btn.textContent='📊 批量分类';
        loadPage('stickers');
        return;
      }
      progEl.textContent = `⏳ ${p.completed}/${p.total} (失败${p.failed||0})`;
    }, 2000);
  } catch(e){
    btn.disabled=false;
    btn.textContent='📊 批量分类';
  }
}

// ── Sticker dedup ──
async function scanDuplicates(ev){
  const btn = ev.target;
  btn.disabled = true;
  btn.textContent = '⏳ 扫描中...';
  const progEl = document.getElementById('dedupProgress');
  try {
    let r = await fetch('/api/stickers/duplicates').then(r=>r.json());
    if(!r.ok) { alert(r.error||'启动失败'); btn.disabled=false; btn.textContent='🔍 查找重复'; return; }
    const iv = setInterval(async()=>{
      let p = await fetch('/api/stickers/duplicates/progress').then(r=>r.json());
      if(!p.running){
        clearInterval(iv);
        const sr = p.scan_result || {};
        if(sr.error){
          progEl.textContent = '❌ 扫描失败';
        } else {
          const waste = sr.waste_bytes ? formatSize(sr.waste_bytes) : '0B';
          progEl.innerHTML = `✅ ${sr.groups||0}组重复，${sr.duplicate_files||0}个冗余文件，浪费${waste}`;
          if(sr.groups > 0){
            if(confirm(`发现 ${sr.groups} 组重复表情包（${sr.duplicate_files} 个冗余文件，浪费 ${waste}）。\n\n是否立即清理？将保留每组中最大的文件。`)){
              setTimeout(()=>{
                const cb = document.querySelector('button[onclick*="cleanupDuplicates"]');
                if(cb) cb.click();
              }, 500);
            }
          }
        }
        btn.disabled=false;
        btn.textContent='🔍 查找重复';
        return;
      }
      progEl.textContent = `⏳ 扫描中...`;
    }, 2000);
  } catch(e){
    btn.disabled=false;
    btn.textContent='🔍 查找重复';
  }
}

async function cleanupDuplicates(ev){
  const btn = ev.target;
  if(!confirm('确定要清理重复表情包吗？\n\n将先预览再确认执行。')) return;
  btn.disabled = true;
  btn.textContent = '⏳ 清理中...';
  const progEl = document.getElementById('dedupProgress');
  // Preview first
  try {
    let r = await fetch('/api/stickers/duplicates/cleanup?dry_run=1', {method:'POST'}).then(r=>r.json());
    if(!r.ok) { alert(r.error||'启动失败'); btn.disabled=false; btn.textContent='🗑️ 清理重复'; return; }
    const iv = setInterval(async()=>{
      let p = await fetch('/api/stickers/duplicates/progress').then(r=>r.json());
      if(!p.running){
        clearInterval(iv);
        const cr = p.cleanup_result || {};
        if(cr.error){
          progEl.textContent = '❌ 清理失败';
          btn.disabled=false;
          btn.textContent='🗑️ 清理重复';
          return;
        }
        if(cr.groups_cleaned === 0){
          progEl.textContent = '✅ 没有发现重复';
          btn.disabled=false;
          btn.textContent='🗑️ 清理重复';
          return;
        }
        const waste = cr.total_waste_bytes ? formatSize(cr.total_waste_bytes) : '0B';
        if(confirm(`预览：${cr.groups_cleaned}组重复，将删除${cr.files_removed}个文件，保留${cr.files_kept}个，释放${waste}。\n\n确认执行？`)){
          // Execute for real
          btn.textContent = '⏳ 执行中...';
          let r2 = await fetch('/api/stickers/duplicates/cleanup?dry_run=0', {method:'POST'}).then(r=>r.json());
          const iv2 = setInterval(async()=>{
            let p2 = await fetch('/api/stickers/duplicates/progress').then(r=>r.json());
            if(!p2.running){
              clearInterval(iv2);
              const cr2 = p2.cleanup_result || {};
              progEl.textContent = cr2.error ? '❌ 清理失败' : `✅ 已删除${cr2.files_removed||0}个重复，保留${cr2.files_kept||0}个`;
              btn.disabled=false;
              btn.textContent='🗑️ 清理重复';
              loadPage('stickers');
            }
          }, 2000);
        } else {
          progEl.textContent = '已取消';
          btn.disabled=false;
          btn.textContent='🗑️ 清理重复';
        }
        return;
      }
      progEl.textContent = '⏳ 扫描中...';
    }, 2000);
  } catch(e){
    btn.disabled=false;
    btn.textContent='🗑️ 清理重复';
  }
}

// ── Groups ──
async function groupsHTML(){
  let d={groups:[],total:0};try{d=await fetch('/api/groups').then(r=>r.json())}catch(_){}
  _groupsCache=d.groups||[];
  let h=`<div class="page-head">
    <div><div class="ph-title">群组 <span class="em">管理</span></div>
      <div class="ph-sub">已接入并产生过消息的群 · 可删除群与其全部数据</div></div>
    <div class="ph-right"><button class="btn primary" onclick="loadPage('groups')">🔄 刷新</button></div>
  </div>
  <div class="bento">
    <div class="tile c2 b-4 reveal" style="--i:0"><div class="tico">👥</div><div class="tbody"><div class="num">${d.total||d.groups.length}</div><div class="lbl">群总数</div></div></div>
    <div class="tile c1 b-4 reveal" style="--i:1"><div class="tico">💬</div><div class="tbody"><div class="num">${d.groups.reduce((a,g)=>a+(g.msg_count||0),0)}</div><div class="lbl">累计消息</div></div></div>
    <div class="tile c3 b-4 reveal" style="--i:2"><div class="tico">🕐</div><div class="tbody"><div class="num" style="font-size:1.05rem">${esc((d.groups.find(g=>g.last_active)||{}).last_active||'—')}</div><div class="lbl">最近活跃</div></div></div>
  </div>
  <div class="panel reveal" style="--i:3;margin-top:16px">
    <div class="panel-header"><span class="hicon">📋</span>群列表</div>
    <div class="panel-body tight"><div class="list">`;
  if(!d.groups.length){
    h+=`<div class="empty"><span class="em-ico">👥</span>暂未发现活跃群组，等机器人收到群消息后会自动注册</div>`;
  } else {
    d.groups.forEach(g=>h+=`<div class="item">
      <div class="iava">${esc((g.group_name||'群').slice(0,1))}</div>
      <div class="imain">
        <div class="ititle">${esc(g.group_name||'未命名群')}</div>
        <div class="isub" style="font-family:var(--mono)">${esc(g.group_id)}</div>
      </div>
      <div class="imeta">
        <span class="tag u">💬 ${g.msg_count||0} 条</span>
        <span class="tag">🕐 ${esc(g.last_active||'未知')}</span>
      </div>
      <div class="iact">
        <button class="btn sm danger" onclick="deleteGroup('${esc(g.group_id)}')" title="删除该群及其全部数据">🗑️ 删除</button>
      </div>
    </div>`);
  }
  return h+`</div></div></div>`;
}

// ── Delete group (purge data, optional leave) ──
let _groupsCache=[];

function closeModal(){
  const m=$('modalMask');
  if(m)m.remove();
}

async function deleteGroup(groupId){
  closeModal();
  const g=_groupsCache.find(x=>String(x.group_id)===String(groupId))||{};
  const gname=g.group_name||groupId;

  let counts={};
  try{
    const r=await fetch('/api/groups/'+encodeURIComponent(groupId)+'/purge-preview').then(r=>r.json());
    counts=r.counts||{};
  }catch(_){}

  const labels={
    users:'涉及用户', group_messages:'群消息', history:'对话记录', reminders:'提醒',
    tool_usage:'工具调用', user_profiles:'用户画像', user_affection:'好感度',
    user_affection_log:'好感度流水', feature_requests:'功能需求',
    group_settings:'群功能开关', learning_log:'学习笔记（按用户，跨群共享）',
    user_settings:'用户功能开关（按用户）',
  };
  const rows=Object.keys(labels)
    .filter(k=>(counts[k]||0)>0)
    .map(k=>`<div class="dl-row"><span>${labels[k]}</span><b>${counts[k]}</b></div>`).join('')
    || '<div class="dl-row"><span>该群暂无数据</span><b>0</b></div>';

  const mask=document.createElement('div');
  mask.className='modal-mask';
  mask.id='modalMask';
  mask.innerHTML=`
    <div class="modal">
      <div class="modal-head">🗑️ 删除群聊 <span style="font-weight:500;color:var(--muted);font-size:.8rem">${esc(gname)}</span></div>
      <div class="modal-body">
        <div class="warn-box">此操作不可撤销。将永久删除该群在机器人里的全部数据（表情包图库是全局共享的，不会被删）。</div>
        <div style="font-size:.75rem;color:var(--muted);margin-top:16px">即将删除：</div>
        <div class="danger-list">${rows}</div>
        <label class="chk"><input type="checkbox" id="dgLeave"> 同时让机器人退出该 QQ 群（需重新邀请才能回来）</label>
        <div style="font-size:.75rem;color:var(--muted);margin-top:16px">请输入群号 <b style="font-family:var(--mono);color:var(--text)">${esc(groupId)}</b> 以确认：</div>
        <input type="text" id="dgConfirm" placeholder="${esc(groupId)}" autocomplete="off">
      </div>
      <div class="modal-foot">
        <button class="btn" onclick="closeModal()">取消</button>
        <button class="btn danger" id="dgSubmit" onclick="confirmDeleteGroup('${esc(groupId)}')">确认删除</button>
      </div>
    </div>`;
  mask.addEventListener('click',e=>{if(e.target===mask)closeModal()});
  document.body.appendChild(mask);
  setTimeout(()=>{const i=$('dgConfirm');if(i)i.focus()},60);
}

async function confirmDeleteGroup(groupId){
  const typed=($('dgConfirm')?.value||'').trim();
  if(typed!==String(groupId)){toast('群号不匹配，请输入完整群号','err');return}
  const leave=!!($('dgLeave')||{}).checked;
  const btn=$('dgSubmit');
  if(btn){btn.disabled=true;btn.textContent='删除中…'}
  try{
    const r=await fetch('/api/groups/'+encodeURIComponent(groupId),{
      method:'DELETE',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({leave}),
    });
    const d=await r.json();
    if(d.ok){
      closeModal();
      const n=Object.values(d.deleted||{}).reduce((a,b)=>a+(typeof b==='number'?b:0),0);
      toast(`已删除群数据（${n} 条）${leave?(d.left?' · 已退出群聊':' · 退出群聊失败'):''}`, leave&&!d.left?'err':'ok');
      loadPage('groups');
    }else{
      toast('删除失败: '+(d.error||'未知错误'),'err');
      if(btn){btn.disabled=false;btn.textContent='确认删除'}
    }
  }catch(_){
    toast('请求失败','err');
    if(btn){btn.disabled=false;btn.textContent='确认删除'}
  }
}

// ── Messages ──
async function messagesHTML(){
  let d={records:[]};try{d=await fetch('/api/messages').then(r=>r.json())}catch(_){}
  let h=`<div class="page-head">
    <div><div class="ph-title">群 <span class="em">消息</span></div>
      <div class="ph-sub">群里采集到的原始消息</div></div>
    <div class="ph-right"><span class="tag u">${d.records.length} 条</span>
      <button class="btn primary" onclick="loadPage('messages')">🔄 刷新</button></div>
  </div>
  <div class="panel reveal" style="--i:0"><div class="panel-body tight"><div class="list">`;
  if(!d.records.length){
    h+=`<div class="empty"><span class="em-ico">📨</span>暂无消息</div>`;
  } else {
    d.records.forEach(x=>h+=`<div class="item">
      <div class="iava">${esc((x.user_name||'?').slice(0,1))}</div>
      <div class="imain">
        <div class="ititle">${esc(x.user_name||'未知用户')}</div>
        <div class="isub">${esc(String(x.content||'').slice(0,140))}</div>
      </div>
      <div class="imeta"><span class="tag">${esc(x.time||'')}</span></div>
    </div>`);
  }
  return h+`</div></div></div>`;
}

// ── Affection page ──────────────────────────────────

async function affectionHTML(){
  // Fetch leaderboard data
  let board=[], groups=[];
  try{
    board=(await fetch('/api/affection/leaderboard').then(r=>r.json())).leaderboard||[];
    groups=(await fetch('/api/groups').then(r=>r.json())).groups||[];
  }catch(_){}
  const top=board[0];
  const groupOpts=groups.map(g=>`<option value="${esc(g.group_id)}">${esc(g.group_name||g.group_id)}</option>`).join('');

  let h=`<div class="page-head">
    <div><div class="ph-title">好感度 <span class="em">排行</span></div>
      <div class="ph-sub">基于互动频率与正面反馈自动计算</div></div>
    <div class="ph-right"><button class="btn primary" onclick="loadPage('affection')">🔄 刷新</button></div>
  </div>
  <div class="bento">
    <div class="tile c3 b-4 reveal" style="--i:0"><div class="tico">💕</div><div class="tbody"><div class="num">${board.length}</div><div class="lbl">记录总数</div></div></div>
    <div class="tile c4 b-4 reveal" style="--i:1"><div class="tico">🥇</div><div class="tbody"><div class="num" style="font-size:1.1rem">${esc(top?(top.user_name||'—'):'—')}</div><div class="lbl">当前榜首</div></div></div>
    <div class="tile c5 b-4 reveal" style="--i:2"><div class="tico">⭐</div><div class="tbody"><div class="num">${top?top.affection_score:0}</div><div class="lbl">最高好感度</div></div></div>
  </div>

  <div class="panel reveal" style="--i:3;margin-top:16px">
    <div class="panel-header"><span class="hicon">🏆</span>排行榜
      <span class="ph-right">
        <select id="aff-group-filter" onchange="loadAffectionBoard()"><option value="">全部群</option>${groupOpts}</select>
        <span class="tag u">共 <b id="aff-total">0</b> 条</span>
      </span>
    </div>
    <table id="aff-board">
      <thead><tr><th>排名</th><th>用户</th><th>好感度</th><th>关系</th><th>互动</th><th>评价</th><th>最后互动</th></tr></thead>
      <tbody><tr><td colspan="7" style="text-align:center;padding:26px;color:var(--muted)">加载中…</td></tr></tbody>
    </table>
  </div>

  <div class="panel reveal" style="--i:4">
    <div class="panel-header"><span class="hicon">🔧</span>手动调整好感度</div>
    <div class="panel-body">
      <div class="toolbar" style="margin:0">
        <select id="aff-adj-group"><option value="">选择群</option>${groupOpts}</select>
        <input id="aff-adj-user" placeholder="用户 QQ号" style="width:140px">
        <input id="aff-adj-delta" type="number" placeholder="调整值 (±)" style="width:120px" step="0.5">
        <input id="aff-adj-note" placeholder="备注（可选）" style="width:170px">
        <button class="green" onclick="adjustAffection()">✅ 应用</button>
      </div>
      <div id="aff-adj-msg" style="font-size:.74rem;margin-top:8px"></div>
    </div>
  </div>`;

  return h;
}

async function loadAffectionBoard(){
  const sel=$('aff-group-filter');
  const gid=sel?sel.value:'';
  try{
    const url=gid?`/api/affection/leaderboard?group_id=${encodeURIComponent(gid)}`:'/api/affection/leaderboard';
    const data=await fetch(url).then(r=>r.json());
    const board=data.leaderboard||[];
    if($('aff-total'))$('aff-total').textContent=board.length;
    renderAffectionBoard(board);
  }catch(_){
    const tbody=document.querySelector('#aff-board tbody');
    if(tbody)tbody.innerHTML='<tr><td colspan="7" style="text-align:center;padding:20px;color:var(--red)">加载失败</td></tr>';
  }
}

function renderAffectionBoard(board){
  const tbody=document.querySelector('#aff-board tbody');
  if(!tbody)return;
  if(!board.length){
    tbody.innerHTML='<tr><td colspan="7" style="text-align:center;padding:20px;color:var(--muted)">还没有好感度数据哦～多和 Kiriko 聊天互动吧！(◕‿◕✿)</td></tr>';
    return;
  }
  let rows='';
  board.forEach((x,i)=>{
    const medal={0:'🥇',1:'🥈',2:'🥉'}[i]||`<span style="color:var(--muted);font-weight:700">${i+1}</span>`;
    const score=Number(x.affection_score)||0;
    const barW=Math.max(3,Math.min(100,Math.round(score)));
    const barColor=score>=80?'var(--accent)':score>=60?'var(--purple)':score>=40?'var(--blue)':score>=20?'var(--yellow)':'var(--muted)';
    rows+=`<tr>
      <td style="font-weight:700;font-size:1rem">${medal}</td>
      <td>
        <div style="display:flex;align-items:center;gap:9px">
          <span class="iava" style="width:30px;height:30px;border-radius:9px;font-size:.82rem">${esc((x.user_name||'?').slice(0,1))}</span>
          <span>${x.emoji||''} ${esc(x.user_name||'')}</span>
        </div>
      </td>
      <td style="min-width:170px">
        <div style="display:flex;align-items:center;gap:9px">
          <span style="font-weight:750;min-width:46px;font-variant-numeric:tabular-nums">${score}分</span>
          <div style="flex:1;height:10px;background:var(--card-2);border-radius:6px;overflow:hidden">
            <div class="aff-fill" style="height:100%;width:${barW}%;background:${barColor};border-radius:6px"></div>
          </div>
        </div>
      </td>
      <td><span class="tag t">${esc(x.relationship||'?')}</span></td>
      <td style="font-variant-numeric:tabular-nums">${x.interaction_count||0} 次</td>
      <td style="font-size:.76rem"><span style="color:var(--green)">👍 ${x.positive_count||0}</span> <span style="color:var(--muted)">/</span> <span style="color:var(--red)">👎 ${x.negative_count||0}</span></td>
      <td style="font-size:.71rem;color:var(--muted)">${esc(x.last_interaction||'从未')}</td>
    </tr>`;
  });
  tbody.innerHTML=rows;
}

async function adjustAffection(){
  const gid=($('aff-adj-group')||{}).value||'';
  const uid=($('aff-adj-user')||{}).value||'';
  const delta=parseFloat(($('aff-adj-delta')||{}).value||'0');
  const note=($('aff-adj-note')||{}).value||'';
  const msg=$('aff-adj-msg');
  if(!gid||!uid){if(msg)msg.innerHTML='<span style="color:var(--red)">请填写群和用户QQ号</span>';return;}
  if(!delta){if(msg)msg.innerHTML='<span style="color:var(--red)">请输入调整值</span>';return;}
  try{
    const r=await fetch('/api/affection/adjust',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({user_id:uid,group_id:gid,delta,note}),
    }).then(r=>r.json());
    if(r.ok){
      if(msg)msg.innerHTML=`<span style="color:var(--green)">✅ 已调整：${esc(r.adjusted.affection_score)}分 ${esc(r.adjusted.emoji)}${esc(r.adjusted.relationship)}</span>`;
      loadAffectionBoard();
    }else{
      if(msg)msg.innerHTML=`<span style="color:var(--red)">❌ ${esc(r.error)}</span>`;
    }
  }catch(e){
    if(msg)msg.innerHTML=`<span style="color:var(--red)">请求失败: ${e.message}</span>`;
  }
}

function bindAffection(){
  loadAffectionBoard();
}

// ── Feature settings (per-group / per-user toggles) ──
let SettingsScope={type:'group',id:''};
let SettingsFeatures=[],SettingsGroups=[],SettingsEnabled={};

async function settingsHTML(){
  try{
    const d=await fetch('/api/settings/groups').then(r=>r.json());
    SettingsFeatures=d.features||[];
    SettingsGroups=d.groups||[];
  }catch(_){}
  return `<div class="page-head">
      <div><div class="ph-title">群功能 <span class="em">设置</span></div>
        <div class="ph-sub">每个群 / 每个用户可独立开关各项功能 · 默认全部开启 · 修改即时生效</div></div>
      <div class="ph-right"><button class="btn primary" onclick="loadPage('settings')">🔄 刷新</button></div>
    </div>
    <div class="tabs">
      <button class="on" data-stab="group">👥 群设置</button>
      <button data-stab="user">🙍 用户设置</button>
    </div>
    <div class="scope-bar" id="scopeBar"></div>
    <div id="setRows"><div class="panel reveal"><div class="empty"><span class="em-ico">⚙️</span>请先选择群聊或用户</div></div></div>`;
}

function bindSettings(){
  $$('.tabs button[data-stab]').forEach(b=>b.addEventListener('click',()=>{
    $$('.tabs button[data-stab]').forEach(x=>x.classList.remove('on'));b.classList.add('on');
    SettingsScope={type:b.dataset.stab,id:''};
    SettingsEnabled={};
    SettingsRenderScopeBar();
    SettingsRenderRows();
  }));
  SettingsRenderScopeBar();
  SettingsRenderRows();
  $$('.tabs').forEach(initTabIndicator);
}

function initTabIndicator(tabs){
  if(!tabs||tabs.querySelector('.tab-ind'))return;
  tabs.style.position='relative';
  const ind=document.createElement('span');ind.className='tab-ind';tabs.appendChild(ind);
  const place=()=>{const on=tabs.querySelector('button.on')||tabs.querySelector('button');if(!on)return;ind.style.left=on.offsetLeft+'px';ind.style.width=on.offsetWidth+'px'};
  place();requestAnimationFrame(place);
  if(document.fonts&&document.fonts.ready)document.fonts.ready.then(place);
  $$('button',tabs).forEach(b=>b.addEventListener('click',()=>requestAnimationFrame(place)));
  window.addEventListener('resize',place);
}

function SettingsRenderScopeBar(){
  const bar=$('scopeBar');if(!bar)return;
  if(SettingsScope.type==='group'){
    const offCount={};
    SettingsGroups.forEach(g=>offCount[g.group_id]=(g.disabled||[]).length);
    bar.innerHTML=`<div class="fld"><label>选择群聊</label>
      <select id="setGroupSelect">
        <option value="">— 请选择群 —</option>
        ${SettingsGroups.map(g=>`<option value="${esc(g.group_id)}" ${g.group_id===SettingsScope.id?'selected':''}>${esc(g.group_name)}（${esc(g.group_id)}）${offCount[g.group_id]?` · 已关${offCount[g.group_id]}项`:''}</option>`).join('')}
      </select></div>
      <button class="btn accent" id="setResetBtn" onclick="SettingsReset()" ${SettingsScope.id?'':'disabled'}>♻️ 恢复默认（全部开启）</button>
      <span class="ph-sub" id="setScopeStatus"></span>`;
    const sel=$('setGroupSelect');
    sel.addEventListener('change',()=>{SettingsScope.id=sel.value;SettingsLoad()});
  }else{
    bar.innerHTML=`<div class="fld"><label>用户 QQ 号</label>
      <input id="setUserInput" placeholder="输入 QQ 号后回车" value="${SettingsScope.id}"></div>
      <button class="btn primary" onclick="SettingsLoadUser()">📂 加载</button>
      <button class="btn accent" id="setResetBtn" onclick="SettingsReset()" ${SettingsScope.id?'':'disabled'}>♻️ 恢复默认（全部开启）</button>
      <span class="ph-sub" id="setScopeStatus"></span>`;
    const uin=$('setUserInput');
    uin.addEventListener('keydown',e=>{if(e.key==='Enter')SettingsLoadUser()});
  }
}

function SettingsRenderRows(){
  const box=$('setRows');if(!box)return;
  if(!SettingsScope.id){
    box.innerHTML='<div class="panel"><div class="panel-body"><div class="empty">请先选择群聊或用户</div></div></div>';
    return;
  }
  const cats=[], byCat={};
  SettingsFeatures.forEach(f=>{if(!byCat[f.category]){byCat[f.category]=[];cats.push(f.category)}byCat[f.category].push(f)});
  let h='';
  cats.forEach(cat=>{
    const items=byCat[cat];
    const off=items.filter(f=>SettingsEnabled[f.key]===false).length;
    h+=`<div class="set-group"><div class="sg-head">${cat}<span class="sg-count">${off?`已关 ${off}/${items.length}`:`${items.length} 项 · 全部开启`}</span></div>`;
    items.forEach(f=>{
      const on=SettingsEnabled[f.key]!==false;
      h+=`<div class="set-row" data-fkey="${f.key}">
        <div class="sr-info">
          <div class="sr-label">${f.label}${on?'':' <span class="sr-off">· 已关闭</span>'}</div>
          <div class="sr-desc">${f.desc||''}</div>
        </div>
        <label class="switch"><input type="checkbox" data-fkey="${f.key}" ${on?'checked':''} onchange="SettingsToggle(this)"><span class="slider"></span></label>
      </div>`;
    });
    h+=`</div>`;
  });
  h+=`<div class="set-note">💡 修改即时生效，无需重启。关闭某项功能后，群内/私聊用户索要该功能时 Kiriko 会礼貌说明暂不可用。</div>`;
  box.innerHTML=h;
}

async function SettingsLoad(){
  if(!SettingsScope.id){SettingsRenderRows();return}
  SettingsEnabled={};
  SettingsRenderRows();
  try{
    const r=await fetch(`/api/settings/${SettingsScope.type}/${SettingsScope.id}`).then(r=>r.json());
    if(r.ok)SettingsEnabled=r.settings||{};
  }catch(_){}
  SettingsRenderRows();
  const st=$('setScopeStatus');
  if(st){
    const disabled=Object.keys(SettingsEnabled).filter(k=>SettingsEnabled[k]===false).length;
    st.textContent=disabled?`${disabled} 项已关闭`:'全部开启';
  }
}

function SettingsLoadUser(){
  const inp=$('setUserInput');if(!inp)return;
  SettingsScope.id=(inp.value||'').trim();
  const rb=$('setResetBtn');if(rb)rb.disabled=!SettingsScope.id;
  SettingsLoad();
}

async function SettingsToggle(cb){
  const key=cb.dataset.fkey;
  const enabled=cb.checked;
  if(!SettingsScope.id){cb.checked=!enabled;toast('请先选择群聊或用户','err');return}
  cb.disabled=true;
  try{
    const r=await fetch(`/api/settings/${SettingsScope.type}/${SettingsScope.id}`,{
      method:'PATCH',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({key,enabled}),
    }).then(r=>r.json());
    if(r.ok){
      SettingsEnabled=r.settings||{};
      const label=(SettingsFeatures.find(f=>f.key===key)||{}).label||key;
      toast((enabled?'✅ 已开启':'🚫 已关闭')+`「${label}」`);
      SettingsRenderRows();
      if(SettingsScope.type==='group')SettingsRefreshGroupList();
    }else{
      cb.checked=!enabled;toast('保存失败: '+(r.error||'未知'),'err');
    }
  }catch(_){cb.checked=!enabled;toast('请求失败','err')}
  cb.disabled=false;
}

async function SettingsReset(){
  if(!SettingsScope.id)return;
  const what=SettingsScope.type==='group'?'群':'用户';
  if(!confirm(`确定恢复该${what}的全部功能开关为默认（全部开启）？`))return;
  try{
    const r=await fetch(`/api/settings/${SettingsScope.type}/${SettingsScope.id}`,{method:'DELETE'}).then(r=>r.json());
    if(r.ok){
      SettingsEnabled=r.settings||{};
      toast('♻️ 已恢复默认，全部功能开启');
      SettingsRenderRows();
      SettingsRefreshGroupList();
    }else toast('操作失败: '+(r.error||'未知'),'err');
  }catch(_){toast('请求失败','err')}
}

async function SettingsRefreshGroupList(){
  try{
    const d=await fetch('/api/settings/groups').then(r=>r.json());
    SettingsGroups=d.groups||[];
    SettingsRenderScopeBar();
  }catch(_){}
}

// ── Theme (day/night) ──
function applyTheme(t){
  document.body.dataset.theme=t;
  const btn=$('themeToggle');
  if(btn){
    btn.textContent=t==='dark'?'☀️':'🌙';
    btn.classList.remove('spin');void btn.offsetWidth;btn.classList.add('spin');
  }
  try{localStorage.setItem('kiriko-theme',t)}catch(_){}
}
function toggleTheme(){
  applyTheme(document.body.dataset.theme==='dark'?'light':'dark');
}
(function initTheme(){
  let t='';
  try{t=localStorage.getItem('kiriko-theme')||''}catch(_){}
  if(!t)t=(window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches)?'dark':'light';
  document.body.dataset.theme=t;
  const btn=$('themeToggle');if(btn)btn.textContent=t==='dark'?'☀️':'🌙';
})();

// ── Counter animation (overview stat cards) ──
function animateCounters(){
  $$('.tile .num, .stat-card .num').forEach(el=>{
    const raw=el.textContent.trim();
    if(!/^\d+$/.test(raw))return;  // only animate pure integers
    const n=parseInt(raw,10), dur=500, t0=performance.now();
    const step=t=>{
      const p=Math.min(1,(t-t0)/dur);
      const e=1-Math.pow(1-p,3); // ease-out cubic
      el.textContent=Math.round(n*(0.2+0.8*e));
      if(p<1)requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });
}

// ══════════════════════════════════════════════════════════
//  LLBot bridge — native panels for the LLBot WebUI
//  All calls go through /llbot-api/*, where the Flask bridge
//  injects the WebUI password hash server-side.
// ══════════════════════════════════════════════════════════

let llbotLogES=null;

async function llbotApi(path,opts){
  const p=String(path||'').replace(/^\/+/,'');
  const r=await fetch('/llbot-api'+(p?('/'+p):''),opts);
  let d={};
  try{d=await r.json()}catch(_){d={success:false,message:'响应解析失败 (HTTP '+r.status+')'}}
  return d;
}

function fmtTime(sec){
  if(!sec)return '—';
  const d=new Date(sec*1000);
  const p=n=>String(n).padStart(2,'0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function fmtAgo(sec){
  if(!sec)return '—';
  const s=Math.floor(Date.now()/1000-sec);
  if(s<0)return '刚刚';
  return fmtUptime(s)+'前';
}

// Small status pill in the topbar; refreshed on a timer.
async function refreshLLBotPill(){
  const el=$('tbLLBot');
  if(!el)return;
  try{
    const st=await llbotApi('');
    if(!st.ok){el.className='pill err';el.textContent='🔌 LLBot 未连接';el.title=st.message||'';return}
    const on=!!(st.data&&st.data.online);
    el.className='pill '+(on?'ok':'off');
    el.textContent=on?('🔌 '+(st.data.nick||'LLBot')+' 在线'):'🔌 LLBot 离线';
    el.title=st.data&&st.data.uin?('QQ '+st.data.uin):'';
  }catch(_){el.className='pill err';el.textContent='🔌 LLBot 错误'}
}

async function llbotHTML(){
  let bridge={ok:false,message:'检测中…'};
  try{bridge=await llbotApi('')}catch(_){bridge={ok:false,message:'桥接服务不可用'}}

  let info={},stats={},dev={},quick=[];
  if(bridge.ok){
    const [a,b,c,d] = await Promise.all([
      llbotApi('login-info').catch(()=>({})),
      llbotApi('dashboard/stats').catch(()=>({})),
      llbotApi('device-info').catch(()=>({})),
      llbotApi('quick-login-list').catch(()=>({})),
    ]);
    info=a.data||{}; stats=b.data||{}; dev=c.data||{}; quick=(d.data&&d.data.LocalLoginInfoList)||[];
  }

  const online=!!info.online;
  const bot=stats.bot||{}, qq=stats.qq||{};
  const base=(window.__KIRIKO__&&window.__KIRIKO__.llbotPublicUrl)||'';

  let h=`<div class="page-head">
    <div>
      <div class="ph-title">🔌 LLBot <span class="em">连接</span></div>
      <div class="ph-sub">登录状态、运行指标与协议配置 · 数据实时取自 LLBot WebUI</div>
    </div>
    <div class="ph-right">
      <button class="btn" onclick="loadPage('llbot')">🔄 刷新</button>
      <button class="btn accent" onclick="loadPage('webqq')">🖥️ 完整 WebUI</button>
    </div>
  </div>`;

  if(!bridge.ok){
    h+=`<div class="panel"><div class="panel-header">⚠️ 无法读取 LLBot</div>
      <div class="panel-body">
        <div class="empty">${esc(bridge.message||'未知错误')}<br><br>
        请确认 llbot 容器在运行，且 <code>llbot_config/webui_token.txt</code> 已挂载到机器人容器
        （<code>docker-compose.yml</code> 中的 <code>./llbot_config:/app/llbot_config:ro</code>）。</div>
      </div></div>`;
    return h;
  }

  h+=`<div class="hero reveal" style="--i:0">
    <div class="av">${online?'🟢':'⚪'}</div>
    <div class="h-main">
      <div class="h-name">${esc(info.nick||'LLBot')}
        <span class="pill ${online?'ok':'err'}"><span class="s-dot" style="background:currentColor"></span>${online?'在线':'离线'}</span>
      </div>
      <div class="h-meta">QQ ${esc(info.uin||'—')} · UID ${esc(info.uid||'—')} · ${esc(dev.devType||'')} ${esc(dev.buildVer||'')}</div>
    </div>
    <div class="h-actions">
      <button class="btn" onclick="llbotShowQR()">📱 扫码登录</button>
      <a class="btn accent" href="${esc(base)}" target="_blank" rel="noopener">↗ 打开官方 WebUI</a>
    </div>
  </div>
  <div id="llbotQrWrap"></div>`;

  h+=`<div class="bento">
    <div class="tile c1 b-3 reveal" style="--i:1"><div class="tico">👥</div><div class="tbody"><div class="num">${stats.friendCount??'—'}</div><div class="lbl">好友数</div></div></div>
    <div class="tile c2 b-3 reveal" style="--i:2"><div class="tico">💬</div><div class="tbody"><div class="num">${stats.groupCount??'—'}</div><div class="lbl">群数</div></div></div>
    <div class="tile c3 b-3 reveal" style="--i:3"><div class="tico">📥</div><div class="tbody"><div class="num">${stats.messageReceived??'—'}</div><div class="lbl">累计接收</div></div></div>
    <div class="tile c4 b-3 reveal" style="--i:4"><div class="tico">📤</div><div class="tbody"><div class="num">${stats.messageSent??'—'}</div><div class="lbl">累计发送</div></div></div>
  </div>`;

  h+=`<div class="panel"><div class="panel-header">📈 运行指标</div><div class="panel-body tight"><div class="kv-list">
    <div class="kv"><span class="k">启动时间</span><span class="v">${fmtTime(stats.startupTime)}（${fmtAgo(stats.startupTime)}）</span></div>
    <div class="kv"><span class="k">最后消息</span><span class="v">${stats.lastMessageTime?fmtAgo(stats.lastMessageTime):'暂无'}</span></div>
    <div class="kv"><span class="k">内存占用</span><span class="v">${(bot.memoryPercent??0).toFixed(2)}% · ${fmtBytes(bot.memory)} / ${fmtBytes(bot.totalMemory)}</span></div>
    <div class="kv"><span class="k">CPU</span><span class="v">${bot.cpu!=null?bot.cpu.toFixed(2)+'%':'—'}</span></div>
    <div class="kv"><span class="k">QQ 内存</span><span class="v">${(qq.memoryPercent??0).toFixed(2)}% · ${fmtBytes(qq.memory)}</span></div>
    <div class="kv"><span class="k">设备 / 版本</span><span class="v">${esc(dev.devType||'—')} · ${esc(dev.buildVer||'—')}</span></div>
  </div></div></div>`;

  if(quick.length){
    h+=`<div class="panel"><div class="panel-header">⚡ 快速登录账号</div><div class="panel-body tight"><table>
      <thead><tr><th>QQ</th><th>昵称</th><th>状态</th></tr></thead><tbody>`;
    quick.forEach(q=>{
      h+=`<tr><td style="font-family:var(--mono)">${esc(q.uin)}</td><td>${esc(q.nickName||'—')}</td>
      <td>${q.isQuickLogin?'<span class="tag ok">可快速登录</span>':'<span class="tag warn">需扫码</span>'}
      ${String(q.uin)===String(info.uin)&&online?'<span class="tag u">当前</span>':''}</td></tr>`;
    });
    h+=`</tbody></table></div></div>`;
  }

  h+=`<div class="panel"><div class="panel-header">📡 LLBot 实时日志
      <span class="ph-right"><button class="btn" onclick="llbotClearLogs()">🗑 清空</button></span></div>
    <div class="panel-body"><div class="logs" id="llbot-logs" style="max-height:420px"><div class="empty">正在连接日志流…</div></div></div></div>`;

  return h;
}

function llbotLogClass(type){
  const t=String(type||'').toLowerCase();
  if(t.includes('err'))return 'E';
  if(t.includes('warn'))return 'W';
  if(t.includes('debug'))return 'D';
  return 'I';
}

function llbotAppendLog(rec){
  const box=$('llbot-logs');
  if(!box)return;
  const ph=box.querySelector('.empty');
  if(ph)ph.remove();
  const div=document.createElement('div');
  div.className='log-entry log-new '+llbotLogClass(rec.type);
  div.innerHTML='<span class="ts"></span><span class="msg"></span>';
  div.querySelector('.ts').textContent=rec.dateTimeStr||'';
  div.querySelector('.msg').textContent=rec.content||'';
  box.appendChild(div);
  while(box.children.length>500)box.removeChild(box.firstChild);
  box.scrollTop=box.scrollHeight;
}

function llbotClearLogs(){const b=$('llbot-logs');if(b)b.innerHTML='<div class="empty">已清空</div>'}

function startLLBotLogs(){
  if(llbotLogES)return;
  try{
    llbotLogES=new EventSource('/llbot-api/logs/stream');
    llbotLogES.onmessage=e=>{
      if(!e.data||e.data==='{}')return;
      try{llbotAppendLog(JSON.parse(e.data))}catch(_){}
    };
    llbotLogES.onerror=()=>{stopLLBotLogs();setTimeout(()=>{if($('llbot-logs'))startLLBotLogs()},4000)};
  }catch(_){}
}

function stopLLBotLogs(){
  if(llbotLogES){try{llbotLogES.close()}catch(_){} llbotLogES=null}
}

async function llbotShowQR(){
  const wrap=$('llbotQrWrap');
  if(!wrap)return;
  wrap.innerHTML='<div class="panel"><div class="panel-body"><div class="empty">正在获取二维码…</div></div></div>';
  try{
    const d=await llbotApi('login-qrcode');
    const b64=d&&d.success&&d.data&&(d.data.pngBase64Qrcode||d.data.qrcode);
    if(!b64){wrap.innerHTML='<div class="panel"><div class="panel-body"><div class="empty">获取二维码失败：'+esc((d&&d.message)||'未知错误')+'</div></div></div>';return}
    const src=b64.startsWith('data:')?b64:('data:image/png;base64,'+b64);
    wrap.innerHTML=`<div class="panel"><div class="panel-header">📱 扫码登录 <span class="ph-right"><button class="btn" onclick="document.getElementById('llbotQrWrap').innerHTML=''">收起</button></span></div>
      <div class="qr-box"><img src="${src}" alt="登录二维码"><div style="margin-top:12px;font-size:.76rem;color:var(--muted)">用手机 QQ 扫码登录（二维码会定期失效，可重新获取）</div></div></div>`;
  }catch(_){
    wrap.innerHTML='<div class="panel"><div class="panel-body"><div class="empty">获取二维码失败</div></div></div>';
  }
}

function bindLLBot(){
  startLLBotLogs();
  refreshLLBotPill();
}

// ── WebQQ embed (LLBot's own WebUI, kept intact) ──
function webqqHTML(){
  const base=(window.__KIRIKO__&&window.__KIRIKO__.llbotPublicUrl)||'';
  return `<div class="page-head">
    <div>
      <div class="ph-title">🖥️ <span class="em">WebQQ</span></div>
      <div class="ph-sub">LLBot 官方 WebUI 完整内嵌 · 收发消息、群成员、通知等全部功能</div>
    </div>
    <div class="ph-right">
      <button class="btn" onclick="var f=document.getElementById('llbotFrame');if(f)f.src=f.src">🔄 重新加载</button>
      <a class="btn accent" href="${esc(base)}" target="_blank" rel="noopener">↗ 新标签打开</a>
    </div>
  </div>
  <div class="embed-shell">
    <div class="embed-bar">
      <span>🔗 ${esc(base)}</span>
      <span class="eb-right"><span class="pill info">首次使用需在此页面登录一次</span></span>
    </div>
    <iframe id="llbotFrame" src="${esc(base)}" title="LLBot WebUI" referrerpolicy="no-referrer"></iframe>
  </div>`;
}

// ══════════════════════════════════════════════════════════
//  Group activity — single-day speech statistics
// ══════════════════════════════════════════════════════════

function _todayStr(){
  const d=new Date();
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

async function _groupOptions(selected){
  if(!_groupsCache.length){
    try{_groupsCache=(await fetch('/api/groups').then(r=>r.json())).groups||[]}catch(_){_groupsCache=[]}
  }
  return _groupsCache.map(g=>
    `<option value="${esc(g.group_id)}"${String(g.group_id)===String(selected)?' selected':''}>${esc(g.group_name||g.group_id)}</option>`
  ).join('');
}

async function activityHTML(){
  const opts=await _groupOptions();
  return `<div class="page-head">
    <div><div class="ph-title">群 <span class="em">活跃</span></div>
      <div class="ph-sub">单日发言统计：谁在说、什么时候最热闹</div></div>
  </div>
  <div class="panel reveal" style="--i:0"><div class="panel-body">
    <div class="toolbar" style="margin:0">
      <label>群</label><select id="actGroup">${opts}</select>
      <label>日期</label><input type="date" id="actDate" value="${_todayStr()}">
      <button class="btn primary" onclick="loadActivity()">查询</button>
    </div>
  </div></div>
  <div id="actBody"><div class="empty"><span class="em-ico">📈</span>选择群和日期后点「查询」</div></div>`;
}

function bindActivity(){ loadActivity(); }

async function loadActivity(){
  const box=$('actBody'); if(!box)return;
  const gid=($('actGroup')||{}).value||'';
  const date=($('actDate')||{}).value||_todayStr();
  if(!gid){box.innerHTML='<div class="empty">还没有任何群</div>';return}
  box.innerHTML='<div class="empty">查询中…</div>';

  let s=null;
  try{
    s=(await fetch(`/api/groups/${encodeURIComponent(gid)}/stats?date=${encodeURIComponent(date)}`).then(r=>r.json())).stats;
  }catch(_){}
  if(!s){box.innerHTML='<div class="empty">查询失败</div>';return}
  if(!s.total){
    box.innerHTML='<div class="empty"><span class="em-ico">🌙</span>'+esc(date)+' 这一天群里没有发言记录</div>';
    return;
  }

  const maxTop=Math.max(1,...s.top.map(t=>t.count));
  const maxHour=Math.max(1,...s.hourly);
  const bars=s.top.map(t=>`<div class="bar"><span class="name">${esc(t.user_name)}</span>
    <div class="track"><div class="fill" style="width:${Math.max(4,(t.count/maxTop)*100)}%">${t.count}</div></div></div>`).join('');

  const hours=s.hourly.map((c,h)=>`<div class="hc" title="${h}:00 — ${c} 条">
    <i style="height:${Math.round((c/maxHour)*100)}%"></i><span>${h}</span></div>`).join('');

  box.innerHTML=`
  <div class="bento" style="margin-top:16px">
    <div class="tile c1 b-4 reveal" style="--i:0"><div class="tico">💬</div><div class="tbody"><div class="num">${s.total}</div><div class="lbl">总消息</div></div></div>
    <div class="tile c2 b-4 reveal" style="--i:1"><div class="tico">👥</div><div class="tbody"><div class="num">${s.active_users}</div><div class="lbl">活跃人数</div></div></div>
    <div class="tile c3 b-4 reveal" style="--i:2"><div class="tico">🖼️</div><div class="tbody"><div class="num">${s.images}</div><div class="lbl">图片 / 表情</div></div></div>
  </div>
  <div class="bento" style="margin-top:16px">
    <div class="panel b-5 reveal" style="--i:3">
      <div class="panel-header"><span class="hicon">🏆</span>发言排行</div>
      <div class="panel-body">${bars||'<div class="empty">暂无数据</div>'}</div>
    </div>
    <div class="panel b-7 reveal" style="--i:4">
      <div class="panel-header"><span class="hicon">🕐</span>时段分布</div>
      <div class="panel-body"><div class="hour-chart">${hours}</div></div>
    </div>
  </div>`;
}

// ══════════════════════════════════════════════════════════
//  Chat review — paginated group transcript
// ══════════════════════════════════════════════════════════

async function historyHTML(){
  const opts=await _groupOptions();
  return `<div class="page-head">
    <div><div class="ph-title">聊天 <span class="em">回看</span></div>
      <div class="ph-sub">按天回看群聊完整记录（含机器人自己说过的话）</div></div>
  </div>
  <div class="panel reveal" style="--i:0"><div class="panel-body">
    <div class="toolbar" style="margin:0">
      <label>群</label><select id="histGroup">${opts}</select>
      <label>日期</label><input type="date" id="histDate" value="${_todayStr()}">
      <input id="histQ" placeholder="搜索关键词…" style="width:150px">
      <input id="histUser" placeholder="按昵称筛选" style="width:130px">
      <button class="btn primary" onclick="loadHistory(1)">查询</button>
      <button class="btn" onclick="resetHistory()">清空筛选</button>
      <button class="btn accent" id="threadToggle" onclick="toggleThreads()">🧩 按话题分组</button>
    </div>
  </div></div>
  <div id="histBody"><div class="empty"><span class="em-ico">🗂️</span>选择群后点「查询」（留空日期 = 不限日期）</div></div>`;
}

function bindHistory(){ loadHistory(1); }

function resetHistory(){
  ['histDate','histQ','histUser'].forEach(id=>{const el=$(id);if(el)el.value=''});
  loadHistory(1);
}

async function loadHistory(page){
  const box=$('histBody'); if(!box)return;
  const gid=($('histGroup')||{}).value||'';
  const date=($('histDate')||{}).value||'';
  const q=($('histQ')||{}).value?.trim()||'';
  const user=($('histUser')||{}).value?.trim()||'';
  if(!gid){box.innerHTML='<div class="empty">还没有任何群</div>';return}

  if(_threadMode){ return loadThreads(gid, date, box); }

  box.innerHTML='<div class="empty">读取中…</div>';

  const params=new URLSearchParams({page:String(page||1),size:'100'});
  if(date)params.set('date',date);
  if(q)params.set('q',q);
  if(user)params.set('user',user);

  let d=null;
  try{d=await fetch(`/api/groups/${encodeURIComponent(gid)}/messages?${params}`).then(r=>r.json())}catch(_){}
  if(!d||!d.ok){box.innerHTML='<div class="empty">读取失败</div>';return}
  if(!d.total){
    box.innerHTML='<div class="empty"><span class="em-ico">🗂️</span>没有符合条件的聊天记录</div>';
    return;
  }

  // Resolve quotes within this page so a reply shows what it answers.
  const seqMap={};
  d.items.forEach(m=>{if(m.message_seq!=null)seqMap[String(m.message_seq)]=m});

  const rows=d.items.map(m=>{
    const stamp=String(m.timestamp||'');
    // Without a date filter the list spans days, so show the date too.
    const t=esc(date?stamp.slice(11,16):stamp.slice(5,16));
    let quote='';
    if(m.reply_to_seq!=null){
      const src=seqMap[String(m.reply_to_seq)];
      const label=src
        ? `${esc(src.user_name)}：${esc(String(src.content||'').slice(0,40))}`
        : `#${m.reply_to_seq}`;
      quote=`<div class="m-quote">↩ ${label}</div>`;
    }
    return `<div class="msg-row${m.is_bot?' bot':''}">
      <div class="m-time">${t}</div>
      <div class="m-who" title="${esc(m.user_name)}">${esc(m.user_name)}</div>
      <div class="m-text">${quote}${esc(m.content)}</div>
    </div>`;
  }).join('');

  box.innerHTML=`
  <div class="panel reveal" style="--i:1">
    <div class="panel-header"><span class="hicon">🗂️</span>聊天记录
      <span class="ph-right"><span class="tag u">共 ${d.total} 条 · 第 ${d.page}/${d.pages} 页</span></span>
    </div>
    <div class="panel-body tight"><div class="msg-list">${rows}</div></div>
    <div class="panel-body" style="border-top:1px solid var(--border);display:flex;gap:8px;align-items:center">
      <button class="btn" ${d.page<=1?'disabled':''} onclick="loadHistory(${d.page-1})">← 上一页</button>
      <button class="btn" ${d.page>=d.pages?'disabled':''} onclick="loadHistory(${d.page+1})">下一页 →</button>
      <span class="ph-sub" style="margin-left:auto">每页 100 条</span>
    </div>
  </div>`;
}

// ══════════════════════════════════════════════════════════
//  AI usage — where the tokens and the seconds go
// ══════════════════════════════════════════════════════════

const AI_SOURCE_LABEL={
  chat:'主对话', followup:'工具追问', judge:'行为判定', learning:'自学习',
  profile:'用户画像', news:'新闻翻译', vision:'图像理解', tarot:'塔罗',
  at_member:'@群友', feature_request:'功能建议', sticker_fallback:'表情包回退',
  quick:'其它后台',
};

function aiUsageHTML(){
  return `<div class="page-head">
    <div><div class="ph-title">AI <span class="em">用量</span></div>
      <div class="ph-sub">调用量、延迟、token 消耗与成本估算 · 成本按官方峰谷价计算</div></div>
    <div class="ph-right">
      <select id="aiHours" onchange="loadAiUsage()">
        <option value="24" selected>最近 24 小时</option>
        <option value="6">最近 6 小时</option>
        <option value="1">最近 1 小时</option>
        <option value="72">最近 3 天</option>
        <option value="168">最近 7 天</option>
      </select>
      <button class="btn primary" onclick="loadAiUsage()">🔄 刷新</button>
    </div>
  </div>
  <div id="aiBody"><div class="empty">读取中…</div></div>`;
}

function bindAiUsage(){ loadAiUsage(); }

async function loadAiUsage(){
  const box=$('aiBody'); if(!box)return;
  const hours=($('aiHours')||{}).value||'24';
  box.innerHTML='<div class="empty">读取中…</div>';

  let d=null;
  try{d=await fetch('/api/ai/metrics?hours='+encodeURIComponent(hours)).then(r=>r.json())}catch(_){}
  if(!d||!d.ok){box.innerHTML='<div class="empty">读取失败</div>';return}

  const t=d.totals, L=d.latency;
  if(!t.calls){
    box.innerHTML='<div class="empty"><span class="em-ico">🤖</span>这段时间还没有 AI 调用记录</div>';
    return;
  }

  const maxHour=Math.max(1,...d.hourly.map(h=>h.calls));
  const hours24=d.hourly.map(h=>`<div class="hc" title="${h.hour}:00 — ${h.calls} 次 / ${h.tokens} token">
    <i style="height:${Math.round((h.calls/maxHour)*100)}%"></i><span>${h.hour}</span></div>`).join('');

  const rows=d.by_source.map(b=>`<tr>
    <td>${esc(AI_SOURCE_LABEL[b.source]||b.source)}</td>
    <td style="font-variant-numeric:tabular-nums">${b.calls}</td>
    <td style="font-variant-numeric:tabular-nums">${b.tokens.toLocaleString()}</td>
    <td style="font-variant-numeric:tabular-nums">$${b.cost_usd.toFixed(4)}</td>
    <td style="font-variant-numeric:tabular-nums">${b.avg_ms} ms</td>
    <td>${b.failed?`<span class="tag err">${b.failed}</span>`:'<span class="tag ok">0</span>'}</td>
  </tr>`).join('');

  const cachePct=t.prompt_tokens?Math.round(t.cache_hit_tokens/t.prompt_tokens*100):0;
  const errBlock=d.recent_errors.length?`
    <div class="panel reveal" style="--i:5;margin-top:16px">
      <div class="panel-header"><span class="hicon">⚠️</span>最近失败（最多 10 条）</div>
      <div class="panel-body tight"><div class="list">
        ${d.recent_errors.map(e=>`<div class="item">
          <div class="iava">⚠️</div>
          <div class="imain"><div class="ititle">${esc(AI_SOURCE_LABEL[e.source]||e.source)}</div>
            <div class="isub">${esc(e.error)}</div></div>
          <div class="imeta"><span class="tag">${esc(e.timestamp)}</span></div>
        </div>`).join('')}
      </div></div>
    </div>`:'';

  box.innerHTML=`
  <div class="bento">
    <div class="tile c1 b-3 reveal" style="--i:0"><div class="tico">📞</div><div class="tbody"><div class="num">${t.calls}</div><div class="lbl">调用次数</div></div></div>
    <div class="tile ${t.success_rate>=99?'c2':'c6'} b-3 reveal" style="--i:1"><div class="tico">✅</div><div class="tbody"><div class="num">${t.success_rate}%</div><div class="lbl">成功率${t.failed?`（失败 ${t.failed}）`:''}</div></div></div>
    <div class="tile c4 b-3 reveal" style="--i:2"><div class="tico">⚡</div><div class="tbody"><div class="num">${L.p95_ms}<span style="font-size:.8rem;font-weight:600"> ms</span></div><div class="lbl">P95 延迟（均值 ${L.avg_ms}）</div></div></div>
    <div class="tile c5 b-3 reveal" style="--i:3"><div class="tico">💵</div><div class="tbody"><div class="num">$${t.cost_usd.toFixed(4)}</div><div class="lbl">成本估算</div></div></div>
  </div>

  <div class="bento" style="margin-top:16px">
    <div class="tile c2 b-3 reveal" style="--i:4"><div class="tico">📥</div><div class="tbody"><div class="num">${t.prompt_tokens.toLocaleString()}</div><div class="lbl">输入 token（缓存命中 ${cachePct}%）</div></div></div>
    <div class="tile c3 b-3 reveal" style="--i:5"><div class="tico">📤</div><div class="tbody"><div class="num">${t.completion_tokens.toLocaleString()}</div><div class="lbl">输出 token</div></div></div>
    <div class="tile c5 b-3 reveal" style="--i:6"><div class="tico">🧠</div><div class="tbody"><div class="num">${t.reasoning_tokens.toLocaleString()}</div><div class="lbl">其中思考 token</div></div></div>
    <div class="tile c4 b-3 reveal" style="--i:7"><div class="tico">🐢</div><div class="tbody"><div class="num">${L.max_ms}<span style="font-size:.8rem;font-weight:600"> ms</span></div><div class="lbl">最慢一次（P50 ${L.p50_ms}）</div></div></div>
  </div>

  <div class="bento" style="margin-top:16px">
    <div class="panel b-12 reveal" style="--i:8">
      <div class="panel-header"><span class="hicon">🕐</span>按小时调用分布</div>
      <div class="panel-body"><div class="hour-chart">${hours24}</div></div>
    </div>
    <div class="panel b-12 reveal" style="--i:9">
      <div class="panel-header"><span class="hicon">📊</span>按来源拆分</div>
      <div class="panel-body tight">
        <table><thead><tr><th>来源</th><th>调用</th><th>Token</th><th>成本</th><th>平均延迟</th><th>失败</th></tr></thead>
        <tbody>${rows}</tbody></table>
      </div>
    </div>
  </div>
  ${errBlock}`;
}

// ══════════════════════════════════════════════════════════
//  Topic threads (群聊话题聚类)
// ══════════════════════════════════════════════════════════

let _threadMode=false;

function toggleThreads(){
  _threadMode=!_threadMode;
  const btn=$('threadToggle');
  if(btn)btn.classList.toggle('on',_threadMode);
  loadHistory(1);
}

async function loadThreads(gid, date, box){
  box.innerHTML='<div class="empty">聚类中…</div>';
  const params=new URLSearchParams();
  if(date)params.set('date',date);

  let d=null;
  try{d=await fetch(`/api/groups/${encodeURIComponent(gid)}/threads?${params}`).then(r=>r.json())}catch(_){}
  if(!d||!d.ok){box.innerHTML='<div class="empty">读取失败</div>';return}
  const threads=d.threads||[];
  if(!threads.length){
    box.innerHTML='<div class="empty"><span class="em-ico">🧩</span>没有可聚类的内容</div>';
    return;
  }

  const blocks=threads.map(t=>`<div class="thread reveal" style="--i:${Math.min(t.index,12)}">
    <div class="thread-head">
      <span class="tag a">话题 ${t.index}</span>
      <b>${esc(t.title)}</b>
      <span class="ph-sub">${esc(String(t.start||'').slice(5,16))} → ${esc(String(t.end||'').slice(11,16))}</span>
      <span class="ph-right"><span class="tag u">${t.size} 条</span><span class="tag">${t.participants.length} 人</span></span>
    </div>
    <div class="thread-body">${t.messages.map(m=>`<div class="msg-row${m.is_bot?' bot':''}">
      <div class="m-time">${esc(String(m.timestamp||'').slice(11,16))}</div>
      <div class="m-who" title="${esc(m.user_name)}">${esc(m.user_name)}</div>
      <div class="m-text">${esc(m.content)}</div>
    </div>`).join('')}</div>
  </div>`).join('');

  box.innerHTML=`<div class="panel reveal" style="--i:0">
    <div class="panel-header"><span class="hicon">🧩</span>按话题分组
      <span class="ph-right"><span class="tag u">共 ${threads.length} 个话题</span></span></div>
    <div class="panel-body tight">${blocks}</div>
  </div>`;
}

// ══════════════════════════════════════════════════════════
//  Push subscriptions (群订阅推送)
// ══════════════════════════════════════════════════════════

const PUSH_TOPICS={
  morning_news:'☀️ 早间新闻',
  gaming_news:'🎮 游戏速递',
  hitokoto:'💬 每日一言',
  daily_roll_call:'📣 今日发言榜',
  amp_head:'🎸 今日箱头',
};
const PUSH_HINT={
  morning_news:'时政要闻 + 游戏资讯 + 每日一言',
  gaming_news:'只推游戏圈热点',
  hitokoto:'一句随机的语录',
  daily_roll_call:'@ 出当天发言最多的三个人',
  amp_head:'每天介绍一款吉他音箱头：年代、音色、市价、选购建议',
};
// Topics that were requested at a specific hour start there instead of 07:00.
const PUSH_DEFAULT_TIME={
  amp_head:'08:00',
};

async function pushHTML(){
  const opts=await _groupOptions();
  return `<div class="page-head">
    <div><div class="ph-title">群 <span class="em">推送</span></div>
      <div class="ph-sub">按群订阅定时推送 · 每天到点自动发，错过超过 2 小时则跳过</div></div>
  </div>
  <div class="panel reveal" style="--i:0"><div class="panel-body">
    <div class="toolbar" style="margin:0">
      <label>群</label><select id="pushGroup" onchange="loadPush()">${opts}</select>
      <span class="ph-sub">修改后即时保存</span>
    </div>
  </div></div>
  <div id="pushBody"><div class="empty">选择一个群</div></div>`;
}

function bindPush(){ loadPush(); }

async function loadPush(){
  const box=$('pushBody'); if(!box)return;
  const gid=($('pushGroup')||{}).value||'';
  if(!gid){box.innerHTML='<div class="empty">还没有任何群</div>';return}
  box.innerHTML='<div class="empty">读取中…</div>';

  let subs=[];
  try{subs=(await fetch('/api/subscriptions?group_id='+encodeURIComponent(gid)).then(r=>r.json())).subscriptions||[]}catch(_){}
  const byTopic={};
  subs.forEach(s=>{byTopic[s.topic]=s});

  const rows=Object.keys(PUSH_TOPICS).map(t=>{
    const cur=byTopic[t]||{enabled:false,push_time:PUSH_DEFAULT_TIME[t]||'07:00'};
    return `<div class="set-row">
      <div class="sr-info">
        <div class="sr-label">${esc(PUSH_TOPICS[t])}</div>
        <div class="sr-desc">${esc(PUSH_HINT[t]||'')}</div>
      </div>
      <input type="time" value="${esc(cur.push_time||PUSH_DEFAULT_TIME[t]||'07:00')}" data-push-time="${esc(t)}"
             style="width:120px" onchange="savePush('${esc(t)}',this.value,null)">
      <label class="switch"><input type="checkbox" data-push-on="${esc(t)}" ${cur.enabled?'checked':''}
             onchange="savePush('${esc(t)}',null,this.checked)"><span class="slider"></span></label>
    </div>`;
  }).join('');

  box.innerHTML=`<div class="panel reveal" style="--i:1">
    <div class="panel-header"><span class="hicon">🔔</span>订阅项
      <span class="ph-right"><span class="tag u">每天 ${subs.filter(s=>s.enabled).length} 项已开</span></span></div>
    <div class="panel-body tight">${rows}</div>
    <div class="set-note">推送受「群设置 → 群推送订阅」总开关控制；群内关掉后这里即使打开也不会发。</div>
  </div>`;
}

async function savePush(topic, time, enabled){
  const gid=($('pushGroup')||{}).value||'';
  if(!gid)return;
  const body={group_id:gid, topic};
  if(time!=null) body.time=time;
  if(enabled!=null) body.enabled=enabled;
  if(time==null){
    const el=document.querySelector(`[data-push-time="${topic}"]`);
    if(el) body.time=el.value;
  }
  if(enabled==null){
    const el=document.querySelector(`[data-push-on="${topic}"]`);
    if(el) body.enabled=el.checked;
  }
  try{
    const r=await fetch('/api/subscriptions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
    if(r.ok) toast('已保存'); else toast('保存失败: '+(r.error||''),'err');
  }catch(_){toast('请求失败','err')}
}

// ── Init ──
startLogSSE();
loadPage('overview');
setInterval(refreshLLBotPill,30000);
