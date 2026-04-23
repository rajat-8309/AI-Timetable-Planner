/**
 * auth.js — Shared authentication utilities
 * Included by every page. Provides token storage, role checks,
 * auth-aware fetch, and the nav auth bar renderer.
 */

const AUTH_KEY = 'tt_auth_v1';

/* ── Storage ──────────────────────────────────────────────────── */

function _getAuthData() {
  try { return JSON.parse(localStorage.getItem(AUTH_KEY)) || null; }
  catch { return null; }
}
function _setAuthData(d) { localStorage.setItem(AUTH_KEY, JSON.stringify(d)); }
function _clearAuthData() { localStorage.removeItem(AUTH_KEY); }

/* ── Token helpers ────────────────────────────────────────────── */

function getToken()   { return _getAuthData()?.token || null; }
function getUser()    { return _getAuthData()?.user  || null; }

function isLoggedIn() {
  const d = _getAuthData();
  if (!d?.token || !d?.user) return false;
  try {
    const pl = JSON.parse(atob(d.token.split('.')[1]));
    if (pl.exp * 1000 < Date.now()) { _clearAuthData(); return false; }
    return true;
  } catch { return false; }
}

function isHead()    { return isLoggedIn() && getUser()?.role === 'head'; }
function isTeacher() { return isLoggedIn() && getUser()?.role === 'teacher'; }

/* ── Session management ───────────────────────────────────────── */

function saveSession(token, user) {
  _setAuthData({ token, user });
}

function logout() {
  _clearAuthData();
  window.location.href = 'login.html';
}

/* ── Route guards ─────────────────────────────────────────────── */

function requireLogin() {
  if (!isLoggedIn()) {
    window.location.href = 'login.html?next=' + encodeURIComponent(location.href);
    return false;
  }
  return true;
}

function requireHead() {
  if (!isLoggedIn()) {
    window.location.href = 'login.html?next=' + encodeURIComponent(location.href);
    return false;
  }
  if (!isHead()) {
    showAuthToast('Head administrator access required.', 'error');
    setTimeout(() => window.location.href = 'index.html', 1800);
    return false;
  }
  return true;
}

/* ── Auth-aware fetch ─────────────────────────────────────────── */

function authHeaders() {
  const t = getToken();
  return t ? { 'Authorization': 'Bearer ' + t } : {};
}

async function authFetch(url, opts = {}) {
  const headers = {
    'Content-Type': 'application/json',
    ...authHeaders(),
    ...(opts.headers || {}),
  };
  const res = await fetch(url, { ...opts, headers });
  if (res.status === 401) {
    _clearAuthData();
    window.location.href = 'login.html?next=' + encodeURIComponent(location.href);
    throw new Error('Session expired');
  }
  return res;
}

/* ── Auth nav bar ─────────────────────────────────────────────── */

/**
 * Call renderAuthBar() in every page's DOMContentLoaded.
 * It injects a floating auth badge in the top-right of .topnav.
 */
