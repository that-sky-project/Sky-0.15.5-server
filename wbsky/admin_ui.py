# -*- coding: utf-8 -*-
"""管理后台单页界面（/admin）的 HTML。

单独一个文件、并且写成一个模块级常量 `PAGE`，是为了：
  · 跟 live_panel.py 的做法一致（那边也是 PAGE 常量 + Flask Response 直接吐）；
  · **不依赖 Jinja/templates 目录** —— wbsky 原来的 index.py 里没有
    render_template 这条链路，引入模板目录会多一个部署时容易漏的东西；
  · 改界面不用重启（每次请求都读这个模块的属性）。

界面风格与 /live 保持一致（深色玻璃拟态），纯手写 CSS + 原生 JS，无外部依赖
（部署环境常常没有外网，不能引 CDN）。
"""

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>skywb 管理后台</title>
<style>
:root{
  --bg:#0a0f1e; --panel:rgba(21,28,51,.86); --line:#26304f; --fg:#e8ecff;
  --dim:#8b96c0; --acc:#6ea8fe; --acc2:#7b5cff; --ok:#41d392; --warn:#ffc857; --err:#ff6b81;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:radial-gradient(1200px 700px at 12% -12%,#1b2a5e 0%,var(--bg) 58%);
 color:var(--fg);font:14px/1.6 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
a{color:var(--acc)}
#app{display:flex;min-height:100vh}
aside{width:208px;flex:0 0 208px;padding:18px 14px;border-right:1px solid var(--line);
 background:rgba(10,15,30,.55);backdrop-filter:blur(8px);position:sticky;top:0;height:100vh}
.brand{font-weight:700;font-size:17px;letter-spacing:.4px;margin:4px 0 2px}
.brand small{display:block;font-weight:400;font-size:11px;color:var(--dim);letter-spacing:0}
nav{margin-top:22px;display:flex;flex-direction:column;gap:6px}
nav button{all:unset;cursor:pointer;padding:9px 12px;border-radius:10px;color:var(--fg);
 font-size:14px;display:flex;align-items:center;gap:8px}
nav button:hover{background:rgba(255,255,255,.07)}
nav button.on{background:linear-gradient(135deg,rgba(79,140,255,.28),rgba(123,92,255,.28));
 border:1px solid rgba(110,168,254,.4)}
nav .ico{width:18px;text-align:center;opacity:.9}
aside .foot{position:absolute;bottom:14px;left:14px;right:14px;font-size:12px;color:var(--dim)}
main{flex:1;min-width:0;padding:22px 26px 60px}
header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:18px}
header h2{margin:0;font-size:19px}
.spacer{flex:1}
.who{color:var(--dim);font-size:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 18px;
 margin-bottom:16px;box-shadow:0 12px 36px rgba(0,0,0,.28)}
