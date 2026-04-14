/* ── State ───────────────────────────────────────────────────────────────── */
const state = {
  running: false,
  elapsed: 0,
  timerInterval: null,
  pollInterval: null,
  speakerColors: {},
  speakerColorNext: 0,
  autoScroll: true,
  pendingRename: null,
  lastTranscriptIndex: 0,
  diarVersion: -1,         // tracks diarization pass version for retroactive relabeling
};

/* ── DOM refs ────────────────────────────────────────────────────────────── */
const $ = id => document.getElementById(id);
const btnStart           = $('btn-start');
const btnNew             = $('btn-new');
const processingBanner   = $('processing-banner');
const processingMsg      = $('processing-msg');
const deviceSelect     = $('device-select');
const sessionNameInput = $('session-name-input');
const timerEl          = $('timer');
const statusDot        = $('status-indicator');
const transcriptBody   = $('transcript-body');
const transcriptEmpty  = $('transcript-empty');
const summaryText      = $('summary-text');
const npcsList         = $('npcs-list');
const locationsList    = $('locations-list');
const plotList         = $('plot-list');
const questionsList    = $('questions-list');
const npcCount         = $('npc-count');
const notesUpdated     = $('notes-updated');
const renamePopover    = $('rename-popover');
const renameInput      = $('rename-input');
const renameConfirm    = $('rename-confirm');
const toast            = $('toast');

/* ── Init ────────────────────────────────────────────────────────────────── */
loadDevices();
checkOnboarding();

/* ── Onboarding Walkthrough ─────────────────────────────────────────────── */
function copyCmd(codeId, btn) {
  const code = document.getElementById(codeId);
  if (!code) return;
  navigator.clipboard.writeText(code.textContent.trim());
  btn.textContent = 'Copied!';
  setTimeout(() => btn.textContent = 'Copy', 1500);
}

function checkOnboarding() {
  if (localStorage.getItem('pp_onboarding_complete')) {
    checkPlayerSetup();
    return;
  }
  $('onboarding-overlay').style.display = 'flex';
  initOnboarding();
}

function initOnboarding() {
  let currentStep = 0;
  const totalSteps = 3;
  const pages = document.querySelectorAll('.onboarding-page');
  const dots = document.querySelectorAll('.onboarding-dot');
  const btnNext = $('onboarding-next');
  const btnBack = $('onboarding-back');

  function showStep(step) {
    pages.forEach(p => p.classList.remove('active'));
    dots.forEach(d => {
      d.classList.remove('active');
      if (parseInt(d.dataset.step) < step) d.classList.add('completed');
      else d.classList.remove('completed');
    });
    pages[step].classList.add('active');
    dots[step].classList.add('active');

    // Back button visibility
    btnBack.style.visibility = step === 0 ? 'hidden' : 'visible';

    // Next button text
    if (step === 0) btnNext.textContent = 'Get Started';
    else if (step === totalSteps - 1) btnNext.textContent = 'Start Playing';
    else btnNext.textContent = 'Next';

    currentStep = step;
  }

  btnNext.addEventListener('click', () => {
    if (currentStep === totalSteps - 1) {
      localStorage.setItem('pp_onboarding_complete', '1');
      $('onboarding-overlay').style.display = 'none';
      checkPlayerSetup();
      return;
    }
    showStep(currentStep + 1);
  });

  btnBack.addEventListener('click', () => {
    if (currentStep > 0) showStep(currentStep - 1);
  });

  showStep(0);
}

/* ── Player Setup ───────────────────────────────────────────────────────── */
function checkPlayerSetup() {
  const saved = localStorage.getItem('pp_player_context');
  if (!saved) {
    $('setup-overlay').style.display = 'flex';
  }
}