function renderAuthBar(navSelector) {
  const nav = document.querySelector(navSelector || '.topnav');
  if (!nav) return;

  const bar = document.createElement('div');
  bar.id = 'authBar';
  bar.style.cssText =
    'display:flex;align-items:center;gap:8px;flex-wrap:wrap;';

  if (isLoggedIn()) {
    const user = getUser();
    const roleColor = isHead()
      ? 'rgba(240,192,96,0.15);color:#f0c060;border:1px solid rgba(240,192,96,0.35)'
      : 'rgba(78,201,148,0.12);color:#4ec994;border:1px solid rgba(78,201,148,0.28)';
    const roleLabel = isHead() ? '✦ Head' : '✎ Teacher';

    bar.innerHTML = `
      <span style="font-size:12px;color:#8fa3be;font-family:'IBM Plex Mono',monospace">
        ${esc(user.username)}
      </span>
      <span style="font-family:'IBM Plex Mono',monospace;font-size:10px;
                   padding:3px 10px;border-radius:20px;letter-spacing:.06em;
                   background:${roleColor}">
        ${roleLabel}
      </span>
      ${isHead()
        ? `<a href="admin.html" style="font-size:12px;color:#8fa3be;
               text-decoration:none;padding:6px 12px;border:1px solid rgba(255,255,255,0.08);
               border-radius:6px;font-family:'IBM Plex Sans',sans-serif;
               transition:all .2s;background:transparent;"
               onmouseover="this.style.color='#f0c060';this.style.borderColor='rgba(240,192,96,0.3)'"
               onmouseout="this.style.color='#8fa3be';this.style.borderColor='rgba(255,255,255,0.08)'">
             ⚙ Admin
           </a>`
        : `<a href="change-password.html" style="font-size:12px;color:#8fa3be;
               text-decoration:none;padding:6px 12px;border:1px solid rgba(255,255,255,0.08);
               border-radius:6px;font-family:'IBM Plex Sans',sans-serif;
               transition:all .2s;background:transparent;"
               onmouseover="this.style.color='#f0c060';this.style.borderColor='rgba(240,192,96,0.3)'"
               onmouseout="this.style.color='#8fa3be';this.style.borderColor='rgba(255,255,255,0.08)'">
             🔒 Password
           </a>`}
      <button onclick="logout()"
        style="padding:6px 14px;background:rgba(224,92,92,0.08);border:1px solid rgba(224,92,92,0.2);
               border-radius:6px;color:#e05c5c;font-family:'IBM Plex Sans',sans-serif;font-size:12px;
               cursor:pointer;transition:all .2s;"
        onmouseover="this.style.background='rgba(224,92,92,0.18)'"
        onmouseout="this.style.background='rgba(224,92,92,0.08)'">
        Sign out
      </button>`;
  } else {
    bar.innerHTML = `
      <a href="login.html" style="padding:7px 18px;background:rgba(240,192,96,0.08);
         border:1px solid rgba(240,192,96,0.25);border-radius:6px;
         color:#f0c060;font-family:'IBM Plex Sans',sans-serif;font-size:13px;
         font-weight:500;text-decoration:none;transition:all .2s;"
         onmouseover="this.style.background='rgba(240,192,96,0.16)'"
         onmouseout="this.style.background='rgba(240,192,96,0.08)'">
        Sign in
      </a>`;
  }

  // Insert before existing last child (brand text) or append
  const brand = nav.querySelector('.topnav-brand');
  if (brand) nav.insertBefore(bar, brand);
  else nav.appendChild(bar);
}

/* ── Toast helper (standalone, used before page toast is ready) ── */

function showAuthToast(msg, type = 'info') {
  let c = document.getElementById('authToastC');
  if (!c) {
    c = document.createElement('div');
    c.id = 'authToastC';
    c.style.cssText =
      'position:fixed;bottom:28px;right:28px;z-index:99999;display:flex;flex-direction:column;gap:8px;';
    document.body.appendChild(c);
  }
  const colors = { success:'#4ec994', error:'#e05c5c', info:'#5ba3d9' };
  const t = document.createElement('div');
  t.style.cssText =
    `display:flex;align-items:center;gap:10px;padding:12px 18px;
     background:#0f1e35;border-radius:6px;font-size:13px;font-family:'IBM Plex Sans',sans-serif;
     color:#eef2f7;min-width:220px;box-shadow:0 8px 28px rgba(0,0,0,0.4);
     border:1px solid rgba(255,255,255,0.07);border-left:3px solid ${colors[type]||colors.info};
     animation:authTstIn .3s ease;`;
  t.textContent = msg;
  const style = document.createElement('style');
  style.textContent =
    '@keyframes authTstIn{from{opacity:0;transform:translateX(14px)}to{opacity:1;transform:translateX(0)}}';
  document.head.appendChild(style);
  c.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

function esc(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
