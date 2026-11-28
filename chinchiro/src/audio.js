// ---- Synthesized sound ----
const SND = (() => {
  let ctx = null, out, wet, nb, on = true, heartTimer = null;
  function init() {
    if (ctx) { if (ctx.state === 'suspended') ctx.resume(); return; }
    try { ctx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) { return; }
    const comp = ctx.createDynamicsCompressor(); comp.threshold.value = -14; comp.ratio.value = 4; comp.connect(ctx.destination);
    out = ctx.createGain(); out.gain.value = .9; out.connect(comp);
    const conv = ctx.createConvolver(); const len = ctx.sampleRate * .9, ir = ctx.createBuffer(2, len, ctx.sampleRate);
    for (let c = 0; c < 2; c++) { const d = ir.getChannelData(c); for (let i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, 3.2); }
    conv.buffer = ir; wet = ctx.createGain(); wet.gain.value = .22; wet.connect(conv); conv.connect(out);
    nb = ctx.createBuffer(1, ctx.sampleRate, ctx.sampleRate); const d = nb.getChannelData(0); for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
  }
  const ready = () => ctx && on && ctx.state === 'running';
  function bus(pan) {
    const g = ctx.createGain(); let node = g;
    if (ctx.createStereoPanner) { const p = ctx.createStereoPanner(); p.pan.value = Math.max(-1, Math.min(1, pan || 0)); g.connect(p); node = p; }
    node.connect(out); node.connect(wet); return g;
  }
  function tone(dst, f, a, dec, t, type = 'sine', fEnd) {
    const o = ctx.createOscillator(), g = ctx.createGain(); o.type = type; o.frequency.setValueAtTime(f, t);
    if (fEnd) o.frequency.exponentialRampToValueAtTime(fEnd, t + dec);
    g.gain.setValueAtTime(0, t); g.gain.linearRampToValueAtTime(a, t + .002); g.gain.exponentialRampToValueAtTime(1e-4, t + dec);
    o.connect(g); g.connect(dst); o.start(t); o.stop(t + dec + .05);
  }
  function noise(dst, a, dec, t, type, f, q = 1) {
    const s = ctx.createBufferSource(); s.buffer = nb; s.playbackRate.value = .8 + Math.random() * .4;
    const fl = ctx.createBiquadFilter(); fl.type = type; fl.frequency.value = f; fl.Q.value = q;
    const g = ctx.createGain(); g.gain.setValueAtTime(a, t); g.gain.exponentialRampToValueAtTime(1e-4, t + dec);
    s.connect(fl); fl.connect(g); g.connect(dst); s.start(t, Math.random() * .5); s.stop(t + dec + .02);
  }
  function hit(kind, v, x) {
    if (!ready()) return;
    const t = ctx.currentTime + .005, a = Math.min(1, Math.pow(v / 40, 1.1)), b = bus(x / 6);
    if (kind === 'bowl') {
      const f0 = 2300 + Math.random() * 900;
      noise(b, a * .5, .012, t, 'highpass', 3500);
      [[1, .22, .3], [1.52, .14, .2], [2.17, .09, .12], [2.93, .06, .08]].forEach(([r, d, g]) => tone(b, f0 * r, a * g, d * (.7 + a), t));
      tone(b, 780 + Math.random() * 40, a * .07, .7, t); tone(b, 1310, a * .04, .5, t);
    } else if (kind === 'die') {
      noise(b, a * .55, .018, t, 'bandpass', 2800, 1.2); tone(b, 3400 + Math.random() * 600, a * .12, .03, t);
    } else {
      noise(b, a * .7, .06, t, 'lowpass', 700); tone(b, 160, a * .25, .08, t, 'sine', 90);
    }
  }
  function rattle() { if (!ready()) return; const b = bus(0), t = ctx.currentTime; for (let i = 0; i < 2; i++) { noise(b, .12 + Math.random() * .1, .015, t + i * .03 + Math.random() * .02, 'bandpass', 2600, 1.5); } }
  function chip(n = 3) { if (!ready()) return; const b = bus(0), t = ctx.currentTime; for (let i = 0; i < n; i++) { noise(b, .25, .025, t + i * .045, 'bandpass', 4200, 2); tone(b, 5200 + i * 200, .04, .04, t + i * .045); } }
  function click() { if (!ready()) return; const b = bus(0); noise(b, .15, .02, ctx.currentTime, 'bandpass', 1800, 2); }
  function drum(big) { if (!ready()) return; const b = bus(0), t = ctx.currentTime; tone(b, 120, big ? .9 : .5, .5, t, 'sine', 48); noise(b, big ? .5 : .25, .12, t, 'lowpass', 400); if (big) { tone(b, 196, .12, 1.6, t + .02, 'triangle'); tone(b, 293, .08, 1.4, t + .02, 'triangle'); } }
  function bad() { if (!ready()) return; const b = bus(0), t = ctx.currentTime; tone(b, 220, .25, .5, t, 'triangle', 110); tone(b, 233, .15, .6, t + .05, 'triangle', 116); }
  function win() { if (!ready()) return; const b = bus(0), t = ctx.currentTime; [523, 659, 784, 1046].forEach((f, i) => tone(b, f, .14, .5, t + i * .09, 'triangle')); }
  function beat() { if (!ready()) return; const b = bus(0), t = ctx.currentTime; tone(b, 62, .8, .16, t, 'sine', 40); tone(b, 55, .55, .16, t + .2, 'sine', 36); }
  function heart(bpm) {
    clearInterval(heartTimer); heartTimer = null;
    if (bpm) { beat(); heartTimer = setInterval(beat, 60000 / bpm); }
  }
  return { init, hit, rattle, chip, click, drum, bad, win, heart, beat, set on(v) { on = v; if (!v) heart(0); }, get on() { return on; } };
})();