$('setup-save').addEventListener('click', async () => {
  const ctx = {
    player_name:   $('setup-player-name').value.trim(),
    char_name:     $('setup-char-name').value.trim(),
    char_race:     $('setup-char-race').value.trim(),
    char_class:    $('setup-char-class').value.trim(),
    char_subclass:     $('setup-char-subclass').value.trim(),
    multiclass:        $('setup-multiclass-check').checked,
    multi_class:       $('setup-multi-class').value.trim(),
    multi_subclass:    $('setup-multi-subclass').value.trim(),
    char_bio:          $('setup-char-bio').value.trim(),
  };

  if (!ctx.player_name && !ctx.char_name) {
    showToast('Enter at least your name or character name.', 'error');
    return;
  }

  localStorage.setItem('pp_player_context', JSON.stringify(ctx));

  try {
    await fetch('/player/context', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(ctx),
    });
  } catch { /* server may not be ready yet — saved locally */ }

  $('setup-overlay').style.display = 'none';
  showToast('Player info saved', 'success');
});

$('setup-skip').addEventListener('click', () => {
  localStorage.setItem('pp_player_context', JSON.stringify({ skipped: true }));
  $('setup-overlay').style.display = 'none';
});

// File upload — read text and put into bio textarea
$('setup-bio-file').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  $('upload-file-name').textContent = file.name;

  try {
    const text = await file.text();
    $('setup-char-bio').value = text;
    showToast(`Loaded ${file.name}`, 'success');
  } catch {
    showToast('Could not read file', 'error');
  }
});

// Restore fields if previously saved (for editing)
function loadSavedContext() {
  const saved = localStorage.getItem('pp_player_context');
  if (!saved) return;
  try {
    const ctx = JSON.parse(saved);
    if (ctx.skipped) return;
    if (ctx.player_name) $('setup-player-name').value = ctx.player_name;
    if (ctx.char_name)   $('setup-char-name').value   = ctx.char_name;
    if (ctx.char_race)   $('setup-char-race').value   = ctx.char_race;
    if (ctx.char_class)    $('setup-char-class').value    = ctx.char_class;
    if (ctx.char_subclass)  $('setup-char-subclass').value  = ctx.char_subclass;
    if (ctx.multiclass) {
      $('setup-multiclass-check').checked = true;
      $('multiclass-fields').style.display = 'block';
    }
    if (ctx.multi_class)    $('setup-multi-class').value    = ctx.multi_class;
    if (ctx.multi_subclass) $('setup-multi-subclass').value = ctx.multi_subclass;
    if (ctx.char_bio)       $('setup-char-bio').value       = ctx.char_bio;
  } catch {}
}
loadSavedContext();

$('setup-multiclass-check').addEventListener('change', (e) => {
  $('multiclass-fields').style.display = e.target.checked ? 'block' : 'none';
});

async function loadDevices() {
  try {
    const res = await fetch('/devices');
    const { devices } = await res.json();
    deviceSelect.innerHTML = '';
    if (!devices.length) {
      deviceSelect.innerHTML = '<option>No input devices found</option>';
      return;
    }
    let hasBlackhole = false;
    devices.forEach(dev => {
      const opt = document.createElement('option');
      opt.value = dev.index;
      opt.textContent = dev.name;
      if (dev.name.toLowerCase().includes('blackhole')) {
        opt.selected = true;
        hasBlackhole = true;
      }
      deviceSelect.appendChild(opt);
    });
    if (!hasBlackhole) {
      showToast('BlackHole not detected — make sure it\'s installed and selected.', 'error');
    }
  } catch {
    deviceSelect.innerHTML = '<option>Could not load devices</option>';
  }
}

/* ── Session controls ────────────────────────────────────────────────────── */
btnStart.addEventListener('click', async () => {
  if (!state.running) {
    await startSession();
  } else {
    await stopSession();
  }
});

async function startSession() {
  const body = {};
  const name = sessionNameInput.value.trim();
  if (name) body.session_name = name;

  // Send player context with session start
  try {
    const saved = localStorage.getItem('pp_player_context');
    if (saved) {
      const ctx = JSON.parse(saved);
      if (!ctx.skipped) {
        await fetch('/player/context', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: saved,
        });
      }
    }
  } catch {}

  try {
    const res = await fetch('/session/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (data.error) { showToast(data.error, 'error'); return; }

    state.running = true;
    state.lastTranscriptIndex = 0;
    state.diarVersion = -1;
    btnStart.textContent = 'Stop Session';
    btnStart.classList.add('stop');
    statusDot.classList.add('running');
    sessionNameInput.disabled = true;
    deviceSelect.disabled = true;

    state.elapsed = 0;
    state.timerInterval = setInterval(() => {
      state.elapsed++;
      timerEl.textContent = fmtTime(state.elapsed);
    }, 1000);

    // Poll for transcript and notes updates every 3 seconds
    state.pollInterval = setInterval(pollUpdates, 3000);

    showToast('Session started', 'success');
  } catch (e) {
    showToast('Failed to start session: ' + e.message, 'error');
  }
}

