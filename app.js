/* Debelu Social Engine browser client. The server owns persisted state and generation. */

const PLATFORM_ORDER = ['instagram', 'threads', 'tiktok'];
const PLATFORMS = {
  instagram: { name: 'Instagram', short: 'IG', mark: '◎' },
  threads: { name: 'Threads', short: 'TH', mark: '@' },
  tiktok: { name: 'TikTok', short: 'TT', mark: '♪' },
};
const STATUS_LABELS = {
  IDEA: 'Idea',
  READY_FOR_REVIEW: 'Needs review',
  APPROVED: 'Approved',
  SCHEDULED: 'Scheduled',
  PUBLISHED: 'Published',
  FAILED: 'Failed',
};
const STATUS_FILTERS = {
  all: null,
  'needs-review': 'READY_FOR_REVIEW',
  approved: 'APPROVED',
  scheduled: 'SCHEDULED',
  ideas: 'IDEA',
};


const ICONS = {
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5M3 16l9 5 9-5"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18"/><path d="M8 14h3M8 17h6"/>',
  sparkles: '<path d="m12 3 1.6 5.4L19 10l-5.4 1.6L12 17l-1.6-5.4L5 10l5.4-1.6L12 3Z"/><path d="m19 15 .8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8L19 15Z"/><path d="m5 3 .5 1.5L7 5l-1.5.5L5 7l-.5-1.5L3 5l1.5-.5L5 3Z"/>',
  chart: '<path d="M4 19V5M4 19h17"/><path d="m7 15 4-4 3 2 6-7"/><path d="M17 6h3v3"/>',
  send: '<path d="m21 3-7.4 18-3.4-7.2L3 10.4 21 3Z"/><path d="M10.2 13.8 15 9"/>',
  shield: '<path d="M12 3 19 6v5c0 4.7-3 8.4-7 10-4-1.6-7-5.3-7-10V6l7-3Z"/><path d="m9 12 2 2 4-4"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  chevron: '<path d="m9 18 6-6-6-6"/>',
  left: '<path d="m15 18-6-6 6-6"/>',
  right: '<path d="m9 18 6-6-6-6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  search: '<circle cx="10.8" cy="10.8" r="6.8"/><path d="m16 16 4.5 4.5"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
  cloud: '<path d="M7 18h10a4 4 0 0 0 .5-8A6 6 0 0 0 6 9.5 4.3 4.3 0 0 0 7 18Z"/><path d="m9 14 2 2 4-4"/>',
  code: '<path d="m8 8-4 4 4 4M16 8l4 4-4 4M14 5l-4 14"/>',
  building: '<path d="M4 21V5l8-3 8 3v16M2 21h20"/><path d="M9 9h1M14 9h1M9 13h1M14 13h1M11 21v-4h2v4"/>',
  message: '<path d="M20 11.5a7.5 7.5 0 0 1-8 7.5 8.5 8.5 0 0 1-4-.9L4 20l1.2-3.1A7.2 7.2 0 0 1 4 12a7.5 7.5 0 0 1 8-7.5 7.5 7.5 0 0 1 8 7Z"/><path d="M8 12h.01M12 12h.01M16 12h.01"/>',
  idea: '<path d="M9 18h6M10 22h4"/><path d="M8.2 14.8A7 7 0 1 1 16 15c-.8.7-1 1.4-1 3H9c0-1.5-.1-2.4-.8-3.2Z"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M5 7l1 14h12l1-14M9 7V4h6v3"/>',
  lock: '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3"/>',
};

