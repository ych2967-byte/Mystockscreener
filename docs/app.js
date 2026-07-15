const state = { market: 'kr', data: [], meta: null };
const $ = (id) => document.getElementById(id);
const inputIds = ['search','exchange','logic','sort','limit','stack','ma5_20','ma20_50','ma50_100','ma100_200','above200','minDay','minW1','minM1','minM3','minVolume','maxHigh20','minGap20','maxGap20','showPreferred','showSpac'];

function n(v) { if (v == null || String(v).trim() === '') return null; const x = Number(v); return Number.isFinite(x) ? x : null; }
function pct(v) { return v == null ? '-' : `${v > 0 ? '+' : ''}${v.toFixed(1)}%`; }
function cls(v) { return v > 0 ? 'pos' : v < 0 ? 'neg' : ''; }
function money(v, market) {
  if (v == null) return '-';
  return market === 'KR' ? `${Math.round(v).toLocaleString('ko-KR')}원` : `$${v.toLocaleString('en-US', {maximumFractionDigits: 2})}`;
}
function compact(v, market) {
  if (v == null) return '-';
  const units = market === 'KR' ? [['조',1e12],['억',1e8],['만',1e4]] : [['B',1e9],['M',1e6],['K',1e3]];
  for (const [u,d] of units) if (Math.abs(v) >= d) return `${(v/d).toFixed(1)}${u}`;
  return Math.round(v).toLocaleString();
}
function getValue(id) { const el = $(id); return el.type === 'checkbox' ? el.checked : el.value; }
function saveSettings() {
  const settings = { market: state.market };
  inputIds.forEach(id => settings[id] = getValue(id));
  localStorage.setItem('screenerSettingsV1', JSON.stringify(settings));
}
function restoreSettings() {
  try {
    const s = JSON.parse(localStorage.getItem('screenerSettingsV1') || '{}');
    if (s.market) state.market = s.market;
    inputIds.forEach(id => {
      if (!(id in s)) return;
      const el = $(id); if (el.type === 'checkbox') el.checked = !!s[id]; else el.value = s[id];
    });
  } catch (_) {}
}
function activeChecks(stock) {
  const checks = [];
  if ($('stack').checked) checks.push(stock.stack === true);
  if ($('ma5_20').checked) checks.push(stock.ma5_20 === true);
  if ($('ma20_50').checked) checks.push(stock.ma20_50 === true);
  if ($('ma50_100').checked) checks.push(stock.ma50_100 === true);
  if ($('ma100_200').checked) checks.push(stock.ma100_200 === true);
  if ($('above200').checked) checks.push(stock.above200 === true);
  const addMin = (id, key) => { const x = n($(id).value); if (x != null) checks.push(stock[key] != null && stock[key] >= x); };
  addMin('minDay','day'); addMin('minW1','w1'); addMin('minM1','m1'); addMin('minM3','m3'); addMin('minVolume','volume_ratio'); addMin('minGap20','gap20');
  const maxHigh20 = n($('maxHigh20').value); if (maxHigh20 != null) checks.push(stock.high20_distance != null && stock.high20_distance <= maxHigh20);
  const maxGap20 = n($('maxGap20').value); if (maxGap20 != null) checks.push(stock.gap20 != null && stock.gap20 <= maxGap20);
  return checks;
}
function filtered() {
  const q = $('search').value.trim().toLowerCase();
  const ex = $('exchange').value;
  const logic = $('logic').value;
  let rows = state.data.filter(s => {
    if (q && !`${s.name} ${s.ticker}`.toLowerCase().includes(q)) return false;
    if (ex !== 'all' && s.exchange !== ex && !(s.indexes || []).includes(ex)) return false;
    if (!$('showPreferred').checked && s.preferred) return false;
    if (!$('showSpac').checked && s.spac) return false;
    const checks = activeChecks(s);
    return checks.length === 0 || (logic === 'and' ? checks.every(Boolean) : checks.some(Boolean));
  });
  const [key, dir] = $('sort').value.split('_');
  const map = { day:'day', w1:'w1', m1:'m1', m3:'m3', volume:'volume_ratio', value:'value_traded', name:'name' };
  const k = map[key];
  rows.sort((a,b) => {
    if (k === 'name') return a.name.localeCompare(b.name, 'ko');
    const av = a[k] ?? -Infinity, bv = b[k] ?? -Infinity;
    return dir === 'asc' ? av-bv : bv-av;
  });
  return rows;
}
function populateExchange() {
  const current = $('exchange').value;
  let savedExchange = current;
  try { savedExchange = JSON.parse(localStorage.getItem('screenerSettingsV1') || '{}').exchange || current; } catch (_) {}
  const exchanges = [...new Set(state.data.map(s => s.exchange).filter(Boolean))].sort();
  const indexes = state.market === 'us' ? [...new Set(state.data.flatMap(s => s.indexes || []))].sort() : [];
  $('exchange').innerHTML = '<option value="all">전체</option>' + [...exchanges, ...indexes.filter(x => !exchanges.includes(x))].map(x => `<option value="${x}">${x}</option>`).join('');
  if ([...exchanges, ...indexes, 'all'].includes(savedExchange)) $('exchange').value = savedExchange;
}
function render() {
  saveSettings();
  const all = filtered();
  const limit = Number($('limit').value);
  const rows = all.slice(0, limit);
  $('count').textContent = `${all.length.toLocaleString()}종목`;
  $('notice').textContent = all.length > rows.length ? `상위 ${rows.length.toLocaleString()}개 표시` : '';
  const hasData = state.data.length > 0;
  $('status').hidden = hasData;
  $('tableWrap').hidden = !hasData;
  $('mobileCards').hidden = !hasData;
  if (!hasData) return;
  $('tbody').innerHTML = rows.map(s => `<tr>
    <td><div class="name">${s.name}</div><div class="ticker">${s.ticker}</div></td>
    <td>${s.exchange}</td><td>${money(s.close, s.market)}</td>
    <td class="${cls(s.day)}">${pct(s.day)}</td><td class="${cls(s.w1)}">${pct(s.w1)}</td><td class="${cls(s.m1)}">${pct(s.m1)}</td><td class="${cls(s.m3)}">${pct(s.m3)}</td>
    <td>${s.volume_ratio == null ? '-' : `${s.volume_ratio.toFixed(0)}%`}<div class="ticker">${compact(s.value_traded, s.market)}</div></td>
    <td class="${cls(s.gap20)}">${pct(s.gap20)}</td><td>${s.high20_distance == null ? '-' : `${s.high20_distance.toFixed(1)}%`}</td>
    <td><span class="badge ${s.stack ? 'good' : ''}">${s.stack ? '정배열' : '-'}</span></td>
  </tr>`).join('');
  $('mobileCards').innerHTML = rows.map(s => `<article class="stock-card">
    <div class="stock-head"><div><div class="name">${s.name}</div><div class="ticker">${s.ticker} · ${s.exchange}</div></div><div style="text-align:right"><b>${money(s.close,s.market)}</b><div class="${cls(s.day)}">${pct(s.day)}</div></div></div>
    <div class="stock-grid">
      <div class="metric"><span>1주</span><b class="${cls(s.w1)}">${pct(s.w1)}</b></div>
      <div class="metric"><span>1개월</span><b class="${cls(s.m1)}">${pct(s.m1)}</b></div>
      <div class="metric"><span>3개월</span><b class="${cls(s.m3)}">${pct(s.m3)}</b></div>
      <div class="metric"><span>거래량</span><b>${s.volume_ratio == null ? '-' : `${s.volume_ratio.toFixed(0)}%`}</b></div>
      <div class="metric"><span>20일선 이격</span><b class="${cls(s.gap20)}">${pct(s.gap20)}</b></div>
      <div class="metric"><span>배열</span><b>${s.stack ? '정배열' : '-'}</b></div>
    </div>
  </article>`).join('');
}
async function loadMarket(market) {
  state.market = market;
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.market === market));
  $('status').hidden = false; $('status').textContent = '데이터를 불러오는 중입니다.';
  $('tableWrap').hidden = true; $('mobileCards').hidden = true;
  try {
    const res = await fetch(`data/${market}.json?v=${Date.now()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const payload = await res.json();
    state.meta = payload; state.data = payload.stocks || [];
    const when = payload.price_date ? `${payload.price_date} 종가` : '아직 갱신 전';
    $('freshness').textContent = `${market === 'kr' ? '한국' : '미국'} 데이터: ${when}`;
    $('status').textContent = payload.message || '검색할 데이터가 없습니다.';
    populateExchange(); render();
  } catch (e) {
    state.data = [];
    $('freshness').textContent = '데이터 불러오기 실패';
    $('status').hidden = false; $('status').textContent = `데이터를 불러오지 못했습니다: ${e.message}`;
  }
}
function reset() {
  ['search','minDay','minW1','minM1','minM3','minVolume','maxHigh20','minGap20','maxGap20'].forEach(id => $(id).value = '');
  ['stack','ma5_20','ma20_50','ma50_100','ma100_200','above200','showPreferred','showSpac'].forEach(id => $(id).checked = false);
  $('exchange').value='all'; $('logic').value='and'; $('sort').value='m1_desc'; $('limit').value='100'; render();
}
restoreSettings();
document.querySelectorAll('.tab').forEach(b => b.addEventListener('click', () => loadMarket(b.dataset.market)));
$('apply').addEventListener('click', render); $('reset').addEventListener('click', reset);
['search','exchange','logic','sort','limit','showPreferred','showSpac'].forEach(id => $(id).addEventListener(id === 'search' ? 'input' : 'change', render));
loadMarket(state.market);
