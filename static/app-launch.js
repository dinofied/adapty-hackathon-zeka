(() => {
  let updateCheck;
  const bounded = (promise, ms = 2000) => new Promise((resolve) => {
    const timer = setTimeout(() => resolve(null), ms);
    Promise.resolve(promise).then((value) => { clearTimeout(timer); resolve(value); },
      () => { clearTimeout(timer); resolve(null); });
  });

  function checkForUpdate() {
    if (!("serviceWorker" in navigator)) return Promise.resolve(null);
    if (updateCheck) return updateCheck;
    updateCheck = bounded((async () => {
      const controller = navigator.serviceWorker.controller;
      const registration = await navigator.serviceWorker.register("/sw.js", { updateViaCache:"none" });
      const previous = controller || registration.active;
      await registration.update();
      const worker = registration.installing || registration.waiting;
      if (worker && !["activated", "redundant"].includes(worker.state)) {
        await new Promise((resolve) => {
          const changed = () => {
            if (worker.state === "installed") worker.postMessage({ type:"SKIP_WAITING" });
            if (["activated", "redundant"].includes(worker.state)) {
              worker.removeEventListener("statechange", changed);
              resolve();
            }
          };
          worker.addEventListener("statechange", changed);
          changed();
        });
      }
      return { registration, updated:registration.active !== previous };
    })()).finally(() => { updateCheck = null; });
    return updateCheck;
  }

  function appUrl(raw) {
    const url = new URL(raw, location.origin);
    if (url.origin !== location.origin || url.pathname !== "/app") throw new Error("Invalid app link");
    return url;
  }

  function androidLink(raw) {
    const url = appUrl(raw);
    return `intent://${url.host}${url.pathname}${url.search}#Intent;scheme=${url.protocol.slice(0, -1)};package=com.lumen.referralsdemo;S.browser_fallback_url=${encodeURIComponent(url.href)};end`;
  }

  async function openApp(raw) {
    const url = appUrl(raw);
    const result = await checkForUpdate();
    if (/android/i.test(navigator.userAgent)) {
      location.assign(androidLink(url.href));
      return;
    }
    const worker = result?.registration.active;
    if (worker && typeof MessageChannel !== "undefined") {
      const channel = new MessageChannel();
      try {
        const opened = await bounded(new Promise((resolve) => {
          channel.port1.onmessage = (event) => resolve(event.data?.opened === true);
          worker.postMessage({ type:"OPEN_LUMEN", url:url.href, refresh:result.updated }, [channel.port2]);
        }), 1000);
        if (opened) return;
      } finally {
        channel.port1.close(); channel.port2.close();
      }
    }
    location.assign(url.href);
  }

  window.LumenLaunch = { checkForUpdate, openApp, androidLink };
})();