function icon(name) {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${ICONS[name] || ICONS.info}</svg>`;
}

function hydrateIcons(root = document) {
  root.querySelectorAll('[data-icon]').forEach((node) => {
    node.innerHTML = icon(node.dataset.icon);
  });
}

function escapeHTML(value = '') {
  return String(value).replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[character]);
}

function dateKey(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function dateFromKey(key) {
  const [year, month, day] = String(key).split('-').map(Number);
  return new Date(year, month - 1, day);
}

function toDate(value) {
  if (!value) return null;
  if (/^\d{4}-\d{2}-\d{2}$/.test(String(value))) return dateFromKey(value);
  const result = new Date(value);
  return Number.isNaN(result.getTime()) ? null : result;
}

function localDateTimeValue(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  const hours = String(date.getHours()).padStart(2, '0');
  const minutes = String(date.getMinutes()).padStart(2, '0');
  return `${year}-${month}-${day}T${hours}:${minutes}`;
}

function weekStart(date) {
  const monday = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  monday.setHours(0, 0, 0, 0);
  monday.setDate(monday.getDate() - ((monday.getDay() + 6) % 7));
  return monday;
}

function formatDate(value, options = { weekday: 'short', month: 'short', day: 'numeric' }) {
  const date = value instanceof Date ? value : toDate(value);
  return date ? new Intl.DateTimeFormat(undefined, options).format(date) : '';
}

function formatTime(value) {
  const date = toDate(value);
  return date ? new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' }).format(date) : '';
}

function formatDateTime(value) {
  const date = toDate(value);
  return date ? `${formatDate(date)} · ${formatTime(date)}` : 'No time selected';
}

function weekRangeLabel(start) {
  const end = new Date(start);
  end.setDate(end.getDate() + 6);
  const sameMonth = start.getMonth() === end.getMonth();
  if (sameMonth) {
    return `${new Intl.DateTimeFormat(undefined, { month: 'long' }).format(start)} ${start.getDate()}–${end.getDate()}, ${end.getFullYear()}`;
  }
  return `${formatDate(start, { month: 'short', day: 'numeric' })} – ${formatDate(end, { month: 'short', day: 'numeric', year: 'numeric' })}`;
}

function getGreeting() {
  const hour = new Date().getHours();
  if (hour < 12) return 'Good morning';
  if (hour < 18) return 'Good afternoon';
  return 'Good evening';
}

function makeVariant(format, body = '', caption = '') {
  return { format, body, caption };
}

let state = {
  items: [],
  brandProfile: {},
  aiConfigured: false,
  aiModel: null,
  timezone: 'Africa/Lagos',
  socialAccounts: [],
  providers: [],
  assets: [],
  publishingJobs: [],
  publishingEnabled: false,
  publicMediaReady: false,
  worker: { healthy: false, status: 'not_seen' },
  analytics: null,
  creatorInfo: {},
};
let currentPage = 'overview';
let activeLibraryFilter = 'all';
let editorId = null;
let editorPlatform = 'instagram';
let modalMode = null;
let calendarOffset = 0;
let sessionAuthenticated = false;
let apiAvailable = false;

const pageContent = document.getElementById('page-content');
const dialog = document.getElementById('app-dialog');
const dialogContent = document.getElementById('dialog-content');
const appShell = document.getElementById('app-shell');
const loginScreen = document.getElementById('login-screen');

function setRuntimeStatus() {
  const badge = document.getElementById('runtime-status');
  const generateButtons = document.querySelectorAll('[data-action="generate-ai-week"]');
  if (badge) {
    const label = state.aiConfigured ? 'AI CONFIGURED' : 'AI NOT CONFIGURED';
    badge.classList.toggle('is-ready', Boolean(state.aiConfigured));
    badge.classList.toggle('is-offline', !apiAvailable);
    badge.innerHTML = `<i></i><span>${label}</span>`;
    badge.title = state.aiConfigured ? `AI provider settings are present${state.aiModel ? ` · ${escapeHTML(state.aiModel)}` : ''}` : 'Set AI_API_KEY in the server .env file to enable generation.';
  }
  generateButtons.forEach((button) => {
    button.disabled = !state.aiConfigured;
    button.title = state.aiConfigured ? 'Generate seven platform-adapted drafts using the configured AI provider.' : 'Configure AI_API_KEY on the server to generate a content week.';
    button.setAttribute('aria-disabled', String(!state.aiConfigured));
  });
}

function showLogin(message = '', { retryable = false } = {}) {
  sessionAuthenticated = false;
  if (dialog.open) dialog.close();
  modalMode = null;
  editorId = null;
  appShell.hidden = true;
  loginScreen.hidden = false;
  const error = document.getElementById('login-error');
  if (error) {
    error.textContent = message;
    error.hidden = !message;
  }
  const retry = document.getElementById('login-retry');
  if (retry) retry.hidden = !retryable;
  if (message.toLowerCase().includes('server')) {
    document.getElementById('login-footnote').textContent = 'Start the backend server, then retry. Workspace data is stored on that server.';
  } else {
    document.getElementById('login-footnote').textContent = 'Your workspace is private. Passwords and provider keys stay on the server.';
  }
}

function enterWorkspace(workspace) {
  state = {
    items: Array.isArray(workspace.items) ? workspace.items : [],
    brandProfile: workspace.brandProfile || {},
    aiConfigured: Boolean(workspace.aiConfigured),
    aiModel: workspace.aiModel || null,
    timezone: workspace.timezone || 'Africa/Lagos',
    socialAccounts: workspace.social.accounts || workspace.socialAccounts || [],
    providers: workspace.social.providers || [],
    assets: Array.isArray(workspace.assets) ? workspace.assets : [],
    publishingJobs: Array.isArray(workspace.publishingJobs) ? workspace.publishingJobs : [],
    publishingEnabled: Boolean(workspace.publishingEnabled),
    publicMediaReady: Boolean(workspace.publicMediaReady),
    worker: workspace.social.worker || { healthy: false, status: 'not_seen' },
    analytics: state.analytics,
    creatorInfo: state.creatorInfo || {},
  };
  apiAvailable = true;
  sessionAuthenticated = true;
  loginScreen.hidden = true;
  appShell.hidden = false;
  renderPage();
}

function apiErrorMessage(detail, fallback) {
  if (Array.isArray(detail)) return detail.map((entry) => entry.msg || 'Invalid request').join(' ');
  return typeof detail === 'string' ? detail : fallback;
}

async function apiRequest(path, options = {}) {
  const headers = { Accept: 'application/json', ...(options.headers || {}) };
  const requestOptions = { ...options, headers, credentials: 'same-origin' };
  if (options.body instanceof FormData) {
    requestOptions.body = options.body; // The browser must supply the multipart boundary.
  } else if (options.body && typeof options.body !== 'string') {
    headers['Content-Type'] = 'application/json';
    requestOptions.body = JSON.stringify(options.body);
  }
  let response;
  try {
    response = await fetch(path, requestOptions);
  } catch (error) {
    apiAvailable = false;
    setRuntimeStatus();
    throw new Error('The app server could not be reached. Your unsaved edits are still in the editor.');
  }
  apiAvailable = true;
  if (response.status === 401 && path !== '/api/auth/login') {
    const expired = new Error('Your session expired. Sign in again to continue.');
    expired.sessionExpired = true;
    showLogin(expired.message);
    throw expired;
  }
  let payload = null;
  if (response.status !== 204) {
    try { payload = await response.json(); } catch { payload = null; }
  }
  if (!response.ok) {
    throw new Error(apiErrorMessage(payload?.detail, `Request failed (${response.status}).`));
  }
  return payload;
}

async function loadWorkspace() {
  const [workspace, social] = await Promise.all([
    apiRequest('/api/workspace'),
    apiRequest('/api/social/status'),
  ]);
  enterWorkspace({ ...workspace, social });
}

async function initializeApp() {
  hydrateIcons();
  try {
    let response;
    try {
      response = await fetch('/api/auth/me', { credentials: 'same-origin', headers: { Accept: 'application/json' } });
    } catch {
      showLogin('Backend server unavailable. Start the API server, then retry.', { retryable: true });
      return;
    }
    if (response.status === 401) {
      showLogin('Your session expired. Sign in again to continue.');
      return;
    }
    if (!response.ok) {
      showLogin('Backend server unavailable. Start the API server, then retry.', { retryable: true });
      return;
    }
    const session = await response.json();
    apiAvailable = true;
    if (!session.authenticated) {
      showLogin();
      return;
    }
    try {
      await loadWorkspace();
    } catch (error) {
      if (error?.sessionExpired) return; // apiRequest already returned the user to the sign-in screen.
      showLogin(
        error?.message || 'The workspace could not be loaded. Retry, then sign in again if the problem continues.',
        { retryable: true },
      );
      return;
    }
    const result = new URLSearchParams(window.location.search).get('social_result');
    if (result) {
      window.history.replaceState({}, document.title, window.location.pathname + window.location.hash);
      const messages = {
        connected: 'Social account connected. Review the platform connection and keep approval required for every post.',
        cancelled: 'Social account connection was cancelled.',
        failed: 'Social account connection did not finish. Check the provider setup and try again.',
      };
      showToast(messages[result] || 'Social account setup returned to the workspace.', result === 'failed');
    }
  } catch (error) {
    apiAvailable = false;
    showLogin(
      error?.message ? `The workspace could not be loaded: ${error.message}` : 'The workspace could not be loaded.',
      { retryable: true },
    );
  }
}

async function signIn(form) {
  const errorBox = document.getElementById('login-error');
  const button = form.querySelector('button[type="submit"]');
  const password = String(new FormData(form).get('password') || '');
  errorBox.hidden = true;
  button.disabled = true;
  try {
    const result = await apiRequest('/api/auth/login', { method: 'POST', body: { password } });
    if (!result?.authenticated) throw new Error('Sign-in was not completed.');
    document.getElementById('login-password').value = '';
    await loadWorkspace();
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally {
    button.disabled = false;
  }
}

function itemStatusLabel(status) {
  return STATUS_LABELS[status] || 'Idea';
}

function statusClass(status) {
  return `status-${String(status || 'IDEA').toLowerCase()}`;
}

function statusPill(status) {
  return `<span class="status-pill ${statusClass(status)}">${escapeHTML(itemStatusLabel(status))}</span>`;
}

function categoryStampClass(category) {
  const value = String(category || '').toLowerCase();
  if (value.includes('cloud')) return 'cloud';
  if (value.includes('build') || value.includes('debelu')) return 'build';
  if (value.includes('community')) return 'community';
  return '';
}

function categoryIcon(category) {
  const value = String(category || '').toLowerCase();
  if (value.includes('cloud')) return 'cloud';
  if (value.includes('build') || value.includes('debelu')) return 'building';
  if (value.includes('community')) return 'message';
  if (value.includes('api') || value.includes('developer') || value.includes('application')) return 'code';
  return 'shield';
}

function platformTag(platform, compact = false) {
  const meta = PLATFORMS[platform];
  if (!meta) return '';
  if (compact) return `<span class="mini-platform ${platform}" title="${escapeHTML(meta.name)}" aria-label="${escapeHTML(meta.name)}">${escapeHTML(meta.mark)}</span>`;
  return `<span class="platform-chip platform-${platform}"><span class="platform-mark">${escapeHTML(meta.mark)}</span>${escapeHTML(meta.name)}</span>`;
}

function platformTags(item) {
  return PLATFORM_ORDER.filter((platform) => item.variants && item.variants[platform])
    .map((platform) => platformTag(platform)).join('');
}

function itemPreview(item) {
  const text = PLATFORM_ORDER.map((platform) => item.variants?.[platform]?.body || '')
    .find((body) => body.trim());
  if (!text) return 'Platform versions have not been drafted yet. Open this idea to add copy.';
  return text.replace(/\s+/g, ' ').trim();
}

function contentCard(item) {
  const searchText = [item.topic, item.category, item.audience, itemPreview(item), ...PLATFORM_ORDER.map((p) => item.variants?.[p]?.caption || '')]
    .join(' ').toLowerCase();
  const date = item.scheduledFor ? formatDateTime(item.scheduledFor) : item.suggestedFor ? `Proposed · ${formatDate(item.suggestedFor)}` : '';
  return `
    <article class="content-card" data-status="${escapeHTML(item.status)}" data-platforms="${PLATFORM_ORDER.filter((p) => item.variants?.[p]).join(',')}" data-search="${escapeHTML(searchText)}">
      <div class="content-card-top"><span class="category-label">${escapeHTML(item.category || 'UNCATEGORIZED')}</span>${statusPill(item.status)}</div>
      <h3>${escapeHTML(item.topic)}</h3>
      <p class="content-summary">${escapeHTML(itemPreview(item))}</p>
      <div class="content-card-bottom">
        <div class="content-platforms">${platformTags(item)}</div>
        <span class="card-date">${escapeHTML(date)}</span>
        <button class="card-open" type="button" data-action="open-editor" data-id="${escapeHTML(item.id)}" aria-label="Open ${escapeHTML(item.topic)}">${icon('chevron')}</button>
      </div>
    </article>`;
}

function renderOverview() {
  const items = state.items;
  const pending = items.filter((item) => item.status === 'READY_FOR_REVIEW');
  const scheduled = items.filter((item) => item.status === 'SCHEDULED' && item.scheduledFor)
    .sort((a, b) => new Date(a.scheduledFor) - new Date(b.scheduledFor));
  const scheduledNext = scheduled.find((item) => new Date(item.scheduledFor) >= new Date()) || scheduled[0];
  const publishedCount = items.filter((item) => item.status === 'PUBLISHED').length;
  const today = new Date();
  const queueRows = pending.slice(0, 4).map((item) => `
    <div class="approval-row">
      <div class="category-stamp ${categoryStampClass(item.category)}">${icon(categoryIcon(item.category))}</div>
      <div class="approval-info">
        <div class="approval-meta"><span>${escapeHTML(item.category)}</span><span class="meta-dot">·</span><span>${PLATFORM_ORDER.filter((p) => item.variants?.[p]).length} formats</span></div>
        <h3>${escapeHTML(item.topic)}</h3>
        <p>${escapeHTML(item.audience || 'Audience not specified')}</p>
      </div>
      <button class="row-review-button" type="button" data-action="open-editor" data-id="${escapeHTML(item.id)}">Review</button>
    </div>`).join('');

  const nextPostHTML = scheduledNext ? `
    <div class="next-post-label"><i></i> Next scheduled</div>
    <h3 class="next-post-title">${escapeHTML(scheduledNext.topic)}</h3>
    <div class="next-post-time">${icon('clock')}<span>${escapeHTML(formatDateTime(scheduledNext.scheduledFor))}</span></div>
    <div class="next-post-footer"><div class="platform-stack">${PLATFORM_ORDER.filter((p) => scheduledNext.variants?.[p]).map((p) => platformTag(p, true)).join('')}</div><button class="text-link" type="button" data-action="open-editor" data-id="${escapeHTML(scheduledNext.id)}">View post <span>→</span></button></div>
  ` : `<div class="next-post-label"><i></i> Next scheduled</div><div class="no-scheduled">No posts are scheduled yet. Approve a draft, then choose a time on the calendar.</div><div class="next-post-footer"><span class="card-date">${state.publishingEnabled ? 'No posts queued' : 'Publishing disabled'}</span><button class="text-link" type="button" data-page="publishing">Connect accounts <span>→</span></button></div>`;

  pageContent.innerHTML = `
    <div class="page-heading dashboard-heading">
      <div><p class="page-kicker">${escapeHTML(formatDate(today, { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' }))}</p><h1>${escapeHTML(getGreeting())}, Debelu</h1><p>Your content workspace, on-brand and in your control.</p></div>
      <div class="page-heading-actions"><button class="button-primary" type="button" data-action="generate-ai-week">${icon('sparkles')}Generate AI week</button></div>
    </div>

    <section class="hero-panel" aria-label="One idea across platforms">
      <div class="hero-copy">
        <div class="hero-kicker"><i class="sparkle-dot"></i> IDEA → PLATFORM-READY CONTENT</div>
        <h2>One good idea.<br />Three ways to show up.</h2>
        <p>Generate real platform-specific drafts from your saved Debelu brand profile. Review and approve every version before it can be scheduled.</p>
        <div class="hero-actions">
          <button class="button-primary" type="button" data-action="generate-ai-week">${icon('sparkles')}Generate AI week</button>
          <button class="button-hero-secondary" type="button" data-page="library">Open content studio <span>→</span></button>
        </div>
        <div class="hero-footnote"><i></i> Human approval required <span>·</span> ${state.publishingEnabled ? 'Official publishing worker' : 'Social connections are fail-closed'}</div>
      </div>
      <div class="hero-art" aria-hidden="true">
        <div class="hero-orbit"></div>
        <div class="hero-node node-top"><span class="node-symbol">IG</span>Instagram</div>
        <div class="hero-node node-left"><span class="node-symbol">✳</span>One idea</div>
        <div class="hero-node node-bottom"><span class="node-symbol">♪</span>TikTok</div>
        <div class="hero-flow">↗</div>
        <div class="hero-idea-card"><small><i></i> SOURCE IDEA</small><strong>Security is more than a lock icon.</strong><div class="hero-platforms"><span>IG</span><span>@</span><span>♪</span><em>3 adapted drafts</em></div></div>
      </div>
    </section>
    ${state.aiConfigured
      ? `<div class="demo-notice live-notice"><span class="demo-notice-icon">${icon('check')}</span><span><strong>AI provider configured:</strong> generation runs server-side using ${escapeHTML(state.aiModel || 'the configured model')}. Generated copy remains a draft until you approve it.</span></div>`
      : `<div class="demo-notice"><span class="demo-notice-icon">${icon('info')}</span><span><strong>Manual mode:</strong> the workspace and database are live. To generate content, add <code>AI_API_KEY</code> to the server’s <code>.env</code> and restart.</span></div>`}

    <section class="stats-grid" aria-label="Workspace summary">
      <article class="stat-card"><span class="stat-icon">${icon('layers')}</span><div class="stat-copy"><div class="stat-label">Content ideas</div><div class="stat-value-line"><strong class="stat-value">${items.length}</strong><span class="stat-hint">in your workspace</span></div></div></article>
      <article class="stat-card"><span class="stat-icon amber">${icon('idea')}</span><div class="stat-copy"><div class="stat-label">Needs your review</div><div class="stat-value-line"><strong class="stat-value">${pending.length}</strong><span class="stat-hint">awaiting approval</span></div></div></article>
      <article class="stat-card"><span class="stat-icon green">${icon('calendar')}</span><div class="stat-copy"><div class="stat-label">Scheduled</div><div class="stat-value-line"><strong class="stat-value">${scheduled.length}</strong><span class="stat-hint">saved on server</span></div></div></article>
      <article class="stat-card"><span class="stat-icon purple">${icon('chart')}</span><div class="stat-copy"><div class="stat-label">Published</div><div class="stat-value-line"><strong class="stat-value">${publishedCount}</strong><span class="stat-hint">no accounts connected</span></div></div></article>
    </section>

    <div class="overview-grid">
      <section class="panel">
        <div class="panel-heading">
          <div class="panel-heading-meta"><h2>Needs your approval</h2><p>Nothing is published until you say so.</p></div>
          <div class="panel-heading-action"><span class="count-pill">${pending.length}</span><button class="button-quiet" type="button" data-action="approve-all" ${pending.length ? '' : 'disabled'}>Approve all</button></div>
        </div>
        <div class="approval-list">
          ${queueRows || `<div class="empty-state"><strong>All caught up</strong><p>Your review queue is clear. Add an idea or generate a real AI content week when a provider is configured.</p></div>`}
        </div>
        ${pending.length > 4 ? `<div class="approval-more"><button class="text-link" type="button" data-page="library">See all ${pending.length} drafts <span>→</span></button></div>` : ''}
      </section>

      <aside class="overview-side">
        <section class="panel next-post-card">${nextPostHTML}</section>
        <section class="guardrail-inline"><span class="inline-shield">${icon('shield')}</span><div><strong>Approval stays with you</strong><p>${state.aiConfigured ? 'AI drafts are generated server-side and still require your approval.' : 'The database is live; add an AI provider key to generate drafts. Social publishing is separately controlled by server configuration.'}</p></div></section>
        <section class="panel quick-next-card"><div class="panel-heading"><div class="panel-heading-meta"><h2>Built to grow in phases</h2><p>A small, useful first step.</p></div></div><div class="quick-steps"><div class="quick-step"><span>01</span><strong>Draft</strong><small>One idea, three formats</small></div><div class="quick-step"><span>02</span><strong>Review</strong><small>You approve the work</small></div><div class="quick-step"><span>03</span><strong>Publish</strong><small>Connect in Publishing</small></div></div></section>
      </aside>
    </div>`;

  hydrateIcons(pageContent);
}

function renderLibrary() {
  const counts = {
    all: state.items.length,
    'needs-review': state.items.filter((item) => item.status === 'READY_FOR_REVIEW').length,
    approved: state.items.filter((item) => item.status === 'APPROVED').length,
    scheduled: state.items.filter((item) => item.status === 'SCHEDULED').length,
    ideas: state.items.filter((item) => item.status === 'IDEA').length,
  };
  const filterLabels = [
    ['all', 'All content'], ['needs-review', 'Needs review'], ['approved', 'Approved'], ['scheduled', 'Scheduled'], ['ideas', 'Ideas'],
  ];
  const cards = [...state.items].sort((a, b) => {
    const order = { READY_FOR_REVIEW: 0, IDEA: 1, APPROVED: 2, SCHEDULED: 3, PUBLISHED: 4, FAILED: 5 };
    return (order[a.status] ?? 9) - (order[b.status] ?? 9) || new Date(b.createdAt) - new Date(a.createdAt);
  });
  pageContent.innerHTML = `
    <div class="page-heading">
      <div><p class="page-kicker">CONTENT WORKSPACE</p><h1>Content studio</h1><p>Shape one clear idea into platform-ready drafts, then review each version.</p></div>
      <div class="page-heading-actions"><button class="button-secondary" type="button" data-action="add-idea">${icon('plus')}Add an idea</button><button class="button-primary" type="button" data-action="generate-ai-week">${icon('sparkles')}Generate AI week</button></div>
    </div>
    <div class="demo-notice"><span class="demo-notice-icon">${icon('info')}</span><span><strong>Server-backed workspace:</strong> content is stored in the configured database and changes are audited. ${state.aiConfigured ? `AI generation is configured for ${escapeHTML(state.aiModel || 'the configured model')}.` : 'AI generation is off until AI_API_KEY is configured on the server.'} Approval is required before any post can be queued.</span></div>
    <div class="library-toolbar">
      <div class="filter-tabs" role="tablist" aria-label="Filter content status">${filterLabels.map(([key, label]) => `<button class="filter-tab ${activeLibraryFilter === key ? 'active' : ''}" type="button" role="tab" aria-selected="${activeLibraryFilter === key}" data-action="library-filter" data-filter="${key}">${label} <span>${counts[key]}</span></button>`).join('')}</div>
      <div class="filter-controls"><label class="search-field">${icon('search')}<input id="library-search" type="search" placeholder="Search ideas" aria-label="Search ideas" /></label><select class="filter-select" id="platform-filter" aria-label="Filter by platform"><option value="all">All platforms</option><option value="instagram">Instagram</option><option value="threads">Threads</option><option value="tiktok">TikTok</option></select></div>
    </div>
    <div class="section-heading"><div><h2>Your content ideas</h2><p><span id="library-result-count">${cards.length}</span> concepts · each can hold up to three platform versions</p></div><button class="text-link" type="button" data-page="brand">Edit brand voice <span>→</span></button></div>
    <div class="content-grid" id="content-grid">${cards.map(contentCard).join('') || `<div class="empty-state library-empty"><strong>Your idea bank is ready</strong><p>Add a topic manually, or configure AI_API_KEY on the server and generate a real content week. Ideas are stored in the database.</p></div>`}</div>
    <div class="empty-state library-empty" id="no-filter-results" hidden><strong>No matching ideas</strong><p>Try another search or change the selected filters.</p></div>`;
  hydrateIcons(pageContent);
  updateLibraryResults();
}

function renderCalendar() {
  const start = weekStart(new Date());
  start.setDate(start.getDate() + calendarOffset * 7);
  const todayKey = dateKey(new Date());
  const days = Array.from({ length: 7 }, (_, index) => {
    const day = new Date(start);
    day.setDate(day.getDate() + index);
    const key = dateKey(day);
    const events = state.items.filter((item) => {
      if (item.status === 'SCHEDULED' && item.scheduledFor) return dateKey(new Date(item.scheduledFor)) === key;
      return item.suggestedFor === key && ['READY_FOR_REVIEW', 'APPROVED'].includes(item.status);
    }).sort((a, b) => {
      const aTime = a.scheduledFor ? new Date(a.scheduledFor).getTime() : 0;
      const bTime = b.scheduledFor ? new Date(b.scheduledFor).getTime() : 0;
      return aTime - bTime;
    });
    return `<section class="calendar-day ${key === todayKey ? 'is-today' : ''}">
      <div class="calendar-day-heading"><span class="calendar-day-name">${escapeHTML(formatDate(day, { weekday: 'short' }))}</span><span class="calendar-day-number">${day.getDate()}</span></div>
      <div class="calendar-events">${events.map((item) => {
        const isScheduled = item.status === 'SCHEDULED';
        const timeLabel = isScheduled ? formatTime(item.scheduledFor) : item.status === 'APPROVED' ? 'Approved · proposed slot' : 'Needs approval · proposed slot';
        const kind = isScheduled ? 'Scheduled' : item.status === 'APPROVED' ? 'Approved draft' : 'Review needed';
        return `<button class="calendar-event ${isScheduled ? 'is-scheduled' : ''}" type="button" data-action="open-editor" data-id="${escapeHTML(item.id)}"><span class="event-time">${escapeHTML(timeLabel)}</span><strong>${escapeHTML(item.topic)}</strong><span class="event-kind">${escapeHTML(kind)}</span></button>`;
      }).join('') || `<div class="calendar-empty">No content planned</div>`}</div>
    </section>`;
  }).join('');
  const approvedUnscheduled = state.items.filter((item) => item.status === 'APPROVED' && !item.scheduledFor);
  const offsetLabel = calendarOffset === 0 ? 'This week' : calendarOffset === 1 ? 'Next week' : calendarOffset === -1 ? 'Last week' : '';
  pageContent.innerHTML = `
    <div class="page-heading"><div><p class="page-kicker">PLAN WITH INTENTION</p><h1>Content calendar</h1><p>Proposed slots are planning suggestions. Only approved content can be scheduled.</p></div><div class="page-heading-actions"><button class="button-secondary" type="button" data-action="add-idea">${icon('plus')}Add an idea</button></div></div>
    <div class="demo-notice"><span class="demo-notice-icon">${icon('info')}</span><span><strong>Server-backed schedule:</strong> choose connected accounts to create durable publishing jobs, or leave targets empty for calendar-only planning. A queued job is not proof of a live post.</span></div>
    <div class="calendar-toolbar"><div><div class="page-kicker">${escapeHTML(offsetLabel || 'WEEK VIEW')}</div><div class="calendar-range">${escapeHTML(weekRangeLabel(start))}</div></div><div class="calendar-controls"><button class="icon-button" type="button" data-action="calendar-prev" aria-label="Previous week">${icon('left')}</button><button class="button-quiet" type="button" data-action="calendar-today">Today</button><button class="icon-button" type="button" data-action="calendar-next" aria-label="Next week">${icon('right')}</button></div></div>
    <div class="calendar-week">${days}</div>
    <div class="calendar-below">
      <section class="panel"><div class="panel-heading"><div class="panel-heading-meta"><h2>Calendar key</h2><p>A clear distinction between a proposed slot and a real approval.</p></div></div><div class="calendar-legend"><span class="legend-item"><i class="legend-dot"></i>Proposed — needs review or approval</span><span class="legend-item"><i class="legend-dot scheduled"></i>Scheduled on server — not published</span></div></section>
      <aside class="panel"><div class="panel-heading"><div class="panel-heading-meta"><h2>Approved, not scheduled</h2><p>${approvedUnscheduled.length} ${approvedUnscheduled.length === 1 ? 'idea' : 'ideas'} ready for a time</p></div></div>${approvedUnscheduled.length ? `<div class="calendar-side-note">${approvedUnscheduled.slice(0, 3).map((item) => `<p><button class="text-link" type="button" data-action="open-editor" data-id="${escapeHTML(item.id)}">${escapeHTML(item.topic)} <span>→</span></button></p>`).join('')}</div>` : `<div class="calendar-side-note"><strong>Nothing waiting here.</strong><p>Approve an idea in Content Studio, then return to its review panel to choose a server schedule time.</p></div>`}</aside>
    </div>`;
  hydrateIcons(pageContent);
}

function renderBrand() {
  const profile = state.brandProfile;
  const voiceTags = String(profile.voice || '').split(/[,.·]/).map((part) => part.trim()).filter(Boolean).slice(0, 8);
  pageContent.innerHTML = `
    <div class="page-heading"><div><p class="page-kicker">THE DEBELU CONTENT BRAIN</p><h1>Brand voice</h1><p>Set the guardrails that keep every future draft recognizably Debelu.</p></div></div>
    <div class="demo-notice"><span class="demo-notice-icon">${icon('info')}</span><span><strong>Workspace profile:</strong> this brand context is saved in the server database and included in generation requests. It is shared by this single-owner workspace.</span></div>
    <div class="brand-layout">
      <form class="panel profile-form" id="brand-profile-form">
        <section class="form-section"><h2>Brand foundation</h2><p>This profile becomes context for server-side content generation. Adjust it as Debelu’s positioning evolves.</p><div class="form-grid">
          <div class="form-field full"><label for="brand-industry">Industry and topics</label><textarea id="brand-industry" name="industry">${escapeHTML(profile.industry)}</textarea></div>
          <div class="form-field full"><label for="brand-voice">Voice and tone</label><textarea id="brand-voice" name="voice">${escapeHTML(profile.voice)}</textarea></div>
        </div></section>
        <section class="form-section"><h2>Guardrails</h2><p>Keep the brand credible, useful and grounded.</p><div class="form-grid">
          <div class="form-field full"><label for="brand-avoid">Avoid</label><textarea id="brand-avoid" name="avoid">${escapeHTML(profile.avoid)}</textarea></div>
          <div class="form-field full"><label for="brand-visual">Visual direction</label><textarea id="brand-visual" name="visual">${escapeHTML(profile.visual)}</textarea></div>
          <div class="form-field"><label for="brand-tagline">Tagline</label><input id="brand-tagline" name="tagline" value="${escapeHTML(profile.tagline)}" /></div>
        </div></section>
        <div class="profile-form-footer"><span class="profile-save-hint">The AI key stays on the server; this profile is sent as generation context.</span><button class="button-primary" type="submit">${icon('check')}Save brand profile</button></div>
      </form>
      <aside class="profile-side">
        <section class="panel brand-preview"><div class="brand-preview-head"><small>BRAND SNAPSHOT</small><h2>Debelu Ventures</h2><p>${escapeHTML(profile.tagline || '')}</p></div><div class="brand-preview-body"><p class="preview-label">VOICE</p><div class="voice-tag-list">${voiceTags.map((tag) => `<span class="voice-tag">${escapeHTML(tag)}</span>`).join('') || '<span class="voice-tag">Add a voice direction</span>'}</div><div class="brand-preview-divider"></div><p class="preview-label">CONTENT SAFETY</p><div class="voice-tag-list"><span class="voice-tag">No fearmongering</span><span class="voice-tag">No invented statistics</span><span class="voice-tag">Human approval</span></div></div></section>
        <section class="next-step-card"><span class="step-number">01</span><h3>Keep the first phase small</h3><p>Server-side generation uses this profile as context. Every output stays in the review queue until a person approves it.</p></section>
        <div class="profile-save-hint">${state.aiConfigured ? `Generation model: ${escapeHTML(state.aiModel || 'configured AI provider')}` : 'Manual content mode · AI provider not configured'}</div>
      </aside>
    </div>`;
  hydrateIcons(pageContent);
}

function safeExternalUrl(value) {
  try {
    const url = new URL(String(value || ''));
    const hostname = url.hostname.toLowerCase();
    const allowed = ['instagram.com', 'threads.net', 'threads.com', 'tiktok.com'].some((domain) => hostname === domain || hostname.endsWith(`.${domain}`));
    return url.protocol === 'https:' && allowed ? url.href : '';
  } catch {
    return '';
  }
}

function formatBytes(value) {
  const bytes = Number(value) || 0;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function assetAttached(assetId) {
  return state.items.some((item) => PLATFORM_ORDER.some((platform) => item.variants?.[platform]?.assetIds?.includes(assetId)));
}

function renderAssetCard(asset) {
  const preview = asset.mimeType?.startsWith('image/')
    ? `<img class="asset-preview" src="/api/assets/${encodeURIComponent(asset.id)}/content" alt="${escapeHTML(asset.name)}" loading="lazy" />`
    : `<div class="asset-video-preview">${icon('send')}<span>MP4 video${asset.durationSeconds ? ` · ${Number(asset.durationSeconds).toFixed(1)}s` : ''}</span></div>`;
  return `<article class="asset-card">${preview}<div class="asset-card-info"><strong title="${escapeHTML(asset.name)}">${escapeHTML(asset.name)}</strong><span>${escapeHTML(asset.mimeType)} · ${formatBytes(asset.sizeBytes)}</span><button class="button-quiet" type="button" data-action="delete-asset" data-id="${escapeHTML(asset.id)}" ${assetAttached(asset.id) ? 'disabled title="Detach from drafts before deleting"' : ''}>${icon('trash')}Delete</button></div></article>`;
}

function renderJobRow(job) {
  const idea = state.items.find((item) => item.id === job.ideaId);
  const canCancel = ['SCHEDULED', 'RETRY'].includes(job.status);
  const canRetry = ['FAILED', 'NEEDS_ATTENTION'].includes(job.status) && !job.providerPostId && state.publishingEnabled;
  const label = idea?.topic || 'Content post';
  const error = job.lastError ? `<p class="job-error">${escapeHTML(job.lastError)}</p>` : '';
  const safeLink = safeExternalUrl(job.providerUrl);
  const link = safeLink ? `<a href="${escapeHTML(safeLink)}" target="_blank" rel="noopener noreferrer">Open post</a>` : '';
  return `<article class="job-row"><span class="job-platform">${platformTag(job.platform, true)}</span><div class="job-main"><strong>${escapeHTML(label)}</strong><span>${escapeHTML(PLATFORMS[job.platform]?.name || job.platform)} · ${escapeHTML(job.status.replaceAll('_', ' '))} · ${escapeHTML(formatDateTime(job.scheduledFor))}</span>${error}${link}</div><div class="job-actions">${job.attemptCount ? `<span class="attempt-count">${Number(job.attemptCount)} attempt${Number(job.attemptCount) === 1 ? '' : 's'}</span>` : ''}${canRetry ? `<button class="button-quiet" type="button" data-action="retry-job" data-id="${escapeHTML(job.id)}">Retry known failure</button>` : ''}${canCancel ? `<button class="button-quiet" type="button" data-action="cancel-job" data-id="${escapeHTML(job.id)}">Cancel</button>` : ''}</div></article>`;
}

function renderPublishing() {
  const providerCards = PLATFORM_ORDER.map((platform) => {
    const provider = state.providers.find((entry) => entry.platform === platform) || {};
    const accounts = state.socialAccounts.filter((account) => account.platform === platform);
    const connectedCount = accounts.filter((account) => account.status === 'CONNECTED').length;
    const details = !provider.configured ? 'Server app credentials, an HTTPS callback and encrypted token storage are required.'
      : !state.publishingEnabled ? 'Connection and publishing are disabled by the server safety switch.'
        : platform === 'tiktok' && !provider.tiktokAudited ? 'TikTok Direct Post is private-only until the app passes platform audit.'
          : 'Authorize an account with the platform. Approval is still required for every post.';
    const status = connectedCount ? `${connectedCount} connected account${connectedCount === 1 ? '' : 's'}` : 'No connected account';
    const action = connectedCount
      ? `<button class="button-secondary" type="button" data-action="connect-social" data-platform="${platform}" ${provider.connectable ? '' : 'disabled'}>Add account</button>`
      : `<button class="button-primary" type="button" data-action="connect-social" data-platform="${platform}" ${provider.connectable ? '' : 'disabled'}>${icon('plus')}Connect account</button>`;
    return `<article class="provider-card"><div class="provider-card-top">${platformTag(platform, true)}<span class="connection-state ${connectedCount ? 'is-ready' : ''}">${escapeHTML(status)}</span></div><h3>${escapeHTML(PLATFORMS[platform].name)}</h3><p>${escapeHTML(details)}</p><div class="provider-card-footer">${provider.accountReviewRequired ? '<span>Professional/business account · platform review may be required</span>' : platform === 'tiktok' ? '<span>User consent · audit may be required for public visibility</span>' : '<span>Official platform API</span>'}${action}</div></article>`;
  }).join('');
  const accountsMarkup = state.socialAccounts.length ? state.socialAccounts.map((account) => `<article class="account-row"><span class="account-platform">${platformTag(account.platform, true)}</span><div class="account-main"><strong>${escapeHTML(account.displayName || account.username || PLATFORMS[account.platform]?.name || 'Social account')}</strong><span>@${escapeHTML(account.username || account.externalAccountId)} · ${escapeHTML(account.platform)} · ${escapeHTML(account.status.replaceAll('_', ' '))}</span>${account.lastError ? `<p class="job-error">${escapeHTML(account.lastError)}</p>` : ''}</div><button class="button-quiet" type="button" data-action="disconnect-account" data-id="${escapeHTML(account.id)}">Disconnect</button></article>`).join('') : '<p class="empty-state-copy">No accounts are connected. Configure a provider app and the public callback URL before connecting.</p>';
  const jobs = [...state.publishingJobs].sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt)).slice(0, 30);
  const media = state.assets.length ? `<div class="asset-grid">${state.assets.map(renderAssetCard).join('')}</div>` : '<p class="empty-state-copy">No media uploaded yet. Add a JPEG or MP4 asset, then attach it to the matching platform draft in Content Studio.</p>';
  const workerCopy = state.worker.healthy ? `Worker online · last heartbeat ${escapeHTML(formatDateTime(state.worker.lastSeenAt))}` : state.worker.status === 'not_seen' ? 'Worker heartbeat has not been recorded. Start the publisher-worker service before expecting posts.' : `Worker offline or stale · last heartbeat ${escapeHTML(formatDateTime(state.worker.lastSeenAt))}`;
  const readiness = !state.publishingEnabled ? 'Connections and publishing are fail-closed until SOCIAL_PUBLISHING_ENABLED is explicitly enabled after production setup.' : !state.worker.healthy ? 'The publishing worker is not healthy. Scheduled jobs will wait safely until it returns.' : !state.publicMediaReady ? 'Configure APP_PUBLIC_URL over HTTPS so platform servers can fetch media.' : 'The server is configured for social publishing. Posts are queued and cancellable before their scheduled time.';
  pageContent.innerHTML = `
    <div class="page-heading"><div><p class="page-kicker">OFFICIAL API · APPROVAL-FIRST</p><h1>Publishing</h1><p>Connect supported accounts, prepare media, and inspect every queued post. Nothing is posted without a recorded human approval.</p></div><div class="page-heading-actions"><button class="button-secondary" type="button" data-action="refresh-workspace">${icon('right')}Refresh status</button></div></div>
    <div class="readiness-banner ${state.publishingEnabled && state.worker.healthy ? 'is-ready' : ''}"><span>${icon(state.publishingEnabled && state.worker.healthy ? 'check' : 'info')}</span><div><strong>Publishing readiness</strong><p>${escapeHTML(readiness)} ${escapeHTML(workerCopy)}</p></div></div>
    <section class="publishing-section"><div class="publishing-section-heading"><div><p class="page-kicker">ACCOUNT CONNECTIONS</p><h2>Official platform access</h2><p>OAuth runs through the platform. Debelu never asks you to paste social passwords here.</p></div></div><div class="provider-grid">${providerCards}</div><div class="accounts-list">${accountsMarkup}</div></section>
    <section class="publishing-section"><div class="publishing-section-heading"><div><p class="page-kicker">MEDIA LIBRARY</p><h2>Upload and review assets</h2><p>Images are normalized on the server. Instagram Reels, Threads videos and TikTok require MP4 duration verification before scheduling.</p></div></div><form class="asset-upload-form" id="asset-upload-form"><label class="form-field"><span>Choose JPEG, PNG, WebP or MP4</span><input id="asset-upload" name="file" type="file" accept="image/jpeg,image/png,image/webp,video/mp4" required /></label><button class="button-primary" type="submit">${icon('plus')}Upload media</button><span class="profile-save-hint">Limits: ${formatBytes(20 * 1024 * 1024)} per image · ${formatBytes(250 * 1024 * 1024)} per video. Server settings may be lower.</span></form>${media}</section>
    <section class="publishing-section"><div class="publishing-section-heading"><div><p class="page-kicker">DURABLE OUTBOX</p><h2>Scheduled posts</h2><p>Retryable errors remain visible. Unknown provider outcomes are never auto-resubmitted.</p></div></div><div class="jobs-list">${jobs.length ? jobs.map(renderJobRow).join('') : '<p class="empty-state-copy">No publishing jobs yet. Approve a draft, select an account, and choose a time in the content editor.</p>'}</div></section>`;
  hydrateIcons(pageContent);
}

function metricLabel(metric) {
  return String(metric || '').replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function renderAnalytics() {
  const analytics = state.analytics || {};
  const metrics = analytics.data?.metrics || [];
  const report = analytics.report || null;
  const currentWeek = dateKey(weekStart(new Date()));
  const reportRows = report?.published || [];
  const aggregate = new Map();
  for (const row of metrics) {
    const key = `${row.platform}:${row.providerPostId}:${row.metric}`;
    const previous = aggregate.get(key);
    if (!previous || new Date(row.collectedAt) > new Date(previous.collectedAt)) aggregate.set(key, row);
  }
  const measured = [...aggregate.values()].sort((a, b) => new Date(b.collectedAt) - new Date(a.collectedAt));
  const metricsTable = measured.length ? `<div class="table-scroll"><table class="metrics-table"><thead><tr><th>Post</th><th>Platform</th><th>Measurement</th><th>Value</th><th>Collected</th></tr></thead><tbody>${measured.slice(0, 100).map((row) => `<tr><td>${escapeHTML(row.topic)}</td><td>${platformTag(row.platform)}</td><td>${escapeHTML(metricLabel(row.metric))}</td><td>${Number(row.value).toLocaleString()}</td><td>${escapeHTML(formatDate(row.collectedAt, { month: 'short', day: 'numeric' }))}</td></tr>`).join('')}</tbody></table></div>` : '<p class="empty-state-copy">No verified post measurements are available yet. Metrics appear only after a platform connection has authorized them and a post has been published.</p>';
  const reportTable = reportRows.length ? `<div class="table-scroll"><table class="metrics-table"><thead><tr><th>Topic</th><th>Platform</th><th>Published</th><th>Result</th></tr></thead><tbody>${reportRows.map((row) => `<tr><td>${escapeHTML(row.topic)}</td><td>${platformTag(row.platform)}</td><td>${escapeHTML(formatDateTime(row.publishedAt))}</td><td>${safeExternalUrl(row.url) ? `<a href="${escapeHTML(safeExternalUrl(row.url))}" target="_blank" rel="noopener noreferrer">Open post</a>` : escapeHTML(row.status)}</td></tr>`).join('')}</tbody></table></div>` : '<p class="empty-state-copy">No posts were recorded for this reporting week.</p>';
  pageContent.innerHTML = `
    <div class="page-heading"><div><p class="page-kicker">PROVIDER-MEASURED · NO INVENTED METRICS</p><h1>Analytics</h1><p>Collected counts are timestamped observations from connected providers, not estimates or AI-generated facts.</p></div><div class="page-heading-actions"><button class="button-primary" type="button" data-action="load-analytics">${icon('right')}Refresh measurements</button></div></div>
    <div class="readiness-banner"><span>${icon('info')}</span><div><strong>Measurement limits</strong><p>Available metrics differ by platform and permission. A missing metric is not treated as zero; no automated replies, DMs or audience targeting run here.</p></div></div>
    <section class="publishing-section"><div class="publishing-section-heading"><div><p class="page-kicker">LAST 30 DAYS</p><h2>Observed post metrics</h2><p>${metrics.length ? `${metrics.length} provider measurements returned · showing the latest observation per post and metric.` : 'Refresh after posts have been published and analytics scopes are granted.'}</p></div></div>${metricsTable}</section>
    <section class="publishing-section"><div class="publishing-section-heading report-heading"><div><p class="page-kicker">WEEKLY OPERATIONS REPORT</p><h2>Published and scheduled posts</h2><p>Evidence only: measured totals are reported separately from planned posts.</p></div><div class="report-controls"><label class="form-field"><span>Week beginning (Monday)</span><input type="date" id="report-week" value="${escapeHTML(report?.weekStart || currentWeek)}" /></label><button class="button-secondary" type="button" data-action="load-weekly-report">${icon('calendar')}Load week</button>${report ? `<a class="button-quiet" href="/api/reports/weekly.csv?week=${encodeURIComponent(report.weekStart)}">Download CSV</a>` : ''}</div></div>${report ? `<div class="report-summary"><div><span>Published</span><strong>${Number(report.publishedCount || 0)}</strong></div><div><span>Scheduled</span><strong>${Number(report.scheduledOrPendingCount || 0)}</strong></div><div><span>Week</span><strong>${escapeHTML(report.weekStart)} – ${escapeHTML(report.weekEndExclusive)}</strong></div></div><div class="metrics-totals">${Object.entries(report.metricsByPlatform || {}).map(([platform, values]) => `<article><strong>${escapeHTML(PLATFORMS[platform]?.name || platform)}</strong>${Object.entries(values).map(([metric, value]) => `<span>${escapeHTML(metricLabel(metric))}: ${Number(value).toLocaleString()}</span>`).join('')}</article>`).join('') || '<p class="empty-state-copy">No provider measurements recorded for this week.</p>'}</div>${reportTable}` : '<p class="empty-state-copy">Load a reporting week to see recorded posts, scheduled work and available provider measurements.</p>'}</section>
    <section class="recommendation-note"><strong>Recommendations stay evidence-based.</strong><span>Comparisons and AI recommendations are intentionally withheld until there is enough comparable, permissioned platform data. Human review remains required for public claims and sensitive topics.</span></section>`;
  hydrateIcons(pageContent);
}

function updateNavigation() {
  document.querySelectorAll('[data-page]').forEach((button) => {
    if (button.classList.contains('nav-link')) button.classList.toggle('active', button.dataset.page === currentPage);
  });
  const current = document.querySelector(`.nav-link[data-page="${currentPage}"]`);
  document.getElementById('topbar-page').textContent = current ? current.querySelector('span:nth-child(2)')?.textContent || 'Workspace' : 'Workspace';
  const pendingCount = state.items.filter((item) => item.status === 'READY_FOR_REVIEW').length;
  const count = document.getElementById('nav-approval-count');
  count.textContent = pendingCount;
  count.hidden = pendingCount === 0;
}

function renderPage() {
  updateNavigation();
  document.title = `Debelu Social Engine · ${currentPage === 'overview' ? 'Overview' : currentPage[0].toUpperCase() + currentPage.slice(1)}`;
  if (currentPage === 'overview') renderOverview();
  else if (currentPage === 'library') renderLibrary();
  else if (currentPage === 'calendar') renderCalendar();
  else if (currentPage === 'brand') renderBrand();
  else if (currentPage === 'publishing') renderPublishing();
  else renderAnalytics();
  setRuntimeStatus();
}

function navigate(page) {
  const allowed = ['overview', 'library', 'calendar', 'brand', 'publishing', 'analytics'];
  if (!allowed.includes(page)) return;
  currentPage = page;
  document.getElementById('sidebar').classList.remove('is-open');
  document.getElementById('mobile-menu').setAttribute('aria-expanded', 'false');
  renderPage();
  if (window.innerWidth <= 760) window.scrollTo({ top: 0, behavior: 'smooth' });
}

function updateLibraryResults() {
  const search = (document.getElementById('library-search')?.value || '').trim().toLowerCase();
  const selectedPlatform = document.getElementById('platform-filter')?.value || 'all';
  const expectedStatus = STATUS_FILTERS[activeLibraryFilter];
  let visible = 0;
  document.querySelectorAll('#content-grid .content-card').forEach((card) => {
    const statusMatches = !expectedStatus || card.dataset.status === expectedStatus;
    const searchMatches = !search || card.dataset.search.includes(search);
    const platforms = card.dataset.platforms.split(',');
    const platformMatches = selectedPlatform === 'all' || platforms.includes(selectedPlatform);
    const show = statusMatches && searchMatches && platformMatches;
    card.hidden = !show;
    if (show) visible += 1;
  });
  const resultCount = document.getElementById('library-result-count');
  if (resultCount) resultCount.textContent = visible;
  const empty = document.getElementById('no-filter-results');
  if (empty) empty.hidden = visible > 0 || state.items.length === 0;
}

function openEditor(id) {
  const item = state.items.find((candidate) => candidate.id === id);
  if (!item) return;
  editorId = id;
  editorPlatform = PLATFORM_ORDER.find((platform) => item.variants?.[platform]) || 'instagram';
  modalMode = 'editor';
  renderDialog();
  if (!dialog.open) dialog.showModal();
}

function openAddIdea() {
  modalMode = 'idea';
  editorId = null;
  renderDialog();
  if (!dialog.open) dialog.showModal();
  window.setTimeout(() => document.getElementById('idea-topic')?.focus(), 0);
}

function renderDialog() {
  if (modalMode === 'idea') {
    dialogContent.innerHTML = `
      <div class="dialog-shell">
        <div class="dialog-header"><div class="dialog-header-copy"><p class="dialog-eyebrow">IDEA BANK · NEW CONCEPT</p><h2 id="dialog-title">Add a content idea</h2><p class="dialog-subtitle">Save this idea to the server workspace. Add platform copy now or use AI generation when it is configured.</p></div><button class="dialog-close" type="button" data-action="close-dialog" aria-label="Close">${icon('close')}</button></div>
        <form class="dialog-form" id="idea-form">
          <div class="form-field"><label for="idea-topic">Topic or working title <span aria-hidden="true">*</span></label><input id="idea-topic" name="topic" required minlength="3" maxlength="120" placeholder="e.g. Why secure defaults matter" /></div>
          <div class="form-grid"><div class="form-field"><label for="idea-category">Content pillar</label><select id="idea-category" name="category"><option>CYBERSECURITY</option><option>APPLICATION SECURITY</option><option>API SECURITY</option><option>CLOUD SECURITY</option><option>DEVSECOPS</option><option>SOFTWARE ENGINEERING</option><option>AI SECURITY</option><option>DATA SECURITY</option><option>BUILD IN PUBLIC</option><option>DEBELU VENTURES</option></select></div><div class="form-field"><label for="idea-audience">Audience (optional)</label><input id="idea-audience" name="audience" maxlength="100" placeholder="e.g. Product teams" /></div></div>
          <div class="dialog-form-note">${icon('info')}<span>This creates an editable idea in the database. No copy is invented; write the platform drafts or generate a week through the configured AI provider.</span></div>
          <div class="dialog-footer"><span></span><div class="dialog-footer-right"><button class="button-secondary" type="button" data-action="close-dialog">Cancel</button><button class="button-primary" type="submit">${icon('plus')}Add to idea bank</button></div></div>
        </form>
      </div>`;
    hydrateIcons(dialogContent);
    return;
  }

  const item = state.items.find((candidate) => candidate.id === editorId);
  if (!item) return closeDialog();
  const variant = item.variants?.[editorPlatform] || makeVariant('Platform draft');
  const formatLabels = { instagram: 'Carousel copy', threads: 'Post copy', tiktok: 'Video script' };
  const platformTabs = PLATFORM_ORDER.map((platform) => {
    const current = item.variants?.[platform] || makeVariant('Platform draft');
    const hasContent = Boolean(current.body.trim());
    return `<button class="variant-tab ${editorPlatform === platform ? 'active' : ''}" type="button" data-action="variant-tab" data-platform="${platform}" role="tab" aria-selected="${editorPlatform === platform}">${platformTag(platform, true)}${escapeHTML(PLATFORMS[platform].name)}${hasContent ? `<span class="variant-tab-check">${icon('check')}</span>` : ''}</button>`;
  }).join('');
  const currentJobs = item.publishingJobs || [];
  const activeTargets = currentJobs.filter((job) => ['SCHEDULED', 'RETRY'].includes(job.status));
  const scheduleAllowed = ['APPROVED', 'SCHEDULED'].includes(item.status);
  const scheduleDefault = item.scheduledFor ? localDateTimeValue(new Date(item.scheduledFor)) : item.suggestedFor ? `${item.suggestedFor}T11:00` : '';
  const mediaPicker = state.assets.length ? `<fieldset class="asset-picker"><legend>Media for ${escapeHTML(PLATFORMS[editorPlatform].name)}</legend><p>For carousels, set the order number shown beside each selected asset.</p>${state.assets.map((asset, index) => {
    const attached = (variant.assetIds || []).includes(asset.id);
    const order = attached ? (variant.assetIds || []).indexOf(asset.id) + 1 : index + 1;
    return `<label class="asset-picker-row"><input type="checkbox" name="selected-assets" value="${escapeHTML(asset.id)}" ${attached ? 'checked' : ''} /><span>${escapeHTML(asset.name)}<small>${escapeHTML(asset.mimeType)} · ${formatBytes(asset.sizeBytes)}</small></span><input type="number" min="1" max="20" value="${order}" data-asset-order="${escapeHTML(asset.id)}" aria-label="Position for ${escapeHTML(asset.name)}" /></label>`;
  }).join('')}</fieldset>` : `<div class="variant-hint">${icon('info')}<span>No media is in the library yet. Upload assets in Publishing, then attach them here before scheduling a media post.</span></div>`;
  const targetRows = PLATFORM_ORDER.map((platform) => {
    const accounts = state.socialAccounts.filter((account) => account.platform === platform && account.status === 'CONNECTED');
    const provider = state.providers.find((entry) => entry.platform === platform) || {};
    const selectedJob = activeTargets.find((job) => job.platform === platform);
    const accountValue = selectedJob?.accountId || accounts[0]?.id || '';
    const canTarget = scheduleAllowed && state.publishingEnabled && provider.connectable && accounts.length > 0;
    const options = accounts.map((account) => `<option value="${escapeHTML(account.id)}" ${account.id === accountValue ? 'selected' : ''}>${escapeHTML(account.displayName || account.username || PLATFORMS[platform].name)}${account.username ? ` · @${escapeHTML(account.username)}` : ''}</option>`).join('');
    const accountControl = accounts.length ? `<select data-account-platform="${platform}" aria-label="${PLATFORMS[platform].name} account" ${canTarget ? '' : 'disabled'}>${options}</select>` : '<span class="target-no-account">Connect an account in Publishing</span>';
    const checked = selectedJob ? 'checked' : '';
    const target = `<div class="target-row"><label class="target-checkbox"><input type="checkbox" name="publish-platform" value="${platform}" data-publish-platform="${platform}" ${checked} ${canTarget ? '' : 'disabled'} />${platformTag(platform, true)}<span>${escapeHTML(PLATFORMS[platform].name)}</span></label>${accountControl}</div>`;
    if (platform !== 'tiktok' || !accounts.length) return target;
    const creator = state.creatorInfo[accountValue];
    const privacyOptions = creator?.privacyLevelOptions || [];
    const savedTiktok = selectedJob?.tiktok || {};
    const privacyChoices = privacyOptions.map((level) => `<option value="${escapeHTML(level)}" ${savedTiktok.privacyLevel === level ? 'selected' : ''}>${escapeHTML(level.replaceAll('_', ' '))}</option>`).join('');
    const creatorText = creator ? `${creator.canPost ? 'Posting available' : 'Posting is temporarily unavailable'}${creator.maxVideoPostDurationSec ? ` · max ${creator.maxVideoPostDurationSec}s` : ''}${creator.unauditedClient ? ' · private-only app' : ''}` : 'Load current TikTok privacy and creator limits before scheduling.';
    const creatorControls = `<div class="tiktok-controls"><div class="tiktok-control-head"><strong>Creator settings</strong><button class="button-quiet" type="button" data-action="refresh-tiktok" data-account-id="${escapeHTML(accountValue)}">${icon('right')}Refresh TikTok options</button></div><p>${escapeHTML(creatorText)}</p><label class="form-field"><span>Who can view this post</span><select data-tiktok-option="privacyLevel" ${privacyOptions.length ? '' : 'disabled'}><option value="">Select a privacy level</option>${privacyChoices}</select></label><div class="tiktok-check-grid"><label><input type="checkbox" data-tiktok-option="allowComment" ${savedTiktok.allowComment ? 'checked' : ''} />Allow comments</label><label><input type="checkbox" data-tiktok-option="allowDuet" ${savedTiktok.allowDuet ? 'checked' : ''} />Allow Duet</label><label><input type="checkbox" data-tiktok-option="allowStitch" ${savedTiktok.allowStitch ? 'checked' : ''} />Allow Stitch</label><label><input type="checkbox" data-tiktok-option="ownBrand" ${savedTiktok.ownBrand ? 'checked' : ''} />Promoting my own brand</label><label><input type="checkbox" data-tiktok-option="brandedContent" ${savedTiktok.brandedContent ? 'checked' : ''} />Paid or sponsored partnership</label><label><input type="checkbox" data-tiktok-option="isAigc" ${savedTiktok.isAigc ? 'checked' : ''} />AI-generated media/content</label></div><label class="tiktok-consent"><input type="checkbox" data-tiktok-option="consentGiven" ${savedTiktok.consentGiven ? 'checked' : ''} /><span>I reviewed this exact video, caption, visibility and interaction settings, and explicitly authorize this TikTok post.</span></label></div>`;
    return `${target}${creatorControls}`;
  }).join('');
  const scheduleBox = scheduleAllowed ? `
    <div class="dialog-schedule-box dialog-schedule-expanded">
      <div class="dialog-schedule-copy"><strong>${item.status === 'SCHEDULED' ? 'Approved schedule' : 'Approved content · choose targets'}</strong><span>Choose zero platforms for a calendar-only plan, or select connected accounts to create durable publishing jobs. Every job uses this approved snapshot.</span></div>
      <div class="form-field"><label for="schedule-at">Date and time · ${escapeHTML(state.timezone)}</label><input id="schedule-at" type="datetime-local" value="${escapeHTML(scheduleDefault)}" /></div>
      <fieldset class="publish-targets"><legend>Optional publishing targets</legend>${targetRows}<p class="target-note">Targets remain empty unless you select them. TikTok requires explicit per-post consent and privacy settings. Scheduling never approves content.</p></fieldset>
      <div class="schedule-action-row"><button class="button-secondary" type="button" data-action="schedule-content">${icon('calendar')}${item.status === 'SCHEDULED' ? 'Save schedule' : 'Schedule approved post'}</button><button class="button-primary" type="button" data-action="publish-content" ${!state.publishingEnabled ? 'disabled' : ''}>${icon('send')}Queue publish in 2 minutes</button></div>
    </div>` : `
    <div class="dialog-schedule-box"><div class="dialog-schedule-disabled">${icon('lock')}<span>Approve the latest content before scheduling or publishing. Publishing is always a separate, explicit action.</span></div></div>`;

  let approvalAction = '';
  if (item.status === 'READY_FOR_REVIEW') approvalAction = `<button class="button-primary" type="button" data-action="approve-content">${icon('check')}Approve idea</button>`;
  else if (item.status === 'IDEA') approvalAction = `<button class="button-primary" type="button" data-action="submit-content">Send for review</button>`;
  else approvalAction = `<span class="status-pill ${statusClass(item.status)}">${escapeHTML(itemStatusLabel(item.status))}</span>`;

  dialogContent.innerHTML = `
    <div class="dialog-shell">
      <div class="dialog-header"><div class="dialog-header-copy"><p class="dialog-eyebrow">CONTENT REVIEW · ${PLATFORM_ORDER.length} PLATFORM VERSIONS</p><h2 id="dialog-title">${escapeHTML(item.topic)}</h2><p class="dialog-subtitle">Edit each version independently. Approval applies to this content idea and its platform drafts.</p></div><button class="dialog-close" type="button" data-action="close-dialog" aria-label="Close">${icon('close')}</button></div>
      <div class="dialog-content">
        <div class="dialog-category-row"><span class="category-label">${escapeHTML(item.category || 'UNCATEGORIZED')} · ${escapeHTML(variant.format || formatLabels[editorPlatform])}</span>${statusPill(item.status)}</div>
        <div class="dialog-edit-meta form-grid"><div class="form-field"><label for="editor-topic">Source idea</label><input id="editor-topic" maxlength="120" value="${escapeHTML(item.topic)}" /></div><div class="form-field"><label for="editor-audience">Target audience</label><input id="editor-audience" maxlength="120" value="${escapeHTML(item.audience || '')}" placeholder="e.g. Product teams" /></div></div>
        <div class="variant-tabs" role="tablist" aria-label="Platform versions">${platformTabs}</div>
        <div class="variant-form-grid">
          <div class="form-field"><label for="variant-body">${escapeHTML(formatLabels[editorPlatform])}</label><textarea id="variant-body" maxlength="9000" placeholder="Add ${escapeHTML(PLATFORMS[editorPlatform].name)} copy here…">${escapeHTML(variant.body)}</textarea></div>
          <div class="form-field caption-field"><label for="variant-caption">Caption and call to action <span style="font-weight:400;color:#9aa5b4">· optional</span></label><textarea id="variant-caption" maxlength="2500" placeholder="Optional caption, CTA or hashtags">${escapeHTML(variant.caption || '')}</textarea></div>
          <div class="variant-hint">${icon('info')}<span>AI output is not verified technical advice. Review accuracy, brand fit, links and platform formatting before approval.</span></div>
          <div><button class="button-quiet" type="button" data-action="copy-variant">${icon('copy')}Copy this version</button></div>
        </div>
        ${scheduleBox}
      </div>
      <div class="dialog-footer">
        <div class="dialog-footer-left">${item.status === 'IDEA' ? `<button class="button-danger" type="button" data-action="delete-idea">${icon('trash')}Delete idea</button>` : `<span class="profile-save-hint">Saved in the server database</span>`}</div>
        <div class="dialog-footer-right"><button class="button-secondary" type="button" data-action="save-variant">Save changes</button>${approvalAction}</div>
      </div>
    </div>`;
  hydrateIcons(dialogContent);
}

async function saveCurrentVariant() {
  const original = state.items.find((candidate) => candidate.id === editorId);
  if (!original) throw new Error('This content item is no longer available. Refresh the workspace.');
  const body = document.getElementById('variant-body');
  const caption = document.getElementById('variant-caption');
  const topic = document.getElementById('editor-topic');
  const audience = document.getElementById('editor-audience');
  const currentVariant = original.variants?.[editorPlatform] || makeVariant('Platform draft');
  const nextBody = body ? body.value.trim() : currentVariant.body;
  const nextCaption = caption ? caption.value.trim() : currentVariant.caption;
  const nextTopic = topic && topic.value.trim() ? topic.value.trim() : original.topic;
  const nextAudience = audience ? audience.value.trim() : (original.audience || '');
  const selected = [...dialogContent.querySelectorAll('input[name="selected-assets"]:checked')].map((input) => ({
    id: input.value,
    order: Number(dialogContent.querySelector(`[data-asset-order="${CSS.escape(input.value)}"]`)?.value || 999),
  })).sort((a, b) => a.order - b.order);
  const nextAssetIds = selected.map((entry) => entry.id);
  if (nextAssetIds.length > 20) throw new Error('A platform draft can use at most 20 attached assets.');
  const originalAssetIds = currentVariant.assetIds || (currentVariant.assets || []).map((asset) => asset.id);
  const contentChanged = nextBody !== currentVariant.body || nextCaption !== currentVariant.caption || nextTopic !== original.topic || nextAudience !== (original.audience || '');
  const assetsChanged = JSON.stringify(nextAssetIds) !== JSON.stringify(originalAssetIds);
  if (!contentChanged && !assetsChanged) return { item: original, requiresReapproval: false };

  let updated = original;
  if (contentChanged) {
    updated = await apiRequest(`/api/ideas/${encodeURIComponent(original.id)}`, {
      method: 'PUT',
      body: {
        topic: nextTopic,
        audience: nextAudience,
        variants: {
          [editorPlatform]: {
            format: currentVariant.format || 'Platform draft',
            body: nextBody,
            caption: nextCaption,
          },
        },
      },
    });
    upsertItem(updated);
  }
  if (assetsChanged) {
    await apiRequest(`/api/ideas/${encodeURIComponent(original.id)}/assets/${editorPlatform}`, {
      method: 'PUT', body: { assetIds: nextAssetIds },
    });
    updated = await apiRequest(`/api/ideas/${encodeURIComponent(original.id)}`);
    upsertItem(updated);
  }
  const requiresReapproval = ['APPROVED', 'SCHEDULED'].includes(original.status) && updated.status === 'READY_FOR_REVIEW';
  return { item: updated, requiresReapproval };
}

function closeDialog() {
  if (dialog.open) dialog.close();
  modalMode = null;
  editorId = null;
}

function showToast(message, isError = false) {
  const region = document.getElementById('toast-region');
  const toast = document.createElement('div');
  toast.className = `toast${isError ? ' error' : ''}`;
  toast.innerHTML = `<span class="toast-icon">${icon(isError ? 'info' : 'check')}</span><span>${escapeHTML(message)}</span>`;
  region.appendChild(toast);
  window.setTimeout(() => toast.remove(), 3600);
}

function upsertItem(item) {
  const index = state.items.findIndex((candidate) => candidate.id === item.id);
  if (index >= 0) state.items[index] = item;
  else state.items.unshift(item);
  state.items.sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt));
  return item;
}

async function generateAIWeek() {
  if (!state.aiConfigured) {
    showToast('AI is not configured. Add AI_API_KEY to the server .env file and restart the app.', true);
    return;
  }
  const result = await apiRequest('/api/generation/week', { method: 'POST' });
  state.items = [...result.items, ...state.items];
  activeLibraryFilter = 'all';
  navigate('library');
  showToast(`Created 7 AI-generated drafts for the week of ${formatDate(result.weekStart, { month: 'short', day: 'numeric' })}. Review them before approval.`);
}

async function addNewIdea(form) {
  const data = new FormData(form);
  const topic = String(data.get('topic') || '').trim();
  if (!topic) {
    document.getElementById('idea-topic')?.focus();
    return;
  }
  const item = await apiRequest('/api/ideas', {
    method: 'POST',
    body: {
      topic,
      category: String(data.get('category') || 'CYBERSECURITY'),
      audience: String(data.get('audience') || '').trim(),
      variants: {
        instagram: { format: 'Carousel', body: '', caption: '' },
        threads: { format: 'Text post', body: '', caption: '' },
        tiktok: { format: 'Short-form video script', body: '', caption: '' },
      },
    },
  });
  upsertItem(item);
  closeDialog();
  navigate('library');
  showToast('Idea saved to the server workspace. Add platform copy when you are ready.');
}

async function updateBrandProfile(form) {
  const data = new FormData(form);
  const updated = await apiRequest('/api/brand-profile', {
    method: 'PUT',
    body: {
      industry: String(data.get('industry') || '').trim(),
      voice: String(data.get('voice') || '').trim(),
      avoid: String(data.get('avoid') || '').trim(),
      visual: String(data.get('visual') || '').trim(),
      tagline: String(data.get('tagline') || '').trim(),
    },
  });
  state.brandProfile = updated;
  renderPage();
  showToast('Brand profile saved to the server.');
}

function collectSelectedTargets({ requireSelection = false } = {}) {
  const checked = [...dialogContent.querySelectorAll('input[data-publish-platform]:checked')];
  if (requireSelection && !checked.length) throw new Error('Choose at least one connected publishing account.');
  return checked.map((input) => {
    const platform = input.dataset.publishPlatform;
    const accountId = dialogContent.querySelector(`[data-account-platform="${CSS.escape(platform)}"]`)?.value;
    if (!accountId) throw new Error(`Choose a connected ${PLATFORMS[platform].name} account.`);
    const target = { platform, accountId };
    if (platform === 'tiktok') {
      const getOption = (name) => dialogContent.querySelector(`[data-tiktok-option="${CSS.escape(name)}"]`);
      const privacyLevel = getOption('privacyLevel')?.value || '';
      if (!privacyLevel) throw new Error('Load TikTok creator options and select a privacy level.');
      target.tiktok = {
        privacyLevel,
        allowComment: Boolean(getOption('allowComment')?.checked),
        allowDuet: Boolean(getOption('allowDuet')?.checked),
        allowStitch: Boolean(getOption('allowStitch')?.checked),
        ownBrand: Boolean(getOption('ownBrand')?.checked),
        brandedContent: Boolean(getOption('brandedContent')?.checked),
        isAigc: Boolean(getOption('isAigc')?.checked),
        consentGiven: Boolean(getOption('consentGiven')?.checked),
      };
      if (!target.tiktok.consentGiven) throw new Error('Review the TikTok settings and confirm explicit consent before queuing.');
    }
    return target;
  });
}

async function scheduleCurrentItem() {
  const original = state.items.find((candidate) => candidate.id === editorId);
  if (!original) return;
  const input = document.getElementById('schedule-at');
  const localDate = input?.value;
  if (!localDate || Number.isNaN(new Date(localDate).getTime())) {
    showToast('Choose a valid date and time first.', true);
    return;
  }
  const { item, requiresReapproval } = await saveCurrentVariant();
  if (requiresReapproval) {
    renderPage();
    renderDialog();
    showToast('This draft changed after approval. Review and approve it again before scheduling.', true);
    return;
  }
  if (!['APPROVED', 'SCHEDULED'].includes(item.status)) {
    showToast('Approve the content before scheduling it.', true);
    return;
  }
  const targets = collectSelectedTargets();
  const updated = await apiRequest(`/api/ideas/${encodeURIComponent(item.id)}/schedule`, {
    method: 'POST',
    body: { scheduledFor: new Date(localDate).toISOString(), targets },
  });
  upsertItem(updated);
  await loadWorkspace();
  renderDialog();
  const queued = updated.scheduledJobs?.length || 0;
  showToast(queued ? `Created ${queued} approved publishing job${queued === 1 ? '' : 's'} for the selected time.` : 'Saved a calendar-only plan. No social post was queued.');
}

async function publishCurrentItem() {
  const original = state.items.find((candidate) => candidate.id === editorId);
  if (!original) return;
  const { item, requiresReapproval } = await saveCurrentVariant();
  if (requiresReapproval || !['APPROVED', 'SCHEDULED'].includes(item.status)) {
    renderPage();
    renderDialog();
    showToast('Approve the latest content before queuing a post.', true);
    return;
  }
  const targets = collectSelectedTargets({ requireSelection: true });
  const names = targets.map((target) => PLATFORMS[target.platform].name).join(', ');
  if (!window.confirm(`Queue the approved post for ${names}? The worker may publish it automatically in about two minutes. You can cancel only before provider submission begins.`)) return;
  const updated = await apiRequest(`/api/ideas/${encodeURIComponent(item.id)}/publish`, { method: 'POST', body: { targets } });
  upsertItem(updated);
  await loadWorkspace();
  renderDialog();
  showToast('Publishing job queued. Check Publishing for its status; provider outcomes are never silently retried when uncertain.');
}

async function refreshTikTokCreatorInfo(accountId) {
  if (!accountId) throw new Error('Choose a connected TikTok account first.');
  if (editorId) {
    const { requiresReapproval } = await saveCurrentVariant();
    if (requiresReapproval) {
      renderPage();
      renderDialog();
      showToast('Your edited content was saved and needs approval again.', true);
    }
  }
  const info = await apiRequest(`/api/social/accounts/${encodeURIComponent(accountId)}/creator-info`, { method: 'POST' });
  state.creatorInfo[accountId] = info;
  renderPage();
  if (editorId) renderDialog();
  showToast('TikTok returned the current creator privacy and duration settings.');
}

async function uploadAssetForm(form) {
  const fileInput = form.querySelector('input[type="file"]');
  if (!fileInput?.files?.length) throw new Error('Choose a media file to upload.');
  const button = form.querySelector('button[type="submit"]');
  if (button) button.disabled = true;
  try {
    const data = new FormData(form);
    await apiRequest('/api/assets', { method: 'POST', body: data });
    await loadWorkspace();
    showToast('Media uploaded and normalized on the server. Attach it to a reviewed platform draft.');
  } finally {
    if (button?.isConnected) button.disabled = false;
  }
}

async function loadAnalytics() {
  const [data, report] = await Promise.all([
    apiRequest('/api/analytics?days=30'),
    apiRequest('/api/reports/weekly'),
  ]);
  state.analytics = { data, report };
  renderPage();
  showToast(`${data.metrics.length} provider measurement${data.metrics.length === 1 ? '' : 's'} loaded. Only returned provider data is shown.`);
}

async function loadWeeklyReport() {
  const input = document.getElementById('report-week');
  const date = input?.value;
  if (!date) throw new Error('Choose a week beginning on Monday.');
  const selected = dateFromKey(date);
  if (selected.getDay() !== 1) throw new Error('Choose a Monday for the start of the report week.');
  const report = await apiRequest(`/api/reports/weekly?week=${encodeURIComponent(date)}`);
  state.analytics = { ...(state.analytics || {}), report };
  renderPage();
  showToast(`Loaded the report for the week beginning ${formatDate(date, { month: 'short', day: 'numeric', year: 'numeric' })}.`);
}

async function handleAction(action, element) {
  switch (action) {
    case 'generate-ai-week':
      await generateAIWeek();
      break;
    case 'logout':
      await apiRequest('/api/auth/logout', { method: 'POST' }).catch(() => null);
      state = { items: [], brandProfile: {}, aiConfigured: false, aiModel: null, timezone: 'Africa/Lagos' };
      showLogin();
      document.getElementById('login-password').focus();
      break;
    case 'add-idea':
      openAddIdea();
      break;
    case 'open-editor':
      openEditor(element.dataset.id);
      break;
    case 'close-dialog':
      closeDialog();
      break;
    case 'library-filter':
      activeLibraryFilter = element.dataset.filter || 'all';
      renderLibrary();
      break;
    case 'approve-all': {
      const result = await apiRequest('/api/ideas/bulk-approve', { method: 'POST' });
      const approved = new Map(result.items.map((item) => [item.id, item]));
      state.items = state.items.map((item) => approved.get(item.id) || item);
      renderPage();
      showToast(`${result.approvedCount} ${result.approvedCount === 1 ? 'idea' : 'ideas'} approved.${result.skippedCount ? ` ${result.skippedCount} empty ideas were skipped.` : ''}`);
      break;
    }
    case 'approve-content': {
      const { item } = await saveCurrentVariant();
      const updated = await apiRequest(`/api/ideas/${encodeURIComponent(item.id)}/approve`, { method: 'POST' });
      upsertItem(updated);
      renderPage();
      renderDialog();
      showToast('Approved. Choose a time to add it to the server calendar.');
      break;
    }
    case 'submit-content': {
      const { item } = await saveCurrentVariant();
      const updated = await apiRequest(`/api/ideas/${encodeURIComponent(item.id)}/submit`, { method: 'POST' });
      upsertItem(updated);
      renderPage();
      renderDialog();
      showToast('Sent to your review queue. It is not scheduled or published.');
      break;
    }
    case 'save-variant': {
      const { requiresReapproval } = await saveCurrentVariant();
      renderPage();
      renderDialog();
      showToast(requiresReapproval ? 'Edits saved. Previous approval and schedule were cleared; review this version again.' : 'Platform draft saved to the server.');
      break;
    }
    case 'variant-tab': {
      const { requiresReapproval } = await saveCurrentVariant();
      editorPlatform = element.dataset.platform || 'instagram';
      renderPage();
      renderDialog();
      if (requiresReapproval) showToast('This approved draft changed. It is back in the review queue.');
      break;
    }
    case 'schedule-content':
      await scheduleCurrentItem();
      break;
    case 'publish-content':
      await publishCurrentItem();
      break;
    case 'connect-social': {
      const platform = element.dataset.platform;
      const result = await apiRequest(`/api/social/${encodeURIComponent(platform)}/connect`, { method: 'POST' });
      if (!result?.authorizationUrl || !safeExternalUrl(result.authorizationUrl)) {
        // OAuth endpoints use their provider's own host, so validate a deliberately narrow allowlist.
        let authUrl;
        try { authUrl = new URL(result?.authorizationUrl || ''); } catch { authUrl = null; }
        const allowed = authUrl && authUrl.protocol === 'https:' && [
          'www.instagram.com', 'threads.com', 'www.threads.com', 'www.tiktok.com',
        ].includes(authUrl.hostname.toLowerCase());
        if (!allowed) throw new Error('The server returned an invalid social authorization URL.');
      }
      const authorizationUrl = new URL(result.authorizationUrl);
      const trustedHosts = ['www.instagram.com', 'threads.com', 'www.threads.com', 'www.tiktok.com'];
      if (authorizationUrl.protocol !== 'https:' || !trustedHosts.includes(authorizationUrl.hostname.toLowerCase())) throw new Error('The authorization URL is not on an approved platform domain.');
      window.location.assign(authorizationUrl.href);
      break;
    }
    case 'disconnect-account': {
      if (!window.confirm('Disconnect this account? Queued, not-yet-submitted posts will be cancelled. Posts with unresolved provider outcomes must be checked before disconnecting.')) break;
      await apiRequest(`/api/social/accounts/${encodeURIComponent(element.dataset.id)}`, { method: 'DELETE' });
      await loadWorkspace();
      showToast('Account disconnected. Any queued posts were cancelled safely.');
      break;
    }
    case 'refresh-tiktok':
      await refreshTikTokCreatorInfo(element.dataset.accountId || '');
      break;
    case 'refresh-workspace':
      await loadWorkspace();
      showToast('Publishing, account and worker status refreshed.');
      break;
    case 'cancel-job': {
      if (!window.confirm('Cancel this post before provider submission begins? A post already in progress cannot be cancelled safely.')) break;
      await apiRequest(`/api/publishing/jobs/${encodeURIComponent(element.dataset.id)}/cancel`, { method: 'POST' });
      await loadWorkspace();
      showToast('Queued post cancelled.');
      break;
    }
    case 'retry-job': {
      if (!window.confirm('Retry this definitive failure once? Do not retry if you suspect the platform accepted the post.')) break;
      await apiRequest(`/api/publishing/jobs/${encodeURIComponent(element.dataset.id)}/retry`, { method: 'POST' });
      await loadWorkspace();
      showToast('Known failure queued for an explicit retry.');
      break;
    }
    case 'delete-asset': {
      if (!window.confirm('Delete this unused media file from the server?')) break;
      await apiRequest(`/api/assets/${encodeURIComponent(element.dataset.id)}`, { method: 'DELETE' });
      await loadWorkspace();
      showToast('Media asset deleted.');
      break;
    }
    case 'load-analytics':
      await loadAnalytics();
      break;
    case 'load-weekly-report':
      await loadWeeklyReport();
      break;
    case 'copy-variant': {
      const { requiresReapproval } = await saveCurrentVariant();
      const item = state.items.find((candidate) => candidate.id === editorId);
      const current = item?.variants?.[editorPlatform];
      const text = [current?.body, current?.caption].filter(Boolean).join('\n\n');
      if (requiresReapproval) {
        renderPage();
        renderDialog();
        showToast('This approved draft changed and needs review again.');
      }
      if (!text) {
        showToast('There is no copy in this platform version yet.', true);
        break;
      }
      if (navigator.clipboard?.writeText) {
        navigator.clipboard.writeText(text).then(() => showToast('Platform version copied to clipboard.')).catch(() => showToast('Clipboard access was blocked by the browser.', true));
      } else {
        showToast('Clipboard access is unavailable in this browser context.', true);
      }
      break;
    }
    case 'delete-idea': {
      const item = state.items.find((candidate) => candidate.id === editorId);
      if (item && item.status === 'IDEA' && window.confirm('Delete this idea from the server workspace?')) {
        await apiRequest(`/api/ideas/${encodeURIComponent(item.id)}`, { method: 'DELETE' });
        state.items = state.items.filter((candidate) => candidate.id !== item.id);
        closeDialog();
        renderPage();
        showToast('Idea deleted from the server workspace.');
      }
      break;
    }
    case 'calendar-prev':
      calendarOffset -= 1;
      renderCalendar();
      break;
    case 'calendar-next':
      calendarOffset += 1;
      renderCalendar();
      break;
    case 'calendar-today':
      calendarOffset = 0;
      renderCalendar();
      break;
    default:
      break;
  }
}

document.addEventListener('click', (event) => {
  if (event.target.closest('#login-retry')) {
    event.preventDefault();
    initializeApp();
    return;
  }
  const pageButton = event.target.closest('[data-page]');
  if (pageButton) {
    event.preventDefault();
    navigate(pageButton.dataset.page);
    return;
  }
  const actionButton = event.target.closest('[data-action]');
  if (actionButton) {
    event.preventDefault();
    handleAction(actionButton.dataset.action, actionButton).catch((error) => showToast(error.message || 'That action could not be completed.', true));
  }
});

document.addEventListener('input', (event) => {
  if (event.target.id === 'library-search') updateLibraryResults();
});

document.addEventListener('change', (event) => {
  if (event.target.id === 'platform-filter') updateLibraryResults();
  const accountSelect = event.target.closest('[data-account-platform="tiktok"]');
  if (accountSelect && accountSelect.value && editorId) {
    refreshTikTokCreatorInfo(accountSelect.value).catch((error) => showToast(error.message || 'TikTok creator settings could not be loaded.', true));
  }
});

document.addEventListener('submit', (event) => {
  if (event.target.id === 'login-form') {
    event.preventDefault();
    signIn(event.target);
  } else if (event.target.id === 'idea-form') {
    event.preventDefault();
    addNewIdea(event.target).catch((error) => showToast(error.message || 'Could not save the idea.', true));
  } else if (event.target.id === 'brand-profile-form') {
    event.preventDefault();
    updateBrandProfile(event.target).catch((error) => showToast(error.message || 'Could not save the brand profile.', true));
  } else if (event.target.id === 'asset-upload-form') {
    event.preventDefault();
    uploadAssetForm(event.target).catch((error) => showToast(error.message || 'Media upload failed.', true));
  }
});

dialog.addEventListener('click', (event) => {
  if (event.target === dialog) closeDialog();
});
dialog.addEventListener('close', () => {
  modalMode = null;
  editorId = null;
});

document.getElementById('mobile-menu').addEventListener('click', (event) => {
  const sidebar = document.getElementById('sidebar');
  const isOpen = sidebar.classList.toggle('is-open');
  event.currentTarget.setAttribute('aria-expanded', String(isOpen));
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') document.getElementById('sidebar').classList.remove('is-open');
});

hydrateIcons();
initializeApp();
