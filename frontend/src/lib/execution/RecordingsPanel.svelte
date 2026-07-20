<!--
  RecordingsPanel — list + play saved sim recordings.

  Embedded inside SimulatorPanel (Lab → Scenario tab). Operator
  workflow:
    • Run a sim with "Record" enabled in the Start payload.
    • On stop, the recording auto-flushes to the sim_recordings table.
    • This panel polls /api/simulator/recordings every ~10 s.
    • Click ▶ to play back at the chosen speed; SimDriver display
      buffers re-emit the events so the screen looks identical to
      the original run.

  Replay controls when active: Pause / Resume / Step / Stop. Cursor
  + progress shown inline. Speed selector (0.5× / 1× / 2× / 5× / 10×).

  Industry analogue: NinjaTrader Market Replay control surface.
-->
<script>
  import { onMount, onDestroy } from 'svelte';
  import Select from '$lib/Select.svelte';
  import ConfirmModal from '$lib/ConfirmModal.svelte';
  import { visibleInterval } from '$lib/stores';
  import {
    fetchSimRecordings, deleteSimRecording,
    fetchSimReplayStatus, startSimReplay, stopSimReplay,
    pauseSimReplay, resumeSimReplay, stepSimReplay,
  } from '$lib/api';

  let recordings = $state(/** @type {any[]} */ ([]));
  let replayStatus = $state(/** @type {any} */ (null));
  let loading = $state(false);
  let error = $state('');

  /** @type {{ ask: (opts: any) => Promise<boolean> } | null} */
  let _confirmRef = $state(null);

  let _selectedRecordingId = $state(/** @type {number|null} */ (null));
  let _speed = $state(1.0);

  /** @type {(() => void) | null} */
  let _pollStop = null;

  async function _loadRecordings() {
    try {
      recordings = await fetchSimRecordings(50);
    } catch (e) {
      error = e?.message || 'failed to load recordings';
    }
  }

  async function _loadStatus() {
    try {
      replayStatus = await fetchSimReplayStatus();
    } catch (_) { /* silent — keep last value */ }
  }

  async function _refresh() {
    loading = true; error = '';
    try {
      await Promise.all([_loadRecordings(), _loadStatus()]);
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    _refresh();
    // 5 s cadence wrapped in visibleInterval so background tabs don't
    // burn requests when the operator switches away from
    // /admin/execution. When a replay is active we always refresh
    // status (cursor advances visibly). When idle we throttle
    // recordings list refresh to every 3rd tick (~15 s) since the
    // list doesn't change without operator action.
    let _tick = 0;
    _pollStop = visibleInterval(() => {
      _tick += 1;
      _loadStatus();
      if (!replayStatus?.active && (_tick % 3 === 0)) _loadRecordings();
    }, 5000);
  });

  onDestroy(() => {
    _pollStop?.();
  });

  async function _play() {
    if (_selectedRecordingId === null) {
      error = 'pick a recording first'; return;
    }
    try {
      replayStatus = await startSimReplay(_selectedRecordingId, _speed);
    } catch (e) {
      error = e?.message || 'replay start failed';
    }
  }

  async function _stop() {
    try { replayStatus = await stopSimReplay(); }
    catch (e) { error = e?.message || 'stop failed'; }
  }

  async function _pauseOrResume() {
    try {
      if (replayStatus?.paused) replayStatus = await resumeSimReplay();
      else                       replayStatus = await pauseSimReplay();
    } catch (e) { error = e?.message || 'pause/resume failed'; }
  }

  async function _step() {
    try { replayStatus = await stepSimReplay(); }
    catch (e) { error = e?.message || 'step failed'; }
  }

  async function _deleteRow(/** @type {any} */ r) {
    if (!_confirmRef) return;
    const ok = await _confirmRef.ask({
      title: `Delete recording #${r.id}?`,
      message: `<b>${r.label}</b> · ${r.event_count} events · ${r.duration_sec?.toFixed(1) ?? '?'}s. This cannot be undone.`,
      confirmLabel: 'Delete',
      cancelLabel: 'Cancel',
      destructive: true,
    });
    if (!ok) return;
    try {
      await deleteSimRecording(r.id);
      await _loadRecordings();
    } catch (e) { error = e?.message || 'delete failed'; }
  }

  function _fmtDuration(s) {
    const v = Number(s) || 0;
    if (v < 60) return `${v.toFixed(1)}s`;
    return `${Math.floor(v/60)}m ${Math.round(v%60)}s`;
  }
  function _fmtDate(iso) {
    if (!iso) return '—';
    try {
      const d = new Date(iso);
      return d.toLocaleString('en-IN', { dateStyle: 'short', timeStyle: 'short' });
    } catch (_) { return iso.slice(0, 19); }
  }

  const replayActive = $derived(!!replayStatus?.active);
  const replayProgress = $derived(replayStatus?.progress ? Math.round(replayStatus.progress * 100) : 0);