async function stopSession() {
  btnStart.disabled = true;
  btnStart.textContent = 'Stopping…';
  try {
    await fetch('/session/stop', { method: 'POST' });

    clearInterval(state.timerInterval);
    clearInterval(state.pollInterval);
    state.running = false;
    btnStart.textContent = 'Start Session';
    btnStart.classList.remove('stop');
    btnStart.disabled = false;
    statusDot.classList.remove('running');
    sessionNameInput.disabled = false;
    deviceSelect.disabled = false;

    // Show processing banner and poll until diarization completes
    processingBanner.style.display = 'flex';
    processingMsg.textContent = 'Starting post-session processing…';
    pollProcessing();
  } catch (e) {
    showToast('Error stopping session: ' + e.message, 'error');
    btnStart.disabled = false;
  }
}

async function pollProcessing() {
  const interval = setInterval(async () => {
    try {
      const res = await fetch('/session/status');
      const data = await res.json();
      if (data.progress) processingMsg.textContent = data.progress;

      if (data.state === 'idle') {
        clearInterval(interval);
        processingBanner.style.display = 'none';
        // Refresh transcript with speaker labels now applied
        await pollUpdates();
        showToast('Session saved ✓  Speaker labels applied', 'success');
      }
    } catch {
      // ignore transient errors
    }
  }, 2000);
}

/* ── Polling ─────────────────────────────────────────────────────────────── */
async function pollUpdates() {
  try {
    const [transcriptRes, notesRes] = await Promise.all([
      fetch(`/session/transcript_lines?offset=${state.lastTranscriptIndex}&diar_version=${state.diarVersion}`),
      fetch('/session/notes'),
    ]);

    const transcriptData = await transcriptRes.json();
    const notesData = await notesRes.json();

    if (transcriptData.diar_version !== undefined) {
      state.diarVersion = transcriptData.diar_version;
    }

    if (transcriptData.full_refresh && transcriptData.lines.length > 0) {
      // Diarization updated — patch speaker labels on existing DOM lines
      transcriptData.lines.forEach(line => updateTranscriptLine(line));
      state.lastTranscriptIndex = transcriptData.total;
    } else if (transcriptData.lines && transcriptData.lines.length > 0) {
      // New lines only
      transcriptData.lines.forEach(line => renderTranscriptLine(line));
      state.lastTranscriptIndex += transcriptData.lines.length;
    }

    if (notesData.notes) {
      renderNotes(notesData.notes);
    }
  } catch (e) {
    // Silently ignore poll errors
  }
}

/* ── Transcript rendering ────────────────────────────────────────────────── */
function renderTranscriptLine(msg) {
  if (transcriptEmpty) transcriptEmpty.style.display = 'none';

  const colorIdx = getSpeakerColor(msg.speaker_id);
  const line = document.createElement('div');
  line.className = `transcript-line spk-${colorIdx}`;
  line.dataset.speakerId = msg.speaker_id;
  line.dataset.lineIndex = msg.index ?? state.lastTranscriptIndex;

  const ts = document.createElement('span');
  ts.className = 'ts';
  ts.textContent = fmtTime(msg.start);

  const speakerBtn = document.createElement('button');
  speakerBtn.className = 'speaker-btn';
  speakerBtn.textContent = msg.speaker_label;
  speakerBtn.dataset.speakerId = msg.speaker_id;
  speakerBtn.addEventListener('click', e => openRenamePopover(e, msg.speaker_id, msg.speaker_label));

  const text = document.createElement('span');
  text.className = 'text';
  text.textContent = msg.text;

  line.appendChild(ts);
  line.appendChild(speakerBtn);
  line.appendChild(text);
  transcriptBody.appendChild(line);

  if (state.autoScroll) {
    transcriptBody.scrollTop = transcriptBody.scrollHeight;
  }
}

