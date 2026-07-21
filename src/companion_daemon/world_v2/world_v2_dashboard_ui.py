"""Local-first, read-only browser shell for the World v2 Dashboard.

The module contains no World/Engine reader and no deployment registry.  It
renders static HTML/JavaScript that can consume only the already-redacted
Dashboard and Room DTO endpoints.  Authentication and host availability stay
in the ASGI composition.  The loopback daemon panel opens directly; the
legacy session helper remains available for non-loopback compatibility and
privileged DTO routes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time


DASHBOARD_SESSION_COOKIE = "girl_agent_v2_dashboard_session"
DASHBOARD_SESSION_TTL_SECONDS = 8 * 60 * 60


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


class DashboardSessionCodec:
    """Issue instance-bound sessions without placing the operator token in a cookie."""

    def __init__(self, *, operator_token: str, instance_secret: bytes) -> None:
        if not operator_token.strip():
            raise ValueError("dashboard session requires a configured operator token")
        if len(instance_secret) < 32:
            raise ValueError("dashboard session instance secret must be at least 32 bytes")
        self._key = hmac.new(
            instance_secret,
            b"world-v2-dashboard-session\0" + operator_token.encode("utf-8"),
            hashlib.sha256,
        ).digest()

    def issue(self, *, now: int | None = None) -> str:
        issued_at = int(time.time()) if now is None else now
        expires_at = issued_at + DASHBOARD_SESSION_TTL_SECONDS
        nonce = _b64(secrets.token_bytes(18))
        body = f"v1.{expires_at}.{nonce}"
        signature = _b64(hmac.new(self._key, body.encode("ascii"), hashlib.sha256).digest())
        return f"{body}.{signature}"

    def verify(self, value: str | None, *, now: int | None = None) -> bool:
        if not value or len(value) > 512:
            return False
        parts = value.split(".")
        if len(parts) != 4 or parts[0] != "v1":
            return False
        try:
            expires_at = int(parts[1])
        except ValueError:
            return False
        current = int(time.time()) if now is None else now
        if expires_at <= current or expires_at > current + DASHBOARD_SESSION_TTL_SECONDS:
            return False
        body = ".".join(parts[:3])
        expected = _b64(hmac.new(self._key, body.encode("ascii"), hashlib.sha256).digest())
        return hmac.compare_digest(expected, parts[3])


LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>World v2 Dashboard 登录</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#d9cdbc;color:#3f342d;font-family:"PingFang SC",system-ui,sans-serif}
main{width:min(420px,calc(100% - 32px));padding:28px;background:#f7eedf;border:3px solid #684f42;box-shadow:6px 6px 0 #b79c84}
h1{font-size:20px}label,input,button{display:block;width:100%}input,button{margin-top:10px;padding:11px;font:inherit;box-sizing:border-box}button{background:#557f78;color:white;border:0}p{line-height:1.6;font-size:13px}
</style></head><body><main><h1>知栀的小屋 · World v2</h1>
<p>请输入本机配置的 operator token。凭证只通过本次 POST 提交，不会写入 URL、页面脚本或浏览器存储。</p>
<form method="post" action="/world-v2/dashboard/session" autocomplete="off">
<label for="operator-token">Operator token</label><input id="operator-token" name="operator_token" type="password" required autocomplete="current-password">
<button type="submit">进入只读 Dashboard</button></form></main></body></html>"""


