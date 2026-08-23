document.addEventListener('DOMContentLoaded', async () => {
  let config, pickerMap, pickerMarker, activeStopRow, databaseState;
  const $=selector=>document.querySelector(selector), value=id=>$(id).value.trim(), number=id=>Number($(id).value);

  function stopRow(stop={id:'',name:'',lat:config.dashboard.defaultCenter.lat,lon:config.dashboard.defaultCenter.lon,sequence:1}){
    const row=document.createElement('div');row.className='editor-row stop-row';
    row.innerHTML=`<input class="field mono" data-key="id" value="${AT.escape(stop.id)}" aria-label="Stop ID"><input class="field" data-key="name" value="${AT.escape(stop.name)}" aria-label="Stop name"><input class="field mono" data-key="lat" type="number" step="any" value="${stop.lat}" aria-label="Latitude"><input class="field mono" data-key="lon" type="number" step="any" value="${stop.lon}" aria-label="Longitude"><input class="field mono" data-key="sequence" type="number" value="${stop.sequence}" aria-label="Sequence"><div class="row-actions"><button class="button small map-pick" type="button">Set on map</button><button class="icon-button remove" type="button" aria-label="Remove stop">×</button></div>`;
    row.querySelector('.remove').onclick=()=>row.remove();row.querySelector('.map-pick').onclick=()=>openPicker(row);return row;
  }
  function busRow(bus={id:'',name:'',color:'#38bdf8'}){
    const row=document.createElement('div');row.className='editor-row bus-row';
    row.innerHTML=`<input class="field mono" data-key="id" value="${AT.escape(bus.id)}" aria-label="Bus ID"><input class="field" data-key="name" value="${AT.escape(bus.name)}" aria-label="Bus name"><div class="color-field"><input type="color" value="${AT.escape(bus.color)}"><input class="field mono" data-key="color" value="${AT.escape(bus.color)}" aria-label="Color"></div><button class="icon-button remove" type="button" aria-label="Remove bus">×</button>`;
    const color=row.querySelector('[type=color]'),text=row.querySelector('[data-key=color]');color.oninput=()=>text.value=color.value;text.oninput=()=>{if(/^#[0-9a-f]{6}$/i.test(text.value))color.value=text.value};row.querySelector('.remove').onclick=()=>row.remove();return row;
  }
  function render(){
    $('#route-name').value=config.route.name;$('#route-loop').checked=config.route.loop;
    $('#stop-rows').replaceChildren();$('#bus-rows').replaceChildren();
    config.route.stops.forEach(stop=>$('#stop-rows').append(stopRow(stop)));config.route.buses.forEach(bus=>$('#bus-rows').append(busRow(bus)));
    $('#wifi-ssid').value=config.network.wifiSsid;$('#wifi-env').value=config.network.wifiPasswordEnvVar;$('#mqtt-host').value=config.network.mqttBrokerHost;$('#mqtt-port').value=config.network.mqttBrokerPort;$('#mqtt-sighting').value=config.network.mqttTopicSighting;$('#mqtt-health').value=config.network.mqttTopicHealthPrefix;$('#mqtt-tls').checked=!!config.network.mqttUseTls;$('#mqtt-user-env').value=config.network.mqttUsernameEnvVar||'';$('#mqtt-pass-env').value=config.network.mqttPasswordEnvVar||'';
    $('#serial-enabled').checked=config.serialFallback.enabled;$('#serial-port').value=config.serialFallback.port;$('#serial-baud').value=config.serialFallback.baudRate;
    $('#eta-method').value=config.eta.method;$('#eta-speed').value=config.eta.assumedSpeedKmph;$('#refresh-ms').value=config.dashboard.refreshIntervalMs;$('#stale-seconds').value=config.dashboard.staleAfterSeconds;$('#map-zoom').value=config.dashboard.mapDefaultZoom;$('#center-lat').value=config.dashboard.defaultCenter.lat;$('#center-lon').value=config.dashboard.defaultCenter.lon;
    $('#ref-channel').textContent=config.espnow.channel;$('#ref-interval').textContent=`${config.espnow.broadcastIntervalMs} ms`;$('#ref-rs-baud').textContent=config.rs485.baudRate;$('#ref-pins').textContent=`GPIO ${config.rs485.dePin} / ${config.rs485.rePin}`;$('#admin-username').value=config.admin.username;
    $('#admin-loading').classList.add('hidden');$('#admin-sections').classList.remove('hidden');
  }
  function openPicker(row){
    activeStopRow=row;const lat=Number(row.querySelector('[data-key=lat]').value)||config.dashboard.defaultCenter.lat,lon=Number(row.querySelector('[data-key=lon]').value)||config.dashboard.defaultCenter.lon;
    $('#coordinate-picker').classList.remove('hidden');$('#picker-label').textContent=`Click the map to set coordinates for ${row.querySelector('[data-key=id]').value||'new stop'}`;
    if(!pickerMap&&window.L){pickerMap=L.map('admin-map',{fadeAnimation:false,markerZoomAnimation:false}).setView([lat,lon],16);L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png',{maxZoom:20,subdomains:'abcd',keepBuffer:1,updateWhenIdle:true,updateWhenZooming:false,attribution:'&copy; OpenStreetMap &copy; CARTO'}).addTo(pickerMap);pickerMap.on('click',event=>{if(!activeStopRow)return;activeStopRow.querySelector('[data-key=lat]').value=event.latlng.lat.toFixed(6);activeStopRow.querySelector('[data-key=lon]').value=event.latlng.lng.toFixed(6);if(pickerMarker)pickerMarker.setLatLng(event.latlng);else pickerMarker=L.marker(event.latlng).addTo(pickerMap);});}else if(pickerMap){pickerMap.setView([lat,lon],16);}
    if(pickerMap){if(pickerMarker)pickerMarker.setLatLng([lat,lon]);else pickerMarker=L.marker([lat,lon]).addTo(pickerMap);setTimeout(()=>pickerMap.invalidateSize(),50);}
    $('#coordinate-picker').scrollIntoView({behavior:'smooth',block:'center'});
  }
  const collectStops=()=>[...document.querySelectorAll('.stop-row')].map(row=>({id:row.querySelector('[data-key=id]').value.trim(),name:row.querySelector('[data-key=name]').value.trim(),lat:Number(row.querySelector('[data-key=lat]').value),lon:Number(row.querySelector('[data-key=lon]').value),sequence:Number(row.querySelector('[data-key=sequence]').value)}));
  const collectBuses=()=>[...document.querySelectorAll('.bus-row')].map(row=>({id:row.querySelector('[data-key=id]').value.trim(),name:row.querySelector('[data-key=name]').value.trim(),color:row.querySelector('[data-key=color]').value.trim()}));
  async function save(payload,button){
    const old=button.textContent;button.disabled=true;button.textContent='Saving…';
    try{const updated=await AT.fetchJSON('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});config={...config,...updated};AT.toast(`${Object.keys(payload).join(' & ')} saved successfully.`);loadDatabase();}
    catch(error){if(error.status===401){location.href='/admin';return;}AT.toast(error.message,'error');}
    finally{button.disabled=false;button.textContent=old;}
  }
  const databaseButtons=()=>['#db-refresh','#db-storage-toggle','#db-reset','#db-delete'].map($).filter(Boolean);
  const formatBytes=bytes=>{if(!bytes)return '0 bytes';const units=['bytes','KB','MB','GB'];const index=Math.min(units.length-1,Math.floor(Math.log(bytes)/Math.log(1024)));return `${(bytes/Math.pow(1024,index)).toFixed(index?1:0)} ${units[index]}`;};
  function jsonRecord(title,subtitle,value){
    const article=document.createElement('article');article.className='database-record';
    const heading=document.createElement('div');heading.innerHTML=`<strong>${AT.escape(title)}</strong><small>${AT.escape(subtitle||'')}</small>`;
    const pre=document.createElement('pre');pre.textContent=JSON.stringify(value,null,2);article.append(heading,pre);return article;
  }
  function renderDatabase(data){
    databaseState=data;const sqlite=data.mode==='sqlite',exists=data.databaseExists;
    $('#db-mode').textContent=sqlite?'SQLite':'JSON file';$('#db-mode').className=sqlite?'storage-mode sqlite':'storage-mode json';
    $('#db-mode-note').textContent=sqlite?'Admin saves are written to the database':'Admin saves are written atomically to config.json';
    $('#db-exists').textContent=exists?'Available':'Not created';$('#db-size').textContent=exists?formatBytes(data.databaseSizeBytes):'Enable SQLite to create it';$('#db-path').textContent=data.databasePath;
    $('#db-storage-toggle').textContent=sqlite?'Switch to JSON':'Store settings in SQLite';
    $('#db-reset').disabled=!exists;$('#db-delete').disabled=!exists;
    const tables=$('#db-tables');tables.replaceChildren();
    if(data.tables.length){data.tables.forEach(table=>{const node=document.createElement('div');node.className='database-table-chip';node.innerHTML=`<span>${AT.escape(table.name)}</span><b>${table.rowCount}</b><small>rows</small>`;tables.append(node);});}
    else tables.innerHTML='<div class="database-empty">No SQLite tables yet. Choose “Store settings in SQLite” to create the database.</div>';
    const rows=$('#db-configuration');rows.replaceChildren();
    if(data.configuration.length)data.configuration.forEach(row=>rows.append(jsonRecord(row.section,`Updated ${row.updatedAt}`,row.value)));
    else rows.innerHTML='<div class="database-empty">No configuration rows in the database.</div>';
    const history=$('#db-history');history.replaceChildren();$('#db-history-count').textContent=data.history.length;
    if(data.history.length)data.history.forEach(row=>history.append(jsonRecord(`#${row.id} · ${row.sections.join(', ')}`,row.updatedAt,row.configuration)));
    else history.innerHTML='<div class="database-empty">No configuration history.</div>';
    const metadata=$('#db-metadata');metadata.replaceChildren();$('#db-metadata-count').textContent=data.metadata.length;
    if(data.metadata.length)data.metadata.forEach(row=>metadata.append(jsonRecord(row.key,'metadata',row.value)));
    else metadata.innerHTML='<div class="database-empty">No database metadata.</div>';
  }
  async function loadDatabase(){
    try{renderDatabase(await AT.fetchJSON('/api/admin/database'));}
    catch(error){if(error.status===401){location.href='/admin';return;}$('#db-tables').innerHTML=`<div class="form-error">${AT.escape(error.message)}</div>`;}
  }
  async function databaseAction(url,method='POST',confirmation){
    databaseButtons().forEach(button=>button.disabled=true);
    try{
      const options={method};
      if(confirmation){options.headers={'Content-Type':'application/json'};options.body=JSON.stringify({confirmation});}
      const data=await AT.fetchJSON(url,options);renderDatabase(data);config=await AT.fetchJSON('/api/config');render();AT.toast('Database operation completed successfully.');
    }catch(error){if(error.status===401){location.href='/admin';return;}AT.toast(error.message,'error');await loadDatabase();}
    finally{databaseButtons().forEach(button=>button.disabled=false);await loadDatabase();}
  }
  function confirmedWord(word,message){
    const answer=window.prompt(`${message}\n\nType ${word} to continue.`);
    if(answer===null)return false;
    if(answer!==word){AT.toast(`Confirmation did not match ${word}. No changes were made.`,'error');return false;}
    return true;
  }
  $('#db-refresh').onclick=loadDatabase;
  $('#db-storage-toggle').onclick=()=>{if(!databaseState){loadDatabase();return;}if(databaseState.mode==='sqlite')databaseAction('/api/admin/database/use-json');else databaseAction('/api/admin/database/activate');};
  $('#db-reset').onclick=()=>{if(confirmedWord('RESET','Restore the first SQLite snapshot and clear database history?'))databaseAction('/api/admin/database/reset','POST','RESET');};
  $('#db-delete').onclick=()=>{if(confirmedWord('DELETE','Delete the SQLite database file? Active settings will first be preserved in config.json.'))databaseAction('/api/admin/database','DELETE','DELETE');};

  document.querySelectorAll('[data-save]').forEach(button=>button.addEventListener('click',()=>{
    const action=button.dataset.save;
    if(action==='route')return save({route:{name:value('#route-name'),loop:$('#route-loop').checked,stops:collectStops(),buses:collectBuses()}},button);
    if(action==='network')return save({network:{wifiSsid:value('#wifi-ssid'),wifiPasswordEnvVar:value('#wifi-env'),mqttBrokerHost:value('#mqtt-host'),mqttBrokerPort:number('#mqtt-port'),mqttTopicSighting:value('#mqtt-sighting'),mqttTopicHealthPrefix:value('#mqtt-health'),mqttUseTls:$('#mqtt-tls').checked,mqttUsernameEnvVar:value('#mqtt-user-env'),mqttPasswordEnvVar:value('#mqtt-pass-env')},serialFallback:{enabled:$('#serial-enabled').checked,port:value('#serial-port'),baudRate:number('#serial-baud')}},button);
    if(action==='thresholds')return save({eta:{method:value('#eta-method'),assumedSpeedKmph:number('#eta-speed')},dashboard:{refreshIntervalMs:number('#refresh-ms'),mapDefaultZoom:number('#map-zoom'),staleAfterSeconds:number('#stale-seconds'),defaultCenter:{lat:number('#center-lat'),lon:number('#center-lon')}}},button);
    if(action==='credentials'){const password=value('#admin-password');if(!password){AT.toast('Enter a new password before changing credentials.','error');return;}return save({admin:{username:value('#admin-username'),password}},button).then(()=>{$('#admin-password').value='';});}
  }));
  $('#add-stop').onclick=()=>{const sequences=collectStops().map(s=>s.sequence).filter(Number.isFinite);$('#stop-rows').append(stopRow({id:'',name:'',lat:number('#center-lat'),lon:number('#center-lon'),sequence:sequences.length?Math.max(...sequences)+1:1}));};
  $('#add-bus').onclick=()=>$('#bus-rows').append(busRow());$('#close-picker').onclick=()=>{$('#coordinate-picker').classList.add('hidden');activeStopRow=null;};
  try{config=await AT.fetchJSON('/api/config');render();loadDatabase();}catch(error){if(error.status===401){location.href='/admin';return;}$('#admin-loading').outerHTML=`<div class="form-error">${AT.escape(error.message)}</div>`;}
});
