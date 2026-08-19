"""Web UI + control endpoints. Receives everything it needs via init_dashboard()
so it never imports the bot (no circular imports, testable in isolation)."""
import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

_ctx = {}

def init_dashboard(store, controller, cfg, shared, state, state_lock, log_buffer, log_lock):
    _ctx.update(store=store, controller=controller, cfg=cfg, shared=shared, state=state,
                state_lock=state_lock, log_buffer=log_buffer, log_lock=log_lock)

def start_server(port):
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()

def _strategy_rows(cfg, stats):
    info = {
        "dip": ("Dip Mean-Reversion", f"drop ≥ {cfg['DIP_THRESHOLD_PCT']}% + RSI < {cfg['RSI_OVERSOLD']} + uptrend + volume"),
        "bb": ("Bollinger Reversion", f"close < {cfg['BB_STD']}σ lower band + RSI ≤ {cfg['BB_RSI_MAX']}, exit at mid band"),
        "breakout": ("Donchian Breakout", f"new {cfg['BREAKOUT_LOOKBACK']}-candle high + ≥{cfg['BREAKOUT_VOLUME_MULT']}x volume, ATR trail"),
    }
    enabled = {"dip": cfg["STRATEGY_DIP_ENABLED"], "bb": cfg["STRATEGY_BB_ENABLED"],
               "breakout": cfg["STRATEGY_BREAKOUT_ENABLED"]}
    rows = []
    for key, (name, detail) in info.items():
        s = stats.get(key, {})
        rows.append({"key": key, "name": name, "enabled": enabled[key], "detail": detail,
                     "closed": s.get("closed", 0), "wins": s.get("wins", 0),
                     "profit": s.get("profit", 0.0)})
    return rows

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trading Bot Dashboard</title>
<style>
  :root{--bg:#0b0f14;--panel:#111827;--border:#1f2937;--text:#e6edf3;--muted:#8b949e;
    --green:#3fb950;--green-bg:#0d3b23;--red:#f85149;--red-bg:#3b0d0d;--yellow:#e3b341;
    --yellow-bg:#3b2d0d;--blue:#58a6ff;--blue-bg:#0d2237}
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text)}
  header{position:sticky;top:0;z-index:10;background:rgba(11,15,20,.94);padding:12px 20px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}
  .header-left{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  h1{font-size:17px;margin:0}
  .badges{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
  .badge{padding:3px 10px;border-radius:999px;font-size:11px;font-weight:700;letter-spacing:.03em}
  .badge.live{background:var(--green-bg);color:var(--green)}
  .badge.testnet{background:var(--yellow-bg);color:var(--yellow)}
  .badge.paused,.badge.circuit,.badge.stale{background:var(--red-bg);color:var(--red)}
  .badge.running{background:var(--green-bg);color:var(--green)}
  .btn{border:1px solid var(--border);background:var(--panel);color:var(--text);padding:7px 14px;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer}
  .btn:hover{filter:brightness(1.3)}
  .btn-sm{padding:3px 10px;font-size:12px;border-radius:6px}
  .btn-danger{background:var(--red-bg);color:var(--red);border-color:#5c1d1d}
  .btn-blue{background:var(--blue-bg);color:var(--blue)}
  main{padding:20px;max-width:1150px;margin:0 auto}
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:22px}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:14px}
  .card .label{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:6px}
  .card .value{font-size:20px;font-weight:700}
  .pos{color:var(--green)}.neg{color:var(--red)}
  section{margin-bottom:26px}
  .section-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;gap:10px;flex-wrap:wrap}
  section h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0}
  .table-wrap{overflow-x:auto;border:1px solid var(--border);border-radius:12px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--border);white-space:nowrap}
  th{color:var(--muted);font-weight:600;font-size:12px}
  tr:last-child td{border-bottom:none}
  tbody tr:hover{background:#161f2e}
  .tag{padding:2px 8px;border-radius:6px;font-size:11px;font-weight:700}
  .tag.buy{background:var(--blue-bg);color:var(--blue)}
  .tag.sell-win{background:var(--green-bg);color:var(--green)}
  .tag.sell-loss{background:var(--red-bg);color:var(--red)}
  .tag.strat{background:#21262d;color:var(--muted)}
  .chart-card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:14px}
  #equityChart{width:100%;height:170px;display:block}
  #logs{background:#0d1117;border:1px solid var(--border);border-radius:12px;padding:12px;max-height:320px;overflow-y:auto;font-family:ui-monospace,Menlo,monospace;font-size:12px;line-height:1.65}
  .log-line{white-space:pre-wrap;word-break:break-word}
  .log-ERROR{color:var(--red)}.log-WARNING{color:var(--yellow)}.log-INFO{color:#c9d1d9}
  .empty{color:var(--muted);font-size:13px;padding:12px;text-align:center}
  #toasts{position:fixed;bottom:18px;right:18px;display:flex;flex-direction:column;gap:8px;z-index:50}
  .toast{background:var(--panel);border:1px solid var(--border);border-left:3px solid var(--green);padding:10px 14px;border-radius:8px;font-size:13px;box-shadow:0 8px 24px rgba(0,0,0,.5)}
  .toast.err{border-left-color:var(--red)}
  footer{text-align:center;color:var(--muted);font-size:12px;padding:18px}
</style>
</head>
<body>
<header>
  <div class="header-left"><h1>🤖 Trading Bot</h1>
    <div class="badges">
      <span id="mode-badge" class="badge testnet">…</span>
      <span id="run-badge" class="badge running">RUNNING</span>
      <span id="stale-badge" class="badge stale" style="display:none">⚠ STALE</span>
    </div>
  </div>
  <div class="badges" id="circuits"></div>
  <button class="btn btn-blue" id="pause-btn" onclick="togglePause()">⏸ Pause entries</button>
</header>
<main>
  <div class="stats">
    <div class="card"><div class="label">Equity</div><div class="value" id="equity">--</div></div>
    <div class="card"><div class="label">Free USDT</div><div class="value" id="cash">--</div></div>
    <div class="card"><div class="label">Realized P/L (net)</div><div class="value" id="pl">--</div></div>
    <div class="card"><div class="label">Unrealized P/L</div><div class="value" id="upl">--</div></div>
    <div class="card"><div class="label">Today's P/L</div><div class="value" id="dpl">--</div></div>
    <div class="card"><div class="label">Win Rate</div><div class="value" id="winrate">--</div></div>
    <div class="card"><div class="label">Closed / Open</div><div class="value" id="co">--</div></div>
    <div class="card"><div class="label">Exposure</div><div class="value" id="exposure">--</div></div>
    <div class="card"><div class="label">Loops</div><div class="value" id="loops">--</div></div>
    <div class="card"><div class="label">Drawdown</div><div class="value" id="dd">--</div></div>
  </div>

  <section><div class="section-head"><h2>Realized P/L Over Time</h2></div>
    <div class="chart-card"><canvas id="equityChart"></canvas>
      <div id="chart-empty" class="empty" style="display:none">No closed trades yet.</div></div>
  </section>

  <section><div class="section-head"><h2>Open Positions</h2>
      <button class="btn btn-danger" onclick="closeAll()">✖ Close all</button></div>
    <div class="table-wrap"><table>
      <thead><tr><th>Symbol</th><th>Strategy</th><th>Buy</th><th>Now</th><th>P/L %</th><th>Value</th><th>Held</th><th>BE</th><th>Ladder</th><th>Trail</th><th></th></tr></thead>
      <tbody id="positions-body"><tr><td colspan="11" class="empty">Loading…</td></tr></tbody>
    </table></div>
  </section>

  <section><div class="section-head"><h2>Trade History</h2>
      <span class="badge running" style="background:#21262d;color:var(--muted)" id="trade-count">--</span></div>
    <div class="table-wrap"><table>
      <thead><tr><th>Time</th><th>Action</th><th>Symbol</th><th>Price</th><th>Qty</th><th>Net P/L</th><th>Reason</th></tr></thead>
      <tbody id="trades-body"><tr><td colspan="7" class="empty">Loading…</td></tr></tbody>
    </table></div>
  </section>

  <section><div class="section-head"><h2>Strategy Performance (live)</h2></div>
    <div class="table-wrap"><table>
      <thead><tr><th>Strategy</th><th>Status</th><th>Closed Trades</th><th>Win Rate</th><th>Net P/L</th><th>Logic</th></tr></thead>
      <tbody id="strategies-body"><tr><td colspan="6" class="empty">Loading…</td></tr></tbody>
    </table></div>
  </section>

  <section><div class="section-head"><h2>Adaptive Per-Symbol Tuning</h2></div>
    <div class="table-wrap"><table>
      <thead><tr><th>Symbol</th><th>Recent Win Rate</th><th>Multiplier</th><th>Meaning</th></tr></thead>
      <tbody id="adaptive-body"><tr><td colspan="4" class="empty">Loading…</td></tr></tbody>
    </table></div>
  </section>

  <section><div class="section-head"><h2>Live Logs</h2></div>
    <div id="logs"><div class="empty">Loading…</div></div></section>
</main>
<div id="toasts"></div>
<footer>Auto-refreshes every 5s · Multi-Strategy Bot v9 · <span id="watching-foot">--</span></footer>

<script>
let controlToken = localStorage.getItem('control_token') || '';
let lastSells = [];
function fmt(n,d){d=(d===undefined)?2:d;return(typeof n==='number'&&isFinite(n))?n.toFixed(d):'--'}
function fmtPrice(p){if(typeof p!=='number'||!isFinite(p))return'--';if(p>=1000)return p.toFixed(2);if(p>=1)return p.toFixed(4);return p.toFixed(6)}
function timeAgo(iso){const s=(Date.now()-new Date(iso).getTime())/1000;if(s<60)return Math.floor(s)+'s';if(s<3600)return Math.floor(s/60)+'m';if(s<86400)return Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m';return Math.floor(s/86400)+'d'}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function setVal(id,text,num){const el=document.getElementById(id);el.textContent=text;if(num!==undefined&&num!==null)el.className='value '+(num>=0?'pos':'neg')}
function toast(msg,ok){const el=document.createElement('div');el.className='toast'+(ok===false?' err':'');el.textContent=msg;document.getElementById('toasts').appendChild(el);setTimeout(()=>{el.style.opacity='0';el.style.transition='opacity .4s'},3200);setTimeout(()=>el.remove(),3700)}
async function post(path,body){
  const res=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Control-Token':controlToken},body:JSON.stringify(body||{})});
  if(res.status===401){const t=prompt('This dashboard is protected. Enter the CONTROL_TOKEN:');if(t!==null){controlToken=t;localStorage.setItem('control_token',t);return post(path,body)}return{ok:false,error:'unauthorized'}}
  return res.json();
}
async function closePosition(sym){if(!confirm('Market-sell the entire '+sym+' position now?'))return;
  const r=await post('/api/close',{symbol:sym});r.ok?toast(r.msg):toast(r.msg||'failed',false);refresh()}
async function closeAll(){if(!confirm('Market-sell ALL open positions now?'))return;
  const r=await post('/api/close',{all:true});toast(r.msg,r.ok);refresh()}
async function togglePause(){const r=await post('/api/pause',{});toast(r.msg,r.ok);refresh()}
function drawEquity(){
  const canvas=document.getElementById('equityChart'),empty=document.getElementById('chart-empty'),sells=lastSells;
  if(!sells.length){canvas.style.display='none';empty.style.display='block';return}
  canvas.style.display='block';empty.style.display='none';
  const dpr=window.devicePixelRatio||1,w=canvas.clientWidth,h=canvas.clientHeight;
  canvas.width=w*dpr;canvas.height=h*dpr;
  const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);ctx.clearRect(0,0,w,h);
  let cum=0;const pts=sells.map(t=>{cum+=t.profit||0;return cum});
  const min=Math.min(0,...pts),max=Math.max(0,...pts),pad=(max-min)*.15||.5,lo=min-pad,hi=max+pad;
  const X=i=>34+(w-50)*(sells.length===1?.5:i/(sells.length-1)),Y=v=>12+(h-32)*(1-(v-lo)/(hi-lo));
  ctx.strokeStyle='#30363d';ctx.setLineDash([4,4]);ctx.beginPath();ctx.moveTo(34,Y(0));ctx.lineTo(w-16,Y(0));ctx.stroke();ctx.setLineDash([]);
  const col=cum>=0?'#3fb950':'#f85149';
  ctx.strokeStyle=col;ctx.lineWidth=2;ctx.beginPath();
  pts.forEach((v,i)=>i?ctx.lineTo(X(i),Y(v)):ctx.moveTo(X(i),Y(v)));ctx.stroke();
  const g=ctx.createLinearGradient(0,0,0,h);g.addColorStop(0,cum>=0?'rgba(63,185,80,.18)':'rgba(248,81,73,.18)');g.addColorStop(1,'rgba(0,0,0,0)');
  ctx.fillStyle=g;ctx.beginPath();pts.forEach((v,i)=>i?ctx.lineTo(X(i),Y(v)):ctx.moveTo(X(i),Y(v)));
  ctx.lineTo(X(pts.length-1),Y(lo));ctx.lineTo(X(0),Y(lo));ctx.closePath();ctx.fill();
  ctx.fillStyle='#8b949e';ctx.font='11px monospace';ctx.fillText('$'+hi.toFixed(2),36,14);ctx.fillText('$'+lo.toFixed(2),36,h-6);
  ctx.fillStyle=col;ctx.fillText('$'+cum.toFixed(2),Math.min(w-76,X(pts.length-1)),Math.max(14,Y(pts[pts.length-1])-8));
}
window.addEventListener('resize',drawEquity);
async function refresh(){
  try{
    const [stR,pR,tR,lR,aR]=await Promise.all([fetch('/api/status'),fetch('/api/positions'),fetch('/api/trades'),fetch('/api/logs'),fetch('/api/adaptive')]);
    const status=await stR.json(),positions=await pR.json(),trades=await tR.json(),logs=await lR.json(),adaptive=await aR.json();
    const mb=document.getElementById('mode-badge');mb.textContent=status.testnet?'TESTNET':'LIVE TRADING';mb.className='badge '+(status.testnet?'testnet':'live');
    const rb=document.getElementById('run-badge');rb.textContent=status.paused?'ENTRIES PAUSED':'RUNNING';rb.className='badge '+(status.paused?'paused':'running');
    document.getElementById('pause-btn').textContent=status.paused?'▶ Resume entries':'⏸ Pause entries';
    const stale=document.getElementById('stale-badge');
    if(status.last_check&&status.check_interval_sec){const age=(Date.now()-new Date(status.last_check).getTime())/1000;
      stale.style.display=age>status.check_interval_sec*4+30?'inline-block':'none'}else stale.style.display='none';
    document.getElementById('circuits').innerHTML=(status.circuits||[]).map(c=>'<span class="badge circuit">'+esc(c)+'</span>').join('');
    setVal('equity',status.equity_usdt===null?'--':'$'+fmt(status.equity_usdt));
    setVal('cash',status.usdt_free===null?'--':'$'+fmt(status.usdt_free));
    setVal('pl',(status.total_profit>=0?'$':'-$')+fmt(Math.abs(status.total_profit)),status.total_profit);
    setVal('upl',status.unrealized_pnl===null?'--':(status.unrealized_pnl>=0?'$':'-$')+fmt(Math.abs(status.unrealized_pnl)),status.unrealized_pnl);
    setVal('dpl',(status.daily_pnl>=0?'$':'-$')+fmt(Math.abs(status.daily_pnl)),status.daily_pnl);
    setVal('winrate',fmt(status.win_rate,1)+'%');
    document.getElementById('co').textContent=status.closed_trades+' / '+status.open_positions+' of '+status.max_positions;
    document.getElementById('exposure').textContent='$'+fmt(status.exposure_usdt)+(status.exposure_pct!==null?' ('+fmt(status.exposure_pct,0)+'%)':'');
    document.getElementById('loops').textContent=status.loops_completed;
    setVal('dd',status.drawdown_pct===null?'--':'-'+fmt(status.drawdown_pct,1)+'%',-(status.drawdown_pct||0));
    document.getElementById('watching-foot').textContent='watching '+status.watchlist_size+(status.market_scan_enabled?' (auto-scan)':' (fixed)')+' pairs · up since '+new Date(status.started_at).toLocaleString();
    const pb=document.getElementById('positions-body'),entries=Object.entries(positions);
    pb.innerHTML=entries.length?entries.map(([sym,p])=>{
      const hasPx=p.current_price!==undefined,pct=hasPx?p.unrealized_pct:null;
      const cls=pct===null?'':(pct>=0?'pos':'neg');
      return '<tr><td><b>'+sym+'</b></td><td><span class="tag strat">'+esc(p.strategy||'dip')+'</span></td>'+
        '<td>$'+fmtPrice(p.buy_price)+'</td><td>'+(hasPx?'$'+fmtPrice(p.current_price):'--')+'</td>'+
        '<td class="'+cls+'">'+(pct===null?'--':(pct>=0?'+':'')+fmt(pct,2)+'%')+'</td>'+
        '<td>'+(hasPx?'$'+fmt(p.qty*p.current_price):'--')+'</td><td>'+timeAgo(p.timestamp)+'</td>'+
        '<td>'+(p.be_armed?'🔒':'—')+'</td><td>'+(p.ladder_step?p.ladder_step+'/'+(p.ladder_total||2):'—')+'</td>'+
        '<td>'+(p.trailing_active?'✅':'—')+'</td>'+
        '<td><button class="btn btn-danger btn-sm" onclick="closePosition(\\''+sym+'\\')">Close</button></td></tr>';
    }).join(''):'<tr><td colspan="11" class="empty">No open positions</td></tr>';
    const tb=document.getElementById('trades-body');
    tb.innerHTML=trades.length?trades.slice(-100).reverse().map(t=>{
      const isSell=t.action==='SELL',p=isSell?(t.profit||0):null;
      const cls=t.action==='BUY'?'buy':(p>=0?'sell-win':'sell-loss');
      return '<tr><td>'+new Date(t.time).toLocaleString()+'</td><td><span class="tag '+cls+'">'+t.action+'</span></td>'+
        '<td>'+t.symbol+'</td><td>$'+fmtPrice(t.price)+'</td><td>'+(t.qty!==null&&t.qty!==undefined?fmt(t.qty,6):'--')+'</td>'+
        '<td class="'+(isSell?(p>=0?'pos':'neg'):'')+'">'+(isSell?'$'+p.toFixed(4):'—')+'</td>'+
        '<td>'+esc(t.reason||'')+(t.strategy?' <span class="tag strat">'+esc(t.strategy)+'</span>':'')+'</td></tr>';
    }).join(''):'<tr><td colspan="7" class="empty">No trades yet</td></tr>';
    document.getElementById('trade-count').textContent=trades.length+' total · latest 100';
    const sb=document.getElementById('strategies-body');
    sb.innerHTML=(status.strategies||[]).map(s=>{
      const wr=s.closed?(s.wins/s.closed*100).toFixed(0)+'%':'—';
      return '<tr><td><b>'+esc(s.name)+'</b></td><td><span class="tag '+(s.enabled?'sell-win':'sell-loss')+'">'+(s.enabled?'ON':'OFF')+'</span></td>'+
        '<td>'+s.closed+'</td><td>'+wr+'</td><td class="'+(s.profit>=0?'pos':'neg')+'">$'+fmt(s.profit)+'</td>'+
        '<td style="white-space:normal">'+esc(s.detail)+'</td></tr>';
    }).join('');
    const ab=document.getElementById('adaptive-body'),ae=Object.entries(adaptive);
    ab.innerHTML=ae.length?ae.map(([sym,s])=>{
      const res=s.recent_results||[],wins=res.reduce((a,b)=>a+b,0);
      const wr=res.length?Math.round(wins/res.length*100)+'%':'—';const m=s.multiplier||1;
      const meaning=m>1.02?'stricter (recent losses)':(m<.98?'more willing (recent wins)':'neutral');
      return '<tr><td>'+sym+'</td><td>'+wr+' ('+res.length+')</td><td>'+m.toFixed(2)+'x</td><td>'+meaning+'</td></tr>';
    }).join(''):'<tr><td colspan="4" class="empty">No trades closed yet</td></tr>';
    const le=document.getElementById('logs');
    le.innerHTML=logs.length?logs.slice().reverse().map(l=>'<div class="log-line log-'+l.level+'">['+new Date(l.time).toLocaleTimeString()+'] '+esc(l.message)+'</div>').join(''):'<div class="empty">No logs yet</div>';
    lastSells=trades.filter(t=>t.action==='SELL'&&t.profit!==null&&t.profit!==undefined);
    drawEquity();
  }catch(e){console.error('refresh failed',e)}
}
refresh();setInterval(refresh,5000);
</script>
</body></html>"""

class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def _html(self, html):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def _authorized(self, body):
        cfg = _ctx["cfg"]
        if not cfg["CONTROL_TOKEN"]: return True
        supplied = self.headers.get("X-Control-Token", "") or (body.get("token") or "")
        return supplied == cfg["CONTROL_TOKEN"]

    def do_GET(self):
        path = urlparse(self.path).path
        cfg, shared, store = _ctx["cfg"], _ctx["shared"], _ctx["store"]
        if path in ("/", "/dashboard"):
self._html(DASHBOARD_HTML); return
        if path == "/api/status":
            with _ctx["state_lock"]:
                positions = {s: dict(p) for s, p in _ctx["state"]["positions"].items()}
                stats = dict(_ctx["state"].get("symbol_stats") or {})
            total = store.profit_summary(); daily = store.daily_summary(datetime.utcnow().date().isoformat())
            unreal = 0.0; exposure = 0.0
            for sym, p in positions.items():
                lp = shared.get("last_prices", {}).get(sym)
                if lp:
                    unreal += (lp - p["buy_price"]) * p.get("qty", 0)
                    exposure += lp * p.get("qty", 0)
            circuits = []
            if shared.get("paused"): circuits.append("entries paused")
            if daily["profit"] <= -abs(cfg["DAILY_MAX_LOSS_USDT"]): circuits.append("daily loss limit")
            equity, peak = shared.get("equity"), shared.get("peak_equity")
            dd = ((peak - equity) / peak * 100) if (equity and peak) else None
            if dd is not None and dd >= cfg["MAX_DRAWDOWN_PCT"]: circuits.append("max drawdown")
            cg = shared.get("crash_guard_until")
            if cg and datetime.fromisoformat(cg) > datetime.utcnow(): circuits.append("crash guard")
            self._json({
                "status": "alive", "testnet": cfg["USE_TESTNET"],
                "started_at": shared.get("started_at"), "last_check": shared.get("last_check"),
                "loops_completed": shared.get("loops", 0), "check_interval_sec": cfg["CHECK_INTERVAL_SEC"],
                "open_positions": len(positions), "max_positions": cfg["MAX_CONCURRENT_POSITIONS"],
                "closed_trades": total["closed"], "win_rate": (total["wins"]/total["closed"]*100) if total["closed"] else 0,
                "total_profit": total["profit"], "unrealized_pnl": unreal,
                "daily_pnl": daily["profit"], "equity_usdt": equity, "usdt_free": shared.get("usdt_free"),
                "peak_equity": peak, "drawdown_pct": dd,
                "exposure_usdt": exposure,
                "exposure_pct": (exposure/equity*100) if equity else None,
                "paused": shared.get("paused", False), "circuits": circuits,
                "market_scan_enabled": cfg["MARKET_SCAN_ENABLED"],
                "watchlist_size": len(shared.get("watchlist") or []),
                "watchlist": shared.get("watchlist") or [],
                "strategies": _strategy_rows(cfg, store.strategy_summary()),
            }); return
        if path == "/api/positions":
            with _ctx["state_lock"]:
                positions = {s: dict(p) for s, p in _ctx["state"]["positions"].items()}
            out = {}
            for sym, p in positions.items():
                lp = _ctx["shared"].get("last_prices", {}).get(sym)
                if lp is not None:
                    p["current_price"] = lp
                    p["unrealized_pct"] = (lp - p["buy_price"]) / p["buy_price"] * 100
                p.setdefault("ladder_total", len(cfg["LADDER_TAKES"]))
                out[sym] = p
            self._json(out); return
        if path == "/api/trades":
            self._json(_ctx["store"].get_trades(limit=500)); return
        if path == "/api/logs":
            with _ctx["log_lock"]:
                self._json(list(_ctx["log_buffer"])); return
        if path == "/api/adaptive":
            with _ctx["state_lock"]:
                self._json({s: dict(v) for s, v in (_ctx["state"].get("symbol_stats") or {}).items()}); return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try: body = json.loads(raw.decode() or "{}")
            except Exception: body = {}
            if not isinstance(body, dict): body = {}
            if not self._authorized(body):
                return self._json({"ok": False, "msg": "unauthorized — set CONTROL_TOKEN"}, 401)
            c = _ctx["controller"]
            if path == "/api/close":
                r = c.close_all() if body.get("all") else c.close_symbol(body.get("symbol") or "")
                return self._json(r)
            if path == "/api/pause":
                return self._json(c.set_paused(not _ctx["shared"].get("paused", False)))
            self._json({"ok": False, "msg": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            try: self._json({"ok": False, "msg": str(e)}, 500)
            except Exception: pass

    def log_message(self, *a):
        pass  # silence default request logging