UNAVAILABLE_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>World v2 Dashboard unavailable</title></head><body>
<main><h1>World v2 Dashboard unavailable</h1><p>只读 World v2 host 尚未初始化。不会回退到旧运行时。</p></main>
</body></html>"""


DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>知栀的小屋 · World v2</title><style>
:root{font-family:"PingFang SC",system-ui,sans-serif;color:#3f342d;background:#d9cdbc}*{box-sizing:border-box}body{margin:0}.bar{padding:16px 24px;background:#4d3b34;color:#fff8ea;display:flex;justify-content:space-between;align-items:center}.bar h1{font-size:18px;margin:0}.bar form{margin:0}.bar button{background:#6f8e84;color:white;border:1px solid #d6c7a5;padding:7px 10px}.wrap{max-width:1180px;margin:auto;padding:22px;display:grid;grid-template-columns:minmax(0,1.5fr) minmax(280px,.7fr);gap:18px}.room,.panel{background:#f7eedf;border:3px solid #684f42;box-shadow:5px 5px 0 #b79c84}.room{position:relative;overflow:hidden}.room iframe{display:block;width:100%;aspect-ratio:7/4;border:0;background:#211b1a;image-rendering:pixelated;pointer-events:none}.room-edit{position:absolute;z-index:1;top:10px;right:10px;padding:7px 10px;border:1px solid #fff3d5;border-radius:6px;background:rgba(77,59,52,.88);color:#fff8ea;font-size:12px;text-decoration:none;box-shadow:0 2px 8px rgba(0,0,0,.28)}.room-edit:focus-visible{outline:3px solid #e8c568;outline-offset:2px}.panel{padding:16px}.value{font-size:20px;color:#557f78}.muted{color:#80685b;font-size:12px}.agenda{padding:0;list-style:none}.agenda li{padding:9px 0;border-bottom:1px solid #decfbd}.error{color:#9c4545}@media(max-width:760px){.wrap{grid-template-columns:1fr}}
</style></head><body><header class="bar"><h1>知栀的小屋 · World v2</h1></header>
<main class="wrap"><section class="room"><iframe id="roomVisual" src="/pixel-home/index.html?embed=1" title="知栀的小屋日常画面" aria-label="知栀的小屋日常画面" scrolling="no"></iframe><a class="room-edit" href="/pixel-home/index.html?edit=1" target="_blank" rel="noopener" aria-label="在独立页面编辑小屋">✎ 编辑小屋</a></section>
<aside><section class="panel"><h2>她现在在做什么</h2><div id="lifeNow" class="value">读取中</div><p id="lifeDetail" class="muted"></p><p id="lifeNext" class="muted"></p><p id="lifeLast" class="muted"></p><p id="lifeMood" class="muted"></p></section><section class="panel"><h2>日历 · 未来几天</h2><ul id="calendar" class="agenda"></ul><p id="calendarEmpty" class="muted"></p></section><section class="panel"><h2>今天的生活</h2><ul id="today" class="agenda"></ul><p id="todayEmpty" class="muted"></p></section><section class="panel"><h2>今天的经历</h2><ul id="experiences" class="agenda"></ul><p id="experiencesEmpty" class="muted"></p></section><section class="panel"><h2>情绪 · 逐条</h2><ul id="affectEpisodes" class="agenda"></ul><p id="affectEpisodesEmpty" class="muted"></p></section><section class="panel"><h2>情绪变化阶段</h2><ul id="changePhases" class="agenda"></ul><p id="changePhasesEmpty" class="muted"></p></section><section class="panel"><h2>她记住的你 · 用户事实</h2><ul id="userFacts" class="agenda"></ul><p id="userFactsEmpty" class="muted"></p></section><section class="panel"><h2>记忆</h2><ul id="memories" class="agenda"></ul><p id="memoriesEmpty" class="muted"></p></section><section class="panel"><h2>私下印象</h2><ul id="impressions" class="agenda"></ul><p id="impressionsEmpty" class="muted"></p></section><section class="panel"><h2>憧憬</h2><ul id="aspirations" class="agenda"></ul><p id="aspirationsEmpty" class="muted"></p></section><section class="panel"><h2>和你的关系</h2><ul id="userRelationship" class="agenda"></ul><p id="userRelationshipEmpty" class="muted"></p></section><section class="panel"><h2>她与身边人</h2><ul id="npcRelationships" class="agenda"></ul><p id="npcRelationshipsEmpty" class="muted"></p></section><section class="panel"><h2>内在机制</h2><ul id="mechanisms" class="agenda"></ul><p id="status" class="muted">只读 · QQ 世界</p></section></aside></main>
<script src="/world-v2/dashboard/app.js" defer></script></body></html>"""


