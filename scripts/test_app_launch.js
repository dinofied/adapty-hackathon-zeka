// Browser API regressions without dependencies. Run: node scripts/test_app_launch.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const root = path.join(__dirname, "..", "static");
const launchSource = fs.readFileSync(path.join(root, "app-launch.js"), "utf8");
const workerSource = fs.readFileSync(path.join(root, "sw.js"), "utf8");
function inlineSource(file) {
  return [...fs.readFileSync(path.join(root, file), "utf8").matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)]
    .map((match) => match[1]).join("\n");
}
const appSource = inlineSource("app.html");
const inviteSource = inlineSource("invite.html").replaceAll("{{VALID}}", "1").replaceAll("{{CODE}}", "ANA-1234ABCD").replaceAll("{{APK}}", "1");
const origin = "https://lumen-referrals.onrender.com";
const link = `${origin}/app?ref=ANA-1234ABCD`;

class Channel {
  constructor() {
    this.port1 = { close() {} };
    this.port2 = { close() {}, postMessage:(data) => queueMicrotask(() => this.port1.onmessage?.({ data })) };
  }
}

function launchFixture(serviceWorker, userAgent = "Desktop") {
  const navigation = [];
  const context = vm.createContext({
    window:{}, navigator:{ userAgent, ...(serviceWorker ? { serviceWorker } : {}) },
    location:{ origin, assign:(url) => navigation.push(url) }, URL, MessageChannel:Channel,
    setTimeout:(fn, ms) => setTimeout(fn, Math.min(ms, 25)), clearTimeout,
  });
  vm.runInContext(launchSource, context);
  return { launch:context.window.LumenLaunch, navigation };
}

test("an update activates before opening and passes the referral to the app worker", async () => {
  const messages = [];
  const old = {}, handlers = new Set();
  const active = { postMessage:(message, ports) => { messages.push(message); ports[0].postMessage({ opened:true }); } };
  const registration = {
    active:old, installing:null,
    async update() {
      registration.installing = {
        state:"installed", addEventListener:(_, fn) => handlers.add(fn), removeEventListener:(_, fn) => handlers.delete(fn),
        postMessage(message) {
          assert.equal(message.type, "SKIP_WAITING");
          queueMicrotask(() => {
            registration.active = active; serviceWorker.controller = active;
            registration.installing.state = "activated";
            for (const handler of handlers) handler();
          });
        },
      };
    },
  };
  const serviceWorker = {
    controller:old,
    async register(url, options) {
      assert.equal(url, "/sw.js");
      assert.equal(options.updateViaCache, "none");
      return registration;
    },
  };
  const fixture = launchFixture(serviceWorker);
  await fixture.launch.openApp(link);
  assert.equal(messages[0].url, link);
  assert.equal(messages[0].refresh, true);
  assert.equal(fixture.navigation.length, 0);
});

test("no update just opens the existing app; a refused focus falls back to navigation", async () => {
  for (const opened of [true, false]) {
    const messages = [];
    const active = { postMessage:(message, ports) => { messages.push(message); ports[0].postMessage({ opened }); } };
    const fixture = launchFixture({ controller:active, register:async () => ({ active, update:async () => {} }) });
    await fixture.launch.openApp(link);
    assert.equal(messages[0].refresh, false);
    assert.deepEqual(fixture.navigation, opened ? [] : [link]);
  }
});

test("unsupported, failing and stalled update checks still open the referral URL", async () => {
  const failure = async () => { throw new Error("Offline"); };
  for (const serviceWorker of [
    null,
    { register:failure },
    { register:async () => ({ update:failure }) },
    { register:async () => ({ update:() => new Promise(() => {}) }) },
  ]) {
    const fixture = launchFixture(serviceWorker);
    await fixture.launch.openApp(link);
    assert.deepEqual(fixture.navigation, [link]);
  }
});

test("overlapping checks share one update attempt and missing worker acknowledgements cannot block opening", async () => {
  let registrations = 0;
  const active = { postMessage() {} };
  const fixture = launchFixture({
    register:async () => { registrations++; return { active, update:async () => {} }; },
  });
  const first = fixture.launch.checkForUpdate();
  assert.equal(first, fixture.launch.checkForUpdate());
  await fixture.launch.openApp(link);
  assert.equal(registrations, 1);
  assert.deepEqual(fixture.navigation, [link]);
});

