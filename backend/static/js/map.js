document.addEventListener('DOMContentLoaded', async () => {
  const list = document.querySelector('#live-bus-list');
  const empty = document.querySelector('#map-empty');
  const mapError = document.querySelector('#map-error');
  const mapElement = document.querySelector('#map');
  let config, map, routeLine;
  let stopsById = {}, stopMarkers = {}, busMarkers = {}, liveBuses = [];
  let firstCenter = true;

  // Create the map immediately. Leaflet is served locally, so the UI controls do
  // not wait on a CDN before the API requests can begin.
  if (window.L) {
    map = L.map(mapElement, {
      zoomControl: true,
      attributionControl: true,
      preferCanvas: true,
      fadeAnimation: false,
      markerZoomAnimation: false
    });
    let tileErrors = 0;
    L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}', {
      maxZoom: 16,
      keepBuffer: 1,
      updateWhenIdle: true,
      updateWhenZooming: false,
      attribution: '&copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors'
    }).addTo(map)
      .on('tileload', () => { tileErrors = 0; mapError.classList.add('hidden'); })
      .on('tileerror', () => {
        tileErrors += 1;
        if (tileErrors >= 4) mapError.classList.remove('hidden');
      });

    // Keep Leaflet's internal viewport synchronized without reloading the map.
    if ('ResizeObserver' in window) {
      let resizeFrame;
      new ResizeObserver(() => {
        cancelAnimationFrame(resizeFrame);
        resizeFrame = requestAnimationFrame(() => map.invalidateSize({pan: false}));
      }).observe(mapElement);
    }
  } else {
    mapError.textContent = 'Leaflet could not load. Refresh the page to retry.';
    mapError.classList.remove('hidden');
  }

  const stopIcon = () => L.divIcon({className: '', html: '<div class="stop-pin"></div>', iconSize: [15, 15], iconAnchor: [8, 8]});
  const busIcon = (color, stale) => L.divIcon({
    className: '',
    html: `<div class="bus-pin ${stale ? 'stale' : ''}" style="background:${AT.escape(color)}"><svg viewBox="0 0 24 24"><path d="M6 17V7a3 3 0 0 1 3-3h6a3 3 0 0 1 3 3v10M6 11h12M8 17h.01M16 17h.01M8 20h8"/></svg></div>`,
    iconSize: [28, 28], iconAnchor: [14, 14]
  });

  function drawStops(stops) {
    stopsById = Object.fromEntries(stops.map(stop => [stop.stopId, stop]));
    if (!map) return;
    const currentIds = new Set(stops.map(stop => stop.stopId));
    Object.entries(stopMarkers).forEach(([id, marker]) => {
      if (!currentIds.has(id)) { map.removeLayer(marker); delete stopMarkers[id]; }
    });
    stops.forEach(stop => {
      const position = [stop.lat, stop.lon];
      if (!stopMarkers[stop.stopId]) {
        stopMarkers[stop.stopId] = L.marker(position, {icon: stopIcon(), zIndexOffset: 100})
          .addTo(map).bindTooltip(stop.name, {direction: 'top'});
      } else {
        stopMarkers[stop.stopId].setLatLng(position).setTooltipContent(stop.name);
      }
    });

    const ordered = [...stops].sort((a, b) => a.sequence - b.sequence);
    const routePoints = ordered.map(stop => [stop.lat, stop.lon]);
    if (routePoints.length > 1) {
      if (routeLine) routeLine.setLatLngs(routePoints);
      else routeLine = L.polyline(routePoints, {color: '#38BDF8', weight: 2, opacity: .45, dashArray: '6 7'}).addTo(map);
    } else if (routeLine) {
      map.removeLayer(routeLine); routeLine = null;
    }

    empty.classList.toggle('hidden', stops.length > 0);
    if (firstCenter) {
      const center = stops.length
        ? [stops.reduce((sum, stop) => sum + stop.lat, 0) / stops.length, stops.reduce((sum, stop) => sum + stop.lon, 0) / stops.length]
        : [config.dashboard.defaultCenter.lat, config.dashboard.defaultCenter.lon];
      map.setView(center, stops.length ? config.dashboard.mapDefaultZoom : Math.min(config.dashboard.mapDefaultZoom, 12), {animate: false});
      firstCenter = false;
    }
  }

  function drawBusList() {
    document.querySelector('#bus-count').textContent = liveBuses.length;
    if (!liveBuses.length) {
      list.innerHTML = '<div class="no-arrivals">No buses configured.</div>';
      return;
    }
    list.innerHTML = liveBuses.map(bus => {
      const last = stopsById[bus.lastStopId]?.name || bus.lastStopId || 'No sighting';
      const next = stopsById[bus.nextStopId]?.name || bus.nextStopId || '—';
      return `<article class="live-bus-item ${bus.stale ? 'stale' : ''}">
        <div class="live-bus-top"><i class="bus-swatch" style="background:${AT.escape(bus.color)}"></i><div><strong>${AT.escape(bus.name)}</strong><small>${AT.escape(bus.busId)}</small></div><div class="eta-value"><b>${AT.eta(bus.etaSecondsToNextStop)}</b><span>TO NEXT STOP</span></div></div>
        <div class="journey"><span>${AT.escape(last)}</span><i class="journey-line"></i><span>${AT.escape(next)}</span></div>
        <div class="item-meta"><span class="status-text ${bus.stale ? 'warn' : 'good'}">${bus.stale ? 'Stale' : 'Reporting live'}</span><span>${AT.age(bus.lastSeenMs)}</span></div>
      </article>`;
    }).join('');
  }

  let lastAnimationAt = 0;
  function animate(frameTime) {
    // Position changes occur over seconds, not milliseconds. Capping marker work
    // at 20 FPS keeps movement smooth while avoiding needless layout updates.
    if (map && !document.hidden && frameTime - lastAnimationAt >= 50) {
      lastAnimationAt = frameTime;
      liveBuses.forEach(bus => {
        const from = stopsById[bus.lastStopId], to = stopsById[bus.nextStopId];
        if (!from) return;
        let ratio = 0;
        if (!bus.stale && to && bus.legDurationSeconds > 0 && bus.lastSeenMs) {
          ratio = Math.min(1, Math.max(0, (Date.now() - bus.lastSeenMs) / 1000 / bus.legDurationSeconds));
        }
        const lat = to ? from.lat + (to.lat - from.lat) * ratio : from.lat;
        const lon = to ? from.lon + (to.lon - from.lon) * ratio : from.lon;
        const iconKey = `${bus.color}|${bus.stale}`;
        const label = `${bus.name}${bus.stale ? ' · STALE' : ''}`;
        let marker = busMarkers[bus.busId];
        if (!marker) {
          marker = busMarkers[bus.busId] = L.marker([lat, lon], {icon: busIcon(bus.color, bus.stale), zIndexOffset: 500})
            .addTo(map).bindTooltip(label, {permanent: true, direction: 'top', offset: [0, -14], className: 'bus-label'});
          marker._aeroIconKey = iconKey;
          marker._aeroLabel = label;
        } else {
          const current = marker.getLatLng();
          if (Math.abs(current.lat - lat) > 1e-8 || Math.abs(current.lng - lon) > 1e-8) {
            marker.setLatLng([lat, lon]);
          }
          // Rebuilding a Leaflet DivIcon on every animation frame causes layout
          // thrashing. Update it only when color/stale state actually changes.
          if (marker._aeroIconKey !== iconKey) {
            marker.setIcon(busIcon(bus.color, bus.stale));
            marker._aeroIconKey = iconKey;
          }
          if (marker._aeroLabel !== label) {
            marker.setTooltipContent(label);
            marker._aeroLabel = label;
          }
        }
      });
    }
    requestAnimationFrame(animate);
  }

  function applyLiveData(stops, buses) {
    drawStops(stops);
    liveBuses = buses;
    drawBusList();
    const ids = new Set(buses.map(bus => bus.busId));
    if (map) Object.entries(busMarkers).forEach(([id, marker]) => {
      if (!ids.has(id)) { map.removeLayer(marker); delete busMarkers[id]; }
    });
  }

  async function refresh() {
    try {
      const [stops, buses] = await Promise.all([
        AT.fetchJSON('/api/stops'),
        AT.fetchJSON('/api/buses/live')
      ]);
      applyLiveData(stops, buses);
    } catch (error) {
      list.innerHTML = `<div class="form-error">Backend unreachable: ${AT.escape(error.message)}</div>`;
    }
  }

  // Fetch config, stops and buses in one network round instead of waiting for
  // config first and then issuing a second pair of requests.
  try {
    const initial = await Promise.all([
      AT.fetchJSON('/api/config'),
      AT.fetchJSON('/api/stops'),
      AT.fetchJSON('/api/buses/live')
    ]);
    config = initial[0];
    applyLiveData(initial[1], initial[2]);
  } catch (error) {
    list.innerHTML = `<div class="form-error">Backend unreachable: ${AT.escape(error.message)}</div>`;
    if (map) map.setView([9.9252, 78.1198], 12, {animate: false});
    return;
  }

  if (map) requestAnimationFrame(animate);
  setInterval(refresh, config.dashboard.refreshIntervalMs);
});