.card h3{margin:0 0 12px;font-size:15px;display:flex;align-items:center;gap:8px}
.grid{display:grid;gap:12px}
.g4{grid-template-columns:repeat(auto-fit,minmax(168px,1fr))}
.g2{grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
.kpi{background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.kpi .n{font-size:22px;font-weight:700}
.kpi .l{color:var(--dim);font-size:12px;margin-top:2px}
button.b{cursor:pointer;border:1px solid var(--line);background:rgba(255,255,255,.06);color:var(--fg);
 padding:7px 12px;border-radius:9px;font-size:13px}
button.b:hover{background:rgba(255,255,255,.12)}
button.p{background:linear-gradient(135deg,#4f8cff,#7b5cff);border:0;color:#fff;font-weight:600}
button.d{border-color:rgba(255,107,129,.5);color:#ffb3bf}
button.d:hover{background:rgba(255,107,129,.16)}
input,select,textarea{background:#0e1428;border:1px solid #2c3757;color:var(--fg);
 border-radius:9px;padding:8px 10px;font-size:13px;font-family:inherit}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--acc)}
textarea{width:100%;min-height:90px;resize:vertical;font-family:ui-monospace,Consolas,monospace}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid rgba(38,48,79,.8);vertical-align:top}
th{color:var(--dim);font-weight:500;position:sticky;top:0;background:#141b33;z-index:1}
tr:hover td{background:rgba(255,255,255,.03)}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}
.tag{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;
 background:rgba(110,168,254,.16);border:1px solid rgba(110,168,254,.35)}
.tag.ok{background:rgba(65,211,146,.16);border-color:rgba(65,211,146,.4)}
.tag.warn{background:rgba(255,200,87,.16);border-color:rgba(255,200,87,.4)}
.tag.err{background:rgba(255,107,129,.16);border-color:rgba(255,107,129,.4)}
.flex{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.scroll{max-height:320px;overflow:auto;border:1px solid var(--line);border-radius:10px}
.chips{display:flex;flex-wrap:wrap;gap:6px;max-height:180px;overflow:auto;padding:8px;
 background:#0e1428;border:1px solid #2c3757;border-radius:10px}
.chip{background:rgba(255,255,255,.06);border:1px solid var(--line);border-radius:999px;
 padding:2px 9px;font-size:12px;cursor:pointer}
.chip:hover{background:rgba(255,107,129,.2)}
.muted{color:var(--dim)}
.err{color:var(--err)}
#modal{position:fixed;inset:0;background:rgba(4,7,16,.66);display:none;align-items:center;
 justify-content:center;z-index:50;padding:20px}
#modal.on{display:flex}
#modal .box{background:#141b33;border:1px solid var(--line);border-radius:14px;max-width:560px;
 width:100%;padding:18px;max-height:86vh;overflow:auto}
#toast{position:fixed;right:18px;bottom:18px;display:flex;flex-direction:column;gap:8px;z-index:60}
#toast div{background:#141b33;border:1px solid var(--line);border-left:3px solid var(--acc);
 border-radius:10px;padding:10px 14px;font-size:13px;box-shadow:0 10px 30px rgba(0,0,0,.4);
 animation:pop .18s ease-out}
#toast div.err{border-left-color:var(--err)}
#toast div.ok{border-left-color:var(--ok)}
@keyframes pop{from{transform:translateY(8px);opacity:0}to{transform:none;opacity:1}}
.pill{cursor:pointer;padding:3px 9px;border-radius:999px;border:1px solid var(--line);
 background:rgba(255,255,255,.05);font-size:12px}
.pill.on{background:rgba(110,168,254,.24);border-color:rgba(110,168,254,.5)}
.pager{display:flex;gap:8px;align-items:center;margin-top:10px;color:var(--dim);font-size:12px}
</style>
</head>
<body>
<div id="app">
  <aside>
    <div class="brand">skywb<small>管理后台 · /admin</small></div>
    <nav id="nav">
      <button data-tab="overview" class="on"><span class="ico">◎</span>总览</button>
      <button data-tab="users"><span class="ico">☰</span>用户</button>
      <button data-tab="friends"><span class="ico">♡</span>好友关系</button>
      <button data-tab="feed"><span class="ico">✉</span>动态 / 邀请</button>
      <button data-tab="tools"><span class="ico">⚙</span>工具 / 日志</button>
    </nav>
    <div class="foot">
      <div id="whoami" class="muted">—</div>
      <div style="margin-top:8px"><a href="/live" target="_blank">实时面板 /live ↗</a></div>
      <div style="margin-top:4px"><a href="#" id="logout">退出登录</a></div>
    </div>
  </aside>
  <main>
    <header>
      <h2 id="title">总览</h2>
      <div class="spacer"></div>
      <span class="who" id="stamp"></span>
    </header>
    <div id="view"></div>
  </main>
</div>
<div id="modal"><div class="box" id="modalBox"></div></div>
<div id="toast"></div>

<script>
/* ============================ 基础设施 ============================ */
const $ = (s, r) => (r || document).querySelector(s);
const view = $('#view');

function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function fmtTime(t){
  if(!t) return '-';
  const n = Number(t); if(!isFinite(n) || n<=0) return '-';
  const d = new Date(n*(n<1e12?1000:1));
  return d.toLocaleString('zh-CN',{hour12:false});
}
function fmtNum(n){ const v=Number(n||0); return isFinite(v)?v.toLocaleString('zh-CN'):'-'; }

function toast(msg, kind){
  const d=document.createElement('div');
  d.className = kind||''; d.textContent=msg;
  $('#toast').appendChild(d);
  setTimeout(()=>d.remove(), 4200);
}

async function api(path, opts){
  opts = opts || {};
  const r = await fetch(path, {
    method: opts.method || 'GET',
    headers: opts.body ? {'Content-Type':'application/json'} : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if(r.status === 401){ location.href='/admin/'; throw new Error('need login'); }
  let j = null;
  try { j = await r.json(); } catch(e){}
  if(!r.ok || (j && j.ok === false)) throw new Error((j && (j.msg||j.error)) || ('HTTP '+r.status));
  return j;
}

let MODAL_OK = null;
function modal(title, bodyHtml, onOk, okText){
  MODAL_OK = onOk || null;
  $('#modalBox').innerHTML =
    '<h3 style="margin:0 0 12px">'+esc(title)+'</h3>'+bodyHtml+
    '<div class="flex" style="justify-content:flex-end;margin-top:16px">'+
      '<button class="b" onclick="closeModal()">取消</button>'+
      (onOk ? '<button class="b p" id="modalOk">'+esc(okText||'确定')+'</button>' : '')+
    '</div>';
  $('#modal').classList.add('on');
  if(onOk) $('#modalOk').onclick = async () => {
    try { await MODAL_OK(); closeModal(); }
    catch(e){ toast('失败: '+e.message, 'err'); }
  };
}
function closeModal(){ $('#modal').classList.remove('on'); MODAL_OK=null; }
$('#modal').addEventListener('click', e => { if(e.target.id==='modal') closeModal(); });

/* ============================ 总览 ============================ */
async function tabOverview(){
  $('#title').textContent='总览';
  const {data} = await api('/admin/api/overview');
  const cs = data.currency_sum || {};
  const pool = data.name_pool || {};
  const kpi = (n,l)=>'<div class="kpi"><div class="n">'+esc(n)+'</div><div class="l">'+esc(l)+'</div></div>';
  view.innerHTML = `
    <div class="card">
      <h3>服务器概览</h3>
      <div class="grid g4">
        ${kpi(fmtNum(data.users),'账号总数')}
        ${kpi(fmtNum(data.friends),'好友关系行')}
        ${kpi(fmtNum(data.friendships),'关系明细行')}
        ${kpi(fmtNum(data.chat_messages),'聊天记录')}
        ${kpi(fmtNum(data.ct),'星座页')}
        ${kpi(fmtNum(data.pending_invites),'待接受邀请')}
        ${kpi(fmtNum(data.gift_messages),'礼物消息')}
        ${kpi(fmtNum(data.currency_rows),'货币记录行')}
      </div>
    </div>
    <div class="card">
      <h3>数据库</h3>
      <div class="flex">
        <span class="tag ${data.db && data.db.ok ? 'ok':'err'}">
          ${esc((data.db&&data.db.mode)||'?')} · ${data.db && data.db.ok ? '已连接':'连不上'}</span>
        <span class="muted mono">${esc((data.db&&data.db.host)||'')}</span>
        ${data.db && data.db.error ? '<span class="err mono">'+esc(data.db.error)+'</span>':''}
        <span class="spacer"></span>
        <span class="muted">表 ${(data.tables||[]).length} 张</span>
      </div>
    </div>
    <div class="card">
      <h3>好友默认昵称池</h3>
      <div class="flex">
        <span class="tag ${pool.enabled?'ok':'warn'}">${pool.enabled?'启用':'已关闭'}</span>
        <span class="muted">${fmtNum(pool.count)} 个名字 · ${pool.file_exists?'文件正常':'文件缺失'} · ${fmtNum(pool.size)} 字节</span>
      </div>
      <div class="muted mono" style="margin-top:6px">${esc(pool.path||'')}</div>
      <div class="chips" style="margin-top:10px" id="poolSample">
        ${(pool.sample||[]).map(n=>'<span class="chip">'+esc(n)+'</span>').join('')}
      </div>
      <div class="flex" style="margin-top:10px">
        <button class="b" id="poolMore">再随机 24 个</button>
        <span class="muted" style="font-size:12px">
          改 <code>wbsky/config/friend_name_pool.json</code> 的 <code>names</code> 即可换名单，不用重启</span>
      </div>
    </div>
    <div class="card">
      <h3>货币总量</h3>
      <div class="grid g4">
        ${['candles','hearts','heart_wax','season_candle','season_heart','wax','season_wax','prestige','prestige_wax','season_pass_token']
          .map(k=>kpi(fmtNum(cs[k]), CURRENCY_CN[k]||k)).join('')}
      </div>
    </div>
    <div class="card">
      <h3>账号一览（按账号表顺序，wbsky 的 users 表没有时间戳列）</h3>
      <div class="scroll"><table><thead><tr><th>User ID</th><th>设备</th><th>蜡烛</th><th></th></tr></thead>
      <tbody>${(data.recent_users||[]).map(u=>`<tr>
        <td class="mono"><a href="#" onclick="openUser('${esc(u.id)}');return false">${esc(u.id)}</a></td>
        <td class="mono muted">${esc((u.device_id||'').slice(0,18))}</td>
        <td>${fmtNum(u.candles)}</td>
        <td><button class="b" onclick="openUser('${esc(u.id)}')">管理</button></td></tr>`).join('')
        || '<tr><td colspan="4" class="muted">还没有账号</td></tr>'}</tbody></table></div>
    </div>`;
  const more = $('#poolMore');
  if(more) more.onclick = async ()=>{
    try{
      const {data} = await api('/admin/api/name_pool/random?n=24');
      $('#poolSample').innerHTML = data.map(n=>'<span class="chip">'+esc(n)+'</span>').join('');
    }catch(e){ toast(e.message,'err'); }
  };
}

/* ============================ 用户列表 ============================ */
const CURRENCY_CN = {candles:'蜡烛',hearts:'爱心',heart_wax:'心蜡',season_candle:'季节蜡烛',
  season_heart:'季节心',season_pass_token:'季卡代币',wax:'烛光',season_wax:'季节烛光',
  prestige:'升华蜡烛',prestige_wax:'升华烛光'};
let U = {q:'', limit:50, offset:0, order:'candles_desc'};

async function tabUsers(){
  $('#title').textContent='用户管理';
  const q = `/admin/api/users?q=${encodeURIComponent(U.q)}&limit=${U.limit}&offset=${U.offset}&order=${U.order}`;
  const {data} = await api(q);
  if(data.error) toast(data.error,'err');
  const rows = data.rows||[];
  view.innerHTML = `
    <div class="card">
      <div class="flex">
        <input id="uq" placeholder="搜索 User ID / 设备号 / 存档码" style="min-width:280px" value="${esc(U.q)}">
        <select id="uo">
          <option value="candles_desc">蜡烛 ↓</option>
          <option value="candles_asc">蜡烛 ↑</option>
          <option value="id">ID ↑</option>
          <option value="id_desc">ID ↓</option>
        </select>
        <button class="b p" id="ub">搜索</button>
        <span class="spacer"></span>
        <span class="muted">共 ${fmtNum(data.total)} 个账号</span>
      </div>
      <p class="muted" style="margin:10px 0 0;font-size:12px">
        ⚠️ wbsky 的 <code>users</code> 表没有时间戳列，所以这里只能按蜡烛/ID 排序，不能按注册时间。</p>
    </div>
    <div class="card">
      <div class="scroll" style="max-height:60vh"><table>
        <thead><tr><th>User ID</th><th>设备号</th><th>蜡烛</th><th>好友</th><th>存档码</th><th></th></tr></thead>
        <tbody>${rows.map(u=>`<tr>
          <td class="mono">${esc(u.id)}</td>
          <td class="mono muted">${esc((u.device_id||'').slice(0,16))}</td>
          <td>${fmtNum(u.candles)}</td>
          <td>${fmtNum(u.friends)}</td>
          <td class="mono muted">${esc((u.recovery||'').slice(0,14))}</td>
          <td><button class="b" onclick="openUser('${esc(u.id)}')">管理</button></td>
        </tr>`).join('') || '<tr><td colspan="6" class="muted">没有匹配的账号</td></tr>'}</tbody>
      </table></div>
      <div class="pager">
        <button class="b" id="uprev" ${U.offset<=0?'disabled':''}>上一页</button>
        <span>${U.offset+1} – ${U.offset+rows.length} / ${fmtNum(data.total)}</span>
        <button class="b" id="unext" ${U.offset+rows.length>=data.total?'disabled':''}>下一页</button>
      </div>
    </div>`;
  $('#uo').value = U.order;
  $('#ub').onclick = ()=>{ U.q=$('#uq').value; U.offset=0; tabUsers(); };
  $('#uq').onkeydown = e=>{ if(e.key==='Enter'){ U.q=$('#uq').value; U.offset=0; tabUsers(); } };
  $('#uo').onchange = ()=>{ U.order=$('#uo').value; U.offset=0; tabUsers(); };
  $('#uprev').onclick = ()=>{ U.offset=Math.max(0,U.offset-U.limit); tabUsers(); };
  $('#unext').onclick = ()=>{ U.offset+=U.limit; tabUsers(); };
}

/* ============================ 用户详情 ============================ */
async function openUser(uid){
  const {data:u} = await api('/admin/api/user/'+encodeURIComponent(uid));
  const {data:unl} = await api('/admin/api/user/'+encodeURIComponent(uid)+'/unlocks');
  const {data:col} = await api('/admin/api/user/'+encodeURIComponent(uid)+'/collects');
  const {data:wb} = await api('/admin/api/user/'+encodeURIComponent(uid)+'/wing_buffs');
  const {data:fr} = await api('/admin/api/friends?uid='+encodeURIComponent(uid));
  const cur = u.currency||{};

  const curRows = Object.keys(CURRENCY_CN).map(k=>`
    <tr><td>${esc(CURRENCY_CN[k])}<div class="muted mono" style="font-size:11px">${esc(k)}</div></td>
      <td class="mono">${fmtNum(cur[k])}</td>
      <td class="flex">
        <input type="number" value="${Number(cur[k]||0)}" style="width:110px" data-cur="${esc(k)}">
        <button class="b" onclick="setCur('${esc(uid)}','${esc(k)}',this)">改</button>
        <button class="b" onclick="addCur('${esc(uid)}','${esc(k)}',100)">+100</button>
        <button class="b" onclick="addCur('${esc(uid)}','${esc(k)}',-100)">-100</button>
      </td></tr>`).join('');

  modal('账号 '+uid, `
    <div class="flex" style="margin-bottom:12px">
      <span class="tag">设备 ${esc((u.device_id||'').slice(0,20))}</span>
      <span class="tag">注册 ${fmtTime(u.created_at)}</span>
      <span class="tag ${u.visited_home?'ok':''}">到过遇境 ${u.visited_home?'是':'否'}</span>
      <span class="tag">checkpoint ${esc(u.checkpoint)}</span>
    </div>

    <div class="card" style="margin:0 0 12px">
      <h3>蜡烛（users.candles）</h3>
      <div class="flex">
        <input type="number" id="cdl" value="${Number(u.candles||0)}" style="width:140px">
        <button class="b p" onclick="setCandles('${esc(uid)}')">保存</button>
        <button class="b" onclick="addCandles('${esc(uid)}',100)">+100</button>
        <button class="b" onclick="addCandles('${esc(uid)}',-100)">-100</button>
      </div>
    </div>

    <div class="card" style="margin:0 0 12px">
      <h3>高级货币（currency 表）</h3>
      <table>${curRows}</table>
    </div>

    <div class="card" style="margin:0 0 12px">
      <h3>解锁 unlocks <span class="tag">${fmtNum(unl.count)}</span></h3>
      <textarea id="unlAdd" placeholder="每行一个解锁名（也可以粘逗号分隔）"></textarea>
      <div class="flex" style="margin-top:8px">
        <button class="b p" onclick="unlAdd('${esc(uid)}')">添加</button>
        <button class="b" onclick="unlRemoveSel('${esc(uid)}')">删除选中的</button>
        <button class="b d" onclick="clearField('${esc(uid)}','unlocks')">全部清空</button>
        <input id="unlQ" placeholder="过滤" style="width:120px">
        <button class="b" onclick="showUnl('${esc(uid)}')">过滤</button>
      </div>
      <div class="chips" id="unlChips" style="margin-top:8px">
        ${unl.items.map(n=>`<span class="chip" onclick="this.classList.toggle('tag');this.dataset.sel=this.dataset.sel?'':'1'">${esc(n)}</span>`).join('')}
      </div>
    </div>

    <div class="card" style="margin:0 0 12px">
      <h3>收集 collects <span class="tag">${fmtNum(col.count)}</span></h3>
      <textarea id="colAdd" placeholder="每行一个 id（数字）"></textarea>
      <div class="flex" style="margin-top:8px">
        <button class="b p" onclick="colAdd('${esc(uid)}')">添加</button>
        <button class="b d" onclick="clearField('${esc(uid)}','collects')">全部清空</button>
      </div>
      <div class="chips" style="margin-top:8px">${col.items.slice(0,300).map(n=>`<span class="chip">${esc(n)}</span>`).join('')}</div>
    </div>

    <div class="card" style="margin:0 0 12px">
      <h3>光翼 wing_buffs <span class="tag">${fmtNum(wb.count)}</span></h3>
      <textarea id="wbAdd" placeholder="每行一个光翼名"></textarea>
      <div class="flex" style="margin-top:8px">
        <button class="b p" onclick="wbAdd('${esc(uid)}')">添加</button>
        <button class="b d" onclick="clearField('${esc(uid)}','wing_buffs')">全部清空</button>
      </div>
      <div class="chips" style="margin-top:8px">${wb.items.map(n=>`<span class="chip">${esc(n)}</span>`).join('')}</div>
    </div>

    <div class="card" style="margin:0 0 12px">
      <h3>好友 <span class="tag">${fmtNum((fr||[]).length)}</span></h3>
      <div class="scroll" style="max-height:220px"><table>
        <thead><tr><th>好友 ID</th><th>昵称</th><th>关系等级</th><th>能力</th><th>操作</th></tr></thead>
        <tbody>${(fr||[]).map(f=>`<tr>
          <td class="mono">${esc(f.friend_id)}</td>
          <td>${f.nickname?'<span class="tag ok">'+esc(f.nickname)+'</span>':'<span class="tag warn">无名字</span>'}</td>
          <td>${fmtNum(f.relationship_level)}</td><td>${fmtNum(f.abilities)}</td>
          <td class="flex">
            <button class="b" onclick="editNick('${esc(uid)}','${esc(f.friend_id)}','${esc(f.nickname)}')">改名</button>
            <button class="b" onclick="reroll('${esc(uid)}','${esc(f.friend_id)}')">随机</button>
            <button class="b d" onclick="rmFriend('${esc(uid)}','${esc(f.friend_id)}')">解除</button>
          </td></tr>`).join('') || '<tr><td colspan="5" class="muted">没有好友</td></tr>'}</tbody>
      </table></div>
    </div>

    <div class="card" style="margin:0">
      <h3>危险操作</h3>
      <div class="flex">
        <button class="b d" onclick="delUser('${esc(uid)}')">删除这个账号（连同好友/货币等）</button>
      </div>
    </div>
  `, null, null);
}

async function setCandles(uid){
  const v = Number($('#cdl').value);
  await api('/admin/api/user/'+encodeURIComponent(uid)+'/set_candles', {method:'POST', body:{value:v}});
  toast('蜡烛已改为 '+v, 'ok'); openUser(uid);
}
async function addCandles(uid, d){
  const cur = Number($('#cdl').value)||0;
  await setCandles2(uid, Math.max(0, cur+d));
}
async function setCandles2(uid, v){
  await api('/admin/api/user/'+encodeURIComponent(uid)+'/set_candles', {method:'POST', body:{value:v}});
  toast('蜡烛已改为 '+v, 'ok'); openUser(uid);
}
async function setCur(uid, field, btn){
  const inp = btn.parentElement.querySelector('input[data-cur]');
  const v = Number(inp.value);
  await api('/admin/api/user/'+encodeURIComponent(uid)+'/set_currency', {method:'POST', body:{field:field, value:v}});
  toast(CURRENCY_CN[field]+' 已改为 '+v, 'ok');
}
async function addCur(uid, field, d){
  const r = await api('/admin/api/user/'+encodeURIComponent(uid)+'/add_currency',
                      {method:'POST', body:{field:field, delta:d}});
  toast(CURRENCY_CN[field]+' → '+r.value, 'ok'); openUser(uid);
}
async function unlAdd(uid){
  const names = $('#unlAdd').value;
  if(!names.trim()) return toast('先填要添加的解锁名','err');
  const r = await api('/admin/api/user/'+encodeURIComponent(uid)+'/unlocks/add',
                      {method:'POST', body:{names:names}});
  toast('已添加 '+r.data.added+' 个解锁', 'ok'); openUser(uid);
}
function _selectedChips(sel){
  return Array.from(document.querySelectorAll(sel+' .chip[data-sel="1"]')).map(e=>e.textContent);
}
async function unlRemoveSel(uid){
  const names = _selectedChips('#unlChips');
  if(!names.length) return toast('先点选要删的解锁（点一下变灰）','err');
  const r = await api('/admin/api/user/'+encodeURIComponent(uid)+'/unlocks/remove',
                      {method:'POST', body:{names:names}});
  toast('已删除 '+r.data.removed+' 个', 'ok'); openUser(uid);
}
async function showUnl(uid){
  const q = $('#unlQ').value;
  const {data} = await api('/admin/api/user/'+encodeURIComponent(uid)+'/unlocks?q='+encodeURIComponent(q));
  $('#unlChips').innerHTML = data.items.map(n=>`<span class="chip" onclick="this.dataset.sel=this.dataset.sel?'':'1';this.classList.toggle('tag')">${esc(n)}</span>`).join('');
}
async function colAdd(uid){
  const ids = $('#colAdd').value;
  if(!ids.trim()) return toast('先填要添加的 id','err');
  const r = await api('/admin/api/user/'+encodeURIComponent(uid)+'/collects/add',
                      {method:'POST', body:{ids:ids}});
  toast('已添加 '+r.data.added+' 个收集', 'ok'); openUser(uid);
}
async function wbAdd(uid){
  const names = $('#wbAdd').value;
  if(!names.trim()) return toast('先填要添加的光翼名','err');
  const r = await api('/admin/api/user/'+encodeURIComponent(uid)+'/wing_buffs/add',
                      {method:'POST', body:{names:names}});
  toast('已添加 '+r.data.added+' 个光翼', 'ok'); openUser(uid);
}
function clearField(uid, field){
  modal('确认清空', '<p>将把该账号的 <b>'+esc(field)+'</b> 全部清空，<b>不可撤销</b>。</p>'+
        '<p class="muted">确认请在下框输入 CLEAR：</p><input id="cfm" style="width:100%">',
    async ()=>{
      const c = $('#cfm').value.trim();
      await api('/admin/api/user/'+encodeURIComponent(uid)+'/clear',
                {method:'POST', body:{field:field, confirm:c}});
      toast('已清空 '+field, 'ok'); openUser(uid);
    }, '清空');
}
function delUser(uid){
  modal('删除账号', '<p>将删除 <b class="mono">'+esc(uid)+'</b> 及其好友/关系/货币/收件箱，<b>不可撤销</b>。</p>'+
        '<p class="muted">确认请在下框输入 DELETE：</p><input id="cfm" style="width:100%">',
    async ()=>{
      const c = $('#cfm').value.trim();
      const r = await api('/admin/api/user/'+encodeURIComponent(uid)+'/delete',
                          {method:'POST', body:{confirm:c}});
      toast('已删除，影响: '+JSON.stringify(r.data), 'ok'); closeModal(); tabUsers();
    }, '删除');
}
function editNick(uid, fid, cur){
  modal('改好友昵称', '<p class="muted mono">'+esc(fid)+'</p>'+
        '<input id="nn" style="width:100%" value="'+esc(cur)+'" maxlength="190">',
    async ()=>{
      const v = $('#nn').value;
      await api('/admin/api/friend/nickname', {method:'POST', body:{uid:uid, friend_id:fid, nickname:v}});
      toast('昵称已改为「'+v+'」','ok'); openUser(uid);
    }, '保存');
}
async function reroll(uid, fid){
  const r = await api('/admin/api/friend/reroll', {method:'POST', body:{uid:uid, friend_id:fid}});
  toast('随机到「'+r.data.nickname+'」','ok'); openUser(uid);
}
function rmFriend(uid, fid){
  modal('解除好友', '<p>解除 <b class="mono">'+esc(uid)+'</b> 与 <b class="mono">'+esc(fid)+'</b> 的双向关系？</p>',
    async ()=>{
      await api('/admin/api/friend/remove', {method:'POST', body:{a:uid, b:fid}});
      toast('已解除','ok'); openUser(uid);
    }, '解除');
}

/* ============================ 好友关系 ============================ */
async function tabFriends(){
  $('#title').textContent='好友关系';
  view.innerHTML = `
    <div class="card">
      <h3>直接加好友（与游戏内同一条逻辑：friends + friendships + 星座页）</h3>
      <div class="flex">
        <input id="fa" placeholder="A 的 User ID" style="min-width:280px">
        <input id="fb" placeholder="B 的 User ID" style="min-width:280px">
        <input id="fn" placeholder="昵称（留空 = 从名字池随机）" style="min-width:180px">
        <button class="b p" id="fadd">建立关系</button>
      </div>
      <p class="muted" style="margin:10px 0 0">
        留空昵称时会用「好友默认昵称池」随机取一个，并写进 friends.nickname 与
        friendships.custom_name（改 config/friend_name_pool.json 即可换名单）。</p>
    </div>
    <div class="card">
      <h3>查某人的好友</h3>
      <div class="flex">
        <input id="fq" placeholder="User ID" style="min-width:320px">
        <button class="b" id="fqb">查询</button>
      </div>
      <div id="flist" style="margin-top:12px"></div>
    </div>
    <div class="card">
      <h3>随机名字预览</h3>
      <div class="flex"><button class="b" id="rnd">再来 20 个</button></div>
      <div class="chips" id="rndBox" style="margin-top:10px"></div>
    </div>`;
  $('#fadd').onclick = async ()=>{
    try{
      const r = await api('/admin/api/friend/add', {method:'POST',
        body:{a:$('#fa').value.trim(), b:$('#fb').value.trim(), nickname:$('#fn').value.trim()}});
      toast('已建立关系（新建 '+ (r.data.created_rows||[]).length +' 行）','ok');
      $('#fq').value = $('#fa').value.trim(); $('#fqb').click();
    }catch(e){ toast(e.message,'err'); }
  };
  $('#fqb').onclick = async ()=>{
    const uid = $('#fq').value.trim();
    if(!uid) return toast('填一个 User ID','err');
    const {data} = await api('/admin/api/friends?uid='+encodeURIComponent(uid));
    $('#flist').innerHTML = '<div class="scroll" style="max-height:44vh"><table>'+
      '<thead><tr><th>好友 ID</th><th>昵称</th><th>关系等级</th><th>能力</th><th>创建</th><th></th></tr></thead><tbody>'+
      ((data||[]).map(f=>`<tr>
        <td class="mono">${esc(f.friend_id)}</td>
        <td>${f.nickname?'<span class="tag ok">'+esc(f.nickname)+'</span>':'<span class="tag warn">无名字</span>'}</td>
        <td>${fmtNum(f.relationship_level)}</td><td>${fmtNum(f.abilities)}</td>
        <td class="muted">${fmtTime(f.created_at)}</td>
        <td class="flex">
          <button class="b" onclick="editNick('${esc(uid)}','${esc(f.friend_id)}','${esc(f.nickname)}')">改名</button>
          <button class="b" onclick="reroll('${esc(uid)}','${esc(f.friend_id)}')">随机</button>
          <button class="b d" onclick="rmFriend('${esc(uid)}','${esc(f.friend_id)}')">解除</button>
        </td></tr>`).join('') || '<tr><td colspan="6" class="muted">没有好友</td></tr>')+
      '</tbody></table></div>';
  };
  const loadRnd = async ()=>{
    const {data} = await api('/admin/api/name_pool/random?n=20');
    $('#rndBox').innerHTML = data.map(n=>'<span class="chip">'+esc(n)+'</span>').join('');
  };
  $('#rnd').onclick = loadRnd; loadRnd();
}

/* ============================ 动态 / 邀请 ============================ */
async function tabFeed(){
  $('#title').textContent='动态 / 邀请';
  const {data:f} = await api('/admin/api/feed?limit=100');
  const {data:inv} = await api('/admin/api/invites');
  view.innerHTML = `
    <div class="card">
      <h3>社交动态 social_feed_post</h3>
      ${f.error? '<div class="err mono">'+esc(f.error)+'</div>':''}
      <div class="scroll" style="max-height:46vh"><table>
        <thead><tr><th>id</th><th>玩家</th><th>池</th><th>内容</th><th>赞</th><th>时间</th><th></th></tr></thead>
        <tbody>${(f.rows||[]).map(p=>`<tr>
          <td class="mono">${esc(p.id)}</td>
          <td class="mono">${esc((p.user_id||'').slice(0,12))}</td>
          <td>${esc(p.pool_name||p.pool_type||'')}</td>
          <td style="max-width:280px">${esc((p.message||'').slice(0,80))}</td>
          <td>${fmtNum(p.likes_count)}</td><td class="muted">${fmtTime(p.created_at)}</td>
          <td><button class="b d" onclick="delFeed(${Number(p.id)})">删</button></td>
        </tr>`).join('') || '<tr><td colspan="7" class="muted">没有动态</td></tr>'}</tbody>
      </table></div>
    </div>
    <div class="card">
      <h3>待接受邀请 pending_invites</h3>
      <div class="scroll" style="max-height:36vh"><table>
        <thead><tr><th>token</th><th>发起</th><th>目标</th><th>昵称</th><th>时间</th><th></th></tr></thead>
        <tbody>${(inv||[]).map(v=>`<tr>
          <td class="mono">${esc((v.token_id||'').slice(0,10))}…</td>
          <td class="mono">${esc((v.from_user||'').slice(0,12))}</td>
          <td class="mono">${esc((v.to_user||'').slice(0,12))}</td>
          <td>${esc(v.nickname||'')}</td><td class="muted">${fmtTime(v.created_at)}</td>
          <td><button class="b d" onclick="delInvite('${esc(v.token_id)}')">删</button></td>
        </tr>`).join('') || '<tr><td colspan="6" class="muted">没有待接受邀请</td></tr>'}</tbody>
      </table></div>
    </div>`;
}
async function delFeed(id){
  await api('/admin/api/feed/delete', {method:'POST', body:{id:id}});
  toast('已删除动态 '+id,'ok'); tabFeed();
}
async function delInvite(t){
  await api('/admin/api/invites/delete', {method:'POST', body:{token_id:t}});
  toast('已删除邀请','ok'); tabFeed();
}

/* ============================ 工具 / 日志 ============================ */
async function tabTools(){
  $('#title').textContent='工具 / 日志';
  const {data:h} = await api('/admin/api/health');
  const {data:a} = await api('/admin/api/audit');
  view.innerHTML = `
    <div class="card">
      <h3>数据库</h3>
      <div class="flex" style="margin-bottom:10px">
        <span class="tag ${h.db && h.db.ok?'ok':'err'}">${esc((h.db&&h.db.mode)||'')} · ${h.db&&h.db.ok?'已连接':'连不上'}</span>
        <span class="muted mono">${esc((h.db&&h.db.host)||'')}</span>
        ${h.db && h.db.error?'<span class="err mono">'+esc(h.db.error)+'</span>':''}
      </div>
      <div class="grid g4">
        ${(h.tables||[]).map(t=>'<div class="kpi"><div class="n" style="font-size:15px" class="mono">'+
          esc(t.t||t.table_name||'')+'</div><div class="l">'+(t.n!=null?fmtNum(t.n):'')+'</div></div>').join('')}
      </div>
    </div>
    <div class="card">
      <h3>操作日志 logs/admin_audit.jsonl</h3>
      <div class="scroll" style="max-height:50vh"><table>
        <thead><tr><th>时间</th><th>操作者</th><th>IP</th><th>动作</th><th>目标</th><th>详情</th></tr></thead>
        <tbody>${(a||[]).map(r=>`<tr>
          <td class="muted">${fmtTime(r.t)}</td><td>${esc(r.user)}</td>
          <td class="mono muted">${esc(r.ip)}</td><td>${esc(r.action)}</td>
          <td class="mono">${esc((r.target||'').slice(0,18))}</td>
          <td class="mono muted" style="max-width:360px">${esc(JSON.stringify(r.detail||{}).slice(0,160))}</td>
        </tr>`).join('') || '<tr><td colspan="6" class="muted">还没有操作记录</td></tr>'}</tbody>
      </table></div>
    </div>`;
}

/* ============================ 路由 ============================ */
const TABS = {overview:tabOverview, users:tabUsers, friends:tabFriends, feed:tabFeed, tools:tabTools};
async function go(tab){
  document.querySelectorAll('#nav button').forEach(b=>b.classList.toggle('on', b.dataset.tab===tab));
  $('#stamp').textContent = '刷新于 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
  try{ await TABS[tab](); }
  catch(e){ view.innerHTML = '<div class="card err">加载失败: '+esc(e.message)+'</div>'; }
}
document.querySelectorAll('#nav button').forEach(b=> b.onclick = ()=>go(b.dataset.tab));
$('#logout').onclick = async e=>{ e.preventDefault();
  await fetch('/admin/api/logout',{method:'POST'}); location.href='/admin/'; };

(async ()=>{
  try{
    const {user} = await api('/admin/api/whoami');
    $('#whoami').textContent = '已登录: '+user;
  }catch(e){ return; }
  go('overview');
})();
</script>
</body></html>
"""