</script>

<section class="rec-card">
  <header class="rec-header">
    <span class="rec-title">Recordings</span>
    <span class="rec-count">{recordings.length} saved</span>
    <button type="button" class="rec-refresh" onclick={_refresh}
            disabled={loading}>{loading ? '…' : '↻'}</button>
  </header>

  {#if error}
    <div class="rec-err">⚠ {error}</div>
  {/if}

  <!-- Replay control strip — only visible during playback or with a
       selected row ready to play. -->
  {#if replayActive}
    <div class="rec-controls rec-controls-on">
      <span class="rec-status">
        ▶ Playing <b>{replayStatus.recording_label}</b>
        · {replayStatus.cursor}/{replayStatus.total_events}
        · {replayProgress}%
        · {replayStatus.speed}×
        {#if replayStatus.paused} · <span class="rec-paused">PAUSED</span>{/if}
      </span>
      <div class="rec-control-btns">
        <button type="button" class="rec-btn" onclick={_pauseOrResume}>
          {replayStatus.paused ? 'Resume' : 'Pause'}
        </button>
        <button type="button" class="rec-btn" onclick={_step}
                disabled={!replayStatus.paused}>Step</button>
        <button type="button" class="rec-btn rec-btn-danger" onclick={_stop}>Stop</button>
      </div>
    </div>
  {:else}
    <div class="rec-controls">
      <label class="rec-field">
        <span>Speed</span>
        <Select
          options={[
            { value: '0.5',  label: '0.5×' },
            { value: '1',    label: '1×' },
            { value: '2',    label: '2×' },
            { value: '5',    label: '5×' },
            { value: '10',   label: '10×' },
          ]}
          value={String(_speed)}
          onValueChange={(v) => { _speed = Number(v); }}
        />
      </label>
      <button type="button" class="rec-btn rec-btn-primary"
              disabled={_selectedRecordingId === null}
              onclick={_play}>▶ Play</button>
    </div>
  {/if}

  {#if recordings.length === 0}
    <div class="rec-empty">
      No recordings yet. Tick the <b>Record</b> box when starting a sim;
      a row will land here when the sim stops.
    </div>
  {:else}
    <ol class="rec-list">
      {#each recordings as r (r.id)}
        <li class="rec-row"
            class:rec-row-selected={_selectedRecordingId === r.id}
            class:rec-row-playing={replayActive && replayStatus?.recording_id === r.id}>
          <button type="button" class="rec-row-main"
                  onclick={() => { _selectedRecordingId = r.id; }}>
            <span class="rec-row-label">{r.label}</span>
            <span class="rec-row-meta">
              <span>{r.scenario || '—'}</span>
              <span>·</span>
              <span>{r.event_count} ev</span>
              <span>·</span>
              <span>{_fmtDuration(r.duration_sec)}</span>
              <span>·</span>
              <span>{r.tick_count} ticks</span>
              <span>·</span>
              <span>{_fmtDate(r.started_at)}</span>
            </span>
          </button>
          <button type="button" class="rec-row-del"
                  title="Delete recording"
                  onclick={() => _deleteRow(r)}>✕</button>
        </li>
      {/each}
    </ol>
  {/if}
</section>

<ConfirmModal bind:this={_confirmRef} />

<style>
  .rec-card {
    background: linear-gradient(180deg, rgba(20,30,55,0.55) 0%, rgba(12,18,38,0.55) 100%);
    border: 1px solid rgba(180,200,230,0.12);
    border-radius: 6px;
    padding: 0.7rem 0.85rem;
    margin: 0.7rem 0;
    display: flex;
    flex-direction: column;
    gap: 0.55rem;
  }
  .rec-header {
    display: flex;
    align-items: center;
    gap: 0.55rem;
  }
  .rec-title {
    font-family: var(--font-numeric);
    font-size: var(--fs-lg);
    font-weight: 700;
    color: #c084fc;
    letter-spacing: 0.04em;
  }
  .rec-count {
    font-size: var(--fs-sm);
    color: rgba(180,200,230,0.65);
    font-family: var(--font-numeric);
  }
  .rec-refresh {
    margin-left: auto;
    background: rgba(126,151,184,0.10);
    border: 1px solid rgba(180,200,230,0.25);
    color: rgba(200,216,240,0.85);
    border-radius: 4px;
    padding: 0.18rem 0.5rem;
    font-size: var(--fs-lg);
    cursor: pointer;
  }
  .rec-refresh:hover { background: rgba(255,255,255,0.08); }
  .rec-refresh:disabled { opacity: 0.45; cursor: not-allowed; }

  .rec-controls {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
    padding: 0.4rem 0.55rem;
    background: rgba(192,132,252,0.06);
    border: 1px solid rgba(192,132,252,0.25);
    border-radius: 4px;
  }
  .rec-controls-on {
    background: rgba(74,222,128,0.08);
    border-color: rgba(74,222,128,0.40);
  }
  .rec-status {
    font-family: var(--font-numeric);
    font-size: var(--fs-md);
    color: rgba(200,216,240,0.92);
  }
  .rec-paused {
    color: var(--c-action);
    font-weight: 700;
    letter-spacing: 0.05em;
  }
  .rec-field {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    font-size: var(--fs-md);
    color: rgba(180,200,230,0.85);
    font-family: var(--font-numeric);
  }
  .rec-control-btns {
    display: flex;
    gap: 0.3rem;
    margin-left: auto;
  }
  .rec-btn {
    padding: 0.28rem 0.7rem;
    font-size: var(--fs-md);
    font-weight: 600;
    font-family: var(--font-numeric);
    color: rgba(200,216,240,0.9);
    background: rgba(126,151,184,0.10);
    border: 1px solid rgba(180,200,230,0.25);
    border-radius: 4px;
    cursor: pointer;
  }
  .rec-btn:hover { background: rgba(255,255,255,0.10); }
  .rec-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .rec-btn-primary {
    color: #c084fc;
    background: rgba(192,132,252,0.12);
    border-color: rgba(192,132,252,0.55);
  }
  .rec-btn-primary:hover {
    background: rgba(192,132,252,0.22);
    color: #d8b4fe;
  }
  .rec-btn-danger {
    color: var(--c-short);
    background: var(--c-short-10);
    border-color: rgba(248,113,113,0.45);
  }
  .rec-btn-danger:hover {
    background: rgba(248,113,113,0.20);
  }

  .rec-empty {
    font-size: var(--fs-lg);
    color: rgba(180,200,230,0.6);
    padding: 0.6rem;
    background: rgba(255,255,255,0.02);
    border: 1px dashed rgba(180,200,230,0.15);
    border-radius: 4px;
  }

  .rec-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
    max-height: 22rem;
    overflow-y: auto;
  }
  .rec-row {
    display: flex;
    align-items: stretch;
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(180,200,230,0.12);
    border-radius: 4px;
    transition: border-color 0.1s, background 0.1s;
  }
  .rec-row:hover { background: rgba(255,255,255,0.06); }
  .rec-row-selected {
    border-color: rgba(192,132,252,0.55);
    background: rgba(192,132,252,0.08);
  }
  .rec-row-playing {
    border-color: rgba(74,222,128,0.55);
    background: rgba(74,222,128,0.08);
  }
  .rec-row-main {
    flex: 1;
    background: transparent;
    border: none;
    color: inherit;
    text-align: left;
    padding: 0.5rem 0.7rem;
    cursor: pointer;
    display: flex;
    flex-direction: column;
    gap: 0.18rem;
  }
  .rec-row-label {
    font-size: var(--fs-lg);
    font-weight: 600;
    color: rgba(220,230,245,0.92);
  }
  .rec-row-meta {
    font-size: var(--fs-sm);
    color: rgba(180,200,230,0.65);
    font-family: var(--font-numeric);
    display: flex;
    gap: 0.3rem;
    flex-wrap: wrap;
  }
  .rec-row-del {
    background: transparent;
    border: none;
    border-left: 1px solid rgba(180,200,230,0.12);
    color: rgba(248,113,113,0.65);
    padding: 0 0.7rem;
    cursor: pointer;
    font-size: var(--fs-lg);
  }
  .rec-row-del:hover {
    color: #fca5a5;
    background: var(--c-short-10);
  }

  .rec-err {
    font-size: var(--fs-sm);
    color: #fca5a5;
    padding: 0.3rem 0.5rem;
    background: var(--c-short-10);
    border: 1px solid rgba(248,113,113,0.30);
    border-radius: 3px;
  }
</style>
