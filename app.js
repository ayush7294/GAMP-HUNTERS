"use strict";

/* =====================================================================
   Nearby Chat - phase 1 front end (no database yet)
   Talks to the FastAPI server:  POST /messages   {text, lat?, lon?}
   Optional (next phase):        GET  /feed       ?lat&lon&radius_km
   ===================================================================== */

const CONFIG = {
  API_BASE: "http://localhost:8000",
  FREE_MESSAGES: 3,          // default only; the server value from /session replaces it
  MAX_LEN: 500,              // must match the API limit
  REQUEST_TIMEOUT_MS: 45000, // moderation can retry, so allow a generous wait
  FEED_POLL_MS: 15000,
  RADII_KM: [10, 100, 1000],
};

/* ---------- Small helpers ---------- */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const store = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* storage unavailable */ } },
};

function loadGuestId() {
  let id = store.get("nc_guest_id");
  if (!id) {
    const n = new Uint32Array(1);
    crypto.getRandomValues(n);
    id = "guest-" + String(n[0]).padStart(10, "0");
    store.set("nc_guest_id", id);
  }
  return id;
}

function loadRadius() {
  const saved = Number(store.get("nc_radius"));
  return CONFIG.RADII_KM.includes(saved) ? saved : CONFIG.RADII_KM[0];
}

/* ---------- API helper (adds the guest id and login token to every request) ---------- */
function apiFetch(path, options = {}) {
  const headers = { "Content-Type": "application/json", "X-Device-Id": state.guestId, ...(options.headers || {}) };
  if (state.token) headers.Authorization = "Bearer " + state.token;
  return fetch(CONFIG.API_BASE + path, { ...options, headers });
}

/* ---------- State ---------- */
const state = {
  guestId: loadGuestId(),
  sent: Number(store.get("nc_sent")) || 0, // cached copy; the server is the source of truth
  radius: loadRadius(),
  location: null,   // {lat, lon} rounded to ~1 km
  locationSource: null, // "device" (browser geolocation) or "ip" (approximate, from the network)
  account: null,    // {name}
  token: store.get("nc_token") || null,
  sending: false,
  resumeSend: false,
  feedToken: 0,
};

/* ---------- Elements ---------- */
const el = {
  app: $("#app"),
  chip: $("#statusChip"), chipDot: $("#chipDot"), chipMain: $("#chipMain"), chipSub: $("#chipSub"),
  openSignup: $("#openSignup"),
  tabs: $$(".tab"),
  radiusSummary: $("#radiusSummary"),
  radiusOpts: $$(".radius-opt"),
  rings: { 10: $("#ring-10"), 100: $("#ring-100"), 1000: $("#ring-1000") },
  feed: $("#feed"), feedEmpty: $("#feedEmpty"),
  log: $("#log"), emptyState: $("#emptyState"), emptyText: $("#emptyText"),
  composer: $("#composer"), input: $("#messageInput"), counter: $("#counter"), sendBtn: $("#sendBtn"),
  locDialog: $("#locDialog"), locAllow: $("#locAllow"), locCancel: $("#locCancel"), locError: $("#locError"),
  signupDialog: $("#signupDialog"), signupForm: $("#signupForm"), signupNote: $("#signupNote"), signupCancel: $("#signupCancel"),
  signupTitle: $("#signupTitle"), nameField: $("#nameField"), nameInput: $("#nameInput"), passwordInput: $("#passwordInput"),
  authSubmit: $("#authSubmit"), authToggle: $("#authToggle"), authSwitchText: $("#authSwitchText"),
};

/* =====================================================================
   Status chip
   ===================================================================== */
function freeLeft() { return Math.max(0, CONFIG.FREE_MESSAGES - state.sent); }

