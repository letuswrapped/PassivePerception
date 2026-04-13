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
checkPlayerSetup();

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

/* ── Past sessions ───────────────────────────────────────────────────────── */
$('sessions-btn').addEventListener('click', async () => {
  const res = await fetch('/sessions');
  const { sessions } = await res.json();
  if (!sessions.length) { showToast('No past sessions found.', ''); return; }
  alert('Past sessions:\n' + sessions.map(s => s.name).join('\n'));
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
async function exportTranscript() {
  try {
    const res = await fetch('/session/export/transcript');
    if (!res.ok) { showToast('No transcript to export yet.', 'error'); return; }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'transcript.md';
    a.click();
    URL.revokeObjectURL(url);
    showToast('Transcript exported', 'success');
  } catch { showToast('Export failed', 'error'); }
}

async function exportNotes() {
  try {
    const res = await fetch('/session/export/notes');
    if (!res.ok) { showToast('No notes to export yet.', 'error'); return; }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'notes.md';
    a.click();
    URL.revokeObjectURL(url);
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
