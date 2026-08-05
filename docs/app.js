const $ = id => document.getElementById(id);
const state = { market: 'kr', data: [], meta: {} };
const boolIds = ['stack','ma5_20','ma20_50','ma50_100','ma100_200','above200'];
const numberIds = ['minDay','minW1','minM1','minM3','minVolume','maxHigh20','minGap20','maxGap20'];

function num(id) { const v=$(id).value.trim(); return v==='' ? null : Number(v); }
function pct(v) { return v==null ? '-' : `${v>=0?'+':''}${Number(v).toFixed(1)}%`; }
function cls(v) { return v==null ? '' : v>0 ? 'pos' : v<0 ? 'neg' : ''; }
function money(v, market) { if(v==null)return '-'; return market==='KR' ? `${Math.round(v).toLocaleString()}원` : `$${Number(v).toLocaleString(undefined,{maximumFractionDigits:2})}`; }
function compact(v, market) { if(v==null)return '-'; const unit=market==='KR'?'원':'$'; if(v>=1e12)return `${(v/1e12).toFixed(1)}조${unit}`; if(v>=1e9)return `${(v/1e9).toFixed(1)}B${unit}`; if(v>=1e6)return `${(v/1e6).toFixed(1)}M${unit}`; return `${Math.round(v).toLocaleString()}${unit}`; }
function assetLabel(s) { if(s.asset_type==='etf') return s.leveraged?'레버리지 ETF':s.inverse?'인버스 ETF':'ETF'; return '일반주'; }
function escapeHtml(x){ return String(x??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }

function settings() {
  const out={market:state.market};
  ['search','exchange','assetType','logic','sort','limit'].forEach(id=>out[id]=$(id).value);
  ['showPreferred','showSpac','showLeveraged','showInverse',...boolIds].forEach(id=>out[id]=$(id).checked);
  numberIds.forEach(id=>out[id]=$(id).value);
  return out;
}
function saveSettings(){ localStorage.setItem('stockScreenerSettingsV2',JSON.stringify(settings())); }
function restoreSettings(){ try { const s=JSON.parse(localStorage.getItem('stockScreenerSettingsV2')||'{}'); state.market=s.market||'kr'; Object.entries(s).forEach(([id,v])=>{const el=$(id); if(!el)return; if(el.type==='checkbox')el.checked=!!v; else el.value=v;}); } catch(_){} }

function matchesConditions(s) {
  const tests=[];
  boolIds.forEach(id=>{if($(id).checked)tests.push(!!s[id]);});
  const map=[['minDay','day','min'],['minW1','w1','min'],['minM1','m1','min'],['minM3','m3','min'],['minVolume','volume_ratio','min'],['maxHigh20','high20_distance','max'],['minGap20','gap20','min'],['maxGap20','gap20','max']];
  map.forEach(([id,key,mode])=>{const n=num(id); if(n!=null)tests.push(s[key]!=null && (mode==='min'?s[key]>=n:s[key]<=n));});
  if(!tests.length)return true;
  return $('logic').value==='or' ? tests.some(Boolean) : tests.every(Boolean);
}
function filtered(){
  const q=$('search').value.trim().toLowerCase(); const ex=$('exchange').value; const type=$('assetType').value;
  let rows=state.data.filter(s=>{
    if(q && !`${s.name} ${s.ticker}`.toLowerCase().includes(q))return false;
    if(ex!=='all' && s.exchange!==ex && !(s.indexes||[]).includes(ex))return false;
    if(type!=='all' && (s.asset_type||'stock')!==type)return false;
    if(state.market==='kr' && s.preferred && !$('showPreferred').checked)return false;
    if(state.market==='kr' && s.spac && !$('showSpac').checked)return false;
    if(state.market==='us' && s.asset_type==='etf'){
      if(s.leveraged && !$('showLeveraged').checked)return false;
      if(s.inverse && !$('showInverse').checked)return false;
    }
    return matchesConditions(s);
  });
  const [key,dir]=$('sort').value.split('_'); const k={day:'day',w1:'w1',m1:'m1',m3:'m3',volume:'volume_ratio',value:'value_traded',name:'name'}[key];
  rows.sort((a,b)=>{ if(k==='name')return String(a.name).localeCompare(String(b.name)); const av=a[k]??-Infinity,bv=b[k]??-Infinity; return dir==='asc'?av-bv:bv-av; });
  return rows;
}
function populateExchange(){
  const saved=$('exchange').value||'all';
  const exchanges=[...new Set(state.data.map(s=>s.exchange).filter(Boolean))].sort();
  const indexes=state.market==='us'?[...new Set(state.data.flatMap(s=>s.indexes||[]))].sort():[];
  const values=[...exchanges,...indexes.filter(x=>!exchanges.includes(x))];
  $('exchange').innerHTML='<option value="all">전체</option>'+values.map(x=>`<option value="${escapeHtml(x)}">${escapeHtml(x)}</option>`).join('');
  if(values.includes(saved))$('exchange').value=saved;
}
function updateMarketControls(){
  $('krOptions').hidden=state.market!=='kr'; $('usOptions').hidden=state.market!=='us';
  $('assetField').hidden=state.market!=='us';
}
function render(){
  saveSettings(); const all=filtered(); const rows=all.slice(0,Number($('limit').value));
  $('count').textContent=`${all.length.toLocaleString()}종목`;
  $('notice').textContent=all.length>rows.length?`상위 ${rows.length.toLocaleString()}개 표시`:'';
  const has=state.data.length>0; $('status').hidden=has; $('tableWrap').hidden=!has; $('mobileCards').hidden=!has; if(!has)return;
  $('tbody').innerHTML=rows.map(s=>`<tr><td><div class="name">${escapeHtml(s.name)}</div><div class="ticker">${escapeHtml(s.ticker)}</div></td><td>${escapeHtml(s.exchange)}<div class="ticker">${assetLabel(s)}</div></td><td>${money(s.close,s.market)}</td><td class="${cls(s.day)}">${pct(s.day)}</td><td class="${cls(s.w1)}">${pct(s.w1)}</td><td class="${cls(s.m1)}">${pct(s.m1)}</td><td class="${cls(s.m3)}">${pct(s.m3)}</td><td>${s.volume_ratio==null?'-':`${Number(s.volume_ratio).toFixed(0)}%`}<div class="ticker">${compact(s.value_traded,s.market)}</div></td><td class="${cls(s.gap20)}">${pct(s.gap20)}</td><td>${s.high20_distance==null?'-':`${Number(s.high20_distance).toFixed(1)}%`}</td><td><span class="badge ${s.stack?'good':''}">${s.stack?'정배열':'-'}</span></td></tr>`).join('');
  $('mobileCards').innerHTML=rows.map(s=>`<article class="stock-card"><div class="stock-head"><div><div class="name">${escapeHtml(s.name)}</div><div class="ticker">${escapeHtml(s.ticker)} · ${escapeHtml(s.exchange)} · ${assetLabel(s)}</div></div><div style="text-align:right"><b>${money(s.close,s.market)}</b><div class="${cls(s.day)}">${pct(s.day)}</div></div></div><div class="stock-grid"><div class="metric"><span>1주</span><b class="${cls(s.w1)}">${pct(s.w1)}</b></div><div class="metric"><span>1개월</span><b class="${cls(s.m1)}">${pct(s.m1)}</b></div><div class="metric"><span>3개월</span><b class="${cls(s.m3)}">${pct(s.m3)}</b></div><div class="metric"><span>거래량</span><b>${s.volume_ratio==null?'-':`${Number(s.volume_ratio).toFixed(0)}%`}</b></div><div class="metric"><span>20일선 이격</span><b class="${cls(s.gap20)}">${pct(s.gap20)}</b></div><div class="metric"><span>배열</span><b>${s.stack?'정배열':'-'}</b></div></div></article>`).join('');
}
async function loadMarket(market){
  state.market=market; document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('active',b.dataset.market===market)); updateMarketControls();
  $('status').hidden=false;$('status').textContent='데이터를 불러오는 중입니다.';$('tableWrap').hidden=true;$('mobileCards').hidden=true;
  try{const r=await fetch(`data/${market}.json?v=${Date.now()}`);if(!r.ok)throw new Error(`HTTP ${r.status}`);const p=await r.json();state.meta=p;state.data=p.stocks||[];const when=p.price_date?`${p.price_date} 종가`:'아직 갱신 전';$('freshness').textContent=`${market==='kr'?'한국':'미국'} 데이터: ${when}`;$('status').textContent=p.message||'검색할 데이터가 없습니다.';populateExchange();render();}catch(e){state.data=[];$('freshness').textContent='데이터 불러오기 실패';$('status').hidden=false;$('status').textContent=`데이터를 불러오지 못했습니다: ${e.message}`;}
}
function reset(){
  ['search',...numberIds].forEach(id=>$(id).value=''); [...boolIds,'showPreferred','showSpac','showLeveraged','showInverse'].forEach(id=>$(id).checked=false);
  $('exchange').value='all';$('assetType').value='all';$('logic').value='and';$('sort').value='m1_desc';$('limit').value='100';render();
}
restoreSettings(); updateMarketControls();
document.querySelectorAll('.tab').forEach(b=>b.addEventListener('click',()=>loadMarket(b.dataset.market)));
$('apply').addEventListener('click',render);$('reset').addEventListener('click',reset);
['search','exchange','assetType','logic','sort','limit','showPreferred','showSpac','showLeveraged','showInverse'].forEach(id=>$(id).addEventListener(id==='search'?'input':'change',render));
loadMarket(state.market);
