document.addEventListener('DOMContentLoaded', async () => {
  const grid=document.querySelector('#buses-grid'), empty=document.querySelector('#buses-empty');
  let interval=3000, stopNames={};
  try{const [config,stops]=await Promise.all([AT.fetchJSON('/api/config'),AT.fetchJSON('/api/stops')]);interval=config.dashboard.refreshIntervalMs;stopNames=Object.fromEntries(stops.map(s=>[s.stopId,s.name]));}catch(_){}
  async function refresh(){
    try{
      const buses=await AT.fetchJSON('/api/buses/live');
      document.querySelector('#active-count').textContent=buses.filter(b=>!b.stale&&b.lastSeenMs).length;
      empty.classList.toggle('hidden',buses.length>0);grid.classList.toggle('hidden',buses.length===0);
      grid.innerHTML=buses.map(bus=>`<article class="data-card">
        <div class="card-head"><span class="sequence-box" style="border-color:${AT.escape(bus.color)}"><i style="width:10px;height:10px;border-radius:50%;background:${AT.escape(bus.color)}"></i></span><div class="card-title"><h3>${AT.escape(bus.name)}</h3><span>${AT.escape(bus.busId)}</span></div><span class="state-badge">${bus.lastSeenMs?(bus.stale?'STALE':'LIVE'):'WAITING'}</span></div>
        <div class="card-stats"><div><span>Current → next</span><b>${AT.escape(stopNames[bus.lastStopId]||bus.lastStopId||'—')} → ${AT.escape(stopNames[bus.nextStopId]||bus.nextStopId||'—')}</b></div><div><span>Arrival estimate</span><b>${AT.eta(bus.etaSecondsToNextStop)}</b></div><div><span>Radio / sequence</span><b>${bus.lastRssi??'—'} dBm · #${bus.lastSeq??'—'}</b></div><div><span>Last sighting</span><b>${AT.age(bus.lastSeenMs)}</b></div></div>
        <div class="card-foot"><span class="mode-badge">DIRECTION ${bus.direction>0?'OUTBOUND':'RETURN'}</span><a class="card-link" href="/">Locate on map →</a></div>
      </article>`).join('');
    }catch(error){grid.innerHTML=`<div class="form-error">Backend unreachable: ${AT.escape(error.message)}</div>`;}
  }
  refresh();setInterval(refresh,interval);
});