test("Android opens the installed wrapper with the code and a same-code web fallback", async () => {
  const fixture = launchFixture(null, "Mozilla Android");
  await fixture.launch.openApp(link);
  const intent = fixture.navigation[0];
  assert.ok(intent.startsWith("intent://lumen-referrals.onrender.com/app?ref=ANA-1234ABCD#Intent;"));
  assert.ok(intent.includes("package=com.lumen.referralsdemo;"));
  const fallback = intent.match(/S\.browser_fallback_url=([^;]+)/)[1];
  assert.equal(decodeURIComponent(fallback), link);
  await assert.rejects(fixture.launch.openApp("https://example.com/app"));
  await assert.rejects(fixture.launch.openApp(`${origin}/dashboard`));
  assert.equal(fixture.navigation.length, 1);
});

test("Android navigates in the tap's turn even when the update check stalls", async () => {
  for (const registration of [new Promise(() => {}), { update:() => new Promise(() => {}) }]) {
    let checks = 0;
    const fixture = launchFixture({
      register:() => { checks++; return Promise.resolve(registration); },
    }, "Mozilla Android");
    const opening = fixture.launch.openApp(link);
    assert.equal(fixture.navigation.length, 1);
    assert.ok(fixture.navigation[0].startsWith("intent://"));
    assert.equal(checks, 1);
    await opening;
  }
});

async function workerOpen(windows, { url = link, refresh = false, failOpen = false } = {}) {
  const handlers = {}, openedUrls = [], focused = [];
  const self = {
    addEventListener:(type, handler) => { handlers[type] = handler; },
    clients:{
      matchAll:async () => windows,
      async openWindow(value) {
        openedUrls.push(value);
        if (failOpen) throw new Error("No user activation");
        return { focus:async () => focused.push(value) };
      },
    },
  };
  vm.runInNewContext(workerSource, { self, location:{ origin }, URL });
  let pending, reply;
  handlers.message({
    data:{ type:"OPEN_LUMEN", url, refresh }, source:{ id:"invite" },
    ports:[{ postMessage:(value) => { reply = value; } }], waitUntil:(promise) => { pending = promise; },
  });
  await pending;
  return { reply, openedUrls, focused };
}

test("worker focuses an unchanged app, but navigates for a changed code or updated shell", async () => {
  for (const [currentUrl, refresh, shouldNavigate] of [[link, false, false], [link, true, true], [`${origin}/app`, false, true]]) {
    const navigation = [], focused = [];
    const client = {
      id:"app", url:currentUrl, navigate:async (url) => { navigation.push(url); return client; },
      focus:async () => focused.push(true),
    };
    const result = await workerOpen([client], { refresh });
    assert.equal(result.reply.opened, true);
    assert.deepEqual(navigation, shouldNavigate ? [link] : []);
    assert.equal(focused.length, 1);
    assert.deepEqual(result.openedUrls, []);
  }
});

test("worker opens a missing app and acknowledges browser refusal or invalid destinations", async () => {
  const result = await workerOpen([{ id:"invite", url:`${origin}/r/ANA-1234ABCD` }]);
  assert.equal(result.reply.opened, true);
  assert.deepEqual(result.openedUrls, [link]);
  assert.equal((await workerOpen([], { failOpen:true })).reply.opened, false);
  for (const url of ["https://example.com/app", `${origin}/dashboard`]) {
    const invalid = await workerOpen([], { url });
    assert.equal(invalid.reply.opened, false);
    assert.deepEqual(invalid.openedUrls, []);
  }
});