function updateTranscriptLine(msg) {
  // Update an existing line's speaker label in the DOM, or render if new
  const existing = transcriptBody.querySelector(`[data-line-index="${msg.index}"]`);
  if (existing) {
    const btn = existing.querySelector('.speaker-btn');
    if (btn && btn.textContent !== msg.speaker_label) {
      btn.textContent = msg.speaker_label;
      btn.dataset.speakerId = msg.speaker_id;
    }
    // Update color class
    const colorIdx = getSpeakerColor(msg.speaker_id);
    existing.className = `transcript-line spk-${colorIdx}`;
    existing.dataset.speakerId = msg.speaker_id;
  } else {
    renderTranscriptLine(msg);
  }
}

function getSpeakerColor(speakerId) {
  if (!(speakerId in state.speakerColors)) {
    state.speakerColors[speakerId] = state.speakerColorNext % 6;
    state.speakerColorNext++;
  }
  return state.speakerColors[speakerId];
}

/* ── Notes rendering ─────────────────────────────────────────────────────── */
function renderNotes(notes) {
  // Summary
  if (notes.summary) {
    summaryText.textContent = notes.summary;
    summaryText.classList.remove('empty-state');
  }

  // NPCs
  if (notes.npcs?.length) {
    npcCount.textContent = `(${notes.npcs.length})`;
    npcsList.innerHTML = notes.npcs.map(npc => `
      <div class="npc-card">
        <div>
          <span class="npc-name">${esc(npc.name)}</span>
          ${npc.relationship ? `<span class="npc-rel">${esc(npc.relationship)}</span>` : ''}
        </div>
        ${npc.description ? `<div class="npc-desc">${esc(npc.description)}</div>` : ''}
        ${npc.notes ? `<div class="npc-notes">${esc(npc.notes)}</div>` : ''}
      </div>`).join('');
  }

  // Locations
  if (notes.locations?.length) {
    locationsList.innerHTML = notes.locations.map(loc => `
      <div class="location-item">
        <div class="loc-name">${esc(loc.name)}</div>
        ${loc.description ? `<div class="loc-sig">${esc(loc.description)}</div>` : ''}
        ${loc.significance ? `<div class="loc-sig" style="font-style:italic">${esc(loc.significance)}</div>` : ''}
      </div>`).join('');
  }

  // Plot points
  if (notes.plot_points?.length) {
    plotList.innerHTML = notes.plot_points.map((pp, i) => `
      <div class="plot-point">
        <span class="pp-num">${i + 1}.</span>
        <div>
          <div class="pp-text">${esc(pp.summary)}</div>
          ${pp.npcs_involved?.length ? `<div class="pp-npcs">${esc(pp.npcs_involved.join(', '))}</div>` : ''}
        </div>
      </div>`).join('');
  }

  // Open questions
  if (notes.open_questions?.length) {
    questionsList.innerHTML = notes.open_questions.map(q =>
      `<div class="question-item">${esc(q)}</div>`
    ).join('');
  }

  // Date stamp
  const now = new Date();
  const dateStr = now.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
  const timeStr = `${now.getHours()}:${String(now.getMinutes()).padStart(2,'0')}`;
  notesUpdated.textContent = `${dateStr} · ${timeStr}`;
}

/* ── Speaker rename ──────────────────────────────────────────────────────── */
function openRenamePopover(event, speakerId, currentLabel) {
  state.pendingRename = { speaker_id: speakerId, label: currentLabel };
  renameInput.value = currentLabel;
  renamePopover.style.display = 'flex';

  const rect = event.target.getBoundingClientRect();
  renamePopover.style.top  = (rect.bottom + 6) + 'px';
  renamePopover.style.left = rect.left + 'px';

  renameInput.focus();
  renameInput.select();
}

renameConfirm.addEventListener('click', async () => {
  const label = renameInput.value.trim();
  if (!label || !state.pendingRename) return;

  renamePopover.style.display = 'none';
  const { speaker_id } = state.pendingRename;
  state.pendingRename = null;

  document.querySelectorAll(`.speaker-btn[data-speaker-id="${speaker_id}"]`)
    .forEach(btn => btn.textContent = label);

  await fetch('/session/rename_speaker', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ speaker_id, label }),
  });
});

