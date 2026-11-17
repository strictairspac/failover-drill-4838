import {store} from '../stores/entities.js';
import {session} from '../stores/session.js';
import {configurationsAPI} from '../api/entities.js';
import {table} from '../components/common/table.js';
import {configBadge} from '../components/common/status.js';
import {esc,formObject} from '../utils/html.js';
import {action,message} from '../hooks/action.js';

const active = () => store.configurations.find(c => c.status === 'active');
const preparing = () => store.configurations.find(c => c.status === 'preparing');
const memberNames = cfg => (cfg?.members || []).map(m => esc(m.name)).join('、') || '—';

function changeCard(){
 const prep = preparing();
 if(!prep) return '<p class="notice">当前没有准备中的组态。可在下方发起一次换组，旧组会在追平期间继续可用。</p>';
 const blockers = prep.blocked_reason;
 const rows = prep.members.map(m => [esc(m.name), esc(m.role || '—'), m.current_log_position ?? '—', m.caught_up ? '已追平' : '追平中', esc(m.caught_up_at ? new Date(m.caught_up_at).toLocaleString('zh-CN') : '—')]);
 return `<div class="section-title"><h2>准备中的组态 <small>变更 #${prep.id}</small></h2>${configBadge('preparing')}</div>
 <p>发起时冻结原主日志位置 <b>${prep.target_log_position}</b>（${esc(prep.target_primary_name)}）。新节点追平前只做演练，不计入生效票；旧组（第 ${active()?.generation ?? '—'} 代）仍可演练。</p>
 ${table(['成员','角色','当前日志','追平发起时原主日志','追平时间'],rows)}
 <div class="notice ${blockers ? 'error' : ''}">${blockers ? '生效受阻：' + esc(blockers) : '全部成员已追平，可申请生效。生效与旧组替代在同一事务提交。'}</div>
 <div class="toolbar"><button id="activate" ${session.role !== 'admin' ? 'disabled' : ''}>${blockers ? '重新检查并生效' : '让新组态生效 →'}</button><button class="secondary" id="cancel" ${session.role !== 'admin' ? 'disabled' : ''}>放弃本次变更</button><button class="secondary" id="refresh">刷新追平状态</button></div>
 <p class="checksum">生效请求可安全重试：同一次变更重复提交不会产生新的代际或重复生效。</p>`;
}

function historyRows(){
 return store.configurations.map(c => [`#${c.id}`, c.generation ?? '—', configBadge(c.status), esc(c.reason), memberNames(c), `${c.catch_up.caught}/${c.catch_up.total}`, esc(c.activated_at ? new Date(c.activated_at).toLocaleString('zh-CN') : '—'), c.blocked_reason ? esc(c.blocked_reason) : '—']);
}

function form(){
 const cfg = active();
 const defaultIds = new Set((cfg?.members || []).map(m => m.node_id));
 return `<form id="config-form">
 <fieldset class="member-picker"><legend>新组态投票成员</legend>
 ${store.nodes.map(n => `<label class="check"><input type="checkbox" name="member" value="${n.id}" ${defaultIds.has(n.id) ? 'checked' : ''} ${preparing() ? 'disabled' : ''}> ${esc(n.name)} <small>${esc(n.role)} · 日志 ${n.log_position}</small></label>`).join('')}
 </fieldset>
 <label>变更原因<input name="reason" value="换班：批量替换投票成员" required minlength="2" maxlength="200"></label>
 <label>幂等请求号（同次重试不会重复生效）<input name="request_id" value="config-${Date.now()}" required minlength="4" maxlength="80"></label>
 <button type="submit" ${preparing() ? 'disabled' : ''}>准备新组态（旧组继续可用）</button>
 </form>`;
}

export function configurationsPage(){
 const cfg = active();
 return `<div class="section-title"><h2>现行组态 ${cfg ? `第 ${cfg.generation} 代` : ''}</h2><span>换班时整套替换，避免逐个改投票资格改变多数派</span></div>
 ${cfg ? `${table(['成员','角色','生效时日志','追平时间'],cfg.members.map(m => [esc(m.name), esc(m.role || '—'), m.log_position_at_activation ?? '—', esc(m.caught_up_at ? new Date(m.caught_up_at).toLocaleString('zh-CN') : '—')]))}
 <p class="big-note">过渡期判定须新旧两组分别达到多数派，并给出同一候选。</p>` : '<p class="notice error">尚无生效组态。管理员需先为现有投票节点建立初始组态。</p>'}
 ${changeCard()}
 <section class="split"><div><h2>发起换组</h2><p>批量替换成员而不逐个改投票开关：提交后先进入“准备中”，旧组保持生效并可继续演练。全部新成员追平发起时原主日志后，才能整体生效；新旧组不会各自放行冲突候选。</p><p>节点巡检仍可更新任期、日志与可达状态，但投票资格只能通过本流程变更。</p></div>${session.role === 'admin' ? form() : '<p class="readonly">发起与生效组态变更需要管理员账号；操作员可继续提交演练，观察员可复核历史。</p>'}</section>
 <div class="section-title"><h2>组态变更历史</h2><span>保留冻结证据，可复核每次准备、生效与受阻原因</span></div>
 ${table(['变更','代际','状态','原因','成员','追平','生效时间','受阻原因'],historyRows())}`;
}

export function bindConfigurations(render){
 document.querySelector('#refresh')?.addEventListener('click',render);
 document.querySelector('#activate')?.addEventListener('click',async e=>{
  const prep = preparing(); if(!prep) return;
  e.target.disabled = true;
  try {
   const out = await configurationsAPI.activate(prep.id);
   await render();
   if(out.activated) message(out.changed ? '新组态已生效，旧组态已替代。' : '该组态此前已生效，本次重试未重复生效。');
   else message('生效受阻：' + (out.blocked_reason || '成员尚未追平'), true);
  } catch(err) { message(err.message, true); e.target.disabled = false; }
 });
 document.querySelector('#cancel')?.addEventListener('click',async e=>{
  const prep = preparing(); if(!prep) return;
  e.target.disabled = true;
  try { await configurationsAPI.cancel(prep.id); await render(); message('已放弃准备中的组态变更，现行组态未受影响。'); }
  catch(err) { message(err.message, true); e.target.disabled = false; }
 });
 action(document.querySelector('#config-form'), async form => {
  const data = formObject(form);
  const member_ids = [...form.querySelectorAll('input[name=member]:checked')].map(i => Number(i.value));
  if(!member_ids.length) throw new Error('请至少选择一个成员节点');
  const out = await configurationsAPI.create({member_ids, reason: data.reason, request_id: data.request_id});
  await render();
  return out.status === 'preparing' ? `已准备组态 #${out.id}，旧组态继续生效。` : '已存在同一请求的组态变更，未重复发起。';
 });
}