async function appFixture({ pathname = "/app", search = "", hash = "", pending = null, lookup = null, offline = false, me = null, signupSuccess = false,
  page = "app", userAgent = "Desktop", displayMode = "browser", referrer = "", iosStandalone = false, session = new Map() } = {}) {
  const nodes = {}, requests = [], removed = [], historyChanges = [], handlers = {};
  const location = { origin, pathname, search, hash, href:`${origin}${pathname}${search}${hash}` };
  const account = me && { name:"Friend", code:"SELF-87654321", offer:{ friend_days:7, inviter_days:14 }, mode:"mock", ...me };
  const getNode = (id) => nodes[id] ||= { value:"", href:"", innerHTML:"", addEventListener() {} };
  const context = vm.createContext({
    window:{}, navigator:{ userAgent, standalone:iosStandalone }, location,
    history:{ replaceState:(_, __, value) => {
      historyChanges.push(value);
      const url = new URL(value, origin);
      Object.assign(location, { href:url.href, pathname:url.pathname, search:url.search, hash:url.hash });
    } },
    document:{ referrer, getElementById:getNode, querySelector:() => null, querySelectorAll:() => [] }, URL, URLSearchParams,
    matchMedia:(query) => ({ matches:query === `(display-mode: ${displayMode})` }),
    sessionStorage:session && { getItem:(key) => session.get(key), setItem:(key, value) => session.set(key, value) },
    localStorage:{
      getItem:(key) => key === "lumen_ref" ? "OLD-12345678" : key === "lumen_token" && me && !signupSuccess ? "fixture-token" : null,
      removeItem:(key) => removed.push(key), setItem() {},
    },
    addEventListener:(type, handler) => { handlers[type] = handler; }, setInterval() {}, setTimeout, clearTimeout, Image:class {},
    fetch:async (url, options) => {
      requests.push({ url, body:options.body && JSON.parse(options.body) });
      if (url.startsWith("/api/app/ref?") && offline) throw new Error("Offline");
      const body = url === "/api/app/pending-ref" ? pending || { code:null }
        : url.startsWith("/api/app/ref?") ? lookup || { code:null }
        : url === "/api/app/me" ? account
        : signupSuccess ? { token:"fixture-token", existing:false, referred_by:"Ana", actions:[] } : { error:"Signup rejected by fixture" };
      return { ok:!options.body || signupSuccess, json:async () => body };
    },
  });
  vm.runInContext(launchSource, context);
  vm.runInContext(page === "invite" ? inviteSource : appSource, context);
  if (page === "app") await vm.runInContext("start()", context);
  return {
    nodes, requests, removed, historyChanges, location,
    installPrompt() {
      handlers.beforeinstallprompt({ preventDefault() {}, prompt() {}, userChoice:Promise.resolve() });
    },
    async signup(code) {
      getNode("ref-code").value = code; getNode("n").value = "Friend"; getNode("e").value = "friend@example.com";
      const button = {};
      context.submitEvent = { preventDefault() {}, target:{ querySelector:() => button } };
      await vm.runInContext("signup(submitEvent)", context);
    },
  };
}

test("installed apps hide installation prompts on signup, in the studio and on invitations", async () => {
  for (const options of [
    { referrer:"android-app://com.lumen.referralsdemo" },
    { displayMode:"standalone" }, { displayMode:"minimal-ui" }, { displayMode:"fullscreen" },
    { userAgent:"iPhone", iosStandalone:true },
  ]) {
    for (const me of [null, {}]) {
      const fixture = await appFixture({ userAgent:"Mozilla Android", ...options, me });
      fixture.installPrompt();
      assert.ok(!fixture.nodes.root.innerHTML.includes("Install Lumen"));
    }
    const invitation = await appFixture({ userAgent:"Mozilla Android", ...options, page:"invite" });
    assert.ok(invitation.nodes.cta.innerHTML.includes('href="/app?ref=ANA-1234ABCD"'));
    assert.ok(!/download|install|data-open-app/i.test(invitation.nodes.cta.innerHTML));
  }
});

test("ordinary browsers retain installation and referral-aware APK download options", async () => {
  for (const options of [
    { userAgent:"Mozilla Android" },
    { userAgent:"Mozilla Android", referrer:"android-app://com.lumen.referralsdemo.other" },
    { userAgent:"iPhone" },
  ]) {
    const fixture = await appFixture(options);
    fixture.installPrompt();
    assert.ok(fixture.nodes.root.innerHTML.includes("Install Lumen"));
    const invitation = await appFixture({ ...options, page:"invite" });
    assert.ok(invitation.nodes.cta.innerHTML.includes("data-open-app"));
    assert.ok(options.userAgent === "iPhone"
      ? invitation.nodes.cta.innerHTML.includes("To install on iPhone")
      : invitation.nodes.cta.innerHTML.includes('/download/lumen.apk?ref=ANA-1234ABCD'));
  }
});