renameInput.addEventListener('keydown', e => {
  if (e.key === 'Enter') renameConfirm.click();
  if (e.key === 'Escape') { renamePopover.style.display = 'none'; state.pendingRename = null; }
});

document.addEventListener('click', e => {
  if (!renamePopover.contains(e.target) && !e.target.classList.contains('speaker-btn')) {
    renamePopover.style.display = 'none';
    state.pendingRename = null;
  }
});

/* ── Collapsible sections ────────────────────────────────────────────────── */
function toggleSection(header) {
  header.classList.toggle('collapsed');
  header.nextElementSibling.classList.toggle('collapsed');
}

/* ── Scroll pause on manual scroll ──────────────────────────────────────── */
transcriptBody.addEventListener('scroll', () => {
  const atBottom = transcriptBody.scrollHeight - transcriptBody.scrollTop - transcriptBody.clientHeight < 40;
  state.autoScroll = atBottom;
});

/* ── New Session ─────────────────────────────────────────────────────────── */
btnNew.addEventListener('click', () => {
  if (state.running) {
    showToast('Stop the current session first.', 'error');
    return;
  }

  // Clear transcript
  transcriptBody.innerHTML = '';
  transcriptBody.insertAdjacentHTML('beforeend',
    '<div class="empty-state" id="transcript-empty">Transcript will appear here once a session starts.</div>'
  );

  // Reset notes panel
  summaryText.textContent = 'No summary yet.';
  summaryText.classList.add('empty-state');
  npcsList.innerHTML     = '<div class="empty-state">No NPCs identified yet.</div>';
  locationsList.innerHTML = '<div class="empty-state">No locations identified yet.</div>';
  plotList.innerHTML     = '<div class="empty-state">No plot points yet.</div>';
  questionsList.innerHTML = '<div class="empty-state">No open questions yet.</div>';
  npcCount.textContent   = '';
  notesUpdated.textContent = '';

  // Reset state
  state.speakerColors    = {};
  state.speakerColorNext = 0;
  state.lastTranscriptIndex = 0;
  state.elapsed          = 0;
  timerEl.textContent    = '00:00';
  sessionNameInput.value = '';
  sessionNameInput.disabled = false;

  showToast('Ready for new session', 'success');
});

/* ── Archives Panel ──────────────────────────────────────────────────────── */
$('sessions-btn').addEventListener('click', () => openArchives());

$('archives-close').addEventListener('click', closeArchives);
$('archives-overlay').addEventListener('click', (e) => {
  if (e.target === $('archives-overlay')) closeArchives();
});
$('archives-back').addEventListener('click', () => {
  $('archives-list-view').style.display = '';
  $('archives-detail-view').style.display = 'none';
});

// Tab switching
document.querySelectorAll('.archives-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.archives-tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    const sessionId = $('archives-detail-view').dataset.sessionId;
    if (tab.dataset.tab === 'notes') loadArchiveNotes(sessionId);
    else loadArchiveTranscript(sessionId);
  });
});

async function openArchives() {
  $('archives-overlay').style.display = 'block';
  $('archives-list-view').style.display = '';
  $('archives-detail-view').style.display = 'none';

  const list = $('archives-list');
  list.innerHTML = '<div class="empty-state" style="padding:24px;">Loading…</div>';

  try {
    const res = await fetch('/sessions');
    const { sessions } = await res.json();

    if (!sessions.length) {
      list.innerHTML = '<div class="empty-state" style="padding:24px;">No past sessions found.</div>';
      return;
    }

    list.innerHTML = '';
    sessions.forEach(s => {
      const item = document.createElement('div');
      item.className = 'archives-session-item';
      item.innerHTML = `
        <div class="archives-session-icon">📜</div>
        <div class="archives-session-info">
          <div class="archives-session-name">${esc(s.name)}</div>
          <div class="archives-session-meta">${s.has_notes ? 'Notes available' : 'Transcript only'}</div>
        </div>
        <div class="archives-session-arrow">›</div>
      `;
      item.addEventListener('click', () => openArchiveSession(s.id, s.name));
      list.appendChild(item);
    });
  } catch {
    list.innerHTML = '<div class="empty-state" style="padding:24px;">Failed to load sessions.</div>';
  }
}

