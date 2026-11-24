import {feedback} from '../components/common/fields.js';
import {empty, metrics, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {handler} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, link, p} from '../utils/dom.js';
import {date, value} from '../utils/format.js';

// 刷新后保留上一次录入的市场、日期与批号，便于连续补报分片。
const draft = {market:'', date:'', batch_no:''};

const num = input => (input === null || input === undefined || input === '' ? 0 : Number(input));
const seconds = input => (input === null || input === undefined ? '—' : `${value(input)} 秒`);
const list = items => (items && items.length ? items.join('、') : '无');

function parseShards(raw) {
  const text = String(raw || '').trim();
  if (!text) throw new Error('至少填写一条分片。');
  let items;
  try { items = JSON.parse(text); }
  catch { items = null; }
  if (items !== null) {
    if (!Array.isArray(items)) throw new Error('分片 JSON 须为数组。');
    return items.map(item => ({shard_no:num(item.shard_no), buckets:item.buckets}));
  }
  // 文本格式：以「片号 N」开头的行声明新分片，其余行是「秒数 车数」。
  const shards = [];
  let current = null;
  for (const line of text.split(/\r?\n/).map(item => item.trim()).filter(Boolean)) {
    const head = line.match(/^(?:片号|分片|shard)[\s:：#]*(\d+)$/i);
    if (head) {
      current = {shard_no:Number(head[1]), buckets:{}};
      shards.push(current);
      continue;
    }
    if (!current) {
      current = {shard_no:shards.length + 1, buckets:{}};
      shards.push(current);
    }
    const [wait, count, ...rest] = line.split(/[\s,，:：]+/).filter(Boolean);
    if (rest.length || !/^\d+$/.test(wait || '') || !/^\d+$/.test(count || '')) {
      throw new Error(`无法识别等候计数「${line}」，请按「秒数 车数」每行一条，或用「片号 N」分隔。`);
    }
    current.buckets[wait] = num(current.buckets[wait]) + num(count);
  }
  return shards;
}

function goTo(id, refresh) {
  const target = `#wait/${id}`;
  if (location.hash === target) return refresh();
  location.hash = target;
}

function entryPanel(refresh, error) {
  const fields = el('div', {class:'form-grid'},
    el('label', {class:'field'}, el('span', {}, '市场'),
      el('input', {name:'market', required:true, maxlength:300, placeholder:'南桥', value:draft.market})),
    el('label', {class:'field'}, el('span', {}, '观测日期'),
      el('input', {name:'date', type:'date', required:true, value:draft.date})),
    el('label', {class:'field'}, el('span', {}, '封账批号'),
      el('input', {name:'batch_no', type:'number', min:1, step:1, required:true, value:draft.batch_no})));
  const read = () => ({
    market: fields.querySelector('[name=market]').value.trim(),
    date: fields.querySelector('[name=date]').value,
    batch_no: num(fields.querySelector('[name=batch_no]').value),
  });
  const remember = target => { Object.assign(draft, target); };

  const manifestButton = button('登记封账单', {type:'submit', class:'primary', value:'manifest'});
  const shardButton = button('登记分片', {type:'submit', class:'primary', value:'shards'});
  const form = el('form', {class:'fields', onSubmit:handler(manifestButton, error, async event => {
    event.preventDefault();
    const target = read();
    if (event.submitter?.value !== 'shards') {
      if (!target.market || !target.date || !target.batch_no) throw new Error('请填写市场、观测日期和封账批号。');
      const total = num(event.target.elements.total_trucks.value);
      const expected = event.target.elements.expected_shards.value.split(/[\s,，、]+/).filter(Boolean).map(Number);
      if (!expected.length || expected.some(item => !Number.isInteger(item) || item < 1)) throw new Error('请填写正整数应到片号。');
      if (!total) throw new Error('请填写封账总车数。');
      remember(target);
      const result = await workspace.submitManifest({...target, expected_shards:expected, total_trucks:total});
      await goTo(result.market_day.id, refresh);
      return;
    }
    if (!target.market || !target.date || !target.batch_no) throw new Error('请先填写市场、观测日期和封账批号。');
    const items = parseShards(event.target.elements.shards.value);
    remember(target);
    const result = await workspace.submitShards({...target, shards:items});
    await goTo(result.market_day.id, refresh);
  })},
    fields,
    el('label', {class:'field'}, el('span', {}, '应到片号'),
      el('input', {name:'expected_shards', placeholder:'1、2、3'})),
    el('label', {class:'field'}, el('span', {}, '封账总车数'),
      el('input', {name:'total_trucks', type:'number', min:1, step:1})),
    el('label', {class:'field'}, el('span', {}, '分片（JSON 或「片号 N / 秒数 车数」文本）'),
      el('textarea', {name:'shards', rows:6,
        placeholder:'[{"shard_no":1,"buckets":{"0":3,"60":4}}]\n或\n片号 1\n0 3\n60 4'})),
    el('div', {class:'actions'}, manifestButton, shardButton));

  return panel('登记封账单与分片',
    p('封账单声明应到片号与总车数；分片按「等候秒数 → 车辆数」上报，可乱序补报。同一片号重试须内容一致。','subtle'),
    form, error);
}

export async function waitPage(route, refresh) {
  const {spec} = workspace;
  const meta = spec.wait_page || {};
  const days = await workspace.waitDays();
  const selectedId = route.id || days[0]?.id || null;
  const selected = selectedId ? await workspace.waitDay(selectedId) : null;
  const message = feedback();
  if (selected) Object.assign(draft, {market:selected.market, date:selected.date});
  const form = session.canWrite && entryPanel(refresh, message);

  const adopted = selected?.adopted;
  const overview = panel(meta.title || spec.pages[6],
    p(meta.subtitle || '按市场日核对封账单与分片，取累计九成车辆的等候秒数。','subtle'),
    adopted
      ? metrics([
          {label:'九成等候', value:seconds(adopted.p90_seconds), detail:`合计 ${value(adopted.total_trucks)} 辆车。`},
          {label:'采用批号', value:adopted.batch_no, detail:`封账单 ${adopted.manifest_id} · 已到 ${adopted.shard_nos.length} 片。`},
          {label:'封账总车数', value:adopted.total_trucks, detail:`应到片号 ${list(adopted.expected_shards)}`},
        ])
      : empty('该市场日尚未齐备，暂无采用的九成秒数。齐备前仍显示上次结果。'),
    adopted && table([
      {key:'batch_no', label:'采用批号'},
      {label:'封账单', render:()=>`#${adopted.manifest_id}`},
      {label:'应到片号', render:()=>list(adopted.expected_shards)},
      {label:'已到片号', render:()=>list(adopted.shard_nos)},
      {label:'封账总车数', render:()=>value(adopted.total_trucks)},
      {label:'九成等候', render:()=>seconds(adopted.p90_seconds)},
      {label:'采用时间', render:()=>date(adopted.adopted_at)},
      {label:'封账单指纹', render:()=>adopted.manifest_checksum},
    ], [adopted], {caption:'采用的封账单与片号'}));

  const dayList = table([
    {label:'市场', render:row=>link(row.market, `#wait/${row.id}`)},
    {key:'date', label:'观测日期'},
    {label:'九成等候', render:row=>row.adopted ? seconds(row.adopted.p90_seconds) : '待齐备'},
    {label:'采用批号', render:row=>row.adopted ? row.adopted.batch_no : '—'},
    {label:'封账单 / 分片', render:row=>`${value(row.manifest_count)} / ${value(row.shard_count)}`},
    {label:'状态', render:row=>pill(row.pending_batches ? `待补齐 ${row.pending_batches}` : '已采用', row.pending_batches ? 'pending' : 'complete')},
  ], days, {caption:'市场日九成等候', empty:'还没有市场日记录。先登记封账单与分片。'});

  const batchTable = selected && table([
    {key:'batch_no', label:'批号'},
    {key:'status', label:'状态', render:row=>pill(row.status, row.adopted ? 'complete' : row.complete ? 'accepted' : 'pending')},
    {label:'总车数', render:row=>row.total_trucks === null ? '—' : `${value(row.observed_trucks)} / ${value(row.total_trucks)}`},
    {label:'缺片', render:row=>list(row.missing_shards)},
    {label:'应到片号', render:row=>list(row.expected_shards)},
    {label:'已到片号', render:row=>list(row.received_shards)},
    {label:'说明', render:row=>row.reason || (row.total_matches ? '总车数一致' : '—')},
  ], selected.batches, {caption:'封账单对账', empty:'该市场日还没有封账单。'});

  return el('div', {class:'two-col'},
    el('div', {class:'wide-panel'}, overview,
      panel('封账单对账', batchTable || empty('请选择一个市场日。')), form),
    el('aside', {class:'sidebar-panel'}, panel('市场日', dayList),
      panel('计算口径', el('ul', {class:'method-list'}, (meta.methodology || []).map(text=>el('li', {}, text))))));
}
