/* 「易销」销售线索智能分析与分发平台 —— Vue 3 SPA (全内联模板) */
(function () {
  "use strict";
  var V = window.Vue;
  if (!V) { document.getElementById("app").innerHTML = '<p style="padding:60px;color:red">Vue 3 未加载</p>'; return; }

  /* ---- 共享状态 ---- */
  var store = V.reactive({
    booted: false, health: false, route: "dashboard",
    customers: [], sales: [], summary: null, memories: [], lastRun: null,
    loadingCustomers: false, loadingSales: false, loadingSummary: false,
    runningPipeline: false, expandedCustomer: null, profileCache: {},
    loadingProfile: "", reassignTarget: null, corrections: {},
    currentSales: null, salesMode: false
  });

  /* ---- Toast ---- */
  var toasts = V.reactive([]);
  function toast(msg, type) {
    var id = Date.now() + Math.random().toString(36).slice(2,6);
    toasts.push({id:id, message:msg, type:type||"info"});
    setTimeout(function(){ var i=toasts.findIndex(function(t){return t.id===id}); if(i>=0)toasts.splice(i,1); }, 3600);
  }

  /* ---- API ---- */
  function api(url, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    if (opts.body && !opts.headers["Content-Type"]) opts.headers["Content-Type"] = "application/json";
    return fetch(url, opts).then(function(r) {
      return r.json().catch(function(){return{};}).then(function(d) {
        if (!r.ok) throw new Error(d && d.detail ? d.detail : "HTTP " + r.status);
        return d;
      });
    });
  }
  function pct(n,m){ return m?Math.round(n/m*100):0; }
  function timeOf(t){ return String(t==null?"":t).replace("T"," ").slice(0,19); }

  /* ---- SVG 图标 (stroke 风格, currentColor) ---- */
  var SVG_OPEN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">';
  var ICONS = {
    dashboard: SVG_OPEN + '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>',
    customers: SVG_OPEN + '<path d="M17 21v-2a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v2"/><circle cx="10" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
    assignments: SVG_OPEN + '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.2" fill="currentColor"/></svg>',
    memories: SVG_OPEN + '<path d="M9 18h6"/><path d="M10 22h4"/><path d="M12 2a7 7 0 0 0-4 12.7c.6.5 1 1.4 1 2.3h6c0-.9.4-1.8 1-2.3A7 7 0 0 0 12 2z"/></svg>',
    sources: SVG_OPEN + '<path d="M9 7V3"/><path d="M15 7V3"/><path d="M12 21v-5"/><path d="M7 7h10v4a5 5 0 0 1-10 0V7z"/></svg>',
    team: SVG_OPEN + '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
    userPlus: SVG_OPEN + '<path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8.5" cy="7" r="4"/><line x1="20" y1="8" x2="20" y2="14"/><line x1="23" y1="11" x2="17" y2="11"/></svg>',
    trash: SVG_OPEN + '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
    refresh: SVG_OPEN + '<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>',
    play: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 4.8v14.4c0 .9 1 1.5 1.8 1L20 13a1.2 1.2 0 0 0 0-2L8.8 3.8A1.2 1.2 0 0 0 7 4.8z"/></svg>',
    flame: SVG_OPEN + '<path d="M12 22c4.4 0 7.5-3 7.5-7.2 0-3.6-2.4-6.6-4.5-8.8-.4 1.3-1.1 2.4-2 3.2C12.5 7 11.5 4.6 11 2c-3.9 2.6-6.5 7-6.5 12.8C4.5 19 7.6 22 12 22z"/></svg>',
    alert: SVG_OPEN + '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    check: SVG_OPEN + '<circle cx="12" cy="12" r="9"/><path d="m8.5 12.3 2.4 2.4 4.6-5"/></svg>',
    search: SVG_OPEN + '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>',
    sparkles: SVG_OPEN + '<path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3L12 3z"/></svg>',
    sync: SVG_OPEN + '<path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/></svg>'
  };

  /* ---- 数据加载 ---- */
  function loadCustomers() {
    store.loadingCustomers = true;
    return api("/customers").then(function(l){store.customers=l||[];})
      .catch(function(e){toast("加载客户失败:"+e.message,"error");store.customers=[];})
      .finally(function(){store.loadingCustomers=false;});
  }
  function loadSales() {
    store.loadingSales = true;
    return api("/sales").then(function(l){store.sales=l||[];})
      .catch(function(e){toast("加载销售失败:"+e.message,"error");store.sales=[];})
      .finally(function(){store.loadingSales=false;});
  }
  function loadSummary() {
    store.loadingSummary = true;
    return api("/pipeline/summary").then(function(s){store.summary=s;})
      .catch(function(e){toast("加载摘要失败:"+e.message,"error");})
      .finally(function(){store.loadingSummary=false;});
  }
  function loadMemories() {
    return api("/memories").then(function(l){
      store.memories=l||[]; store.corrections={};
      (store.memories||[]).forEach(function(m){if(m.customer_id&&m.correct_sales_id)store.corrections[m.customer_id]=m.correct_sales_id;});
    }).catch(function(e){toast("加载记忆失败:"+e.message,"error");store.memories=[];});
  }
  function loadProfile(cid) {
    if(store.profileCache[cid]){store.expandedCustomer=cid;return Promise.resolve();}
    store.loadingProfile=cid;
    return api("/history/"+encodeURIComponent(cid)).then(function(d){
      store.profileCache[cid]=d;store.expandedCustomer=cid;
    }).catch(function(e){toast("加载画像失败:"+e.message,"error");})
      .finally(function(){store.loadingProfile="";});
  }
  /* 最近一次分配清单持久化(localStorage), 刷新后恢复 */
  function saveAssignments(assignments) {
    try { localStorage.setItem("yx_last_assignments", JSON.stringify(assignments||[])); } catch(e){}
  }
  function restoreAssignments() {
    try {
      var raw = localStorage.getItem("yx_last_assignments");
      if (raw) store.lastRun = { assignments: JSON.parse(raw) };
    } catch(e){}
  }
  function runPipeline() {
    if(store.runningPipeline)return;
    store.runningPipeline=true;
    toast("正在运行今日流水线...","info");
    return api("/pipeline/run",{method:"POST"}).then(function(d){
      store.lastRun=d;
      saveAssignments(d.assignments);
      store.summary={ran:true,last_run:new Date().toLocaleString("zh-CN"),records:d.saved_records||0,
        intention_stats:d.intention_stats,churn_stats:d.churn_stats,hint:d.push_hint||""};
      toast("流水线完成: "+(d.assignment_count||0)+" 条分配","success");
      return loadMemories();
    }).catch(function(e){toast("流水线失败:"+e.message,"error");throw e;})
      .finally(function(){store.runningPipeline=false;});
  }
  function submitReassignment(cid,sid,note) {
    return api("/feedback",{method:"POST",body:JSON.stringify({customer_id:cid,correct_sales_id:sid,note:note||"易销平台改派"})})
      .then(function(e){toast("改派成功: "+cid+" → "+sid,"success");store.corrections[cid]=sid;return loadMemories();});
  }
  function addSalesMember(data) {
    return api("/sales", {method:"POST", body:JSON.stringify(data)})
      .then(function(s){toast("成功添加销售: "+s.name,"success"); return loadSales();});
  }
  function removeSalesMember(sid, name) {
    return api("/sales/"+encodeURIComponent(sid), {method:"DELETE"})
      .then(function(){toast("已移除销售: "+name,"success"); return loadSales();});
  }

  /* ---- 模板 ---- */
  var T_ROOT = '<div>'
    + '<div v-if="!booted" class="boot-screen"><div class="boot-logo">易</div><div class="spinner"></div><p>易销平台加载中…</p></div>'
    + '<div v-else class="yx-layout">'
    + '<aside class="yx-sidebar">'
    + '<div class="yx-brand"><div class="yx-logo">易</div><div class="yx-brand-t"><div class="yx-brand-name">易销</div><div class="yx-brand-sub">销售线索智能分发</div></div></div>'
    + '<div v-if="salesMode&&currentSales" style="margin:12px 14px;padding:12px;background:rgba(255,255,255,0.06);border-radius:12px;border:1px solid rgba(255,255,255,0.1);display:flex;align-items:center;gap:10px">'
    + '<div style="width:34px;height:34px;border-radius:10px;background:#3b82f6;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:15px" v-text="(currentSales.name||\'销\').slice(0,1)"></div>'
    + '<div><div style="font-weight:700;font-size:13.5px;color:#f8fafc" v-text="currentSales.name"></div><div style="font-size:11px;color:#94a3b8" v-text="currentSales.sales_id+\' · 销售顾问\'"></div></div>'
    + '</div>'
    + '<div class="yx-nav-label" v-text="salesMode?\'销售专属工作台\':\'主导航\'"></div>'
    + '<nav class="yx-nav">'
    + '<template v-if="!salesMode">'
    + '<a :class="navClass(\'dashboard\')" href="#/dashboard"><span class="nav-ico" v-html="icons.dashboard"></span><span>工作台</span></a>'
    + '<a :class="navClass(\'customers\')" href="#/customers"><span class="nav-ico" v-html="icons.customers"></span><span>客户画像</span></a>'
    + '<a :class="navClass(\'assignments\')" href="#/assignments"><span class="nav-ico" v-html="icons.assignments"></span><span>智能分配</span></a>'
    + '<a :class="navClass(\'team\')" href="#/team"><span class="nav-ico" v-html="icons.team"></span><span>销售团队</span></a>'
    + '<a :class="navClass(\'memories\')" href="#/memories"><span class="nav-ico" v-html="icons.memories"></span><span>记忆中心</span></a>'
    + '<a :class="navClass(\'sources\')" href="#/sources"><span class="nav-ico" v-html="icons.sources"></span><span>数据接入</span></a>'
    + '</template>'
    + '<template v-else>'
    + '<a :class="navClass(\'customers\')" href="#/customers"><span class="nav-ico" v-html="icons.customers"></span><span>我的客户画像</span></a>'
    + '</template>'
    + '</nav>'
    + '<div class="yx-sidebar-foot"><div class="health-chip"><span class="yx-health-dot" :class="health?\'ok\':\'err\'"></span><span v-text="health?\'后端在线\':\'后端离线\'"></span></div><span class="foot-ver">v1.0</span></div>'
    + '</aside>'
    + '<div class="yx-main">'
    + '<header class="yx-topbar"><div class="yx-topbar-title" v-text="salesMode?(currentSales?currentSales.name+\' · 客户画像\':\'客户画像\'):pageTitle"></div>'
    + '<div class="yx-topbar-actions">'
    + '<button class="btn btn-ghost" @click="refresh()"><span class="btn-ico" v-html="icons.refresh"></span>刷新</button>'
    + '<button v-if="!salesMode" class="btn btn-primary" @click="run()" :disabled="runningPipeline"><span class="btn-ico" v-html="icons.play"></span><span v-text="runningPipeline?\'运行中…\':\'运行今日流水线\'"></span></button>'
    + '</div></header>'
    + '<div v-if="runningPipeline" class="pipeline-scan-bar"><div class="pipeline-scan-beam"></div></div>'
    + '<main class="yx-content">'
    + '<view-dashboard v-if="route===\'dashboard\'"></view-dashboard>'
    + '<view-customers v-if="route===\'customers\'"></view-customers>'
    + '<view-assignments v-if="route===\'assignments\'"></view-assignments>'
    + '<view-team v-if="route===\'team\'"></view-team>'
    + '<view-memories v-if="route===\'memories\'"></view-memories>'
    + '<view-sources v-if="route===\'sources\'"></view-sources>'
    + '</main></div>'
    + '<div class="toast-container"><toast-stack></toast-stack></div>'
    + '</div></div>';

  var T_DASH = '<div class="view-anim">'
    + '<div class="hero">'
    + '<div><div class="hero-title" v-text="greeting+\'，欢迎回来\'"></div>'
    + '<div class="hero-sub" v-text="today+\' · 易销智能分发平台\'"></div></div>'
    + '<div class="hero-right">'
    + '<span class="pill" :class="s&&s.ran?\'pill-ok\':\'pill-warn\'" v-text="s&&s.ran?\'今日流水线已运行\':\'今日流水线未运行\'"></span>'
    + '<span class="hero-time" v-if="s&&s.last_run" v-text="\'最近运行 \'+timeOf(s.last_run)"></span>'
    + '</div></div>'
    + '<div class="stat-grid">'
    + '<div class="stat-card"><div class="stat-ico ico-indigo" v-html="ico.customers"></div><div class="stat-body"><div class="stat-num" v-text="s?s.records||0:0"></div><div class="stat-label">历史画像</div><div class="stat-hint">累计分析客户数</div></div></div>'
    + '<div class="stat-card"><div class="stat-ico ico-orange" v-html="ico.flame"></div><div class="stat-body"><div class="stat-num" v-text="s&&s.intention_stats?s.intention_stats[\'高\']||0:\'–\'"></div><div class="stat-label">高意向客户</div><div class="stat-hint">建议 24h 内跟进</div></div></div>'
    + '<div class="stat-card"><div class="stat-ico ico-red" v-html="ico.alert"></div><div class="stat-body"><div class="stat-num" v-text="s&&s.churn_stats?s.churn_stats[\'高\']||0:\'–\'"></div><div class="stat-label">高流失风险</div><div class="stat-hint">需要挽回动作</div></div></div>'
    + '<div class="stat-card"><div class="stat-ico ico-green" v-html="ico.check"></div><div class="stat-body"><div class="stat-num" v-text="assignmentCount"></div><div class="stat-label">分配条数</div><div class="stat-hint">本次流水线产出</div></div></div>'
    + '</div>'
    + '<div class="two-col">'
    + '<section class="card"><div class="card-head"><h3>意向分层</h3><span class="card-sub">按意向等级分布</span></div>'
    + '<div class="bar-chart" v-if="s&&s.intention_stats">'
    + '<div class="bar-row" v-for="lvl in [\'高\',\'中\',\'低\']"><span class="bar-chip" :class="\'chip-int-\'+lvl" v-text="lvl"></span>'
    + '<div class="bar-track"><div class="bar-fill" :style="{width:pct(s.intention_stats[lvl]||0,intentMax)+\'%\'}" :class="\'bar-int-\'+lvl"></div></div>'
    + '<span class="bar-val" v-text="s.intention_stats[lvl]||0"></span></div></div>'
    + '<div class="empty-hint" v-else>暂无数据 — 运行流水线后生成</div></section>'
    + '<section class="card"><div class="card-head"><h3>流失风险</h3><span class="card-sub">按风险等级分布</span></div>'
    + '<div class="bar-chart" v-if="s&&s.churn_stats">'
    + '<div class="bar-row" v-for="lvl in [\'高\',\'中\',\'低\']"><span class="bar-chip" :class="\'chip-churn-\'+lvl" v-text="lvl"></span>'
    + '<div class="bar-track"><div class="bar-fill" :style="{width:pct(s.churn_stats[lvl]||0,churnMax)+\'%\'}" :class="\'bar-churn-\'+lvl"></div></div>'
    + '<span class="bar-val" v-text="s.churn_stats[lvl]||0"></span></div></div>'
    + '<div class="empty-hint" v-else>暂无数据 — 运行流水线后生成</div></section>'
    + '</div></div>';

  var T_CUSTOMERS = '<div class="view-anim"><section class="card">'
    + '<div class="card-head"><h3>客户列表</h3>'
    + '<span class="card-sub" v-text="\'共 \'+filtered.length+\' 家 · 点击行展开画像\'+(salesMode?\'（当前专属销售视角）\':\'\')"></span></div>'
    + '<div v-if="salesMode&&currentSales" style="margin:0 0 14px;padding:10px 14px;background:#eff6ff;border-radius:10px;border:1px solid #bfdbfe;display:flex;align-items:center;justify-content:space-between;font-size:13px;color:#1e40af">'
    + '<div><strong>👤 {{ currentSales.name }} ({{ currentSales.sales_id }}) 的专属客户池</strong> · 系统已为您自动过滤归属名下的跟进客户</div>'
    + '<div style="display:flex;gap:6px">'
    + '<button class="chip" :class="onlyMine?\'chip-on\':\'\'" @click="onlyMine=true">仅看我的 ({{ myCount }})</button>'
    + '<button class="chip" :class="!onlyMine?\'chip-on\':\'\'" @click="onlyMine=false">查看全部公海 ({{ allCount }})</button>'
    + '</div></div>'
    + '<div class="filter-bar">'
    + '<span class="filter-label">意向</span>'
    + '<button class="chip" :class="fInt===\'\'?\'chip-on\':\'\'" @click="fInt=\'\'">全部</button>'
    + '<button class="chip" :class="fInt===\'高\'?\'chip-on\':\'\'" @click="fInt=\'高\'">高意向</button>'
    + '<button class="chip" :class="fInt===\'中\'?\'chip-on\':\'\'" @click="fInt=\'中\'">中意向</button>'
    + '<button class="chip" :class="fInt===\'低\'?\'chip-on\':\'\'" @click="fInt=\'低\'">低意向</button>'
    + '<span class="filter-sep"></span>'
    + '<span class="filter-label">流失</span>'
    + '<button class="chip" :class="fChurn===\'\'?\'chip-on\':\'\'" @click="fChurn=\'\'">全部</button>'
    + '<button class="chip" :class="fChurn===\'高\'?\'chip-on\':\'\'" @click="fChurn=\'高\'">高风险</button>'
    + '<button class="chip" :class="fChurn===\'中\'?\'chip-on\':\'\'" @click="fChurn=\'中\'">中风险</button>'
    + '<button class="chip" :class="fChurn===\'低\'?\'chip-on\':\'\'" @click="fChurn=\'低\'">低风险</button>'
    + '</div>'
    + '<div class="empty-hint" v-if="!loading&&list.length===0">暂无客户数据</div>'
    + '<div class="empty-hint" v-else-if="!loading&&filtered.length===0">无匹配筛选的客户</div>'
    + '<table class="yx-table" v-if="filtered.length>0"><thead><tr><th>客户</th><th>行业</th><th>城市</th><th>规模</th><th>归属</th><th>意向</th><th>流失</th><th class="row-action"></th></tr></thead><tbody>'
    + '<template v-for="c in filtered" :key="c.customer_id">'
    + '<tr :class="[\'clickable\',expanded===c.customer_id?\'row-open\':\'\']" @click="toggle(c)">'
    + '<td><div><strong v-text="c.customer_name"></strong><div class="sub" v-text="c.customer_id"></div></div></td>'
    + '<td v-text="c.industry"></td><td v-text="c.city"></td><td v-text="c.scale"></td>'
    + '<td v-text="c.owner_sales_id||\'未分配\'"></td>'
    + '<td><span v-if="c.intention_level" class="badge" :class="\'badge-int-\'+c.intention_level" v-text="c.intention_level"></span><span v-else class="muted">—</span></td>'
    + '<td><span v-if="c.churn_risk" class="badge" :class="\'badge-churn-\'+c.churn_risk" v-text="c.churn_risk"></span><span v-else class="muted">—</span></td>'
    + '<td class="row-action"><span class="row-caret" :class="expanded===c.customer_id?\'open\':\'\'"></span></td></tr>'
    + '<tr v-if="expanded===c.customer_id" class="profile-row"><td colspan="8">'
    + '<div v-if="profLoading" class="loading-hint">加载中…</div>'
    + '<div v-else-if="prof" class="profile-box">'
    + '<div class="profile-head">'
    + '<span class="badge" :class="\'badge-int-\'+profLevel" v-text="\'意向 · \'+profLevel"></span>'
    + '<span class="badge" :class="\'badge-churn-\'+profChurn" v-text="\'流失 · \'+profChurn"></span>'
    + '</div>'
    + '<div class="profile-sec" v-if="prof.result">'
    + '<div class="profile-sec-title">核心诉求</div><ul><li v-for="d in (prof.result.core_demands||[])" :key="d" v-text="d"></li></ul>'
    + '<div class="profile-sec-title">跟进建议</div><p v-text="prof.result.follow_up_suggestion"></p>'
    + '<div class="profile-sec-title">分析理由</div><p class="muted" v-text="prof.result.intention_reason"></p>'
    + '</div></div>'
    + '<div v-else class="empty-hint">暂无画像快照</div>'
    + '</td></tr></template></tbody></table></section></div>';

  var T_ASSIGN = '<div class="view-anim"><div class="toolbar">'
    + '<div class="search-wrap"><span class="search-ico" v-html="ico.search"></span><input class="search-input" v-model="q" placeholder="搜索客户 / 销售 ID…"></div>'
    + '<span class="toolbar-meta" v-if="hasData" v-text="\'共 \'+filtered.length+\' 条分配\'"></span></div>'
    + '<section class="card">'
    + '<div class="empty-hint" v-if="!hasData">暂无分配 — 点击右上角「运行今日流水线」生成推荐</div>'
    + '<table class="yx-table" v-if="hasData"><thead><tr><th>客户</th><th>意向</th><th>推荐销售</th><th>匹配理由</th><th>负载</th><th class="row-action">操作</th></tr></thead><tbody>'
    + '<tr v-for="a in filtered" :key="a.customer_id" :class="corr(a.customer_id)?\'row-corrected\':\'\'">'
    + '<td><div><strong v-text="a.customer_name||a.customer_id"></strong><div class="sub" v-text="a.customer_id"></div></div></td>'
    + '<td><span class="badge" :class="\'badge-int-\'+(a.intention_level||\'\')" v-text="a.intention_level||\'—\'"></span></td>'
    + '<td><strong v-text="a.sales_name||a.sales_id"></strong>'
    + '<div v-if="corr(a.customer_id)" class="correction-tag" v-text="\'→ 已改派 \'+corr(a.customer_id)"></div></td>'
    + '<td class="reason-cell" v-text="a.match_reason||\'—\'"></td>'
    + '<td v-text="loadOf(a)+\' 单\'"></td>'
    + '<td class="row-action"><button class="btn btn-small" @click="openRe(a)">改派</button></td></tr>'
    + '</tbody></table></section>'
    + '<div class="modal-mask" v-if="rt" @click.self="rt=null">'
    + '<div class="modal"><h3>人工复核 · 改派</h3>'
    + '<p>客户 <strong v-text="rt.customer_name"></strong> 当前推荐 <strong v-text="rt.current_sales_name"></strong></p>'
    + '<p class="muted">改派后将升级为强记忆, 影响后续分单。</p>'
    + '<div class="modal-body"><label>改派给(销售 ID)</label>'
    + '<input v-model="ri" class="search-input full" placeholder="如 S002">'
    + '<div class="sales-quick"><button v-for="s in salesList" :key="s.sales_id" class="chip" '
    + ':class="s.sales_id===ri?\'chip-on\':\'\'" @click="ri=s.sales_id" v-text="s.name+\' \'+s.sales_id"></button></div></div>'
    + '<div class="modal-actions"><button class="btn btn-ghost" @click="rt=null">取消</button>'
    + '<button class="btn btn-primary" @click="confirm()">确认改派</button></div></div></div></div>';

  var T_TEAM = '<div class="view-anim">'
    + '<div class="toolbar">'
    + '<div class="search-wrap"><span class="search-ico" v-html="ico.search"></span><input class="search-input" v-model="q" placeholder="搜索销售姓名 / 工号 / 城市…"></div>'
    + '<div style="flex:1"></div>'
    + '<button class="btn btn-ghost" @click="syncAll()" :disabled="syncingAll"><span class="btn-ico" v-html="ico.sync"></span><span v-text="syncingAll?\'正在扫描CRM商机…\':\'全员AI成单扫描\'"></span></button>'
    + '<button class="btn btn-primary" @click="openAddModal()"><span class="btn-ico" v-html="ico.userPlus"></span>添加销售员工</button>'
    + '</div>'
    + '<section class="card">'
    + '<div class="card-head"><h3>销售团队成员</h3><span class="card-sub" v-text="\'共 \'+filtered.length+\' 名成员 · 支持 CRM 成交商机 AI 深度画像提炼与自动反哺打标\'"></span></div>'
    + '<div class="empty-hint" v-if="filtered.length===0">暂无匹配的销售人员</div>'
    + '<table class="yx-table" v-if="filtered.length>0"><thead><tr><th>销售成员</th><th>AI提炼擅长行业</th><th>负责城市</th><th>当前负载</th><th>飞书绑定(手机号/open_id)</th><th class="row-action">操作</th></tr></thead><tbody>'
    + '<tr v-for="s in filtered" :key="s.sales_id">'
    + '<td><div><strong v-text="s.name"></strong><div class="sub" v-text="s.sales_id"></div></div></td>'
    + '<td><div class="chip-group"><span v-for="ind in (s.good_at_industries||[])" :key="ind" class="badge badge-int-高" style="background:#eef2ff;color:#4338ca;border:1px solid #c7d2fe" v-text="ind"></span><span v-if="!(s.good_at_industries&&s.good_at_industries.length)" class="muted">待AI扫描打标</span></div></td>'
    + '<td><div class="chip-group"><span v-for="city in (s.responsible_cities||[])" :key="city" class="badge" style="background:#f1f5f9;color:#475569" v-text="city"></span><span v-if="!(s.responsible_cities&&s.responsible_cities.length)" class="muted">全国通用</span></div></td>'
    + '<td><div style="display:flex;align-items:center;gap:8px"><div class="bar-track" style="width:80px;height:10px"><div class="bar-fill" :style="{width:Math.min(100, s.current_load*20)+\'%\', background:s.current_load>=5?\'#ef4444\':\'#4f46e5\'}"></div></div><strong v-text="s.current_load+\' 单\'"></strong></div></td>'
    + '<td><div style="display:flex;flex-direction:column;gap:3px;align-items:flex-start">'
    + '<span v-if="s.mobile" style="font-family:monospace;font-size:13px;font-weight:600;color:#047857" v-text="s.mobile"></span><span v-else class="muted" style="font-size:12px">未绑手机号</span>'
    + '<span v-if="s.open_id" style="font-family:monospace;font-size:11px;color:#4338ca;max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" :title="s.open_id">open_id: {{ (s.open_id||"").slice(0,18) }}…</span>'
    + '<span v-else style="font-size:11px;color:#b45309;background:#fef3c7;padding:1px 8px;border-radius:10px">未绑 open_id，无法用飞书看客户</span>'
    + '</div></td>'
    + '<td class="row-action">'
    + '<div style="display:flex;gap:6px;justify-content:center">'
    + '<button v-if="s.sales_id!==\'admin\'" class="btn btn-small" style="background:#eef2ff;color:#4338ca;font-weight:700" @click="bindOpenId(s)"><span class="btn-ico" v-html="ico.userPlus"></span>绑open_id</button>'
    + '<button v-if="s.sales_id!==\'admin\'" class="btn btn-small" style="background:linear-gradient(135deg,#e0e7ff,#fae8ff);color:#4338ca;font-weight:700" @click="viewProfile(s)"><span class="btn-ico" v-html="ico.sparkles"></span>AI画像</button>'
    + '<button v-if="s.sales_id!==\'admin\'" class="btn btn-small" style="background:#fee2e2;color:#ef4444" @click="del(s)"><span class="btn-ico" v-html="ico.trash"></span>删除</button>'
    + '<span v-else class="muted" style="font-size:12px">系统保留</span>'
    + '</div></td></tr>'
    + '</tbody></table></section>'
    + '<div class="modal-mask" v-if="showModal" @click.self="showModal=false">'
    + '<div class="modal"><h3>添加销售团队成员</h3>'
    + '<div class="modal-body">'
    + '<div style="margin-bottom:12px"><label>销售工号 (ID)</label><input v-model="form.sales_id" class="search-input full" placeholder="例如: S005"></div>'
    + '<div style="margin-bottom:12px"><label>姓名</label><input v-model="form.name" class="search-input full" placeholder="例如: 陈晨"></div>'
    + '<div style="margin-bottom:12px"><label>擅长行业 (可留空，支持保存后点击AI画像自动提炼)</label><input v-model="form.industries_str" class="search-input full" placeholder="例如: 智能制造, 软件服务"></div>'
    + '<div style="margin-bottom:12px"><label>负责城市 (逗号分隔)</label><input v-model="form.cities_str" class="search-input full" placeholder="例如: 杭州, 上海, 苏州"></div>'
    + '<div style="margin-bottom:12px"><label>飞书接收通知手机号</label><input v-model="form.mobile" class="search-input full" placeholder="例如: 15990070647"></div>'
    + '<div style="margin-bottom:12px"><label>飞书 open_id (选填，用于销售从飞书进入易销看自己的客户；可从飞书网页应用URL?open_id=后复制)</label><input v-model="form.open_id" class="search-input full" placeholder="例如: ou_5a3f22e10391fa12d541c1c033f29dd5"></div>'
    + '</div>'
    + '<div class="modal-actions"><button class="btn btn-ghost" @click="showModal=false">取消</button>'
    + '<button class="btn btn-primary" @click="submitAdd()">确认保存</button></div></div></div>'
    + '<div class="modal-mask" v-if="bindModal" @click.self="bindModal=null">'
    + '<div class="modal"><h3>绑定飞书 open_id —— <span v-text="(bindSales.name||\'\')+\' (\'+(bindSales.sales_id||\'\')+\')\'"></span></h3>'
    + '<div class="modal-body">'
    + '<p style="font-size:13px;color:#6b7280;line-height:1.7;margin-bottom:12px">销售从飞书进入易销时，系统用 open_id 识别身份。请把该销售在飞书网页应用 URL 中 <code>?open_id=</code> 后面的值粘贴到下方（形如 <code>ou_xxxxxxxx</code>）。</p>'
    + '<label>飞书 open_id</label><input v-model="bindOpenIdVal" class="search-input full" placeholder="例如: ou_5a3f22e10391fa12d541c1c033f29dd5">'
    + '</div>'
    + '<div class="modal-actions"><button class="btn btn-ghost" @click="bindModal=null">取消</button>'
    + '<button class="btn btn-primary" @click="saveBindOpenId()">保存绑定</button></div></div></div>'
    + '<div class="modal-mask" v-if="curProfile" @click.self="curProfile=null">'
    + '<div class="modal" style="width:680px;max-width:95vw">'
    + '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">'
    + '<div style="display:flex;align-items:center;gap:10px"><span class="cell-avatar" style="width:42px;height:42px;font-size:18px" v-text="(curSales.name||\'?\').slice(0,1)"></span><div><h3 style="margin:0" v-text="curSales.name+\' · AI 能力画像图谱\'"></h3><div class="sub" v-text="curSales.sales_id+\' · 基于 CRM 历史成交商机大数据提炼\'"></div></div></div>'
    + '<button class="btn btn-small" style="background:#4f46e5;color:#fff" @click="syncSingle(curSales)" :disabled="syncingSingle"><span class="btn-ico" v-html="ico.sync"></span><span v-text="syncingSingle?\'同步中…\':\'反哺并同步标签\'"></span></button>'
    + '</div>'
    + '<div class="modal-body" style="max-height:70vh;overflow-y:auto;padding-right:4px">'
    + '<div class="profile-sec" style="background:#f8fafc;padding:14px;border-radius:12px;border:1px solid #e2e8f0;margin-bottom:14px">'
    + '<div style="font-size:12px;font-weight:700;color:#64748b;margin-bottom:6px">🤖 AI 综合效能评估</div>'
    + '<div style="font-size:13.5px;line-height:1.6;color:#1e293b;font-weight:500" v-text="curProfile.ai_summary"></div>'
    + '</div>'
    + '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px">'
    + '<div class="stat-card" style="padding:12px;flex-direction:column;align-items:flex-start;gap:4px"><div class="sub">CRM 成单数</div><div style="font-size:22px;font-weight:800;color:#10b981" v-text="curProfile.deal_stats.won_deals+\' 笔\'"></div><div style="font-size:11px;color:#64748b" v-text="\'总商机 \'+curProfile.deal_stats.total_deals"></div></div>'
    + '<div class="stat-card" style="padding:12px;flex-direction:column;align-items:flex-start;gap:4px"><div class="sub">成单胜率</div><div style="font-size:22px;font-weight:800;color:#4f46e5" v-text="curProfile.deal_stats.win_rate_percent+\'%\'"></div><div style="font-size:11px;color:#64748b">根据历史转化</div></div>'
    + '<div class="stat-card" style="padding:12px;flex-direction:column;align-items:flex-start;gap:4px"><div class="sub">擅长客单价</div><div style="font-size:14px;font-weight:800;color:#d97706;margin-top:4px" v-text="curProfile.preferred_ticket_size"></div><div style="font-size:11px;color:#64748b">主力突破区间</div></div>'
    + '<div class="stat-card" style="padding:12px;flex-direction:column;align-items:flex-start;gap:4px"><div class="sub">擅长决策人</div><div style="font-size:14px;font-weight:800;color:#7c3aed;margin-top:4px" v-text="curProfile.decision_maker_affinity"></div><div style="font-size:11px;color:#64748b">突破攻坚首选</div></div>'
    + '</div>'
    + '<div style="margin-bottom:14px">'
    + '<div style="font-size:12px;font-weight:700;color:#64748b;margin-bottom:6px">🎯 AI 提炼擅长行业与战术标签</div>'
    + '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">'
    + '<span v-for="ind in curProfile.recommended_industries" :key="ind" class="badge" style="background:#eef2ff;color:#4338ca;border:1px solid #c7d2fe;font-size:13px;padding:4px 12px" v-text="\'行业 · \'+ind"></span>'
    + '<span v-for="tag in curProfile.tactical_tags" :key="tag" class="badge" style="background:#fdf4ff;color:#a855f7;border:1px solid #f0abfc;font-size:13px;padding:4px 12px" v-text="\'⚡ \'+tag"></span>'
    + '</div>'
    + '</div>'
    + '<div style="margin-bottom:14px">'
    + '<div style="font-size:12px;font-weight:700;color:#64748b;margin-bottom:6px">📊 综合胜任力能力评分 (AI Radar)</div>'
    + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">'
    + '<div style="background:#f8fafc;padding:10px 14px;border-radius:10px"><div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px"><span>行业专业度</span><strong v-text="curProfile.radar_scores.industry_depth+\' 分\'"></strong></div><div class="bar-track" style="height:6px"><div class="bar-fill" :style="{width:curProfile.radar_scores.industry_depth+\'%\',background:\'#4f46e5\'}"></div></div></div>'
    + '<div style="background:#f8fafc;padding:10px 14px;border-radius:10px"><div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px"><span>大单攻坚力</span><strong v-text="curProfile.radar_scores.enterprise_closing+\' 分\'"></strong></div><div class="bar-track" style="height:6px"><div class="bar-fill" :style="{width:curProfile.radar_scores.enterprise_closing+\'%\',background:\'#10b981\'}"></div></div></div>'
    + '<div style="background:#f8fafc;padding:10px 14px;border-radius:10px"><div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px"><span>决策人破冰力</span><strong v-text="curProfile.radar_scores.decision_maker_breakthrough+\' 分\'"></strong></div><div class="bar-track" style="height:6px"><div class="bar-fill" :style="{width:curProfile.radar_scores.decision_maker_breakthrough+\'%\',background:\'#7c3aed\'}"></div></div></div>'
    + '<div style="background:#f8fafc;padding:10px 14px;border-radius:10px"><div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px"><span>成单周期推进效率</span><strong v-text="curProfile.radar_scores.cycle_efficiency+\' 分\'"></strong></div><div class="bar-track" style="height:6px"><div class="bar-fill" :style="{width:curProfile.radar_scores.cycle_efficiency+\'%\',background:\'#f59e0b\'}"></div></div></div>'
    + '</div></div>'
    + '<div>'
    + '<div style="font-size:12px;font-weight:700;color:#64748b;margin-bottom:6px">📁 关联 CRM 历史商机案例复盘 ({{ (curProfile.deals||[]).length }} 笔)</div>'
    + '<div style="display:flex;flex-direction:column;gap:8px">'
    + '<div v-for="d in (curProfile.deals||[])" :key="d.deal_id" style="padding:10px 12px;border:1px solid #e2e8f0;border-radius:8px;font-size:12.5px;background:#fff">'
    + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">'
    + '<strong>{{ d.customer_name }} ({{ d.industry }})</strong>'
    + '<span class="badge" :class="d.result===\'成单\'?\'badge-ok\':\'badge-flip\'">{{ d.result }} · {{ d.deal_amount===\'0\'?\'未成单\':d.deal_amount }}</span>'
    + '</div>'
    + '<div class="muted" style="margin-bottom:4px">痛点: {{ (d.pain_points||[]).join(\'、\') }} | 决策人: {{ d.decision_maker }} | 预算: {{ d.budget }}</div>'
    + '<div style="color:#4338ca;background:#f5f3ff;padding:4px 8px;border-radius:4px">💡 战术复盘: {{ d.tactic_summary }}</div>'
    + '</div>'
    + '</div></div>'
    + '</div>'
    + '<div class="modal-actions"><button class="btn btn-ghost" @click="curProfile=null">关闭</button></div>'
    + '</div></div></div>';

  var T_MEM = '<div class="view-anim"><section class="card">'
    + '<div class="memory-banner">'
    + '<div class="mem-ico" v-html="ico.memories"></div>'
    + '<div class="mem-text"><div class="mem-title">Agent Memory 学习闭环</div>'
    + '<div class="mem-sub">每次人工改派都会固化为强记忆, 参与后续相似客户的分单决策</div></div>'
    + '<div class="mem-stats"><span class="badge badge-strong" v-text="\'强记忆 \'+sCount"></span>'
    + '<span class="badge badge-weak" v-text="\'弱记忆 \'+wCount"></span></div></div>'
    + '<div class="empty-hint" v-if="mems.length===0">暂无记忆 — 运行流水线后, 在「智能分配」改派即可产生</div>'
    + '<table class="yx-table" v-if="mems.length>0"><thead><tr><th>客户</th><th>原推荐</th><th>人工判断</th><th>结论</th><th>来源</th></tr></thead><tbody>'
    + '<tr v-for="m in mems" :key="m.memory_id" :class="m.source===\'strong\'?\'row-strong\':\'\'">'
    + '<td><strong v-text="m.customer_id"></strong></td><td v-text="m.sales_id"></td>'
    + '<td><strong v-text="m.correct_sales_id"></strong></td>'
    + '<td><span class="badge" :class="m.decision===\'correct\'?\'badge-flip\':\'badge-ok\'" v-text="m.decision===\'correct\'?\'修正\':\'确认\'"></span></td>'
    + '<td><span class="badge" :class="m.source===\'strong\'?\'badge-strong\':\'badge-weak\'" v-text="m.source===\'strong\'?\'强记忆\':\'弱记忆\'"></span></td>'
    + '</tr></tbody></table></section></div>';

  var T_SRC = '<div class="view-anim"><section class="card">'
    + '<div class="card-head"><h3>真实数据适配层</h3><span class="card-sub">输出与 mock 同构, 流水线零改动</span></div>'
    + '<table class="yx-table"><thead><tr><th>接口</th><th>来源</th><th>状态</th></tr></thead><tbody>'
    + '<tr><td><code>fetch_crm_customers()</code></td><td>CRM 客户列表</td><td><span class="badge badge-ok">已预留</span></td></tr>'
    + '<tr><td><code>fetch_wework_chat()</code></td><td>企微会话存档</td><td><span class="badge badge-ok">已预留</span></td></tr>'
    + '<tr><td><code>fetch_crm_deals()</code></td><td>CRM 成交商机</td><td><span class="badge badge-ok">已预留</span></td></tr>'
    + '<tr><td><code>adapters/crm_data_adapter</code></td><td>CSV + 企微 JSON → 同构</td><td><span class="badge badge-flip">可运行</span></td></tr>'
    + '</tbody></table></section>'
    + '<section class="card"><div class="card-head"><h3>通知通道</h3><span class="card-sub">加签 HMAC-SHA256 · 配置后自动推送日报</span></div>'
    + '<table class="yx-table"><thead><tr><th>通道</th><th>配置项</th><th>状态</th></tr></thead><tbody>'
    + '<tr><td>飞书群机器人</td><td><code>FEISHU_WEBHOOK_URL / FEISHU_SECRET</code></td><td><span class="badge badge-flip">待配置</span></td></tr>'
    + '<tr><td>钉钉群机器人</td><td><code>DINGTALK_WEBHOOK_URL / DINGTALK_SECRET</code></td><td><span class="badge badge-flip">待配置</span></td></tr>'
    + '<tr><td>个人工作通知</td><td>飞书企业自建应用 <code>FEISHU_APP_ID / SECRET</code></td><td><span class="badge badge-ok">已接入</span></td></tr>'
    + '</tbody></table></section></div>';

  var T_TOAST = '<div class="toast-stack">'
    + '<div v-for="t in ts" :key="t.id" :class="[\'toast\',\'toast-\'+(t.type||\'info\')]" v-text="t.message"></div></div>';

  /* ---- 组件 ---- */
  var C_Dash = {
    template: T_DASH,
    computed: {
      ico: function(){return ICONS;},
      s: function(){return store.summary;},
      assignmentCount: function(){return store.lastRun&&store.lastRun.assignments?store.lastRun.assignments.length:0;},
      intentMax: function(){var d=store.summary&&store.summary.intention_stats||{};return Math.max(d["高"]||0,d["中"]||0,d["低"]||0,1);},
      churnMax: function(){var d=store.summary&&store.summary.churn_stats||{};return Math.max(d["高"]||0,d["中"]||0,d["低"]||0,1);},
      greeting: function(){var h=new Date().getHours();if(h<6)return"夜深了";if(h<11)return"早上好";if(h<14)return"中午好";if(h<18)return"下午好";return"晚上好";},
      today: function(){return new Date().toLocaleDateString("zh-CN",{month:"long",day:"numeric",weekday:"long"});}
    },
    methods: {pct: pct, timeOf: timeOf}
  };

  var C_Cust = {
    template: T_CUSTOMERS,
    data: function(){return {expanded:"", fInt:"", fChurn:"", onlyMine: true};},
    computed: {
      salesMode: function(){return store.salesMode;},
      currentSales: function(){return store.currentSales;},
      list: function(){return store.customers;},
      loading: function(){return store.loadingCustomers;},
      myCount: function(){
        var sid = store.currentSales ? store.currentSales.sales_id : "";
        if (!sid) return 0;
        return (store.customers||[]).filter(function(c){ return c.owner_sales_id === sid; }).length;
      },
      allCount: function(){ return (store.customers||[]).length; },
      filtered: function(){
        var l = store.customers||[];
        // 销售专属视角: 默认且仅看属于当前销售的客户
        if (store.salesMode && store.currentSales && this.onlyMine) {
          var sid = store.currentSales.sales_id;
          l = l.filter(function(c){ return c.owner_sales_id === sid; });
        }
        if(this.fInt) l = l.filter(function(c){return c.intention_level===this.fInt;}.bind(this));
        if(this.fChurn) l = l.filter(function(c){return c.churn_risk===this.fChurn;}.bind(this));
        return l;
      },
      prof: function(){var d=store.profileCache[this.expanded];return d&&d.records&&d.records.length?d.records[0]:null;},
      profLoading: function(){return store.loadingProfile===this.expanded;},
      profLevel: function(){return this.prof&&this.prof.result?this.prof.result.intention_level:"";},
      profChurn: function(){return this.prof&&this.prof.result?this.prof.result.churn_risk:"";}
    },
    methods: {
      toggle: function(c){var id=c.customer_id;this.expanded=this.expanded===id?"":id;if(this.expanded)loadProfile(id);}
    }
  };

  var C_Assign = {
    template: T_ASSIGN,
    data: function(){return {q:"",ri:""};},
    computed: {
      ico: function(){return ICONS;},
      list: function(){return store.lastRun&&store.lastRun.assignments||[];},
      hasData: function(){return this.list.length>0;},
      salesList: function(){return store.sales||[];},
      filtered: function(){
        var kw=this.q.trim(),l=this.list;
        if(!kw)return l;
        return l.filter(function(a){return(a.customer_name||"").indexOf(kw)>=0||(a.sales_id||"").indexOf(kw)>=0;});
      },
      rt: {
        get: function(){return store.reassignTarget;},
        set: function(v){store.reassignTarget=v;}
      }
    },
    methods: {
      corr: function(cid){return store.corrections[cid];},
      loadOf: function(a){var s=(store.sales||[]).find(function(x){return x.sales_id===a.sales_id;});return s?s.current_load:0;},
      openRe: function(a){store.reassignTarget={customer_id:a.customer_id,customer_name:a.customer_name,current_sales_id:a.sales_id,current_sales_name:a.sales_name};this.ri="";},
      confirm: function(){
        var t=this.ri.trim().toUpperCase();
        if(!t){toast("请输入销售ID","error");return;}
        var self=this;
        submitReassignment(store.reassignTarget.customer_id,t).then(function(){store.reassignTarget=null;self.ri="";});
      }
    }
  };

  var C_Team = {
    template: T_TEAM,
    data: function(){
      return {
        q: "",
        showModal: false,
        form: { sales_id: "", name: "", industries_str: "", cities_str: "", mobile: "", open_id: "" },
        curProfile: null,
        curSales: {},
        syncingSingle: false,
        syncingAll: false,
        bindModal: null,
        bindSales: {},
        bindOpenIdVal: ""
      };
    },
    computed: {
      ico: function(){return ICONS;},
      list: function(){return store.sales || [];},
      filtered: function(){
        var kw = this.q.trim().toLowerCase(), l = this.list;
        if(!kw) return l;
        return l.filter(function(s){
          return (s.name||"").toLowerCase().indexOf(kw) >= 0 ||
                 (s.sales_id||"").toLowerCase().indexOf(kw) >= 0 ||
                 (s.responsible_cities||[]).some(function(c){return c.toLowerCase().indexOf(kw)>=0;}) ||
                 (s.good_at_industries||[]).some(function(i){return i.toLowerCase().indexOf(kw)>=0;});
        });
      }
    },
    methods: {
      openAddModal: function(){
        this.form = { sales_id: "S00" + (this.list.length + 1), name: "", industries_str: "", cities_str: "", mobile: "15990070647", open_id: "" };
        this.showModal = true;
      },
      bindOpenId: function(s){
        this.bindSales = s;
        this.bindOpenIdVal = s.open_id || "";
        this.bindModal = s;
      },
      saveBindOpenId: function(){
        var self = this;
        var f = this.bindSales;
        var openId = (this.bindOpenIdVal || "").trim();
        var payload = {
          sales_id: f.sales_id,
          name: f.name,
          good_at_industries: f.good_at_industries || [],
          responsible_cities: f.responsible_cities || [],
          current_load: f.current_load || 0,
          mobile: f.mobile || "",
          open_id: openId
        };
        api("/sales/" + encodeURIComponent(f.sales_id), {method:"PATCH", body:JSON.stringify(payload)})
          .then(function(){ return loadSales(); })
          .then(function(){
            self.bindModal = null;
            self.bindSales = {};
            self.bindOpenIdVal = "";
            toast("open_id 绑定成功", "success");
          })
          .catch(function(e){ toast("绑定失败: "+e.message, "error"); });
      },
      submitAdd: function(){
        var f = this.form;
        if(!f.sales_id.trim()) { toast("请填写销售工号", "error"); return; }
        if(!f.name.trim()) { toast("请填写销售姓名", "error"); return; }
        var inds = f.industries_str.split(/[,，\s]+/).map(function(s){return s.trim();}).filter(Boolean);
        var cities = f.cities_str.split(/[,，\s]+/).map(function(s){return s.trim();}).filter(Boolean);
        var self = this;
        addSalesMember({
          sales_id: f.sales_id.trim().toUpperCase(),
          name: f.name.trim(),
          good_at_industries: inds,
          responsible_cities: cities,
          current_load: 0,
          mobile: f.mobile.trim(),
          open_id: f.open_id.trim()
        }).then(function(){
          self.showModal = false;
        });
      },
      del: function(s){
        if(confirm("确定要移除销售成员「" + s.name + " (" + s.sales_id + ")」吗？")) {
          removeSalesMember(s.sales_id, s.name);
        }
      },
      viewProfile: function(s){
        var self = this;
        this.curSales = s;
        toast("正在调取 CRM 历史商机并由 AI 深度提炼画像…", "info");
        api("/sales/" + encodeURIComponent(s.sales_id) + "/profile")
          .then(function(res){
            self.curProfile = res;
          })
          .catch(function(err){
            toast("获取销售画像失败: " + err.message, "error");
          });
      },
      syncSingle: function(s){
        var self = this;
        this.syncingSingle = true;
        api("/sales/" + encodeURIComponent(s.sales_id) + "/sync-profile", {method:"POST"})
          .then(function(res){
            toast("成功反哺更新销售 " + s.name + " 擅长行业与能力标签！", "success");
            self.curProfile = res.profile;
            return loadSales();
          })
          .catch(function(err){
            toast("同步失败: " + err.message, "error");
          })
          .finally(function(){
            self.syncingSingle = false;
          });
      },
      syncAll: function(){
        var self = this;
        this.syncingAll = true;
        toast("正在对全员执行 CRM 历史商机 AI 扫描与标签反哺…", "info");
        api("/sales/sync-all-profiles", {method:"POST"})
          .then(function(res){
            toast("全员 AI 扫描完成，已同步 " + res.synced_count + " 位销售标签！", "success");
            return loadSales();
          })
          .catch(function(err){
            toast("全员同步失败: " + err.message, "error");
          })
          .finally(function(){
            self.syncingAll = false;
          });
      }
    }
  };

  var C_Mem = {
    template: T_MEM,
    computed: {
      ico: function(){return ICONS;},
      mems: function(){return store.memories;},
      sCount: function(){return(store.memories||[]).filter(function(m){return m.source==="strong";}).length;},
      wCount: function(){return(store.memories||[]).length-this.sCount;}
    }
  };

  var C_Src = { template: T_SRC };

  var C_Toast = {
    template: T_TOAST,
    computed: { ts: function(){return toasts;} }
  };

  /* ---- 根应用 ---- */
  var app = V.createApp({
    template: T_ROOT,
    computed: {
      icons: function(){return ICONS;},
      booted: function(){return store.booted;},
      route: function(){return store.route;},
      health: function(){return store.health;},
      runningPipeline: function(){return store.runningPipeline;},
      pageTitle: function(){return{dashboard:"工作台",customers:"客户画像",assignments:"智能分配",team:"销售团队",memories:"记忆中心",sources:"数据接入"}[store.route]||"易销";}
    },
    methods: {
      navClass: function(name){ return store.route === name ? "yx-nav-item active" : "yx-nav-item"; },
      refresh: function(){loadCustomers();loadSales();loadSummary();loadMemories();toast("已刷新","info");},
      run: runPipeline
    }
  });

  app.component("view-dashboard", C_Dash);
  app.component("view-customers", C_Cust);
  app.component("view-assignments", C_Assign);
  app.component("view-team", C_Team);
  app.component("view-memories", C_Mem);
  app.component("view-sources", C_Src);
  app.component("toast-stack", C_Toast);

  function applyRoute(hash) {
    var h = hash || location.hash || "#/dashboard";
    var n = h.replace(/^#\//,"").split("?")[0] || "dashboard";
    // 销售专属视角下: 仅开放 customers(客户画像)
    if (store.salesMode) {
      store.route = "customers";
      return;
    }
    store.route = ["dashboard","customers","assignments","team","memories","sources"].indexOf(n) >= 0 ? n : "dashboard";
  }

  window.addEventListener("hashchange", function(){
    applyRoute(location.hash);
  });

  app.mount("#app");
  store.booted = true;

  api("/health").then(function(){store.health=true;}).catch(function(){store.health=false;toast("后端离线","error");});
  restoreAssignments();
  loadCustomers();
  loadSales().then(function(){
    try {
      var p = new URLSearchParams(location.search);
      var oid = p.get("open_id") || p.get("openId") || p.get("user_id") || "";
      if (oid) {
        var matched = (store.sales||[]).find(function(s){ return s.open_id === oid; });
        if (matched) {
          store.currentSales = matched;
          store.salesMode = true;
          // 销售进入: 自动锁定至客户画像页
          store.route = "customers";
          location.hash = "#/customers";
          toast("欢迎回来，" + matched.name + " (" + matched.sales_id + ")！已为您呈现专属客户画像", "success");
        }
      }
    } catch(e){}
    applyRoute(location.hash);
  });
  loadSummary(); loadMemories();
})();
