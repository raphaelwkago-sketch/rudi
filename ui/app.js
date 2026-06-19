/* Rudi dashboard — auth + API key management */

let supabase = null;
let currentMode = 'signin';
let currentKey = null;

async function init() {
  let cfg;
  try {
    cfg = await fetch('/config').then(r => r.json());
  } catch (e) {
    showAuthScreen();
    showAuthError('Cannot reach Rudi server.');
    return;
  }

  if (!cfg.supabase_url || !cfg.supabase_anon_key) {
    showAuthScreen();
    showAuthError('Server not configured (missing SUPABASE_URL / SUPABASE_ANON_KEY).');
    return;
  }

  supabase = window.supabase.createClient(cfg.supabase_url, cfg.supabase_anon_key);

  const { data: { session } } = await supabase.auth.getSession();
  if (session) {
    showDashboard(session);
  } else {
    showAuthScreen();
  }

  supabase.auth.onAuthStateChange((_event, session) => {
    if (session) showDashboard(session);
    else showAuthScreen();
  });
}

// ── auth ──────────────────────────────────────────────────────────────────────

function switchTab(mode) {
  currentMode = mode;
  document.getElementById('tab-signin').classList.toggle('active', mode === 'signin');
  document.getElementById('tab-signup').classList.toggle('active', mode === 'signup');
  document.getElementById('auth-submit').textContent = mode === 'signin' ? 'Sign in' : 'Create account';
  document.getElementById('auth-password').autocomplete = mode === 'signin' ? 'current-password' : 'new-password';
  clearAuthError();
}

async function handleAuth(e) {
  e.preventDefault();
  clearAuthError();
  const email    = document.getElementById('auth-email').value.trim();
  const password = document.getElementById('auth-password').value;
  const btn      = document.getElementById('auth-submit');
  btn.disabled = true;
  btn.textContent = 'Please wait…';

  try {
    let error;
    if (currentMode === 'signin') {
      ({ error } = await supabase.auth.signInWithPassword({ email, password }));
    } else {
      ({ error } = await supabase.auth.signUp({ email, password }));
      if (!error) {
        showAuthError('Check your email for a confirmation link.', 'info');
        btn.disabled = false;
        btn.textContent = 'Create account';
        return;
      }
    }
    if (error) throw error;
  } catch (err) {
    showAuthError(err.message || 'Authentication failed.');
    btn.disabled = false;
    btn.textContent = currentMode === 'signin' ? 'Sign in' : 'Create account';
  }
}

async function signOut() {
  await supabase.auth.signOut();
}

function showAuthError(msg, type = 'error') {
  const el = document.getElementById('auth-error');
  el.textContent = msg;
  el.className = `error-msg ${type}`;
  el.style.display = 'block';
}

function clearAuthError() {
  const el = document.getElementById('auth-error');
  el.style.display = 'none';
}

// ── screens ───────────────────────────────────────────────────────────────────

function showAuthScreen() {
  document.getElementById('auth-screen').style.display = 'flex';
  document.getElementById('dashboard-screen').style.display = 'none';
}

async function showDashboard(session) {
  document.getElementById('auth-screen').style.display = 'none';
  document.getElementById('dashboard-screen').style.display = 'block';
  document.getElementById('user-email').textContent = session.user.email;
  await loadKey(session.access_token);
}

// ── API key management ────────────────────────────────────────────────────────

async function loadKey(jwt) {
  document.getElementById('key-area-loading').style.display = 'block';
  document.getElementById('key-area').style.display = 'none';

  try {
    const res  = await fetch('/keys', { headers: { 'Authorization': `Bearer ${jwt}` } });
    const data = await res.json();
    if (data.key) {
      displayKey(data.key, data.created_at, data.last_used_at);
    } else {
      document.getElementById('key-value').textContent = '—';
      document.getElementById('key-meta').textContent = 'No key yet. Generate one below.';
      document.getElementById('key-status').textContent = 'No key';
      document.getElementById('key-status').className = 'badge badge-none';
      document.getElementById('key-area-loading').style.display = 'none';
      document.getElementById('key-area').style.display = 'block';
      currentKey = null;
    }
  } catch (e) {
    document.getElementById('key-area-loading').textContent = 'Failed to load key.';
  }
}

function displayKey(key, createdAt, lastUsedAt) {
  currentKey = key;
  document.getElementById('key-value').textContent = key;
  const created = createdAt ? new Date(createdAt).toLocaleDateString() : '—';
  const used    = lastUsedAt ? new Date(lastUsedAt).toLocaleString() : 'Never';
  document.getElementById('key-meta').textContent = `Created ${created} · Last used: ${used}`;
  document.getElementById('key-status').textContent = 'Active';
  document.getElementById('key-status').className = 'badge badge-active';
  document.getElementById('key-area-loading').style.display = 'none';
  document.getElementById('key-area').style.display = 'block';
}

async function generateKey(isRotate) {
  const { data: { session } } = await supabase.auth.getSession();
  if (!session) { signOut(); return; }

  const res  = await fetch('/keys', {
    method:  'POST',
    headers: { 'Authorization': `Bearer ${session.access_token}`, 'Content-Type': 'application/json' },
  });
  const data = await res.json();
  if (data.key) {
    displayKey(data.key, new Date().toISOString(), null);
    showToast(isRotate ? 'Key rotated — old key is now invalid.' : 'API key generated!');
  }
}

function confirmRotate() {
  if (confirm('Rotate your key? Your current key will stop working immediately.')) {
    generateKey(true);
  }
}

function copyKey() {
  if (!currentKey) return;
  navigator.clipboard.writeText(currentKey).then(() => showToast('Copied to clipboard.'));
}

// ── toast ─────────────────────────────────────────────────────────────────────

function showToast(msg) {
  let t = document.getElementById('toast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'toast';
    t.className = 'toast';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2800);
}

init();