test("Android wrapper context survives same-tab navigation but does not mark fresh browser tabs", async () => {
  const session = new Map();
  await appFixture({ userAgent:"Mozilla Android", referrer:"android-app://com.lumen.referralsdemo", session });
  const fixture = await appFixture({ userAgent:"Mozilla Android", referrer:link, session });
  fixture.installPrompt();
  assert.ok(!fixture.nodes.root.innerHTML.includes("Install Lumen"));
  const invitation = await appFixture({ userAgent:"Mozilla Android", referrer:link, session, page:"invite" });
  assert.ok(!invitation.nodes.cta.innerHTML.includes("/download/"));
  const browser = await appFixture({ userAgent:"Mozilla Android", referrer:link, page:"invite" });
  assert.ok(browser.nodes.cta.innerHTML.includes("/download/"));
});

test("Android wrapper detection still hides downloads when session storage is unavailable", async () => {
  const options = { userAgent:"Mozilla Android", referrer:"android-app://com.lumen.referralsdemo", session:null };
  const fixture = await appFixture(options);
  fixture.installPrompt();
  assert.ok(!fixture.nodes.root.innerHTML.includes("Install Lumen"));
  const invitation = await appFixture({ ...options, page:"invite" });
  assert.ok(!invitation.nodes.cta.innerHTML.includes("/download/"));
});

test("fresh install handoff pre-fills signup and keeps the code in the install manifest", async () => {
  const fixture = await appFixture({ pending:{ code:"ANA-1234ABCD", inviter:"Ana" } });
  assert.ok(fixture.nodes.root.innerHTML.includes('value="ANA-1234ABCD"'));
  assert.ok(fixture.nodes.manifest.href.includes("ref=ANA-1234ABCD"));
  await fixture.signup("ANA-1234ABCD");
  assert.equal(fixture.requests.find((request) => request.body).body.ref, "ANA-1234ABCD");
  await fixture.signup("");
  assert.equal(fixture.requests.filter((request) => request.body).at(-1).body.use_pending_ref, false);
});

test("direct visits do not consult legacy referral matching", async () => {
  const fixture = await appFixture();
  assert.ok(fixture.nodes.root.innerHTML.includes('value=""'));
  assert.ok(fixture.removed.includes("lumen_ref"));
  assert.ok(!fixture.requests.some((request) => request.url.startsWith("/api/app/ref?") || request.url === "/api/app/ref"));
});

test("signed-in launches only warn for a different non-self invitation", async () => {
  for (const [code, warning] of [["ana-1234abcd", false], ["self-87654321", false], ["OTHER-ABCDEF12", true]]) {
    const fixture = await appFixture({
      search:`?ref=${code}`, me:{ attributes:{ referred_by:"ANA-1234ABCD" } },
    });
    assert.equal(fixture.nodes.root.innerHTML.includes("Invitations apply to new accounts"), warning);
  }
});

test("successful signup removes the invitation URL but preserves other parameters and the hash", async () => {
  for (const [pathname, search] of [["/app", "?ref=ANA-1234ABCD&view=studio"], ["/r/ANA-1234ABCD", "?view=studio"]]) {
    const fixture = await appFixture({
      pathname, search, hash:"#photo", signupSuccess:true,
      lookup:{ code:"ANA-1234ABCD", inviter:"Ana" }, me:{ attributes:{ referred_by:"ANA-1234ABCD" } },
    });
    await fixture.signup("ANA-1234ABCD");
    assert.deepEqual(fixture.historyChanges, ["/app?view=studio#photo"]);
    assert.equal(fixture.location.search, "?view=studio");
    assert.equal(fixture.nodes.manifest.href, "/manifest.webmanifest");
  }
});

test("failed, mismatched and unknown explicit lookups never silently submit an organic signup", async () => {
  for (const options of [{ offline:true }, { lookup:{ code:"OTHER-12345678", inviter:"Someone else" } }, {}]) {
    const fixture = await appFixture({ search:"?ref=ANA-1234ABCD", ...options });
    assert.ok(fixture.nodes.root.innerHTML.includes('value="ANA-1234ABCD"'));
    assert.ok(!fixture.nodes.root.innerHTML.includes('class="banner"'));
    await fixture.signup("ANA-1234ABCD");
    assert.equal(fixture.requests.find((request) => request.body).body.ref, "ANA-1234ABCD");
    assert.ok(!fixture.requests.some((request) => request.url === "/api/app/pending-ref"));
    assert.deepEqual(fixture.historyChanges, []);
  }
  const invalid = await appFixture({ search:"?ref=%21%21" });
  await invalid.signup("!!");
  assert.ok(!invalid.requests.some((request) => request.body));
  assert.ok(invalid.nodes.msg.innerHTML.includes("valid invitation code"));
});
