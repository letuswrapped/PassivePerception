/* ──────────────────────────────────────────────────────────────────────────
   Passive Perception — frontend
   View router + polling + campaign editor + speaker labeling.
   All state lives in memory; disk state comes from the backend via REST.
   ────────────────────────────────────────────────────────────────────────── */

const state = {
  view: null,                 // current visible view id
  session: {                  // mirrors /session/status
    state: 'idle',
    elapsed: 0,
    progress: '',
    session_id: null,
  },
  campaigns: [],              // list from /campaigns
  activeCampaign: null,       // full Campaign object
  pollTimer: null,
  liveTimerTimer: null,
  pass1: null,                // last Pass1Result + transcript from /session/pass1
  currentLabels: {},          // SPEAKER_XX -> label string (draft)
  editingCampaign: null,      // the campaign currently open in the editor
  onboardingStep: 0,
  apiKeys: { deepgram: false, gemini: false },
  showOther: true,
  resumeOffered: null,        // session id currently shown in the resume banner
};

/* ── Tiny helpers ─────────────────────────────────────────────────────── */

const $ = (id) => document.getElementById(id);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const fmtTime = (sec) => {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
};

const esc = (s) => String(s ?? '')
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#039;');

function toast(message, kind = 'info', ms = 3500) {
  const t = $('toast');
  t.textContent = message;
  t.className = `toast toast-${kind}`;
  setTimeout(() => t.classList.add('hidden'), ms);
  // Force reflow so re-invocations restart the fade
  void t.offsetWidth;
  t.classList.remove('hidden');
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body && body.detail) msg = body.detail;
      else if (body && body.error) msg = body.error;
    } catch {}
    throw new Error(msg);
  }
  const ct = res.headers.get('Content-Type') || '';
  if (ct.includes('application/json')) return res.json();
  return res.text();
}

/* ── View router ──────────────────────────────────────────────────────── */

const ALL_VIEWS = ['view-onboarding', 'view-home', 'view-live', 'view-labeling', 'view-notes', 'view-archives'];

function showView(viewId) {
  state.view = viewId;
  ALL_VIEWS.forEach((id) => $(id)?.classList.toggle('hidden', id !== viewId));
}

function setTopbarSubtitle(text) {
  const el = $('topbar-subtitle');
  if (el) el.textContent = text || '';
}

function setStatus(text, kind = 'idle') {
  $('status-text').textContent = text;
  $('status-indicator').dataset.kind = kind;
}

/* ── API key status ───────────────────────────────────────────────────── */

async function loadApiKeyStatus() {
  try {
    state.apiKeys = await api('/settings/api-keys');
  } catch {
    state.apiKeys = { deepgram: false, gemini: false };
  }
  const paint = (elId, ok) => {
    const el = $(elId);
    if (!el) return;
    el.textContent = ok ? 'set' : 'not set';
    el.className = 'status-pill ' + (ok ? 'ok' : 'missing');
  };
  paint('onb-dg-status', state.apiKeys.deepgram);
  paint('onb-gem-status', state.apiKeys.gemini);
  paint('set-dg-status', state.apiKeys.deepgram);
  paint('set-gem-status', state.apiKeys.gemini);
}

async function saveApiKeys(deepgram, gemini) {
  const body = {};
  if (deepgram) body.deepgram = deepgram;
  if (gemini) body.gemini = gemini;
  await api('/settings/api-keys', { method: 'POST', body: JSON.stringify(body) });
  await loadApiKeyStatus();
}

/* ── Campaigns ────────────────────────────────────────────────────────── */

async function loadCampaigns() {
  const data = await api('/campaigns');
  state.campaigns = data.campaigns || [];
  return data;
}

async function loadActiveCampaign() {
  const data = await api('/campaigns/active');
  state.activeCampaign = data.campaign;
  return data.campaign;
}

async function setActiveCampaign(id) {
  await api('/campaigns/active', { method: 'POST', body: JSON.stringify({ id }) });
  await loadActiveCampaign();
}

async function saveCampaign(campaign) {
  const res = await api('/campaigns', { method: 'POST', body: JSON.stringify(campaign) });
  return res.campaign;
}

