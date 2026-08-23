document.addEventListener('DOMContentLoaded', async () => {
  const grid=document.querySelector('#stops-grid'), empty=document.querySelector('#stops-empty');
  let interval=3000;
  try { interval=(await AT.fetchJSON('/api/config')).dashboard.refreshIntervalMs; } catch(_) {}
  async function refresh(){
    try{
      const stops=await AT.fetchJSON('/api/stops');
      document.querySelector('#online-count').textContent=stops.filter(s=>s.online).length;
      empty.classList.toggle('hidden',stops.length>0); grid.classList.toggle('hidden',stops.length===0);
      grid.innerHTML=stops.map(stop=>`<article class="data-card">
        <div class="card-head"><span class="sequence-box">${String(stop.sequence).padStart(2,'0')}</span><div class="card-title"><h3>${AT.escape(stop.name)}</h3><span>${AT.escape(stop.stopId)}</span></div><i class="status-dot ${stop.online?'online':''}" title="${stop.online?'Online':'Offline'}"></i></div>
        <div class="card-stats"><div><span>Last heartbeat</span><b>${AT.age(stop.lastHeartbeatMs)}</b></div><div><span>Coordinates</span><b>${Number(stop.lat).toFixed(4)}, ${Number(stop.lon).toFixed(4)}</b></div></div>
        <div class="card-foot"><span class="mode-badge">${AT.escape(AT.mode(stop.linkMode))}</span><a class="card-link" href="/stops/${encodeURIComponent(stop.stopId)}">View arrivals →</a></div>
      </article>`).join('');
    }catch(error){grid.innerHTML=`<div class="form-error">Backend unreachable: ${AT.escape(error.message)}</div>`;}
  }
  refresh(); setInterval(refresh,interval);
});