function closeArchives() {
  $('archives-overlay').style.display = 'none';
}

async function openArchiveSession(sessionId, name) {
  $('archives-list-view').style.display = 'none';
  $('archives-detail-view').style.display = '';
  $('archives-detail-view').dataset.sessionId = sessionId;
  $('archives-detail-title').textContent = name;

  // Reset to notes tab
  document.querySelectorAll('.archives-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === 'notes');
  });

  await loadArchiveNotes(sessionId);
}

async function loadArchiveNotes(sessionId) {
  const body = $('archives-detail-body');
  body.textContent = 'Loading…';
  try {
    const res = await fetch(`/sessions/${encodeURIComponent(sessionId)}/notes`);
    const data = await res.json();
    if (data.error) {
      body.textContent = 'No notes found for this session.';
    } else {
      body.textContent = data.notes;
    }
  } catch {
    body.textContent = 'Failed to load notes.';
  }
}

async function loadArchiveTranscript(sessionId) {
  const body = $('archives-detail-body');
  body.textContent = 'Loading…';
  try {
    const res = await fetch(`/sessions/${encodeURIComponent(sessionId)}/transcript`);
    const data = await res.json();
    if (data.error) {
      body.textContent = 'No transcript found for this session.';
    } else {
      body.textContent = data.transcript;
    }
  } catch {
    body.textContent = 'Failed to load transcript.';
  }
}

// Archive export — exports whichever tab is currently active
$('archives-export').addEventListener('click', () => {
  const body = $('archives-detail-body');
  const content = body.textContent || '';
  if (!content || content === 'Loading…') {
    showToast('Nothing to export', 'error');
    return;
  }
  const activeTab = document.querySelector('.archives-tab.active');
  const isNotes = activeTab && activeTab.dataset.tab === 'notes';
  const sessionId = $('archives-detail-view').dataset.sessionId || 'session';
  const filename = isNotes ? `${sessionId}_notes.txt` : `${sessionId}_transcript.txt`;
  _downloadText(content, filename);
  showToast(`${isNotes ? 'Notes' : 'Transcript'} exported`, 'success');
});

// Archive delete — show confirmation
$('archives-delete').addEventListener('click', () => {
  $('archives-delete-confirm').style.display = 'flex';
});

$('archives-delete-cancel').addEventListener('click', () => {
  $('archives-delete-confirm').style.display = 'none';
});

