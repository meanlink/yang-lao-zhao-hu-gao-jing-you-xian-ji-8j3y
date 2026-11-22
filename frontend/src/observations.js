import {api} from './store.js';
import {OBSERVATIONS, FEATURES, button, empty, escape, heading, panel, signalInputs, status, time} from './shared.js';

export async function observationsPage(state) {
  const query = state.observationResident ? `?resident_id=${state.observationResident}` : '';
  const [residents, payload] = await Promise.all([api('/residents'), api(`/observations${query}${query ? '&' : '?'}include_revoked=true`)]);
  const rows = payload.items;
  const revocations = payload.revocations || [];
  const selected = rows.find(row => row.id === state.supplement && row.status === 'incomplete');
  const options = residents.map(r => `<option value="${r.id}" ${String(r.id) === state.observationResident ? 'selected' : ''}>${escape(r.room)} · ${escape(r.name)}</option>`).join('');
  const canWrite = state.user.role !== 'receiver';
  const canRevoke = state.user.role === 'lead';
  const editor = !canWrite ? `<div class="side-paper"><h3>接班员视角</h3><p>可查看观察记录和告警，交班签收在交班记录页完成。</p></div>` : selected ? `
    <section class="paper">${panel('补齐关键观察', selected.ref)}<form data-do="supplement" data-id="${selected.id}" class="stack form-pad">
      <div class="note warn">待补字段确认后再填写。已登记的字段会保留。</div>
      ${signalInputs(selected)}<div class="actions">${button('保存补录')}<button type="button" data-action="cancel-supplement" class="secondary">取消</button></div>
    </form></section>` : `<section class="paper">${panel('录入新观察','带上巡房单或值班本的记录编号。')}
    <form data-do="observe" class="stack form-pad"><div class="form-pair"><label>住户<select name="resident_id" required>
      <option value="">选择床位</option>${options}</select></label><label>观察编号<input name="ref" required pattern="[A-Z0-9][A-Z0-9\\-]{4,39}" placeholder="OBS-20261121-01"></label></div>
      ${signalInputs()}<label>观察摘要<textarea name="author_note" minlength="3" maxlength="600" required placeholder="记录本班看到的情况和需要核对的事项"></textarea></label>
      ${button('保存观察')}</form></section>`;
  const journal = rows.map(r => {
    const revoked = r.status === 'revoked';
    return `<article class="journal-entry ${revoked ? 'is-revoked' : ''}"><div class="data-line">${status(r.status, OBSERVATIONS)}<span>${escape(r.ref)} · ${time(r.observed_at)}</span></div>
    <h3>${escape(r.room)} · ${escape(r.resident_name)}</h3><p>${escape(r.author_note)}</p>
    ${r.status === 'incomplete' ? `<p class="missing">待核对 ${Object.entries(FEATURES).filter(([key])=>r[key]===null).map(([,label])=>label).join('、')}</p>` : ''}
    ${revoked ? `<div class="note warn"><strong>撤销留档 · ${escape(r.revoked_by_name)} · ${time(r.revoked_at)}</strong><p>${escape(r.revocation_reason)}</p>
      <p class="data-line"><span>撤销前状态 ${OBSERVATIONS[r.previous_observation_status] || r.previous_observation_status} · 剩余观察 ${r.remaining_observations} 条</span>${r.superseded_handover_id ? `<span>旧待签单 #${r.superseded_handover_id} 已作废</span>` : ''}</p></div>` : ''}
    <div class="data-line"><span>记录人 ${escape(r.author_name)}</span>${canWrite && r.status==='incomplete' ? `<button type="button" class="text-link" data-action="supplement" data-value="${r.id}">补录</button>` : ''}${canRevoke && !revoked ? `<button type="button" class="text-link danger-link" data-action="revoke" data-value="${r.id}">记错撤销</button>` : ''}<a href="#alerts/${r.alert_id}">查看告警</a></div>
    ${canRevoke && state.revoke === r.id && !revoked ? `<form data-do="revokeObservation" data-id="${r.id}" class="stack revoke-form">
      <label>撤销缘由（必填，留档可查）<textarea name="reason" minlength="3" maxlength="600" required placeholder="说明记错的内容与核对依据，例如巡房单记错房号、信号来源核实为误记"></textarea></label>
      <div class="actions">${button('确认撤销','secondary')}<button type="button" class="text-link" data-action="cancel-revoke">取消</button></div></form>` : ''}
    </article>`;
  }).join('');
  const archive = revocations.length ? `<section class="paper">${panel('撤销台账', `${revocations.length} 条撤销记录，原观察均留档`)}
    ${revocations.map(v => `<article class="journal-entry is-revoked"><div class="data-line"><span class="pill warn">已撤销</span><span>#${v.observation_id} · ${time(v.created_at)}</span></div>
      <p><strong>${escape(v.observation_ref)}</strong></p><p>${escape(v.reason)}</p>
      <div class="data-line"><span>${escape(v.revoked_by_name)} · 撤销前 ${OBSERVATIONS[v.previous_status] || v.previous_status}</span><span>剩余观察 ${v.remaining_observations} 条</span>${v.superseded_handover_id ? `<a href="#handovers">旧待签单 #${v.superseded_handover_id} 已作废</a>` : ''}</div></article>`).join('')}</section>` : '';
  return `${heading('照护观察','巡视与任务观察','逐项确认信号。待确认的记录会进入待补队列；记错的观察由值班长撤销并留档。')}
    <div class="split"><section class="paper">${panel('观察流水','按录入时间倒序，含已撤销留档')}
    <div class="filter-row"><label>按住户筛选 <select data-change="observation-resident"><option value="">全部住户</option>${options}</select></label>
      <button class="text-link" data-action="refresh" type="button">刷新</button></div>
    ${rows.length ? journal : empty('暂无观察。可选择其他住户或录入新观察。')}
    </section><div class="side-stack">${editor}${archive}</div></div>`;
}
