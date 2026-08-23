document.addEventListener('DOMContentLoaded', async () => {
  const root=document.querySelector('#stop-detail-root'), stopId=root.dataset.stopId, list=document.querySelector('#arrivals-list');
  let interval=3000;try{interval=(await AT.fetchJSON('/api/config')).dashboard.refreshIntervalMs;}catch(_){}
  const uptime=value=>value==null?'—':value<3600?`${Math.floor(value/60)} min`:`${Math.floor(value/3600)}h ${Math.floor(value%3600/60)}m`;
  async function refresh(){
    try{
      const data=await AT.fetchJSON(`/api/stops/${encodeURIComponent(stopId)}/arrivals`), stop=data.stop;
      const status=document.querySelector('#detail-status');status.className=`detail-status ${stop.online?'online':''}`;status.querySelector('b').textContent=stop.online?'Relay online':'Relay offline';
      document.querySelector('#relay-telemetry').innerHTML=`<div><dt>Last heartbeat</dt><dd>${AT.age(stop.lastHeartbeatMs)}</dd></div><div><dt>Link mode</dt><dd>${AT.escape(AT.mode(stop.linkMode))}</dd></div><div><dt>Uptime</dt><dd>${uptime(stop.uptimeSec)}</dd></div><div><dt>Noise floor</dt><dd>${stop.rssiFloorNoise??'—'}${stop.rssiFloorNoise!=null?' dBm':''}</dd></div>`;
      list.innerHTML=data.arrivals.length?data.arrivals.map(arrival=>`<div class="arrival-row"><i class="arrival-color" style="background:${AT.escape(arrival.color)}"></i><div><strong>${AT.escape(arrival.name)}</strong><small>${AT.escape(arrival.busId)} · from ${AT.escape(arrival.lastStopId||'unknown')}</small></div><div class="arrival-eta"><b>${AT.eta(arrival.etaSeconds)}</b><span>${arrival.stale?'STALE ESTIMATE':'EXPECTED'}</span></div></div>`).join(''):'<div class="no-arrivals">No vehicle has reported enough data for an arrival prediction.</div>';
    }catch(error){list.innerHTML=`<div class="form-error">${AT.escape(error.message)}</div>`;}
  }
  refresh();setInterval(refresh,interval);
});