async function deleteCampaign(id) {
  await api(`/campaigns/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

/* ── Polling ──────────────────────────────────────────────────────────── */

async function pollStatus() {
  try {
    const s = await api('/session/status');
    const prevState = state.session.state;
    state.session = s;
    onStatusChanged(prevState, s.state);
  } catch {
    // ignore transient errors
  }
}

function startPolling(intervalMs = 2000) {
  stopPolling();
  pollStatus();
  state.pollTimer = setInterval(pollStatus, intervalMs);
}

function stopPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = null;
}

async function onStatusChanged(prev, curr) {
  // Update topbar status chip
  const label = ({
    idle: 'Idle',
    running: 'Recording',
    stopping: 'Stopping…',
    processing_pass1: 'Identifying…',
    awaiting_labels: 'Labeling',
    processing_pass2: 'Generating…',
  })[curr] || curr;
  setStatus(label, curr);

  if (curr === 'running') {
    if (state.view !== 'view-live') showView('view-live');
    // Timer tick
    if (!state.liveTimerTimer) {
      state.liveTimerTimer = setInterval(() => {
        $('live-timer').textContent = fmtTime(state.session.elapsed);
      }, 1000);
    }
    // Fetch fresh preview notes periodically (every ~10s is fine — server updates every 15 min anyway)
    try {
      const n = await api('/session/notes');
      if (n && n.notes) renderNotes($('live-notes-body'), n.notes);
    } catch {}
  } else if (state.liveTimerTimer) {
    clearInterval(state.liveTimerTimer);
    state.liveTimerTimer = null;
  }

  if (curr === 'processing_pass1' || curr === 'stopping') {
    if (state.view !== 'view-live') showView('view-live');
    $('live-progress').textContent = state.session.progress || 'Processing…';
  }

  if (curr === 'awaiting_labels' && prev !== 'awaiting_labels') {
    await openLabelingView();
  }

  if (curr === 'processing_pass2') {
    $('live-progress').textContent = state.session.progress || 'Generating notes…';
    if (state.view !== 'view-live') showView('view-live');
  }

  if (curr === 'idle' && (prev === 'processing_pass2' || prev === 'processing_pass1')) {
    // Session just finished — show notes
    await openNotesView();
  }
}

/* ── Home view ────────────────────────────────────────────────────────── */

async function showHome() {
  showView('view-home');
  setTopbarSubtitle('');
  await loadApiKeyStatus();
  await loadCampaigns();
  await loadActiveCampaign();
  paintHomeCampaignPicker();
  paintHomeCampaignPreview();
  await loadBrief();
  updateRecordPreflight();
}

function paintHomeCampaignPicker() {
  const sel = $('home-campaign-picker');
  sel.innerHTML = '';
  if (state.campaigns.length === 0) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = '(no campaigns — create one in Settings)';
    sel.appendChild(opt);
    sel.disabled = true;
    return;
  }
  sel.disabled = false;
  const activeId = state.activeCampaign?.id;
  state.campaigns.forEach((c) => {
    const opt = document.createElement('option');
    opt.value = c.id;
    opt.textContent = c.name;
    if (c.id === activeId) opt.selected = true;
    sel.appendChild(opt);
  });
}

function paintHomeCampaignPreview() {
  const el = $('home-campaign-preview');
  const c = state.activeCampaign;
  if (!c) {
    el.innerHTML = '<p class="muted">No active campaign.</p>';
    return;
  }
  const bits = [];
  const p = c.player || {};
  const charDesc = [p.race, p.char_class, p.subclass].filter(Boolean).join(' ');
  bits.push(`<div class="preview-section">
    <div class="preview-label">Your character</div>
    <div class="preview-value">${esc(p.name || '—')} ${charDesc ? `<span class="muted small">(${esc(charDesc)})</span>` : ''}</div>
  </div>`);
  if (p.goals) bits.push(`<div class="preview-section"><div class="preview-label">Goals</div><div class="preview-value small">${esc(p.goals)}</div></div>`);
  if (c.perspective_notes) bits.push(`<div class="preview-section"><div class="preview-label">Perspective</div><div class="preview-value small">${esc(c.perspective_notes)}</div></div>`);
  bits.push(`<div class="preview-row">
    <div class="preview-pill">${c.party?.length || 0} party</div>
    <div class="preview-pill">${c.npcs?.length || 0} NPCs</div>
    <div class="preview-pill">${c.locations?.length || 0} locations</div>
    <div class="preview-pill">${c.plot_threads?.filter(p => p.status === 'active').length || 0} plots</div>
  </div>`);
  if (c.state?.summary) bits.push(`<div class="preview-section"><div class="preview-label">Last time</div><div class="preview-value small">${esc(c.state.summary)}</div></div>`);
  el.innerHTML = bits.join('');
}

async function loadBrief() {
  try {
    const res = await api('/session/pre-brief');
    $('home-brief').value = res.brief || '';
  } catch {}
}

async function saveBrief() {
  if (!state.activeCampaign) return;
  const brief = $('home-brief').value;
  try {
    await api('/session/pre-brief', { method: 'POST', body: JSON.stringify({ brief }) });
  } catch (e) {
    toast('Could not save brief: ' + e.message, 'error');
  }
}

function updateRecordPreflight() {
  const msg = $('home-preflight-msg');
  const btn = $('btn-record');
  const missing = [];
  if (!state.apiKeys.deepgram) missing.push('Deepgram key');
  if (!state.apiKeys.gemini) missing.push('Gemini key');
  if (!state.activeCampaign) missing.push('active campaign');
  if (missing.length) {
    msg.textContent = 'Missing: ' + missing.join(', ') + '. Open Settings to fix.';
    msg.className = 'field-hint warn';
    btn.disabled = true;
  } else {
    msg.textContent = '';
    btn.disabled = false;
  }
}

async function startSession() {
  const name = $('home-session-name').value.trim();
  await saveBrief();
  try {
    const res = await api('/session/start', {
      method: 'POST',
      body: JSON.stringify({ session_name: name || null }),
    });
    if (res.error) { toast(res.error, 'error'); return; }
    showView('view-live');
    $('live-timer').textContent = '00:00';
    paintLiveRoster();
  } catch (e) {
    toast('Failed to start session: ' + e.message, 'error');
  }
}

async function stopSession() {
  try {
    await api('/session/stop', { method: 'POST' });
    $('live-progress').textContent = 'Stopping…';
  } catch (e) {
    toast('Stop failed: ' + e.message, 'error');
  }
}

function paintLiveRoster() {
  const el = $('live-roster');
  const c = state.activeCampaign;
  if (!c) { el.innerHTML = ''; return; }
  const brief = $('home-brief').value.trim();
  const bits = [];
  bits.push(`<div class="roster-section"><div class="roster-label">Character</div><div>${esc(c.player?.name || '—')}</div></div>`);
  if (brief) bits.push(`<div class="roster-section"><div class="roster-label">Tonight's brief</div><div class="small">${esc(brief)}</div></div>`);
  if ((c.party || []).length) {
    bits.push(`<div class="roster-section"><div class="roster-label">Party</div><div class="small">${c.party.map(p => esc(p.name)).join(', ')}</div></div>`);
  }
  if ((c.npcs || []).length) {
    const names = c.npcs.slice(0, 12).map(n => esc(n.name)).join(', ');
    const more = c.npcs.length > 12 ? ` <span class="muted">+${c.npcs.length - 12}</span>` : '';
    bits.push(`<div class="roster-section"><div class="roster-label">Known NPCs</div><div class="small">${names}${more}</div></div>`);
  }
  if ((c.locations || []).length) {
    const names = c.locations.slice(0, 10).map(l => esc(l.name)).join(', ');
    bits.push(`<div class="roster-section"><div class="roster-label">Locations</div><div class="small">${names}</div></div>`);
  }
  const activeThreads = (c.plot_threads || []).filter(p => p.status === 'active');
  if (activeThreads.length) {
    bits.push(`<div class="roster-section"><div class="roster-label">Active plots</div><ul class="small">${activeThreads.slice(0, 6).map(p => `<li>${esc(p.summary)}</li>`).join('')}</ul></div>`);
  }
  el.innerHTML = bits.join('');
}

/* ── Notes rendering (used in live + notes + archives) ────────────────── */

function renderNotes(host, notes) {
  if (!notes) { host.innerHTML = '<p class="muted">No notes.</p>'; return; }
  const parts = [];
  if (notes.summary) parts.push(`<section class="notes-section"><h4>Summary</h4><p>${esc(notes.summary)}</p></section>`);
  if ((notes.npcs || []).length) {
    parts.push(`<section class="notes-section"><h4>NPCs</h4><ul class="notes-list">${
      notes.npcs.map(n => `<li><strong>${esc(n.name)}</strong>${n.relationship ? ` <span class="muted small">(${esc(n.relationship)})</span>` : ''}${n.description ? `<br><span class="small">${esc(n.description)}</span>` : ''}${n.notes ? `<br><span class="small muted">${esc(n.notes)}</span>` : ''}</li>`).join('')
    }</ul></section>`);
  }
  if ((notes.locations || []).length) {
    parts.push(`<section class="notes-section"><h4>Locations</h4><ul class="notes-list">${
      notes.locations.map(l => `<li><strong>${esc(l.name)}</strong>${l.description ? `<br><span class="small">${esc(l.description)}</span>` : ''}${l.significance ? `<br><em class="small muted">${esc(l.significance)}</em>` : ''}</li>`).join('')
    }</ul></section>`);
  }
  if ((notes.plot_points || []).length) {
    parts.push(`<section class="notes-section"><h4>Plot points</h4><ol class="notes-list">${
      notes.plot_points.map(p => `<li><strong>${esc(p.summary)}</strong>${p.npcs_involved?.length ? ` <span class="muted small">— ${p.npcs_involved.map(esc).join(', ')}</span>` : ''}${p.context ? `<br><span class="small">${esc(p.context)}</span>` : ''}</li>`).join('')
    }</ol></section>`);
  }
  if ((notes.open_questions || []).length) {
    parts.push(`<section class="notes-section"><h4>Open questions</h4><ul class="notes-list">${
      notes.open_questions.map(q => `<li>${esc(q)}</li>`).join('')
    }</ul></section>`);
  }
  host.innerHTML = parts.join('') || '<p class="muted">No notes extracted.</p>';
}

/* ── Labeling view ────────────────────────────────────────────────────── */

async function openLabelingView() {
  try {
    const res = await api('/session/pass1');
    if (!res.ready) {
      // Not ready yet — stay where we are; next poll will retry
      return;
    }
    state.pass1 = res;
    state.currentLabels = {};
    showView('view-labeling');
    paintSpeakerCards();
    paintLabelingTranscript();
  } catch (e) {
    toast('Could not load Pass 1 result: ' + e.message, 'error');
  }
}

function paintSpeakerCards() {
  const host = $('speaker-cards');
  const { speakers } = state.pass1.pass1;
  const transcriptCount = state.pass1.transcript?.length || 0;
  if (!speakers.length || transcriptCount === 0) {
    host.innerHTML = `
      <div class="speaker-card" style="grid-column: 1 / -1;">
        <p><strong>No speech was detected in this session.</strong></p>
        <p class="muted small">The recording produced an empty transcript. The most common cause is that Discord audio wasn't routed through BlackHole — open Settings → Audio (or Audio MIDI Setup) and make sure your Discord output device is a Multi-Output Device that includes BlackHole 2ch.</p>
        <p class="small" style="margin-top:12px;"><button id="labeling-abandon" class="btn-ghost">Back home</button></p>
      </div>
    `;
    $('labeling-abandon').addEventListener('click', () => showHome());
    return;
  }
  const transcript = state.pass1.transcript;
  host.innerHTML = speakers.map((s, i) => {
    const colorClass = `spk-${i % 6}`;
    const roleLabel = s.role_guess && s.role_guess !== 'unknown' ? s.role_guess : '';
    const quotes = (s.sample_quote_indices || [])
      .slice(0, 2)
      .map(idx => transcript[idx])
      .filter(Boolean)
      .map(t => `<blockquote class="sample-quote">${esc(t.text.slice(0, 180))}${t.text.length > 180 ? '…' : ''}</blockquote>`)
      .join('');
    const initial = s.roster_guess || state.currentLabels[s.speaker_id] || '';
    return `
      <div class="speaker-card ${colorClass}" data-speaker="${esc(s.speaker_id)}">
        <div class="speaker-card-head">
          <span class="speaker-chip">${esc(s.speaker_id.replace('SPEAKER_', 'Speaker '))}</span>
          ${roleLabel ? `<span class="role-pill">${esc(roleLabel)}</span>` : ''}
          <span class="muted small">${s.utterance_count} utterances · ${fmtTime(s.total_seconds)}</span>
        </div>
        <p class="speaker-summary">${esc(s.summary || '(no summary)')}</p>
        ${quotes}
        <label class="speaker-label-field">
          Label
          <input type="text" class="speaker-label-input" placeholder="e.g. Sarah (DM), Me, Mike (Grognak)" value="${esc(initial)}" data-speaker="${esc(s.speaker_id)}">
        </label>
      </div>
    `;
  }).join('');

  // Wire inputs
  $$('.speaker-label-input', host).forEach((inp) => {
    inp.addEventListener('input', (e) => {
      const sid = e.target.dataset.speaker;
      state.currentLabels[sid] = e.target.value;
      // Keep the transcript dropdowns in sync so reassignment shows current labels
      syncTranscriptSpeakerLabels(sid);
    });
  });
}

function syncTranscriptSpeakerLabels(speakerId) {
  // Update the <option> text for this speaker across every transcript dropdown
  const label = state.currentLabels[speakerId]?.trim() || _defaultSpeakerLabel(speakerId);
  $$(`.transcript-speaker-select option[value="${speakerId}"]`).forEach((opt) => {
    opt.textContent = label;
  });
}

function paintLabelingTranscript() {
  const host = $('labeling-transcript-body');
  const { transcript, pass1 } = state.pass1;
  const tagByIdx = {};
  (pass1.tags || []).forEach(t => { tagByIdx[t.index] = t.tag; });

  const inChar = (pass1.tags || []).filter(t => t.tag === 'in_character').length;
  const other = (pass1.tags || []).length - inChar;
  $('tag-stats').textContent = `${inChar} in-character · ${other} table-talk`;

  // All known speaker_ids, so the line-level dropdown can offer them
  const allSpeakerIds = (pass1.speakers || []).map(s => s.speaker_id);
  // Include any speaker_ids that only appear in the transcript (edge case)
  transcript.forEach(ln => {
    if (ln.speaker_id && !allSpeakerIds.includes(ln.speaker_id)) allSpeakerIds.push(ln.speaker_id);
  });
  allSpeakerIds.sort();

  const rows = transcript.map((line, i) => {
    const tag = tagByIdx[i] || 'in_character';
    const colorIdx = _speakerColorIndex(line.speaker_id);
    const options = allSpeakerIds.map((sid) => {
      const draft = state.currentLabels[sid] || _defaultSpeakerLabel(sid);
      return `<option value="${esc(sid)}" ${sid === line.speaker_id ? 'selected' : ''}>${esc(draft)}</option>`;
    }).join('');
    return `
      <div class="transcript-line ${tag === 'other' ? 'tag-other' : ''}" data-tag="${tag}" data-idx="${i}">
        <span class="transcript-time">${fmtTime(line.start)}</span>
        <select class="transcript-speaker-select spk-${colorIdx}" data-idx="${i}" title="Reassign this line to a different speaker">${options}</select>
        <span class="transcript-text">${esc(line.text)}</span>
      </div>
    `;
  });
  host.innerHTML = rows.join('');
  applyOtherVisibility();

  // Wire change handlers for speaker reassignment
  $$('.transcript-speaker-select', host).forEach((sel) => {
    sel.addEventListener('change', async (e) => {
      const idx = parseInt(e.target.dataset.idx, 10);
      const newSpeaker = e.target.value;
      try {
        await api('/session/transcript/reassign', {
          method: 'POST',
          body: JSON.stringify({ line_index: idx, speaker_id: newSpeaker }),
        });
        // Update local state so any repaint reflects the change
        state.pass1.transcript[idx].speaker_id = newSpeaker;
        // Update the row's color class
        const row = sel.closest('.transcript-line');
        const newColor = _speakerColorIndex(newSpeaker);
        sel.className = sel.className.replace(/spk-\d+/, `spk-${newColor}`);
        row?.classList.add('line-flash');
        setTimeout(() => row?.classList.remove('line-flash'), 600);
      } catch (err) {
        toast('Reassign failed: ' + err.message, 'error');
        // Revert the select to the old value
        const old = state.pass1.transcript[idx].speaker_id;
        e.target.value = old;
      }
    });
  });
}

function _defaultSpeakerLabel(speakerId) {
  const m = /SPEAKER_(\d+)/.exec(speakerId);
  return m ? `Speaker ${parseInt(m[1], 10) + 1}` : speakerId;
}

function _speakerColorIndex(speakerId) {
  const m = /SPEAKER_(\d+)/.exec(speakerId);
  return m ? parseInt(m[1], 10) % 6 : 0;
}

function applyOtherVisibility() {
  const show = $('show-other').checked;
  $$('#labeling-transcript-body .transcript-line').forEach((row) => {
    if (row.dataset.tag === 'other') {
      row.style.display = show ? '' : 'none';
    }
  });
}

async function finalizeSession(skip) {
  const labels = {};
  if (!skip) {
    Object.entries(state.currentLabels).forEach(([sid, val]) => {
      const v = (val || '').trim();
      if (v) labels[sid] = v;
    });
  }
  try {
    await api('/session/finalize', {
      method: 'POST',
      body: JSON.stringify({ labels: skip ? null : labels, skip: !!skip }),
    });
    showView('view-live');
    $('live-progress').textContent = 'Generating notes…';
  } catch (e) {
    toast('Finalize failed: ' + e.message, 'error');
  }
}

/* ── Notes view (post-pass-2) ─────────────────────────────────────────── */

async function openNotesView() {
  showView('view-notes');
  try {
    const n = await api('/session/notes');
    renderNotes($('notes-body'), n.notes || {});
    const transcript = await api('/session/transcript_lines?offset=0');
    renderPlainTranscript($('notes-transcript-body'), transcript.lines || []);
    $('notes-session-title').textContent = state.session.session_id || 'Session notes';
  } catch (e) {
    toast('Could not load notes: ' + e.message, 'error');
  }
}

function renderPlainTranscript(host, lines) {
  const rows = lines.map((line) => {
    const colorIdx = _speakerColorIndex(line.speaker_id);
    return `
      <div class="transcript-line">
        <span class="transcript-time">${fmtTime(line.start)}</span>
        <span class="transcript-speaker spk-${colorIdx}">${esc(line.speaker_label)}</span>
        <span class="transcript-text">${esc(line.text)}</span>
      </div>
    `;
  });
  host.innerHTML = rows.join('') || '<p class="muted">(empty)</p>';
}

/* ── Archives ─────────────────────────────────────────────────────────── */

async function openArchives() {
  showView('view-archives');
  $('archives-detail').classList.add('hidden');
  try {
    const { sessions } = await api('/sessions');
    const list = $('archives-list');
    if (!sessions.length) {
      list.innerHTML = '<p class="muted">No saved sessions yet.</p>';
      return;
    }
    list.innerHTML = sessions.map((s) => `
      <div class="archive-row" data-id="${esc(s.id)}">
        <div class="archive-row-name">${esc(s.name)}</div>
        <div class="archive-row-meta muted small">${s.has_notes ? 'Notes saved' : 'Pass 1 only — resume to finish'}</div>
      </div>
    `).join('');
    $$('.archive-row', list).forEach((row) => {
      row.addEventListener('click', () => openArchiveDetail(row.dataset.id));
    });
  } catch (e) {
    toast('Could not load archives: ' + e.message, 'error');
  }
}

async function openArchiveDetail(id) {
  $('archives-list').classList.add('hidden');
  $('archives-detail').classList.remove('hidden');
  $('archive-detail-title').textContent = id;
  $('archive-detail-body').dataset.sessionId = id;
  $$('.archive-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === 'notes'));
  await loadArchiveTab(id, 'notes');
}

async function loadArchiveTab(id, tab) {
  const body = $('archive-detail-body');
  body.textContent = 'Loading…';
  try {
    if (tab === 'notes') {
      const res = await api(`/sessions/${encodeURIComponent(id)}/notes`);
      if (res.error) { body.textContent = res.error; return; }
      body.innerHTML = `<pre class="md-output">${esc(res.notes)}</pre>`;
    } else {
      const res = await api(`/sessions/${encodeURIComponent(id)}/transcript`);
      if (res.error) { body.textContent = res.error; return; }
      body.innerHTML = `<pre class="md-output">${esc(res.transcript)}</pre>`;
    }
  } catch (e) {
    body.textContent = 'Error: ' + e.message;
  }
}

async function deleteArchive(id) {
  if (!confirm(`Delete session "${id}"? This cannot be undone.`)) return;
  try {
    await api(`/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' });
    toast('Session deleted');
    openArchives();
  } catch (e) {
    toast('Delete failed: ' + e.message, 'error');
  }
}

/* ── Resume banner ────────────────────────────────────────────────────── */

async function checkResumable() {
  try {
    const { sessions } = await api('/session/resumable');
    if (!sessions.length) {
      $('resume-banner').classList.add('hidden');
      return;
    }
    const s = sessions[0];
    state.resumeOffered = s.id;
    $('resume-banner-text').textContent = `Unfinished session: "${s.name}" — Pass 1 done, waiting for speaker labels.`;
    $('resume-banner').classList.remove('hidden');
  } catch {
    $('resume-banner').classList.add('hidden');
  }
}

async function resumeSession(id) {
  try {
    await api(`/session/resume/${encodeURIComponent(id)}`, { method: 'POST' });
    $('resume-banner').classList.add('hidden');
    await openLabelingView();
  } catch (e) {
    toast('Resume failed: ' + e.message, 'error');
  }
}

/* ── Settings panel ───────────────────────────────────────────────────── */

function openSettings(tab = 'keys') {
  $('settings-panel').classList.remove('hidden');
  switchSettingsTab(tab);
  loadApiKeyStatus();
  paintSettingsCampaignList();
  loadSettingsMic();
  loadSettingsObsidian();
  loadSettingsTheme();
}

function closeSettings() {
  $('settings-panel').classList.add('hidden');
}

function switchSettingsTab(tab) {
  $$('.settings-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  $$('.settings-pane').forEach(p => p.classList.toggle('hidden', p.dataset.pane !== tab));
}

async function paintSettingsCampaignList() {
  await loadCampaigns();
  await loadActiveCampaign();
  const host = $('set-campaign-list');
  if (!state.campaigns.length) {
    host.innerHTML = '<p class="muted small">No campaigns yet. Create one below.</p>';
    return;
  }
  const activeId = state.activeCampaign?.id;
  host.innerHTML = state.campaigns.map((c) => `
    <div class="campaign-row">
      <label class="row">
        <input type="radio" name="active-campaign" value="${esc(c.id)}" ${c.id === activeId ? 'checked' : ''}>
        <span class="campaign-name">${esc(c.name)}</span>
        <span class="muted small">${esc(c.system || '')} · ${c.session_count} sessions</span>
      </label>
      <div class="row">
        <button class="btn-tiny" data-edit="${esc(c.id)}">Edit</button>
        <button class="btn-tiny btn-danger-ghost" data-delete="${esc(c.id)}">Delete</button>
      </div>
    </div>
  `).join('');
  $$('input[name="active-campaign"]', host).forEach((r) => {
    r.addEventListener('change', async () => {
      await setActiveCampaign(r.value);
      toast('Active campaign set');
      // If we're on home, repaint
      if (state.view === 'view-home') await showHome();
    });
  });
  $$('button[data-edit]', host).forEach((b) => {
    b.addEventListener('click', () => openCampaignEditor(b.dataset.edit));
  });
  $$('button[data-delete]', host).forEach((b) => {
    b.addEventListener('click', async () => {
      if (!confirm(`Delete campaign "${b.dataset.delete}"? All roster data is lost.`)) return;
      await deleteCampaign(b.dataset.delete);
      await paintSettingsCampaignList();
      if (state.view === 'view-home') await showHome();
    });
  });
}

async function loadSettingsMic() {
  const sel = $('set-mic-device');
  sel.innerHTML = '<option value="">(none)</option>';
  try {
    const { devices } = await api('/devices');
    (devices || []).forEach((d) => {
      const opt = document.createElement('option');
      opt.value = d.name;
      opt.textContent = d.name;
      sel.appendChild(opt);
    });
  } catch {}
  // Remember selection locally (backend doesn't persist)
  const saved = localStorage.getItem('pp_mic_device') || '';
  sel.value = saved;
}

async function saveSettingsMic() {
  const v = $('set-mic-device').value;
  localStorage.setItem('pp_mic_device', v);
  try {
    await api('/settings/mic-device', { method: 'POST', body: JSON.stringify({ device: v }) });
  } catch (e) {
    toast('Mic save failed: ' + e.message, 'error');
  }
}

async function loadSettingsObsidian() {
  try {
    const cfg = await api('/settings/obsidian');
    $('set-obsidian-vault').value = cfg.vault_path || '';
    $('set-obsidian-subfolder').value = cfg.subfolder || 'D&D Sessions';
    $('set-obsidian-auto').checked = cfg.auto_export !== false;
    $('set-obsidian-status').textContent = cfg.vault_path ? 'Connected' : 'Not connected';
  } catch {
    $('set-obsidian-status').textContent = 'Error loading';
  }
}

function loadSettingsTheme() {
  const theme = localStorage.getItem('pp_theme') || 'dark';
  document.body.dataset.theme = theme;
  $('set-theme-dark').checked = theme === 'dark';
}

/* ── Campaign editor ──────────────────────────────────────────────────── */

function blankCampaign() {
  return {
    id: '',
    name: '',
    system: 'D&D 5e',
    setting: '',
    player: { name: '', role: 'player', race: '', char_class: '', subclass: '', multi_class: '', multi_subclass: '', pronouns: '', description: '', backstory: '', goals: '', notes: '' },
    perspective_notes: '',
    party: [],
    npcs: [],
    locations: [],
    plot_threads: [],
    state: { summary: '', current_location: '', party_status: '', immediate_next_steps: '', unresolved_hooks: [] },
    pending_session_brief: '',
    session_ids: [],
  };
}

async function openCampaignEditor(id) {
  closeSettings();
  const panel = $('campaign-editor');
  let campaign;
  if (id) {
    campaign = await api(`/campaigns/${encodeURIComponent(id)}`);
    $('ce-title').textContent = `Edit: ${campaign.name}`;
  } else {
    campaign = blankCampaign();
    $('ce-title').textContent = 'New campaign';
  }
  state.editingCampaign = campaign;
  paintCampaignEditor(campaign);
  panel.classList.remove('hidden');
}

function closeCampaignEditor() {
  $('campaign-editor').classList.add('hidden');
  state.editingCampaign = null;
}

function paintCampaignEditor(c) {
  $('ce-name').value = c.name || '';
  $('ce-system').value = c.system || '';
  $('ce-setting').value = c.setting || '';

  const p = c.player || {};
  $('ce-player-name').value = p.name || '';
  $('ce-player-pronouns').value = p.pronouns || '';
  $('ce-player-race').value = p.race || '';
  $('ce-player-class').value = p.char_class || '';
  $('ce-player-subclass').value = p.subclass || '';
  $('ce-player-multi').value = p.multi_class || '';
  $('ce-player-description').value = p.description || '';
  $('ce-player-backstory').value = p.backstory || '';
  $('ce-player-goals').value = p.goals || '';
  $('ce-perspective').value = c.perspective_notes || '';

  paintEntityList('ce-party-list', c.party || [], partyFields(), 'party');
  paintEntityList('ce-npc-list', c.npcs || [], npcFields(), 'npcs');
  paintEntityList('ce-loc-list', c.locations || [], locFields(), 'locations');
  paintEntityList('ce-plot-list', c.plot_threads || [], plotFields(), 'plot_threads');

  $('ce-state-summary').value = c.state?.summary || '';
  $('ce-state-location').value = c.state?.current_location || '';
  $('ce-state-next').value = c.state?.immediate_next_steps || '';

  $('ce-delete').style.display = c.id ? '' : 'none';
}

function partyFields() {
  return [
    { k: 'name', label: 'Name', type: 'text' },
    { k: 'race', label: 'Race', type: 'text' },
    { k: 'char_class', label: 'Class', type: 'text' },
    { k: 'subclass', label: 'Subclass', type: 'text' },
  ];
}
function npcFields() {
  return [
    { k: 'name', label: 'Name', type: 'text' },
    { k: 'relationship', label: 'Relationship', type: 'text' },
    { k: 'description', label: 'Description', type: 'text' },
    { k: 'notes', label: 'Notes', type: 'text' },
  ];
}
function locFields() {
  return [
    { k: 'name', label: 'Name', type: 'text' },
    { k: 'description', label: 'Description', type: 'text' },
    { k: 'significance', label: 'Significance', type: 'text' },
  ];
}
function plotFields() {
  return [
    { k: 'summary', label: 'Summary', type: 'text' },
    { k: 'status', label: 'Status (active/resolved/dormant)', type: 'text' },
  ];
}

function paintEntityList(hostId, items, fields, key) {
  const host = $(hostId);
  host.innerHTML = '';
  items.forEach((item, i) => host.appendChild(renderEntityRow(item, fields, i, key)));
}

function renderEntityRow(item, fields, index, key) {
  const row = document.createElement('div');
  row.className = 'entity-row';
  row.dataset.index = index;
  row.dataset.key = key;
  fields.forEach((f) => {
    const wrap = document.createElement('div');
    wrap.className = 'entity-field';
    const inp = document.createElement('input');
    inp.type = f.type;
    inp.placeholder = f.label;
    inp.value = item[f.k] || '';
    inp.dataset.field = f.k;
    wrap.appendChild(inp);
    row.appendChild(wrap);
  });
  const del = document.createElement('button');
  del.className = 'btn-tiny btn-danger-ghost';
  del.textContent = '✕';
  del.addEventListener('click', () => {
    state.editingCampaign[key].splice(index, 1);
    paintEntityList(`ce-${hostIdSuffix(key)}-list`, state.editingCampaign[key], fieldsForKey(key), key);
  });
  row.appendChild(del);
  return row;
}

function hostIdSuffix(key) {
  return ({ party: 'party', npcs: 'npc', locations: 'loc', plot_threads: 'plot' })[key];
}

function fieldsForKey(key) {
  return ({ party: partyFields(), npcs: npcFields(), locations: locFields(), plot_threads: plotFields() })[key];
}

function collectCampaignFromEditor() {
  const c = state.editingCampaign;
  c.name = $('ce-name').value.trim();
  c.system = $('ce-system').value.trim();
  c.setting = $('ce-setting').value.trim();

  c.player = c.player || {};
  c.player.name = $('ce-player-name').value.trim();
  c.player.role = 'player';
  c.player.pronouns = $('ce-player-pronouns').value.trim();
  c.player.race = $('ce-player-race').value.trim();
  c.player.char_class = $('ce-player-class').value.trim();
  c.player.subclass = $('ce-player-subclass').value.trim();
  c.player.multi_class = $('ce-player-multi').value.trim();
  c.player.description = $('ce-player-description').value.trim();
  c.player.backstory = $('ce-player-backstory').value.trim();
  c.player.goals = $('ce-player-goals').value.trim();
  c.perspective_notes = $('ce-perspective').value.trim();

  ['party', 'npcs', 'locations', 'plot_threads'].forEach((key) => {
    const host = $(`ce-${hostIdSuffix(key)}-list`);
    const rows = $$('.entity-row', host);
    const fields = fieldsForKey(key);
    c[key] = rows.map((row) => {
      const item = {};
      fields.forEach((f) => {
        const inp = row.querySelector(`input[data-field="${f.k}"]`);
        item[f.k] = inp?.value.trim() || '';
      });
      return item;
    }).filter((item) => item.name || item.summary);  // drop empties
  });

  c.state = c.state || {};
  c.state.summary = $('ce-state-summary').value.trim();
  c.state.current_location = $('ce-state-location').value.trim();
  c.state.immediate_next_steps = $('ce-state-next').value.trim();
  c.state.unresolved_hooks = c.state.unresolved_hooks || [];

  return c;
}

async function saveCampaignFromEditor() {
  const c = collectCampaignFromEditor();
  if (!c.name) { toast('Campaign needs a name', 'error'); return; }
  if (!c.player.name) { toast('Your character needs a name', 'error'); return; }
  try {
    const saved = await saveCampaign(c);
    state.editingCampaign = saved;
    toast('Campaign saved');
    // If this was the first campaign, make it active
    await loadCampaigns();
    await loadActiveCampaign();
    if (!state.activeCampaign && state.campaigns.length === 1) {
      await setActiveCampaign(saved.id);
    }
    closeCampaignEditor();
    if (state.view === 'view-home') await showHome();
    paintSettingsCampaignList();
  } catch (e) {
    toast('Save failed: ' + e.message, 'error');
  }
}

async function deleteCampaignFromEditor() {
  const c = state.editingCampaign;
  if (!c || !c.id) return;
  if (!confirm(`Delete "${c.name}"? All roster data will be lost.`)) return;
  try {
    await deleteCampaign(c.id);
    closeCampaignEditor();
    await paintSettingsCampaignList();
    if (state.view === 'view-home') await showHome();
  } catch (e) {
    toast('Delete failed: ' + e.message, 'error');
  }
}

/* ── Onboarding ───────────────────────────────────────────────────────── */

function onboardingOpen() {
  state.onboardingStep = 0;
  showView('view-onboarding');
  paintOnboardingStep();
  loadApiKeyStatus();
}

function paintOnboardingStep() {
  $$('.onboarding-step').forEach(el => el.classList.toggle('hidden', parseInt(el.dataset.step, 10) !== state.onboardingStep));
  const totalSteps = 4;
  $('onboarding-step-dots').innerHTML = Array.from({length: totalSteps}, (_, i) =>
    `<span class="step-dot ${i === state.onboardingStep ? 'active' : ''}"></span>`
  ).join('');
}

function onboardingNext() {
  state.onboardingStep = Math.min(state.onboardingStep + 1, 3);
  paintOnboardingStep();
}

function onboardingBack() {
  state.onboardingStep = Math.max(state.onboardingStep - 1, 0);
  paintOnboardingStep();
}

async function onboardingSaveKeys() {
  const dg = $('onb-deepgram').value.trim();
  const gem = $('onb-gemini').value.trim();
  try {
    await saveApiKeys(dg, gem);
    toast('Keys saved');
    onboardingNext();
  } catch (e) {
    toast('Save failed: ' + e.message, 'error');
  }
}

async function onboardingCreateCampaign() {
  const name = $('onb-campaign-name').value.trim();
  const charName = $('onb-char-name').value.trim();
  if (!name) { toast('Campaign name required', 'error'); return; }
  if (!charName) { toast('Character name required', 'error'); return; }
  const c = blankCampaign();
  c.name = name;
  c.system = $('onb-campaign-system').value.trim() || 'D&D 5e';
  c.player.name = charName;
  c.player.char_class = $('onb-char-class').value.trim();
  c.player.race = $('onb-char-race').value.trim();
  c.player.subclass = $('onb-char-subclass').value.trim();
  c.player.backstory = $('onb-char-backstory').value.trim();
  c.perspective_notes = $('onb-perspective').value.trim();
  try {
    const saved = await saveCampaign(c);
    await setActiveCampaign(saved.id);
    localStorage.setItem('pp_onboarding_complete', '1');
    await showHome();
  } catch (e) {
    toast('Save failed: ' + e.message, 'error');
  }
}

/* ── Event wiring ─────────────────────────────────────────────────────── */

function wireEvents() {
  // Topbar
  $('btn-archives').addEventListener('click', openArchives);
  $('btn-settings').addEventListener('click', () => openSettings('keys'));
  $('settings-close').addEventListener('click', closeSettings);

  // Settings tabs
  $$('.settings-tab').forEach(t => t.addEventListener('click', () => switchSettingsTab(t.dataset.tab)));

  // API keys
  $('set-save-keys').addEventListener('click', async () => {
    const dg = $('set-deepgram').value.trim();
    const gem = $('set-gemini').value.trim();
    try {
      await saveApiKeys(dg, gem);
      $('set-deepgram').value = '';
      $('set-gemini').value = '';
      toast('Keys saved');
      updateRecordPreflight();
    } catch (e) {
      toast('Save failed: ' + e.message, 'error');
    }
  });

  // Campaigns
  $('set-new-campaign').addEventListener('click', () => openCampaignEditor(null));
  $('ce-save').addEventListener('click', saveCampaignFromEditor);
  $('ce-close').addEventListener('click', closeCampaignEditor);
  $('ce-delete').addEventListener('click', deleteCampaignFromEditor);
  $('ce-party-add').addEventListener('click', () => {
    state.editingCampaign.party.push({ name: '', race: '', char_class: '', subclass: '' });
    paintEntityList('ce-party-list', state.editingCampaign.party, partyFields(), 'party');
  });
  $('ce-npc-add').addEventListener('click', () => {
    state.editingCampaign.npcs.push({ name: '', relationship: 'unknown', description: '', notes: '' });
    paintEntityList('ce-npc-list', state.editingCampaign.npcs, npcFields(), 'npcs');
  });
  $('ce-loc-add').addEventListener('click', () => {
    state.editingCampaign.locations.push({ name: '', description: '', significance: '' });
    paintEntityList('ce-loc-list', state.editingCampaign.locations, locFields(), 'locations');
  });
  $('ce-plot-add').addEventListener('click', () => {
    state.editingCampaign.plot_threads.push({ summary: '', status: 'active' });
    paintEntityList('ce-plot-list', state.editingCampaign.plot_threads, plotFields(), 'plot_threads');
  });

  // Audio
  $('set-mic-device').addEventListener('change', saveSettingsMic);
  $('set-open-midi').addEventListener('click', () => api('/system/open-midi-setup'));

  // Obsidian
  $('set-obsidian-browse').addEventListener('click', async () => {
    try {
      const r = await api('/settings/obsidian/browse', { method: 'POST' });
      if (r.ok && r.path) $('set-obsidian-vault').value = r.path;
    } catch {}
  });
  $('set-obsidian-save').addEventListener('click', async () => {
    const body = {
      vault_path: $('set-obsidian-vault').value.trim(),
      subfolder: $('set-obsidian-subfolder').value.trim(),
      auto_export: $('set-obsidian-auto').checked,
    };
    try {
      const r = await api('/settings/obsidian', { method: 'POST', body: JSON.stringify(body) });
      if (r.ok) {
        toast('Obsidian saved');
        loadSettingsObsidian();
      } else {
        toast(r.error || 'Save failed', 'error');
      }
    } catch (e) {
      toast('Save failed: ' + e.message, 'error');
    }
  });
  $('set-obsidian-disconnect').addEventListener('click', async () => {
    try {
      await api('/settings/obsidian/disconnect', { method: 'POST' });
      toast('Disconnected');
      loadSettingsObsidian();
    } catch (e) {
      toast('Failed: ' + e.message, 'error');
    }
  });

  // Appearance
  $('set-theme-dark').addEventListener('change', (e) => {
    const theme = e.target.checked ? 'dark' : 'light';
    document.body.dataset.theme = theme;
    localStorage.setItem('pp_theme', theme);
  });

  // Onboarding
  $$('[data-action="next"]').forEach(b => b.addEventListener('click', onboardingNext));
  $$('[data-action="back"]').forEach(b => b.addEventListener('click', onboardingBack));
  $('onb-save-keys').addEventListener('click', onboardingSaveKeys);
  $('onb-open-midi').addEventListener('click', () => api('/system/open-midi-setup'));
  $('onb-save-campaign').addEventListener('click', onboardingCreateCampaign);

  // Home
  $('home-campaign-picker').addEventListener('change', async (e) => {
    await setActiveCampaign(e.target.value);
    await showHome();
  });
  $('home-edit-campaign').addEventListener('click', () => {
    if (state.activeCampaign) openCampaignEditor(state.activeCampaign.id);
  });
  $('home-brief').addEventListener('blur', saveBrief);
  $('btn-record').addEventListener('click', startSession);

  // Live
  $('btn-stop').addEventListener('click', stopSession);

  // Labeling
  $('btn-finalize').addEventListener('click', () => finalizeSession(false));
  $('btn-finalize-skip').addEventListener('click', () => finalizeSession(true));
  $('show-other').addEventListener('change', applyOtherVisibility);

  // Notes view
  $('btn-notes-home').addEventListener('click', showHome);
  $('btn-export-notes').addEventListener('click', () => window.open('/session/export/notes'));
  $('btn-export-transcript').addEventListener('click', () => window.open('/session/export/transcript'));

  // Archives
  $('archives-close').addEventListener('click', showHome);
  $('archive-detail-back').addEventListener('click', () => {
    $('archives-list').classList.remove('hidden');
    $('archives-detail').classList.add('hidden');
  });
  $$('.archive-tab').forEach(t => t.addEventListener('click', () => {
    $$('.archive-tab').forEach(x => x.classList.toggle('active', x === t));
    const id = $('archive-detail-body').dataset.sessionId;
    if (id) loadArchiveTab(id, t.dataset.tab);
  }));
  $('archive-delete').addEventListener('click', () => {
    const id = $('archive-detail-body').dataset.sessionId;
    if (id) deleteArchive(id);
  });

  // Resume banner
  $('resume-banner-action').addEventListener('click', () => {
    if (state.resumeOffered) resumeSession(state.resumeOffered);
  });
  $('resume-banner-dismiss').addEventListener('click', () => {
    $('resume-banner').classList.add('hidden');
  });
}

/* ── Startup ──────────────────────────────────────────────────────────── */

async function boot() {
  wireEvents();
  loadSettingsTheme();

  await loadApiKeyStatus();
  await loadCampaigns();
  await loadActiveCampaign();

  // Decide where to land
  const needsOnboarding =
    !state.apiKeys.deepgram || !state.apiKeys.gemini || !state.activeCampaign;

  if (needsOnboarding) {
    onboardingOpen();
  } else {
    await showHome();
  }

  await checkResumable();
  startPolling(2000);
}

document.addEventListener('DOMContentLoaded', boot);
