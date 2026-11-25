// QuickLynx symbol-rate detector bench: synthetic QO-100 wideband spectra
// on BATC's exact scale (3000 counts/dB, floor pinned at 9000, clip at
// 65535, Hann window), and two versions of quicklynx.html run side by side
// over the same clicks.
//
//   node tools/quicklynx_bench.js OLD.html NEW.html
//
// e.g. compare against the last commit:
//   git show HEAD:quicklynx.html > /tmp/old.html
//   node tools/quicklynx_bench.js /tmp/old.html quicklynx.html
//
// Seeded, so every run gives the same numbers. Set NOWINDOW=1 to drop the
// Hann window - not realistic, but useful to see what the window does.
const fs = require('fs'), vm = require('vm');

function loadDetector(path) {
  const s = fs.readFileSync(path, 'utf8');
  const c0 = s.indexOf('const FFT_SIZE'), c1 = s.indexOf('// IF frequency (Hz) of the original');
  const r0 = s.indexOf('const REF_SYMBOL_RATES'), r1 = s.indexOf('\n', r0);
  const d0 = s.indexOf('let lastDiagnostic'), d1 = s.indexOf('// ── Click to tune');
  const code = s.slice(c0, c1) + '\n' + s.slice(r0, r1) + '\n' + s.slice(d0, d1) +
    '\nthis.est = estimateSymbolRateKsps; this.N = N_BINS; this.BIN = BIN_HZ; this.diag = () => lastDiagnostic;';
  const ctx = { latestData: null, Math, Array, Object, Infinity };
  vm.createContext(ctx); vm.runInContext(code, ctx);
  return ctx;
}