DASHBOARD_APP_JS = """'use strict';
const text=(id,value)=>{document.getElementById(id).textContent=value;};
const ACTIVITY_LABELS={
  'routine.morning_settle':'早上收拾洗漱',
  'sleep.prepare_for_bed':'睡前收拾，准备休息',
  'sleep.late_wind_down':'深夜收心，准备睡了',
  'sleep.early_morning_wake':'清晨早醒，还没起',
  'study.focused_reading':'专注读书',
  'meal.make_drink':'弄点吃的喝的',
  'creative.edit_photo_notes':'整理照片和随手笔记',
  'commute.short_walk':'出门走一小段',
  'household.tidy_small_things':'收拾屋里的小东西',
  'recovery.quiet_rest':'安静歇一会儿',
  'leisure.digital_browse':'窝着刷手机',
  'social.literature_reading_list':'忙文学社书单的事',
  'social.literature_club_meetup':'和范予安约了文学社碰头',
  'commute.lakeside_walk':'去丽娃河边走一段',
  'creative.photo_batch_organize':'集中整理一批照片',
  'study.reading_notes':'写读书笔记',
  'study.attend_class':'去教学楼上课',
  'study.essay_writing':'赶论文',
  'study.evening_self_study':'晚上在图书馆自习',
  'study.seminar_room_session':'预约了研讨间整理思路',
  'creative.write_essay':'写随笔',
  'creative.film_scan_sort':'翻扫整理胶片',
  'creative.write_diary':'写日记',
  'creative.bund_night_shoot':'去外滩拍夜景',
  'household.do_laundry':'洗衣服',
  'errand.pick_up_parcel':'去驿站取快递',
  'errand.buy_fruit':'买水果和零嘴',
  'errand.print_shop':'去打印店',
  'meal.canteen_meal':'去食堂吃饭',
  'meal.dorm_cooking':'在宿舍煮饭试新菜',
  'recovery.evening_stretch':'睡前拉伸',
  'recovery.window_daydream':'靠窗发呆',
  'sleep.afternoon_nap':'午睡',
  'leisure.podcast_listen':'听播客',
  'leisure.browse_book_stall':'逛旧书摊',
  'leisure.book_market_hunt':'去二手书市淘书',
  'social.family_call':'给家里打电话',
  'social.roommate_chat':'和林晚闲聊',
  'social.literature_club_admin':'处理文学社事务',
  'social.exhibition_outing':'和范予安去看展',
  'family.bookstore_help':'回嘉兴帮家里看店',
  'shared.movie_call':'和你连麦一起看电影',
};
const activityLabel=kind=>ACTIVITY_LABELS[kind]||kind||'未知活动';
const STATUS_LABELS={planned:'已计划',active:'进行中',paused:'暂停',completed:'完成',abandoned:'放弃'};
const statusLabel=value=>STATUS_LABELS[value]||value||'';
const fmtClock=value=>{try{return new Intl.DateTimeFormat('zh-CN',{hour:'2-digit',minute:'2-digit'}).format(new Date(value));}catch{return '';}};
const fmtDay=value=>{try{return new Intl.DateTimeFormat('zh-CN',{weekday:'short',month:'numeric',day:'numeric'}).format(new Date(value));}catch{return '';}};
const pct=bp=>typeof bp==='number'?`${Math.round(bp/100)}%`:'';
const byOpensAt=(a,b)=>new Date(a.window_opens_at||0)-new Date(b.window_opens_at||0);
const PHASE_LABELS={departing:'刚陷入',holding:'持续中',returning:'正在走出',recovering:'刚平复'};
const EPISODE_STATUS_LABELS={active:'活跃',resolved:'已平复',superseded:'已被替代'};
const MEANING_LABELS={ordinary:'普通往来',care:'被关心',support:'被支持',shared_joy:'共同的开心',goal_progress:'事情有进展',uncertainty:'不确定',misunderstanding:'误会',disappointment:'失望',dismissal:'被敷衍',boundary_violation:'越界',dehumanization:'不被当人',coercion:'被强迫',control_pressure:'被控制',betrayal:'被辜负',loss:'失去',user_withdrawing:'对方在退开',user_confused:'对方困惑',repair_attempt:'想修复',npc_conflict:'与人摩擦'};
const CUE_LABELS={identity:'身份',relationship:'关系',boundary:'边界',unfinished_business:'未完成的事',repeated_pattern:'重复的模式',future_utility:'以后有用',emotional_residue:'情绪残留',world_continuity:'生活连续性'};
const SALIENCE_LABELS={autobiographical_relevance:'自传相关',relationship_relevance:'关系相关',emotional_residue:'情绪残留',unfinished_business:'未完成',recurrence:'反复出现',novelty:'新鲜',future_utility:'以后有用',world_continuity:'生活连续'};
const SOURCE_KIND_LABELS={fact:'来自事实',experience:'来自经历'};
const ASPIRATION_STATUS_LABELS={active:'还惦记着',crystallized:'已经写进计划',faded:'慢慢淡了'};
const STAGE_LABELS={stranger:'陌生',acquaintance:'认识了',friend:'朋友',close_friend:'很熟的朋友',ambiguous:'有点暧昧',lover:'恋人'};
const REL_VAR_LABELS={trust_bp:'信任',closeness_bp:'亲近',respect_bp:'尊重',reliability_bp:'可靠',mutuality_bp:'相互',repair_confidence_bp:'修复信心'};
const NPC_NAMES={'literature-fan':'范予安','roommate-lin':'林晚','roommate-qiao':'乔宁','mother-shen':'沈岚','father-shen':'陈远','photography-zhou':'周栩','hometown-xu':'徐青禾'};
// --- pixel-home room bridge -------------------------------------------------
// The embedded /pixel-home iframe renders her room; every life-state poll is
// relayed to it as a versioned postMessage.  bridge.js inside the prototype
// maps the message onto the engine; the room stays fully usable standalone.
const ROOM_SCENE_STATE_TYPE='zhizhi-scene-state';
// Her own dorm room is the only life-state location that maps onto the home
// diorama; every other location_ref means she is out.
const HOME_LOCATION_REF='location:ecnu-dorm-room';
const activityIsAtHome=active=>!active||!active.location_ref||active.location_ref===HOME_LOCATION_REF;
const localHourOf=value=>{const when=value?new Date(value):null;return when&&!Number.isNaN(when.getTime())?when.getHours()+when.getMinutes()/60:null;};
const buildRoomSceneState=(active,logicalTime)=>({
  type:ROOM_SCENE_STATE_TYPE,v:1,
  active:active?{activity_kind:active.activity_kind||null,location_ref:active.location_ref||null}:null,
  at_home:activityIsAtHome(active),
  local_hour:localHourOf(logicalTime),
});
const roomFrame=document.getElementById('roomVisual');
let roomSceneState=null;
let roomClockInitialized=false;
function pushRoomSceneState(){
  if(roomSceneState&&roomFrame&&roomFrame.contentWindow)roomFrame.contentWindow.postMessage(roomSceneState,window.location.origin);
}
function syncRoomClock(){
  // The engine only accepts a start-of-day hour via its ?hour= URL parameter,
  // so the world clock is applied once by reloading the iframe with it.
  if(roomClockInitialized||!roomFrame||!roomSceneState||roomSceneState.local_hour===null)return;
  roomClockInitialized=true;
  roomFrame.src='/pixel-home/index.html?embed=1&hour='+roomSceneState.local_hour.toFixed(2);
}
if(roomFrame)roomFrame.addEventListener('load',pushRoomSceneState);
function fillList(listId,emptyId,rows,emptyText){
  const list=document.getElementById(listId);list.replaceChildren();
  for(const row of rows){const li=document.createElement('li');li.textContent=row;list.appendChild(li);}
  text(emptyId,rows.length?'':emptyText);
}
async function loadLifeState(){
  try{
    const response=await fetch('/world-v2/life-state',{credentials:'same-origin',headers:{Accept:'application/json'}});
    if(!response.ok)throw new Error('life state unavailable');
    const life=await response.json();
    const mech=life.mechanisms||{};
    const situation=mech.current_situation||{};
    const affect=mech.affect||{};
    const active=(situation.active_activities||[])[0];
    roomSceneState=buildRoomSceneState(active||null,situation.logical_time);
    syncRoomClock();
    pushRoomSceneState();
    if(active){
      text('lifeNow',activityLabel(active.activity_kind));
      const since=active.last_transitioned_at?`从 ${fmtClock(active.last_transitioned_at)} 开始`:'';
      const until=active.window_closes_at?`，预计到 ${fmtClock(active.window_closes_at)}`:'';
      text('lifeDetail',`${since}${until}`);
    }else{
      text('lifeNow','这会儿没有安排具体的事');
      text('lifeDetail','空档期：可能在歇着或随便待着。');
    }
    const next=situation.next_planned_activity;
    text('lifeNext',next?`接下来：${activityLabel(next.activity_kind)}（${fmtClock(next.window_opens_at)} 起）`:'接下来暂时没有已确定的安排。');
    const last=situation.last_completed_activity;
    text('lifeLast',last?`刚做完：${activityLabel(last.activity_kind)}（${fmtClock(last.last_transitioned_at)}）`:'');
    const upcoming=(situation.upcoming_activities||[]).slice().sort(byOpensAt);
    fillList('calendar','calendarEmpty',upcoming.map(item=>
      `${fmtDay(item.window_opens_at)} ${fmtClock(item.window_opens_at)} · ${activityLabel(item.activity_kind)} · ${statusLabel(item.status)}`
    ),'接下来几天还没有写进日历的安排。');
    const episodeCount=affect.active_episode_count;
    text('lifeMood',typeof episodeCount==='number'?`情绪线索：${episodeCount} 条进行中 · 世界时间 ${fmtClock(situation.logical_time)}`:'');
    const dayItems=(situation.today_activities||[]).slice().sort(byOpensAt);
    fillList('today','todayEmpty',dayItems.map(item=>
      `${fmtClock(item.window_opens_at)} · ${activityLabel(item.activity_kind)} · ${statusLabel(item.status)}`
    ),'过去一天还没有留下活动记录。');
    const eco2=mech.life_ecology||{};
    fillList('experiences','experiencesEmpty',(eco2.recent_experiences||[]).map(item=>
      `${fmtClock(item.occurred_to)} · ${item.summary_excerpt||'（正文暂不可读）'}`
    ),'最近还没有落定的经历。');
    fillList('affectEpisodes','affectEpisodesEmpty',(affect.episodes||[]).map(item=>{
      const parts=(item.components||[]).map(c=>`${c.label||c.dimension} ${pct(c.intensity_bp)}${c.decaying?'（在消退）':''}`).join('、');
      return `${fmtClock(item.opened_at)} 起 · ${EPISODE_STATUS_LABELS[item.status]||item.status} · ${parts}`;
    }),'现在没有记录在案的情绪片段。');
    fillList('changePhases','changePhasesEmpty',(affect.change_phases||[]).map(item=>
      `${item.prose||`${item.label||item.dimension} · ${PHASE_LABELS[item.phase]||item.phase}`} · ${pct(item.intensity_bp)}`
    ),'情绪都在基线附近，没有明显起落。');
    const memory2=mech.memory||{};
    fillList('userFacts','userFactsEmpty',(memory2.facts||[]).map(item=>
      `${item.value_excerpt||item.predicate_code}（${item.predicate_code} · 置信 ${pct(item.confidence_bp)} · ${fmtDay(item.committed_at)} ${fmtClock(item.committed_at)} 记下）`
    ),'她还没有确认记下关于你的事实。');
    fillList('memories','memoriesEmpty',(memory2.candidates||[]).map(item=>{
      const cue=CUE_LABELS[item.cue_kind]||item.cue_kind;
      const source=(item.source_kinds||[]).map(k=>SOURCE_KIND_LABELS[k]||k).join('/');
      const salience=(item.salience_highlights||[]).map(s=>`${SALIENCE_LABELS[s.dimension]||s.dimension} ${pct(s.bp)}`).join('、');
      return `${item.summary_excerpt||`（${cue}线索）`} · ${cue} · ${source}${salience?` · ${salience}`:''}`;
    }),'还没有留下的记忆候选。');
    const inner=mech.inner||{};
    fillList('impressions','impressionsEmpty',(inner.impressions||[]).map(item=>{
      const meanings=(item.meanings||[]).map(m=>MEANING_LABELS[m]||m).join('、')||'（原始假设不可读）';
      return `她觉得：${meanings} · 把握 ${pct(item.confidence_bp)} · ${fmtDay(item.first_seen)}起`;
    }),'她心里暂时没有挂着的猜测。');
    fillList('aspirations','aspirationsEmpty',(inner.aspirations||[]).map(item=>
      `${item.text} · ${ASPIRATION_STATUS_LABELS[item.status]||item.status} · ${fmtDay(item.planted_at)}种下${item.reinforcement_count?` · 被想起 ${item.reinforcement_count} 次`:''}`
    ),'还没有生根的憧憬。');
    const relationship2=mech.relationship||{};
    const userRows=[];
    if(relationship2.user_state){
      const state=relationship2.user_state;
      userRows.push(`阶段：${STAGE_LABELS[state.stage]||state.stage}${state.last_adjusted_at?`（${fmtDay(state.last_adjusted_at)} 最近调整）`:''}`);
      for(const [k,v] of Object.entries(state.variables||{})){
        userRows.push(`${REL_VAR_LABELS[k]||k}：${pct(v)}`);
      }
    }
    fillList('userRelationship','userRelationshipEmpty',userRows,'和你的关系还没有落进账本的状态。');
    fillList('npcRelationships','npcRelationshipsEmpty',(relationship2.npc_states||[]).map(npc=>
      `${NPC_NAMES[npc.npc_id]||npc.npc_id}：亲近 ${pct(npc.closeness_bp)} · 熟悉 ${pct(npc.familiarity_bp)} · 摩擦 ${pct(npc.friction_bp)} · 一起经历 ${npc.settled_shared_count} 件事${npc.last_shared_at?` · 上次 ${fmtDay(npc.last_shared_at)}`:''}`
    ),'她身边还没有留下相处痕迹的人。');
    const rows=[];
    const eco=mech.life_ecology||{};
    if(eco.plans_by_status){const parts=Object.entries(eco.plans_by_status).map(([k,v])=>`${statusLabel(k)} ${v}`).join(' · ');rows.push(`生活计划：${parts||'无'}`);}
    rows.push(`情绪：${affect.active_episode_count||0} 条进行中 / 共 ${affect.episode_count||0} 条 · 评估 ${affect.appraisal_count||0} 次`);
    const memory=mech.memory||{};
    rows.push(`记忆：事实 ${memory.fact_count||0} · 候选 ${memory.active_candidate_count||0}/${memory.candidate_count||0}`);
    const relationship=mech.relationship||{};
    rows.push(`关系：状态 ${relationship.state_count||0} · 信号 ${relationship.signal_count||0} · 调整 ${relationship.adjustment_count||0}`);
    const npc=mech.npc||{};
    rows.push(`NPC：${npc.registered_count||0} 位注册 · 世界评估 ${npc.world_appraisal_count||0} 次`);
    const activity=life.world_activity||{};
    rows.push(`世界事件：${activity.life_event_count||0} · 发生 ${activity.occurrence_count||0} · 经历 ${activity.experience_count||0}`);
    const mechanisms=document.getElementById('mechanisms');mechanisms.replaceChildren();
    for(const row of rows){const li=document.createElement('li');li.textContent=row;mechanisms.appendChild(li);}
    text('status',`只读 · QQ 世界 · 适配器 ${life.adapter_status||'unknown'}`);
    document.getElementById('status').classList.remove('error');
  }catch(error){
    text('lifeNow','QQ 世界暂时读不到');
    text('lifeDetail','适配器可能正在重启，稍后会自动重试。');
    text('status','QQ 世界适配器暂时不可达 · 稍后自动重试');
    document.getElementById('status').classList.add('error');
  }
}
loadLifeState();
setInterval(loadLifeState,30000);
"""


__all__ = [
    "DASHBOARD_APP_JS",
    "DASHBOARD_HTML",
    "DASHBOARD_SESSION_COOKIE",
    "DASHBOARD_SESSION_TTL_SECONDS",
    "DashboardSessionCodec",
    "LOGIN_HTML",
    "UNAVAILABLE_HTML",
]