function updateStatus() {
  const left = freeLeft();
  el.chipMain.textContent = state.account ? state.account.name : "Guest " + state.guestId.slice(-4);

  if (state.location) {
    el.chipSub.textContent = state.locationSource === "ip" ? "Approximate location" : "Location on";
    el.chipDot.className = "chip-dot is-on";
  } else if (left > 0) {
    el.chipSub.textContent = `${left} free message${left === 1 ? "" : "s"} left`;
    el.chipDot.className = "chip-dot is-free";
  } else {
    el.chipSub.textContent = "Location needed";
    el.chipDot.className = "chip-dot is-warn";
  }
  el.openSignup.textContent = state.account ? "Log out" : "Sign up or log in";
}

/* =====================================================================
   Chat log
   ===================================================================== */
function scrollLogToBottom() { el.log.scrollTop = el.log.scrollHeight; }

function hideEmptyState() { el.emptyState.hidden = true; }

function clearNotices() { $$(".notice", el.log).forEach((n) => n.remove()); }

function addOwnMessage(text) {
  hideEmptyState();
  const box = document.createElement("article");
  box.className = "msg";
  const p = document.createElement("p");
  p.textContent = text;
  const t = document.createElement("time");
  const now = new Date();
  t.dateTime = now.toISOString();
  t.textContent = now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  box.append(p, t);
  el.log.append(box);
  scrollLogToBottom();
}

function addPending() {
  hideEmptyState();
  const box = document.createElement("div");
  box.className = "pending";
  box.innerHTML = '<span class="dots" aria-hidden="true"><i></i><i></i><i></i></span><span>Checking your message</span>';
  el.log.append(box);
  scrollLogToBottom();
  return box;
}

function addNotice({ kind = "info", title, text, actionLabel, onAction }) {
  hideEmptyState();
  const box = document.createElement("div");
  box.className = "notice is-" + kind;
  box.setAttribute("role", kind === "info" ? "status" : "alert");
  if (title) {
    const s = document.createElement("strong");
    s.textContent = title;
    box.append(s);
  }
  if (text) {
    const p = document.createElement("span");
    p.textContent = text;
    box.append(p);
  }
  if (actionLabel && onAction) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "btn btn-primary";
    b.textContent = actionLabel;
    b.addEventListener("click", onAction);
    box.append(document.createElement("br"), b);
  }
  el.log.append(box);
  scrollLogToBottom();
}

/* =====================================================================
   Composer
   ===================================================================== */
function autosize() {
  el.input.style.height = "auto";
  el.input.style.height = Math.min(el.input.scrollHeight, 140) + "px";
}

function updateComposerState() {
  const len = el.input.value.length;
  el.counter.textContent = `${len}/${CONFIG.MAX_LEN}`;
  el.counter.classList.toggle("is-near", len > CONFIG.MAX_LEN * 0.9);
  el.sendBtn.disabled = state.sending || el.input.value.trim().length === 0;
}

function setBusy(busy) {
  state.sending = busy;
  el.input.readOnly = busy;
  updateComposerState();
}

/* =====================================================================
   Sending a message
   ===================================================================== */
function needsLocation() { return state.sent >= CONFIG.FREE_MESSAGES && !state.location; }