$('archives-delete-yes').addEventListener('click', async () => {
  const sessionId = $('archives-detail-view').dataset.sessionId;
  $('archives-delete-confirm').style.display = 'none';
  try {
    const res = await fetch(`/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' });
    const data = await res.json();
    if (data.ok) {
      showToast('Session deleted', 'success');
      // Go back to session list and refresh
      $('archives-list-view').style.display = '';
      $('archives-detail-view').style.display = 'none';
      openArchives();
    } else {
      showToast(data.error || 'Delete failed', 'error');
    }
  } catch {
    showToast('Failed to delete session', 'error');
  }
});

/* ── Settings Panel ─────────────────────────────────────────────────────── */
$('settings-btn').addEventListener('click', () => {
  $('settings-overlay').style.display = 'block';
  loadSettingsPlayerInfo();
  loadMicDevices();
  loadObsidianConfig();
});

$('settings-close').addEventListener('click', closeSettings);
$('settings-overlay').addEventListener('click', (e) => {
  if (e.target === $('settings-overlay')) closeSettings();
});

function closeSettings() {
  $('settings-overlay').style.display = 'none';
}

// Theme toggle
document.querySelectorAll('#theme-toggle .settings-toggle-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#theme-toggle .settings-toggle-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const theme = btn.dataset.theme;
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('pp_theme', theme);
  });
});

// Apply saved theme on load
(function() {
  const saved = localStorage.getItem('pp_theme') || 'light';
  document.documentElement.setAttribute('data-theme', saved);
  document.querySelectorAll('#theme-toggle .settings-toggle-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.theme === saved);
  });
})();

// Player info — load existing data into settings fields
async function loadSettingsPlayerInfo() {
  let ctx = null;
  // Try localStorage first
  const saved = localStorage.getItem('pp_player_context');
  if (saved) {
    try {
      const parsed = JSON.parse(saved);
      if (!parsed.skipped) ctx = parsed;
    } catch { /* ignore */ }
  }
  // Fallback: fetch from server
  if (!ctx) {
    try {
      const res = await fetch('/player/context');
      const data = await res.json();
      if (data && data.player_name) ctx = data;
    } catch { /* ignore */ }
  }
  if (!ctx) return;
  $('settings-player-name').value = ctx.player_name || '';
  $('settings-char-name').value = ctx.char_name || '';
  $('settings-char-race').value = ctx.char_race || '';
  $('settings-char-class').value = ctx.char_class || '';
  $('settings-char-subclass').value = ctx.char_subclass || '';
  $('settings-char-bio').value = ctx.char_bio || '';
}

// Save player info from settings
$('settings-save-player').addEventListener('click', async () => {
  const ctx = {
    player_name:   $('settings-player-name').value.trim(),
    char_name:     $('settings-char-name').value.trim(),
    char_race:     $('settings-char-race').value.trim(),
    char_class:    $('settings-char-class').value.trim(),
    char_subclass: $('settings-char-subclass').value.trim(),
    char_bio:      $('settings-char-bio').value.trim(),
  };
  localStorage.setItem('pp_player_context', JSON.stringify(ctx));
  try {
    await fetch('/player/context', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(ctx),
    });
  } catch { /* best effort */ }
  showToast('Player info updated', 'success');
});

// Mic device selector
async function loadMicDevices() {
  const select = $('settings-mic-select');
  try {
    const res = await fetch('/devices');
    const data = await res.json();
    const devices = data.devices || [];
    const saved = localStorage.getItem('pp_mic_device') || '';

    // Keep the "None" option, clear the rest
    select.innerHTML = '<option value="">None (Discord audio only)</option>';

    // Filter out BlackHole and multi-output (those aren't mics)
    const mics = devices.filter(d =>
      !d.name.toLowerCase().includes('blackhole') &&
      !d.name.toLowerCase().includes('multi-output')
    );
    console.log('[settings] Found mic devices:', mics.map(d => d.name));
    if (mics.length) {
      // Update default option text to show mic count
      select.options[0].textContent = `None — ${mics.length} mic${mics.length > 1 ? 's' : ''} available ▾`;
    }
    mics.forEach(d => {
      const opt = document.createElement('option');
      opt.value = d.name;
      opt.textContent = d.name;
      if (d.name === saved) opt.selected = true;
      select.appendChild(opt);
    });

    if (!mics.length) {
      const opt = document.createElement('option');
      opt.disabled = true;
      opt.textContent = 'No microphones detected';
      select.appendChild(opt);
    }
  } catch (err) {
    console.error('[settings] Failed to load mic devices:', err);
    select.innerHTML = '<option value="">None (Discord audio only)</option>';
    const opt = document.createElement('option');
    opt.disabled = true;
    opt.textContent = 'Error loading devices';
    select.appendChild(opt);
  }
}

$('settings-mic-select').addEventListener('change', async (e) => {
  const device = e.target.value;
  localStorage.setItem('pp_mic_device', device);
  try {
    await fetch('/settings/mic-device', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device }),
    });
    showToast(device ? `Mic set: ${device}` : 'Mic disabled', 'success');
  } catch { showToast('Failed to set mic', 'error'); }
});

// Restore mic device on app load
(async function() {
  const saved = localStorage.getItem('pp_mic_device');
  if (saved) {
    try {
      await fetch('/settings/mic-device', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device: saved }),
      });
    } catch { /* best effort */ }
  }
})();

// ── Obsidian Integration ──────────────────────────────────────────────────

async function loadObsidianConfig() {
  try {
    const res = await fetch('/settings/obsidian');
    const config = await res.json();
    if (config.vault_path) {
      // Connected — show setup view
      $('obsidian-disconnected').style.display = 'none';
      $('obsidian-setup').style.display = '';
      $('obsidian-vault-path').value = config.vault_path || '';
      $('obsidian-subfolder').value = config.subfolder || 'D&D Sessions';
      // Set auto-export toggle
      const autoOn = config.auto_export !== false;
      document.querySelectorAll('#obsidian-auto-toggle .settings-toggle-btn').forEach(b => {
        b.classList.toggle('active', (b.dataset.val === 'true') === autoOn);
      });
      $('obsidian-status').textContent = '✓ Connected to vault';
      $('obsidian-status').style.color = 'var(--success)';
    } else {
      $('obsidian-disconnected').style.display = '';
      $('obsidian-setup').style.display = 'none';
    }
  } catch { /* best effort */ }
}

$('obsidian-connect-btn').addEventListener('click', () => {
  $('obsidian-disconnected').style.display = 'none';
  $('obsidian-setup').style.display = '';
  $('obsidian-status').textContent = '';
});

$('obsidian-browse').addEventListener('click', async () => {
  $('obsidian-browse').textContent = 'Picking…';
  $('obsidian-browse').disabled = true;
  try {
    const res = await fetch('/settings/obsidian/browse', { method: 'POST' });
    const data = await res.json();
    if (data.ok && data.path) {
      $('obsidian-vault-path').value = data.path;
    }
  } catch { /* cancelled or error */ }
  $('obsidian-browse').textContent = 'Browse';
  $('obsidian-browse').disabled = false;
});

// Auto-export toggle
document.querySelectorAll('#obsidian-auto-toggle .settings-toggle-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#obsidian-auto-toggle .settings-toggle-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
  });
});

$('obsidian-save').addEventListener('click', async () => {
  const vaultPath = $('obsidian-vault-path').value.trim();
  const subfolder = $('obsidian-subfolder').value.trim();
  const autoBtn = document.querySelector('#obsidian-auto-toggle .settings-toggle-btn.active');
  const autoExport = autoBtn ? autoBtn.dataset.val === 'true' : true;

  if (!vaultPath) {
    $('obsidian-status').textContent = 'Please enter a vault path.';
    $('obsidian-status').style.color = 'var(--danger)';
    return;
  }

  try {
    const res = await fetch('/settings/obsidian', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ vault_path: vaultPath, subfolder, auto_export: autoExport }),
    });
    const data = await res.json();
    if (data.ok) {
      $('obsidian-status').textContent = '✓ Connected to vault';
      $('obsidian-status').style.color = 'var(--success)';
      showToast('Obsidian vault connected', 'success');
    } else {
      $('obsidian-status').textContent = data.error || 'Failed to connect';
      $('obsidian-status').style.color = 'var(--danger)';
    }
  } catch {
    $('obsidian-status').textContent = 'Connection failed';
    $('obsidian-status').style.color = 'var(--danger)';
  }
});

$('obsidian-disconnect').addEventListener('click', async () => {
  try {
    await fetch('/settings/obsidian/disconnect', { method: 'POST' });
  } catch { /* best effort */ }
  $('obsidian-disconnected').style.display = '';
  $('obsidian-setup').style.display = 'none';
  showToast('Obsidian disconnected', '');
});

/* ── Helpers ─────────────────────────────────────────────────────────────── */
function fmtTime(seconds) {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
}

function esc(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/* ── Export ──────────────────────────────────────────────────────────────── */
function _downloadText(text, filename) {
  // Works reliably in pywebview + regular browsers
  const blob = new Blob([text], { type: 'application/octet-stream' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  // Clean up after a tick
  setTimeout(() => {
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, 100);
}

async function exportTranscript() {
  try {
    const res = await fetch('/session/export/transcript');
    if (!res.ok) { showToast('No transcript to export yet.', 'error'); return; }
    const text = await res.text();
    _downloadText(text, 'transcript.txt');
    showToast('Transcript exported', 'success');
  } catch { showToast('Export failed', 'error'); }
}

async function exportNotes() {
  try {
    const res = await fetch('/session/export/notes');
    if (!res.ok) { showToast('No notes to export yet.', 'error'); return; }
    const text = await res.text();
    _downloadText(text, 'notes.txt');
    showToast('Notes exported', 'success');
  } catch { showToast('Export failed', 'error'); }
}

let toastTimeout;
function showToast(msg, type = '') {
  toast.textContent = msg;
  toast.className = 'show' + (type ? ' ' + type : '');
  clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => toast.className = '', 3500);
}
