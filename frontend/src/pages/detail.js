import {store} from '../stores/entities.js';
import {table} from '../components/common/table.js';
import {badge} from '../components/common/status.js';
import {esc} from '../utils/html.js';
import {download} from '../api/client.js';
import {message} from '../hooks/action.js';

const GROUP_LABELS = {legacy:'历史隐含组',active:'生效组',preparing:'准备中新组'};

function configSection(r){
 const c = r.configurations;
 if(!c) return '';
 const transition = c.transition
  ? `<p class="notice">入队时组态正在过渡：判定须新旧两组分别达到多数派并给出同一候选。新组票只计入已追平发起时原主日志 <b>${c.preparing_target_log_position}</b> 的成员。</p>`
  : '<p class="notice">入队时组态稳定，使用生效组的法定票数与成员范围判定。</p>';
 return `<h2>入队时组态</h2>${transition}${table(['组态','代际','成员'],[[c.active_members ? '生效组' : '历史隐含组', c.active_generation ?? '—', esc((c.active_members || []).join('、') || '按节点投票开关')],['准备中新组', c.preparing_generation ?? '—', esc((c.preparing_members || []).join('、') || '无')]])}`;
}

export function detailPage(job){if(!job)return '<section class="empty"><h2>还没有演练记录</h2><p>先在演练队列提交一条任务，随后可查看每项判定证据。</p><a href="#drills">前往演练队列 →</a></section>';const r=job.result;return `<div class="section-title"><h2>${esc(job.label)} <small>#${job.id}</small></h2>${badge(job.status)}</div>${r?`<section class="verdict"><div><p>演练决策</p><h2>${r.decision==='approved'?'允许切主':r.decision==='blocked'?'安全阻断':'计算失败'}</h2></div><p>${r.winner?'候选节点 '+esc(r.winner):esc(r.error||'检查下方规则，调整输入后重新提交')}<br>新任期 ${r.new_term??'—'} · 多数派 ${r.reachable_votes??'—'}/${r.quorum??'—'}</p></section>${r.error?`<p class="notice error">${esc(r.error)}</p>`:''}${configSection(r)}${table(['规则','判定','输入证据'],(r.checks||[]).map(c=>[esc(c.rule),c.pass?'通过':'阻断',esc(c.evidence)]))}<h2>候选节点复核</h2>${table(['组态','节点','日志位置','落后条数','满足候选条件'],(r.candidates||[]).map(n=>[esc(GROUP_LABELS[n.configuration]||n.configuration||'—'),esc(n.name),n.log_position,n.lag,n.eligible?'是':'否']))}<p class="checksum">输入摘要 ${esc(r.input_sha256||'无')} · 模型 ${esc(r.model_version||'—')}</p>`:'<p class="notice">后台正在计算，点击刷新获取状态。</p>'}<div class="toolbar"><button id="export">导出冻结输入与结果</button><button class="secondary" id="refresh">刷新状态</button></div><details><summary>查看冻结输入</summary><pre>${esc(JSON.stringify(job.snapshot,null,2))}</pre></details>`;}
export function bindDetail(job,render){document.querySelector('#refresh')?.addEventListener('click',render);document.querySelector('#export')?.addEventListener('click',()=>download(job.id).catch(e=>message(e.message,true)));}