async function postMessage(text) {
  const body = { text };
  if (state.location) { body.lat = state.location.lat; body.lon = state.location.lon; }

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), CONFIG.REQUEST_TIMEOUT_MS);
  try {
    return await apiFetch("/messages", { method: "POST", body: JSON.stringify(body), signal: ctrl.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function sendMessage() {
  if (state.sending) return;
  const original = el.input.value;
  const text = original.trim();
  if (!text) return;

  if (needsLocation()) {
    openLocationDialog(true);
    return;
  }

  clearNotices();
  setBusy(true);
  el.input.value = "";
  autosize();
  const pending = addPending();

  const restoreText = () => { if (!el.input.value) el.input.value = original; autosize(); };

  try {
    const res = await postMessage(text);

    if (res.status === 428) {            // server says location is required
      state.sent = Math.max(state.sent, CONFIG.FREE_MESSAGES);
      store.set("nc_sent", String(state.sent));
      updateStatus();
      restoreText();
      openLocationDialog(true);
      return;
    }
    if (res.status === 503) {
      restoreText();
      const info = await res.json().catch(() => ({}));
      addNotice({ kind: "error", title: "Can't send right now", text: (info.detail || "Something is temporarily unavailable.") + " Your message was not sent." });
      return;
    }
    if (res.status === 429) {
      restoreText();
      addNotice({ kind: "error", title: "You're sending messages too fast", text: "Wait a few seconds, then send again." });
      return;
    }
    if (res.status === 422) {
      restoreText();
      addNotice({ kind: "error", title: "That message can't be sent", text: `Messages must be 1 to ${CONFIG.MAX_LEN} characters.` });
      return;
    }
    if (!res.ok) {
      restoreText();
      addNotice({ kind: "error", title: "Something went wrong", text: `The server answered with error ${res.status}. Your message was not sent.` });
      return;
    }

    const data = await res.json();
    if (typeof data.sent === "number") state.sent = data.sent;
    if (!data.allowed) {
      restoreText();
      addNotice({ kind: "blocked", title: "Message not sent", text: data.reason || "It breaks the content rules. Edit it and try again." });
      return;
    }

    // Passed the check: show it straight away, without waiting for anything else.
    addOwnMessage(data.text ?? text);
    if (typeof data.sent !== "number") state.sent += 1;
    store.set("nc_sent", String(state.sent));
    updateStatus();

    if (state.sent === CONFIG.FREE_MESSAGES && !state.location) {
      addNotice({
        kind: "info",
        title: "That was your last free message",
        text: "Turn on location to keep chatting and see messages near you.",
        actionLabel: "Turn on location",
        onAction: () => openLocationDialog(false),
      });
    }
    if (state.location) refreshFeed();
  } catch (err) {
    restoreText();
    if (err.name === "AbortError") {
      addNotice({ kind: "error", title: "The check took too long", text: "Your message was not sent. Try again." });
    } else {
      addNotice({ kind: "error", title: "Can't reach the server", text: `Make sure the API is running at ${CONFIG.API_BASE}.` });
    }
  } finally {
    pending.remove();
    setBusy(false);
    el.input.focus();
  }
}

/* =====================================================================
   Location (browser Geolocation API)
   ===================================================================== */
function requestLocation() {
  return new Promise((resolve, reject) => {
    if (!window.isSecureContext) return reject({ code: "insecure" });
    if (!("geolocation" in navigator)) return reject({ code: "unsupported" });
    navigator.geolocation.getCurrentPosition(
      (pos) => resolve({
        // ~1 km precision is plenty for "who is near me" and avoids sending exact coordinates
        lat: Number(pos.coords.latitude.toFixed(2)),
        lon: Number(pos.coords.longitude.toFixed(2)),
      }),
      (err) => reject(err),
      { enableHighAccuracy: false, timeout: 12000, maximumAge: 300000 }
    );
  });
}

function locationErrorText(err) {
  if (err.code === "insecure") return "Location only works on a secure page. Open this page from http://localhost or an https address.";
  if (err.code === "unsupported") return "This browser doesn't support location.";
  if (err.code === 1) return "Location is blocked for this site. Allow it in your browser's site settings, then try again.";
  if (err.code === 2) return "Your device couldn't work out where it is. Check that location services are on.";
  if (err.code === 3) return "Finding your location took too long, and your network location wasn't available either. Try again.";
  return "Location couldn't be turned on. Try again.";
}

function openLocationDialog(resumeSend) {
  state.resumeSend = Boolean(resumeSend);
  el.locError.hidden = true;
  if (!el.locDialog.open) el.locDialog.showModal();
}

/* Backup for browser geolocation timeouts: the server estimates a rough location from the IP address. */
async function requestIpLocation() {
  const res = await apiFetch("/ip-location");
  if (!res.ok) throw new Error("ip-location " + res.status);
  const data = await res.json();
  return { lat: data.lat, lon: data.lon, place: data.place || "" };
}

async function enableLocation() {
  el.locAllow.disabled = true;
  el.locAllow.textContent = "Finding your location";
  el.locError.hidden = true;
  let approxPlace = null; // set when the IP backup was used
  try {
    try {
      state.location = await requestLocation();
      state.locationSource = "device";
    } catch (err) {
      if (err.code !== 3) throw err; // only a timeout (3) falls back; a denial (1) is respected
      try {
        const ip = await requestIpLocation();
        state.location = { lat: ip.lat, lon: ip.lon };
        state.locationSource = "ip";
        approxPlace = ip.place || "your area";
      } catch {
        throw err; // backup failed too: show the original timeout message
      }
    }
    el.locDialog.close();
    updateStatus();
    refreshFeed();
    startFeedPolling();
    if (state.resumeSend && el.input.value.trim()) sendMessage();
    if (approxPlace) {
      addNotice({
        kind: "info",
        title: "Using your approximate location",
        text: `Your device was slow to answer, so we used your network instead (near ${approxPlace}). It can be off by many kilometres.`,
        actionLabel: "Try precise location",
        onAction: () => openLocationDialog(false),
      });
    }
  } catch (err) {
    el.locError.textContent = locationErrorText(err);
    el.locError.hidden = false;
  } finally {
    state.resumeSend = false;
    el.locAllow.disabled = false;
    el.locAllow.textContent = "Turn on location";
  }
}

async function restoreLocationIfAlreadyAllowed() {
  try {
    const perm = await navigator.permissions?.query({ name: "geolocation" });
    if (perm && perm.state === "granted") {
      state.location = await requestLocation();
      state.locationSource = "device";
      updateStatus();
      refreshFeed();
      startFeedPolling();
    }
  } catch { /* not supported or not granted: do nothing */ }
}

/* =====================================================================
   Radius + nearby feed
   ===================================================================== */
function applyRadius() {
  const km = state.radius;
  el.radiusOpts.forEach((b) => {
    const on = Number(b.dataset.km) === km;
    b.setAttribute("aria-checked", String(on));
    b.tabIndex = on ? 0 : -1;
  });
  CONFIG.RADII_KM.forEach((r) => {
    el.rings[r].classList.toggle("is-covered", r <= km);
    el.rings[r].classList.toggle("is-selected", r === km);
  });
  el.radiusSummary.textContent = `Messages from within ${km.toLocaleString()} km`;
}

function setRadius(km) {
  if (km === state.radius) return;
  state.radius = km;
  store.set("nc_radius", String(km));
  applyRadius();
  refreshFeed();
}

function toDate(v) {
  if (typeof v === "number") return new Date(v < 1e12 ? v * 1000 : v);
  return new Date(v);
}

function timeAgo(date) {
  const s = Math.max(0, (Date.now() - date.getTime()) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

function showFeedEmpty(kind) {
  el.feed.replaceChildren();
  el.feedEmpty.replaceChildren();
  const p = document.createElement("p");
  const km = state.radius.toLocaleString();

  if (kind === "no-location") {
    p.textContent = "Turn on location to see what people nearby are saying.";
    const b = document.createElement("button");
    b.type = "button";
    b.className = "btn btn-primary";
    b.textContent = "Turn on location";
    b.addEventListener("click", () => openLocationDialog(false));
    el.feedEmpty.append(p, b);
  } else if (kind === "no-endpoint") {
    p.textContent = "Nearby messages will appear here once the message database is connected.";
    el.feedEmpty.append(p);
  } else if (kind === "offline") {
    p.textContent = "Can't reach the server, so nearby messages aren't loading.";
    el.feedEmpty.append(p);
  } else {
    p.textContent = `No messages within ${km} km yet. Send one to start the conversation.`;
    el.feedEmpty.append(p);
  }
  el.feedEmpty.hidden = false;
}

function renderFeed(messages) {
  if (!messages.length) return showFeedEmpty("empty");
  el.feedEmpty.hidden = true;
  const frag = document.createDocumentFragment();

  messages.forEach((m) => {
    const li = document.createElement("li");
    li.className = "feed-item" + (m.mine ? " is-mine" : "");
    const p = document.createElement("p");
    p.textContent = m.text;

    const meta = document.createElement("div");
    meta.className = "feed-meta";
    const where = document.createElement("span");
    const km = Number(m.distance_km);
    where.textContent = m.mine ? "You" : km < 1 ? "Under 1 km away" : `About ${km.toLocaleString()} km away`;
    const when = document.createElement("span");
    when.textContent = timeAgo(toDate(m.created_at));
    meta.append(where, when);

    li.append(p, meta);
    frag.append(li);
  });
  el.feed.replaceChildren(frag);
}

async function refreshFeed() {
  if (!state.location) return showFeedEmpty("no-location");

  const token = ++state.feedToken; // ignore stale responses if the radius changes quickly
  const params = new URLSearchParams({
    lat: state.location.lat,
    lon: state.location.lon,
    radius_km: state.radius,
  });

  try {
    const res = await apiFetch(`/feed?${params}`);
    if (token !== state.feedToken) return;
    if (res.status === 404) return showFeedEmpty("no-endpoint");
    if (!res.ok) throw new Error("feed " + res.status);
    const data = await res.json();
    if (token !== state.feedToken) return;
    renderFeed(data.messages || []);
  } catch {
    if (token === state.feedToken) showFeedEmpty("offline");
  }
}

let feedTimer = null;
function startFeedPolling() {
  if (feedTimer) return;
  feedTimer = setInterval(() => {
    if (document.visibilityState === "visible" && state.location) refreshFeed();
  }, CONFIG.FEED_POLL_MS);
}

/* =====================================================================
   Accounts: sign up, log in, log out, restore session
   ===================================================================== */
let authMode = "signup";

function setAuthMode(mode) {
  authMode = mode;
  const signup = mode === "signup";
  el.signupTitle.textContent = signup ? "Create an account" : "Log in";
  el.nameField.hidden = !signup;
  el.nameInput.required = signup;
  el.authSubmit.textContent = signup ? "Create account" : "Log in";
  el.authSwitchText.textContent = signup ? "Already have an account?" : "New here?";
  el.authToggle.textContent = signup ? "Log in" : "Sign up";
  el.passwordInput.autocomplete = signup ? "new-password" : "current-password";
  el.signupNote.hidden = true;
}

function openAuth(mode) {
  setAuthMode(mode);
  el.signupDialog.showModal();
}

function showSignupNote(text, isError) {
  el.signupNote.textContent = text;
  el.signupNote.classList.toggle("is-error", Boolean(isError));
  el.signupNote.hidden = false;
}

function authErrorText(status, signup) {
  if (status === 409) return "That email already has an account. Log in instead.";
  if (status === 401) return "Email or password is incorrect.";
  if (status === 429) return "Too many attempts. Wait a minute and try again.";
  if (status === 422) return "Check your details. The password needs at least 8 characters.";
  if (status === 503) return "The service is temporarily unavailable. Try again in a moment.";
  return signup ? "The account couldn't be created. Try again." : "You couldn't be logged in. Try again.";
}

async function handleAuth(event) {
  event.preventDefault();
  if (!el.signupForm.checkValidity()) {
    el.signupForm.reportValidity();
    return;
  }
  const form = Object.fromEntries(new FormData(el.signupForm));
  const signup = authMode === "signup";
  const payload = signup
    ? { name: form.name, email: form.email, password: form.password }
    : { email: form.email, password: form.password };

  el.authSubmit.disabled = true;
  try {
    const res = await apiFetch(signup ? "/signup" : "/login", { method: "POST", body: JSON.stringify(payload) });
    if (!res.ok) {
      showSignupNote(authErrorText(res.status, signup), true);
      return;
    }
    const data = await res.json();
    state.token = data.token;
    store.set("nc_token", data.token);
    state.account = { name: data.name };
    el.signupForm.reset();
    el.signupDialog.close();
    updateStatus();
    if (state.location) refreshFeed();
  } catch {
    showSignupNote(`Can't reach the server. Make sure the API is running at ${CONFIG.API_BASE}.`, true);
  } finally {
    el.authSubmit.disabled = false;
  }
}

async function logout() {
  try { await apiFetch("/logout", { method: "POST" }); } catch { /* token is dropped locally anyway */ }
  state.token = null;
  store.set("nc_token", "");
  state.account = null;
  updateStatus();
  if (state.location) refreshFeed();
}

function setEmptyText() {
  el.emptyText.textContent =
    `Your first ${CONFIG.FREE_MESSAGES} messages need no sign-in and no location. After that, you'll turn on location so your messages reach the people around you.`;
}

/* Ask the server how many free messages are used and whether the saved login is still valid. */
async function syncSession() {
  try {
    const res = await apiFetch("/session");
    if (!res.ok) return;
    const data = await res.json();
    if (typeof data.free_messages === "number") CONFIG.FREE_MESSAGES = data.free_messages;
    if (typeof data.sent === "number") state.sent = data.sent;
    store.set("nc_sent", String(state.sent));
    state.account = data.account || null;
    if (!data.account && state.token) { state.token = null; store.set("nc_token", ""); }
    setEmptyText();
    updateStatus();
  } catch { /* server offline: keep the cached values */ }
}

/* =====================================================================
   Wiring
   ===================================================================== */
function closeOnBackdrop(dialog) {
  dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });
}

function init() {
  setEmptyText();
  el.input.maxLength = CONFIG.MAX_LEN;

  // Composer
  el.input.addEventListener("input", () => { autosize(); updateComposerState(); });
  el.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      sendMessage();
    }
  });
  el.composer.addEventListener("submit", (e) => { e.preventDefault(); sendMessage(); });

  // Radius (radio group with arrow-key support)
  el.radiusOpts.forEach((btn, i) => {
    btn.addEventListener("click", () => setRadius(Number(btn.dataset.km)));
    btn.addEventListener("keydown", (e) => {
      const keys = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
      if (!(e.key in keys)) return;
      e.preventDefault();
      const next = el.radiusOpts[(i + keys[e.key] + el.radiusOpts.length) % el.radiusOpts.length];
      setRadius(Number(next.dataset.km));
      next.focus();
    });
  });

  // Mobile tabs
  el.tabs.forEach((tab) =>
    tab.addEventListener("click", () => {
      el.app.dataset.view = tab.dataset.view;
      el.tabs.forEach((t) => {
        const on = t === tab;
        t.classList.toggle("is-active", on);
        t.setAttribute("aria-pressed", String(on));
      });
    })
  );

  // Dialogs
  el.locAllow.addEventListener("click", enableLocation);
  el.locCancel.addEventListener("click", () => { state.resumeSend = false; el.locDialog.close(); });
  el.openSignup.addEventListener("click", () => (state.account ? logout() : openAuth("signup")));
  el.authToggle.addEventListener("click", () => setAuthMode(authMode === "signup" ? "login" : "signup"));
  el.signupCancel.addEventListener("click", () => el.signupDialog.close());
  el.signupForm.addEventListener("submit", handleAuth);
  closeOnBackdrop(el.locDialog);
  closeOnBackdrop(el.signupDialog);

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && state.location) refreshFeed();
  });

  applyRadius();
  updateStatus();
  updateComposerState();
  showFeedEmpty("no-location");
  syncSession();
  restoreLocationIfAlreadyAllowed();
}

init();
