(function(){
  // NOTE: se la pagina è HTTPS, molti browser bloccano ws://localhost (mixed content).
  // In quel caso usa la Bridge Page locale: http://127.0.0.1:17971/bridge.html
  const WS_URLS = ["ws://127.0.0.1:17970", "ws://localhost:17970"];
  const LATEST_URLS = ["/.netlify/functions/latest", "/api/latest"];

  const elStatus = document.getElementById("xtr-app-status");
  const elLast = document.getElementById("xtr-last-forward");

  function setStatus(ok){
    if(!elStatus) return;
    elStatus.textContent = ok ? "CONNESSA" : "NON CONNESSA";
    elStatus.className = ok ? "font-bold text-emerald-300" : "font-bold text-amber-300";
  }

  let ws = null;
  let connected = false;
  let lastAt = null;
  let wsIdx = 0;

  async function fetchLatest(){
    let lastErr = null;
    for(const url of LATEST_URLS){
      try{
        const r = await fetch(url, {cache:"no-store"});
        if(!r.ok) throw new Error("http_" + r.status);
        return await r.json();
      }catch(e){
        lastErr = e;
      }
    }
    throw lastErr || new Error("latest_http");
  }

  function sleep(ms){ return new Promise(res=>setTimeout(res, ms)); }

  async function pollLoop(){
    while(true){
      try{
        if(!connected){ await sleep(800); continue; }
        const j = await fetchLatest();
        const at = j?.at || j?.timestamp_utc || j?.ts || null;
        if(j && at && at !== lastAt){
          lastAt = at;
          const sig = String(j.signal||"hold").toUpperCase();
          if(elLast) elLast.textContent = `${sig} (${at})`;
          ws.send(JSON.stringify({type:"signal", data:j}));
        }
      }catch(e){
        // silent
      }
      await sleep(1200);
    }
  }

  function connect(){
    try{
      const url = WS_URLS[wsIdx] || WS_URLS[0];
      wsIdx = (wsIdx + 1) % WS_URLS.length;

      ws = new WebSocket(url);

      ws.onopen = () => {
        connected = true;
        setStatus(true);
      };

      ws.onclose = () => {
        connected = false;
        setStatus(false);
        setTimeout(connect, 1500);
      };

      ws.onerror = () => {
        connected = false;
        setStatus(false);
      };

      ws.onmessage = () => {};
    }catch(e){
      connected = false;
      setStatus(false);
      setTimeout(connect, 1500);
    }
  }

  connect();
  pollLoop();
})();