// Seeded RNG so every run is the same.
let seed = 12345;
const rnd = () => (seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648;
const gauss = () => Math.sqrt(-2 * Math.log(rnd() + 1e-12)) * Math.cos(2 * Math.PI * rnd());

// Raised-cosine POWER spectrum (an RRC-filtered signal's PSD), peak 1.
function rc(fHz, srHz, a) {
  const x = Math.abs(fHz) / srHz;
  if (x <= (1 - a) / 2) return 1;
  if (x >= (1 + a) / 2) return 0;
  return 0.5 * (1 + Math.cos(Math.PI / a * (x - (1 - a) / 2)));
}

// stations: [{centreBin, sr (kS/s), a, snr (dB of spectral density over noise)}]
// BATC windows each FFT with a Hann window (main.c, hanning_window_const),
// so every bin sees the true spectrum through the window's power response.
// Sampled at a tenth of a bin over +/-4 bins and normalised.
const D = x => Math.abs(x) < 1e-9 ? 1 : Math.sin(Math.PI * x) / (Math.PI * x);
const HANN = [];
for (let i = -40; i <= 40; i++) {
  const x = i / 10, w = 0.5 * D(x) + 0.25 * (D(x - 1) + D(x + 1));
  HANN.push([x, w * w]);
}
const HANN_SUM = HANN.reduce((t, [, w]) => t + w, 0);
const WINDOWED = process.env.NOWINDOW ? false : true;

function spectrum(N, BIN, stations, rippleDb = 0.3) {
  const db = new Float64Array(N);
  for (let k = 0; k < N; k++) {
    let p = 1;   // noise (flat, so the window leaves it alone)
    for (const st of stations) {
      const pk = Math.pow(10, st.snr / 10);
      if (!WINDOWED) { p += pk * rc((k - st.centreBin) * BIN, st.sr * 1000, st.a); continue; }
      let acc = 0;
      for (const [x, w] of HANN) acc += w * rc((k + x - st.centreBin) * BIN, st.sr * 1000, st.a);
      p += pk * acc / HANN_SUM;
    }
    db[k] = 10 * Math.log10(p) + rippleDb * gauss();
  }
  // The server's AGC: quietest bin pinned to 9000 counts, 3000 per dB, clip at 65535.
  let mn = Infinity; for (const v of db) mn = Math.min(mn, v);
  const out = new Uint16Array(N);
  for (let k = 0; k < N; k++) out[k] = Math.max(0, Math.min(65535, Math.round(9000 + (db[k] - mn) * 3000)));
  return out;
}

const RATES = [1500, 1000, 500, 333, 250, 125, 66, 33];
const ROLLOFFS = [0.20, 0.25, 0.35];

// A random busy transponder: stations packed with realistic guards.
function busyBand(N, BIN, nStations) {
  const st = []; let tries = 0;
  while (st.length < nStations && tries++ < 2000) {
    const sr = RATES[Math.floor(rnd() * RATES.length)];
    const a = ROLLOFFS[Math.floor(rnd() * ROLLOFFS.length)];
    const halfBins = sr * 1000 * (1 + a) / 2 / BIN;
    const c = 60 + rnd() * (N - 120);
    const guardBins = (20 + rnd() * 80) * 1000 / BIN;            // 20-100 kHz between skirts
    if (st.some(o => Math.abs(o.centreBin - c) < halfBins + o.half + guardBins)) continue;
    st.push({ centreBin: c, sr, a, snr: 3 + rnd() * 17, half: halfBins });
  }
  return st;
}

const [oldDet, newDet] = [loadDetector(process.argv[2]), loadDetector(process.argv[3])];
const N = newDet.N, BIN = newDet.BIN;

function run(det, data, clickBin) { det.latestData = data; const r = det.est(clickBin); return r ? r.value : null; }

// ---- Martin's case, rebuilt: a 500 among busy neighbours ----
const martin = [
  { centreBin: 120, sr: 1500, a: 0.35, snr: 22 },   // the beacon, clipped
  { centreBin: 330, sr: 333, a: 0.25, snr: 12 },
  { centreBin: 400, sr: 500, a: 0.25, snr: 10 },    // the one clicked
  { centreBin: 470, sr: 333, a: 0.35, snr: 13 },
  { centreBin: 530, sr: 250, a: 0.20, snr: 9 },
  { centreBin: 640, sr: 1000, a: 0.20, snr: 14 },
  { centreBin: 760, sr: 500, a: 0.35, snr: 11 },
  { centreBin: 850, sr: 125, a: 0.25, snr: 8 },
];
const md = spectrum(N, BIN, martin);
console.log("Martin's busy band, clicking each station:");
for (const st of martin) {
  const c = Math.round(st.centreBin);
  console.log(`  ${String(st.sr).padStart(4)} kS/s a=${st.a} ${String(st.snr).padStart(2)} dB  old -> ${String(run(oldDet, md, c)).padStart(4)}   new -> ${String(run(newDet, md, c)).padStart(4)}`);
}

// ---- Randomised: quiet bands vs busy bands ----
function trial(label, nStations, nBands) {
  const score = { old: 0, new: 0, total: 0 }; const byRate = {};
  for (let b = 0; b < nBands; b++) {
    const st = busyBand(N, BIN, nStations); const data = spectrum(N, BIN, st);
    for (const s of st) {
      const click = Math.round(s.centreBin + (rnd() - 0.5) * 6);
      const o = run(oldDet, data, click), n = run(newDet, data, click);
      score.total++; if (o === s.sr) score.old++; if (n === s.sr) score.new++;
      const k = s.sr; byRate[k] = byRate[k] || { o: 0, n: 0, t: 0 };
      byRate[k].t++; if (o === s.sr) byRate[k].o++; if (n === s.sr) byRate[k].n++;
    }
  }
  const pc = x => (100 * x / score.total).toFixed(0) + '%';
  console.log(`\n${label}: ${score.total} clicks   old ${pc(score.old)} right   new ${pc(score.new)} right`);
  for (const r of RATES) if (byRate[r]) {
    const b = byRate[r];
    console.log(`  ${String(r).padStart(4)} kS/s  old ${String(Math.round(100*b.o/b.t)).padStart(3)}%   new ${String(Math.round(100*b.n/b.t)).padStart(3)}%   (${b.t})`);
  }
}
trial('Quiet band (2 stations)', 2, 150);
// What the new detector actually reads for narrow signals, single and clean.
for (const sr of [33, 66, 125]) {
  const vals = [];
  for (let i = 0; i < 40; i++) {
    const st = [{ centreBin: 300 + rnd(), sr, a: ROLLOFFS[i % 3], snr: 6 + rnd() * 14 }];
    newDet.latestData = spectrum(N, BIN, st);
    newDet.est(300); const d = newDet.diag();
    if (d && d.estimatedKsps) vals.push(d.estimatedKsps);
  }
  vals.sort((a, b) => a - b);
  console.log(`  ${sr} kS/s raw estimates: min ${vals[0].toFixed(1)}  median ${vals[vals.length >> 1].toFixed(1)}  max ${vals[vals.length - 1].toFixed(1)}`);
}
trial('Busy band (up to 8 stations)', 8, 150);
