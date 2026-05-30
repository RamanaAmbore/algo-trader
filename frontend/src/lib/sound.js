/**
 * Sound helpers for in-app notifications.
 *
 * Web Audio API — no audio files, no bundle bloat. A tier-aware
 * chirp pattern signals the urgency level of the popup that just
 * appeared. Browser autoplay restrictions block sound until the
 * user has interacted with the page; this is silent failure by
 * design — the user is staring at the popup anyway.
 *
 * Mute preference persists per browser under
 *   ramboq.sound.muted
 * Toggle via the speaker icon in the AgentNotifications bell panel
 * (or programmatically: soundMuted.set(true / false)).
 */
import { browser } from '$app/environment';
import { writable, get } from 'svelte/store';

const MUTED_KEY = 'ramboq.sound.muted';

function _initialMuted() {
  if (!browser) return false;
  try { return localStorage.getItem(MUTED_KEY) === '1'; }
  catch { return false; }
}

/** Bindable in any component: `<button onclick={() => soundMuted.update(v => !v)}>`. */
export const soundMuted = writable(_initialMuted());
if (browser) {
  soundMuted.subscribe((v) => {
    try { localStorage.setItem(MUTED_KEY, v ? '1' : '0'); } catch {}
  });
}

/** Lazy AudioContext — created on first beep so Safari doesn't bark
 *  on cold load. Re-used across every subsequent beep. */
let _ctx = null;
function _ensureCtx() {
  if (!browser) return null;
  if (_ctx) return _ctx;
  try {
    const Ctor = window.AudioContext || /** @type {any} */ (window).webkitAudioContext;
    if (!Ctor) return null;
    _ctx = new Ctor();
  } catch { _ctx = null; }
  return _ctx;
}

// Tier → note sequence. Critical lands on a descending two-note
// urgent pattern (alarm-clock cadence), info chirps once high.
// Pitches are in the 660 – 1320 Hz band — comfortably audible on
// laptop speakers without being shrill.
const TIER_TONES = {
  critical: [{ f: 880, d: 0.10 }, { f: 660, d: 0.18 }],
  high:     [{ f: 880, d: 0.10 }, { f: 1100, d: 0.10 }],
  medium:   [{ f: 1100, d: 0.10 }],
  info:     [{ f: 1320, d: 0.08 }],
};

/** Play the tier-mapped chirp. Mute + autoplay block both silently
 *  no-op. Safe to call from a hot loop — overlapping beeps mix. */
export function playAgentBeep(/** @type {string|undefined} */ tier) {
  if (!browser) return;
  if (get(soundMuted)) return;
  const ctx = _ensureCtx();
  if (!ctx) return;
  try {
    // resume() — autoplay policy parks fresh contexts in 'suspended'
    // until the first user gesture. After the first click anywhere
    // on the page the context flips to 'running' and stays there.
    if (ctx.state === 'suspended') ctx.resume();
    const notes = TIER_TONES[/** @type {keyof typeof TIER_TONES} */ (tier || 'info')] || TIER_TONES.info;
    let t = ctx.currentTime;
    for (const n of notes) {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.value = n.f;
      osc.connect(gain);
      gain.connect(ctx.destination);
      // Short attack + decay envelope so each note reads as a chirp,
      // not a sustained tone. Peak gain 0.18 keeps it audible without
      // startling on max system volume.
      gain.gain.setValueAtTime(0, t);
      gain.gain.linearRampToValueAtTime(0.18, t + 0.02);
      gain.gain.linearRampToValueAtTime(0.0001, t + n.d);
      osc.start(t);
      osc.stop(t + n.d + 0.02);
      t += n.d + 0.02;
    }
  } catch { /* AudioContext can throw on memory pressure — swallow */ }
}
