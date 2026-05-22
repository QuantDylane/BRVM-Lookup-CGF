/* ============================================================
 * Indicateurs Techniques — onglet "Indicateurs Techniques"
 * Rendu Plotly.js client-side, calculs en pur JS.
 * Structure : 4 familles (Momentum, Tendance, Volatilité, Volume)
 *           x sous-onglets x gabarit (Paramètres + Graphique + Métriques)
 * Données injectées via window.IND_TECH_DATA = {dates, opens, highs, lows, closes, volumes}
 * ============================================================ */
(function () {
  'use strict';

  // ---------- Palette adaptée au thème dark (sémantique du brief) ----------
  const C = {
    text: '#e2e8f0',
    muted: '#94a3b8',
    grid: 'rgba(148,163,184,0.15)',
    bull: '#10B981', bullLite: '#34D399',
    bear: '#EF4444', bearLite: '#F87171',
    blue: '#3B82F6', blueDk: '#1D4ED8',
    orange: '#F59E0B',
    purple: '#A855F7',
    cloud: 'rgba(139,115,85,0.15)',
    bbFill: 'rgba(59,130,246,0.08)',
    volBar: 'rgba(148,163,184,0.4)',
  };

  function baseLayout(rows = 1) {
    return {
      autosize: true,
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(0,0,0,0)',
      font: { color: C.text, size: 11 },
      hovermode: 'x unified',
      margin: { l: 50, r: 30, t: 30, b: 30 },
      legend: { orientation: 'h', yanchor: 'bottom', y: 1.02, xanchor: 'right', x: 1, font: { size: 10 } },
      xaxis: { gridcolor: C.grid, zerolinecolor: C.grid },
      yaxis: { gridcolor: C.grid, zerolinecolor: C.grid },
      ...(rows >= 2 ? { xaxis2: { gridcolor: C.grid }, yaxis2: { gridcolor: C.grid } } : {}),
      ...(rows >= 3 ? { xaxis3: { gridcolor: C.grid }, yaxis3: { gridcolor: C.grid } } : {}),
    };
  }

  const PLOTLY_CFG = { displayModeBar: false, responsive: true };

  // ---------- Helpers numériques ----------
  const isN = v => typeof v === 'number' && isFinite(v);
  const round = (v, d = 2) => isN(v) ? Math.round(v * Math.pow(10, d)) / Math.pow(10, d) : null;
  const last = arr => { for (let i = arr.length - 1; i >= 0; i--) if (isN(arr[i])) return arr[i]; return null; };
  const fmt = (v, d = 0) => v == null ? '—' : new Intl.NumberFormat('fr-FR', { minimumFractionDigits: d, maximumFractionDigits: d }).format(v);

  function sma(data, period) {
    const out = new Array(data.length).fill(null);
    if (period <= 0) return out;
    let sum = 0, count = 0;
    for (let i = 0; i < data.length; i++) {
      if (isN(data[i])) { sum += data[i]; count++; }
      if (i >= period && isN(data[i - period])) { sum -= data[i - period]; count--; }
      if (i >= period - 1 && count === period) out[i] = sum / period;
    }
    return out;
  }
  function ema(data, period) {
    const out = new Array(data.length).fill(null);
    if (period <= 0 || data.length < period) return out;
    const k = 2 / (period + 1);
    let s = 0, c = 0;
    for (let i = 0; i < period; i++) if (isN(data[i])) { s += data[i]; c++; }
    if (c === 0) return out;
    let prev = s / c;
    out[period - 1] = prev;
    for (let i = period; i < data.length; i++) {
      if (!isN(data[i])) { out[i] = prev; continue; }
      prev = data[i] * k + prev * (1 - k);
      out[i] = prev;
    }
    return out;
  }
  function wma(data, period) {
    const out = new Array(data.length).fill(null);
    const wsum = period * (period + 1) / 2;
    for (let i = period - 1; i < data.length; i++) {
      let s = 0, ok = true;
      for (let j = 0; j < period; j++) {
        const v = data[i - period + 1 + j];
        if (!isN(v)) { ok = false; break; }
        s += v * (j + 1);
      }
      if (ok) out[i] = s / wsum;
    }
    return out;
  }
  function dema(data, period) {
    const e1 = ema(data, period);
    const e2 = ema(e1.map(v => v == null ? NaN : v), period);
    return e1.map((v, i) => (v != null && isN(e2[i])) ? 2 * v - e2[i] : null);
  }
  function tema(data, period) {
    const e1 = ema(data, period);
    const e2 = ema(e1.map(v => v == null ? NaN : v), period);
    const e3 = ema(e2.map(v => v == null ? NaN : v), period);
    return e1.map((v, i) => (v != null && isN(e2[i]) && isN(e3[i])) ? 3 * v - 3 * e2[i] + e3[i] : null);
  }

  function rsi(data, period) {
    const out = new Array(data.length).fill(null);
    if (data.length <= period) return out;
    let g = 0, l = 0;
    for (let i = 1; i <= period; i++) {
      const d = data[i] - data[i - 1];
      if (d > 0) g += d; else l -= d;
    }
    g /= period; l /= period;
    out[period] = l === 0 ? 100 : 100 - 100 / (1 + g / l);
    for (let i = period + 1; i < data.length; i++) {
      const d = data[i] - data[i - 1];
      const cg = d > 0 ? d : 0, cl = d < 0 ? -d : 0;
      g = (g * (period - 1) + cg) / period;
      l = (l * (period - 1) + cl) / period;
      out[i] = l === 0 ? 100 : 100 - 100 / (1 + g / l);
    }
    return out;
  }
  function macd(data, fast = 12, slow = 26, sig = 9) {
    const ef = ema(data, fast), es = ema(data, slow);
    const m = ef.map((v, i) => (v != null && es[i] != null) ? v - es[i] : null);
    const s = ema(m.map(v => v == null ? NaN : v), sig);
    const h = m.map((v, i) => (v != null && s[i] != null) ? v - s[i] : null);
    return { macd: m, signal: s, hist: h };
  }
  function ppo(data, fast = 12, slow = 26, sig = 9) {
    const ef = ema(data, fast), es = ema(data, slow);
    const p = ef.map((v, i) => (v != null && es[i] != null && es[i] !== 0) ? 100 * (v - es[i]) / es[i] : null);
    const s = ema(p.map(v => v == null ? NaN : v), sig);
    const h = p.map((v, i) => (v != null && s[i] != null) ? v - s[i] : null);
    return { macd: p, signal: s, hist: h };
  }
  function stochastic(highs, lows, closes, period = 14, smoothK = 3, smoothD = 3) {
    const k0 = new Array(closes.length).fill(null);
    for (let i = period - 1; i < closes.length; i++) {
      let h = -Infinity, l = Infinity;
      for (let j = i - period + 1; j <= i; j++) {
        if (isN(highs[j]) && highs[j] > h) h = highs[j];
        if (isN(lows[j]) && lows[j] < l) l = lows[j];
      }
      k0[i] = (h - l === 0) ? 50 : 100 * (closes[i] - l) / (h - l);
    }
    const k = sma(k0, smoothK);
    const d = sma(k, smoothD);
    return { k, d };
  }
  function stochRsi(closes, rsiPeriod = 14, kPeriod = 14, smoothK = 3, smoothD = 3) {
    const r = rsi(closes, rsiPeriod);
    const k0 = new Array(r.length).fill(null);
    for (let i = kPeriod - 1; i < r.length; i++) {
      let mx = -Infinity, mn = Infinity, ok = true;
      for (let j = i - kPeriod + 1; j <= i; j++) {
        if (!isN(r[j])) { ok = false; break; }
        if (r[j] > mx) mx = r[j]; if (r[j] < mn) mn = r[j];
      }
      if (ok) k0[i] = (mx - mn === 0) ? 50 : 100 * (r[i] - mn) / (mx - mn);
    }
    const k = sma(k0, smoothK);
    const d = sma(k, smoothD);
    return { k, d };
  }
  function cci(highs, lows, closes, period = 20) {
    const tp = closes.map((c, i) => (highs[i] + lows[i] + c) / 3);
    const sm = sma(tp, period);
    const out = new Array(closes.length).fill(null);
    for (let i = period - 1; i < closes.length; i++) {
      let mad = 0;
      for (let j = i - period + 1; j <= i; j++) mad += Math.abs(tp[j] - sm[i]);
      mad /= period;
      out[i] = mad === 0 ? 0 : (tp[i] - sm[i]) / (0.015 * mad);
    }
    return out;
  }
  function williamsR(highs, lows, closes, period = 14) {
    const out = new Array(closes.length).fill(null);
    for (let i = period - 1; i < closes.length; i++) {
      let h = -Infinity, l = Infinity;
      for (let j = i - period + 1; j <= i; j++) {
        if (isN(highs[j]) && highs[j] > h) h = highs[j];
        if (isN(lows[j]) && lows[j] < l) l = lows[j];
      }
      out[i] = (h - l === 0) ? -50 : -100 * (h - closes[i]) / (h - l);
    }
    return out;
  }
  function roc(data, period = 10) {
    const out = new Array(data.length).fill(null);
    for (let i = period; i < data.length; i++) {
      if (data[i - period]) out[i] = (data[i] - data[i - period]) / data[i - period] * 100;
    }
    return out;
  }
  function momentum(data, period = 10) {
    const out = new Array(data.length).fill(null);
    for (let i = period; i < data.length; i++) out[i] = data[i] - data[i - period];
    return out;
  }
  function trix(data, period = 15) {
    const e1 = ema(data, period);
    const e2 = ema(e1.map(v => v == null ? NaN : v), period);
    const e3 = ema(e2.map(v => v == null ? NaN : v), period);
    const out = new Array(data.length).fill(null);
    for (let i = 1; i < e3.length; i++) {
      if (isN(e3[i]) && isN(e3[i - 1]) && e3[i - 1] !== 0) out[i] = (e3[i] - e3[i - 1]) / e3[i - 1] * 100;
    }
    return out;
  }
  function adxDmi(highs, lows, closes, period = 14) {
    const n = closes.length;
    const plusDM = new Array(n).fill(0), minusDM = new Array(n).fill(0), tr = new Array(n).fill(0);
    for (let i = 1; i < n; i++) {
      const up = highs[i] - highs[i - 1], dn = lows[i - 1] - lows[i];
      plusDM[i] = (up > dn && up > 0) ? up : 0;
      minusDM[i] = (dn > up && dn > 0) ? dn : 0;
      tr[i] = Math.max(highs[i] - lows[i], Math.abs(highs[i] - closes[i - 1]), Math.abs(lows[i] - closes[i - 1]));
    }
    const wilder = (arr) => {
      const out = new Array(n).fill(null);
      let s = 0;
      for (let i = 1; i <= period; i++) s += arr[i];
      out[period] = s;
      for (let i = period + 1; i < n; i++) out[i] = out[i - 1] - out[i - 1] / period + arr[i];
      return out;
    };
    const aTr = wilder(tr), pDM = wilder(plusDM), mDM = wilder(minusDM);
    const pDi = new Array(n).fill(null), mDi = new Array(n).fill(null), dx = new Array(n).fill(null);
    for (let i = period; i < n; i++) {
      if (aTr[i]) {
        pDi[i] = 100 * pDM[i] / aTr[i];
        mDi[i] = 100 * mDM[i] / aTr[i];
        const sum = pDi[i] + mDi[i];
        dx[i] = sum === 0 ? 0 : 100 * Math.abs(pDi[i] - mDi[i]) / sum;
      }
    }
    const adx = new Array(n).fill(null);
    const start = period * 2;
    if (start < n) {
      let s = 0;
      for (let i = period + 1; i <= start; i++) s += dx[i];
      adx[start] = s / period;
      for (let i = start + 1; i < n; i++) adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period;
    }
    return { adx, plusDi: pDi, minusDi: mDi };
  }
  function bbands(data, period = 20, k = 2) {
    const mid = sma(data, period);
    const up = new Array(data.length).fill(null), lo = new Array(data.length).fill(null), bw = new Array(data.length).fill(null), pb = new Array(data.length).fill(null);
    for (let i = period - 1; i < data.length; i++) {
      let s = 0;
      for (let j = i - period + 1; j <= i; j++) s += Math.pow(data[j] - mid[i], 2);
      const std = Math.sqrt(s / period);
      up[i] = mid[i] + k * std;
      lo[i] = mid[i] - k * std;
      bw[i] = mid[i] === 0 ? 0 : (up[i] - lo[i]) / mid[i] * 100;
      pb[i] = (up[i] - lo[i]) === 0 ? 0.5 : (data[i] - lo[i]) / (up[i] - lo[i]);
    }
    return { mid, up, lo, bw, pb };
  }
  function atr(highs, lows, closes, period = 14) {
    const n = closes.length;
    const tr = new Array(n).fill(null);
    tr[0] = highs[0] - lows[0];
    for (let i = 1; i < n; i++) tr[i] = Math.max(highs[i] - lows[i], Math.abs(highs[i] - closes[i - 1]), Math.abs(lows[i] - closes[i - 1]));
    const out = new Array(n).fill(null);
    let s = 0;
    for (let i = 0; i < period; i++) s += tr[i];
    out[period - 1] = s / period;
    for (let i = period; i < n; i++) out[i] = (out[i - 1] * (period - 1) + tr[i]) / period;
    return out;
  }
  function obv(closes, volumes) {
    const out = new Array(closes.length).fill(0);
    for (let i = 1; i < closes.length; i++) {
      if (closes[i] > closes[i - 1]) out[i] = out[i - 1] + (volumes[i] || 0);
      else if (closes[i] < closes[i - 1]) out[i] = out[i - 1] - (volumes[i] || 0);
      else out[i] = out[i - 1];
    }
    return out;
  }
  function mfi(highs, lows, closes, volumes, period = 14) {
    const n = closes.length;
    const tp = closes.map((c, i) => (highs[i] + lows[i] + c) / 3);
    const rmf = tp.map((t, i) => t * (volumes[i] || 0));
    const pos = new Array(n).fill(0), neg = new Array(n).fill(0);
    for (let i = 1; i < n; i++) {
      if (tp[i] > tp[i - 1]) pos[i] = rmf[i];
      else if (tp[i] < tp[i - 1]) neg[i] = rmf[i];
    }
    const out = new Array(n).fill(null);
    for (let i = period; i < n; i++) {
      let p = 0, ng = 0;
      for (let j = i - period + 1; j <= i; j++) { p += pos[j]; ng += neg[j]; }
      out[i] = ng === 0 ? 100 : 100 - 100 / (1 + p / ng);
    }
    return out;
  }
  function ad(highs, lows, closes, volumes) {
    const out = new Array(closes.length).fill(0);
    for (let i = 0; i < closes.length; i++) {
      const denom = highs[i] - lows[i];
      const mfm = denom === 0 ? 0 : ((closes[i] - lows[i]) - (highs[i] - closes[i])) / denom;
      const mfv = mfm * (volumes[i] || 0);
      out[i] = (i > 0 ? out[i - 1] : 0) + mfv;
    }
    return out;
  }
  function parabolicSar(highs, lows, afStart = 0.02, afStep = 0.02, afMax = 0.2) {
    const n = highs.length;
    const sar = new Array(n).fill(null);
    if (n < 2) return sar;
    let bull = highs[1] > highs[0];
    let af = afStart;
    let ep = bull ? highs[0] : lows[0];
    sar[0] = bull ? lows[0] : highs[0];
    for (let i = 1; i < n; i++) {
      sar[i] = sar[i - 1] + af * (ep - sar[i - 1]);
      if (bull) {
        sar[i] = Math.min(sar[i], lows[i - 1], i >= 2 ? lows[i - 2] : lows[i - 1]);
        if (lows[i] < sar[i]) {
          bull = false; sar[i] = ep; ep = lows[i]; af = afStart;
        } else if (highs[i] > ep) { ep = highs[i]; af = Math.min(af + afStep, afMax); }
      } else {
        sar[i] = Math.max(sar[i], highs[i - 1], i >= 2 ? highs[i - 2] : highs[i - 1]);
        if (highs[i] > sar[i]) {
          bull = true; sar[i] = ep; ep = highs[i]; af = afStart;
        } else if (lows[i] < ep) { ep = lows[i]; af = Math.min(af + afStep, afMax); }
      }
    }
    return sar;
  }
  function ichimoku(highs, lows, closes, tenkanP = 9, kijunP = 26, senkouBP = 52) {
    const n = closes.length;
    const high = (p, i) => { let m = -Infinity; for (let j = Math.max(0, i - p + 1); j <= i; j++) if (highs[j] > m) m = highs[j]; return m; };
    const low = (p, i) => { let m = Infinity; for (let j = Math.max(0, i - p + 1); j <= i; j++) if (lows[j] < m) m = lows[j]; return m; };
    const tenkan = new Array(n).fill(null), kijun = new Array(n).fill(null);
    for (let i = 0; i < n; i++) {
      if (i >= tenkanP - 1) tenkan[i] = (high(tenkanP, i) + low(tenkanP, i)) / 2;
      if (i >= kijunP - 1) kijun[i] = (high(kijunP, i) + low(kijunP, i)) / 2;
    }
    // Senkou A/B shifted forward by kijunP
    const senkouA = new Array(n).fill(null), senkouB = new Array(n).fill(null);
    for (let i = 0; i < n; i++) {
      const target = i + kijunP;
      if (target < n) {
        if (tenkan[i] != null && kijun[i] != null) senkouA[target] = (tenkan[i] + kijun[i]) / 2;
        if (i >= senkouBP - 1) senkouB[target] = (high(senkouBP, i) + low(senkouBP, i)) / 2;
      }
    }
    // Chikou: close shifted backward by kijunP
    const chikou = new Array(n).fill(null);
    for (let i = 0; i < n; i++) if (i - kijunP >= 0) chikou[i - kijunP] = closes[i];
    return { tenkan, kijun, senkouA, senkouB, chikou };
  }

  // ---------- Données globales (injectées par le template) ----------
  const D = window.IND_TECH_DATA || {};
  if (!D.dates || !D.dates.length) return;
  const N_ALL = D.dates.length;
  const DATES = D.dates.map(s => new Date(s));
  const O = D.opens || D.closes;
  const H = D.highs || D.closes;
  const L = D.lows || D.closes;
  const CLO = D.closes;
  const V = D.volumes || new Array(N_ALL).fill(0);

  // ---------- Filtre période ----------
  let PERIOD = '1y';
  function periodRange() {
    const end = DATES[DATES.length - 1];
    let start;
    const d = new Date(end);
    switch (PERIOD) {
      case '1m': start = new Date(d.getTime() - 30 * 86400000); break;
      case '3m': start = new Date(d.getTime() - 90 * 86400000); break;
      case '6m': start = new Date(d.getTime() - 180 * 86400000); break;
      case 'ytd': start = new Date(end.getFullYear(), 0, 1); break;
      case '1y': start = new Date(d.getTime() - 365 * 86400000); break;
      case '2y': start = new Date(d.getTime() - 2 * 365 * 86400000); break;
      case '5y': start = new Date(d.getTime() - 5 * 365 * 86400000); break;
      case 'max':
      default: start = DATES[0];
    }
    return [start, end];
  }
  function applyPeriod(layout, traces) {
    const [s, e] = periodRange();
    layout.xaxis = { ...(layout.xaxis || {}), range: [s, e], gridcolor: C.grid };
    if (layout.xaxis2) layout.xaxis2 = { ...layout.xaxis2, range: [s, e], gridcolor: C.grid };
    if (layout.xaxis3) layout.xaxis3 = { ...layout.xaxis3, range: [s, e], gridcolor: C.grid };

    // Calcul des bornes y par axe à partir des seules valeurs visibles dans [s, e].
    // Les axes ayant déjà un `range` explicite dans le layout (ex. 0-100 pour RSI)
    // ne sont pas écrasés.
    if (Array.isArray(traces) && traces.length) {
      const ts = s.getTime(), te = e.getTime();
      // traceY -> nom d'axe layout (y -> yaxis, y2 -> yaxis2 …)
      const axes = {};
      traces.forEach(tr => {
        const axisKey = tr.yaxis ? 'yaxis' + tr.yaxis.slice(1) : 'yaxis';
        if (!layout[axisKey]) return;
        if (layout[axisKey].range && !layout[axisKey]._autoFromVisible) return; // range explicite : conserver
        const xs = tr.x || [];
        const ys = tr.y || [];
        if (!axes[axisKey]) axes[axisKey] = { min: Infinity, max: -Infinity };
        const A = axes[axisKey];
        for (let i = 0; i < xs.length; i++) {
          const v = ys[i];
          if (v == null || !isFinite(v)) continue;
          const xv = xs[i] instanceof Date ? xs[i].getTime() : new Date(xs[i]).getTime();
          if (xv < ts || xv > te) continue;
          if (v < A.min) A.min = v;
          if (v > A.max) A.max = v;
        }
      });
      Object.keys(axes).forEach(k => {
        const a = axes[k];
        if (!isFinite(a.min) || !isFinite(a.max)) {
          layout[k].autorange = true;
          return;
        }
        const isBar = traces.some(t => t.type === 'bar' && (('yaxis' + (t.yaxis || 'y').slice(1)) === k));
        let lo = a.min, hi = a.max;
        if (lo === hi) { lo -= 1; hi += 1; }
        const span = hi - lo;
        const pad = Math.max(span * 0.06, span === 0 ? 1 : 0);
        if (isBar && lo >= 0) lo = 0; else lo = lo - pad;
        hi = hi + pad;
        layout[k] = { ...layout[k], range: [lo, hi], autorange: false };
      });
    } else {
      if (layout.yaxis) layout.yaxis.autorange = true;
      if (layout.yaxis2 && !layout.yaxis2.range) layout.yaxis2.autorange = true;
      if (layout.yaxis3 && !layout.yaxis3.range) layout.yaxis3.autorange = true;
    }
    return layout;
  }

  // ---------- Helpers DOM ----------
  function el(id) { return document.getElementById(id); }
  function setHTML(id, html) { const e = el(id); if (e) e.innerHTML = html; }
  function setText(id, t) { const e = el(id); if (e) e.textContent = t; }

  // ---------- Box d'interprétation ----------
  function box(html) { return `<div class="indicator-box">${html}</div>`; }

  // ---------- Rendu d'une carte signal ----------
  function signalCard(title, value, status, color) {
    return `<div class="signal-card" style="border-left-color:${color};">
      <div class="signal-card-title">${title}</div>
      <div class="signal-card-value">${value}</div>
      <div class="signal-card-status" style="color:${color};">${status}</div>
    </div>`;
  }

  // ---------- Verdict & jauge (4 axes / 5 modalites Sikafinance) ----------
  function verdictMapping(score) {
    if (score == null || isNaN(score)) return { label: 'Indisponible', emoji: '\u2014', color: '#6B7280' };
    if (score >= 0.50) return { label: 'Acheter', emoji: '\ud83d\udfe2', color: '#10B981' };
    if (score >= 0.15) return { label: 'Renforcer', emoji: '\ud83d\udfe2', color: '#34D399' };
    if (score > -0.15) return { label: 'Conserver', emoji: '\u26aa', color: '#9CA3AF' };
    if (score > -0.50) return { label: 'Alleger', emoji: '\ud83d\udfe0', color: '#F59E0B' };
    return { label: 'Vendre', emoji: '\ud83d\udd34', color: '#EF4444' };
  }
  function _clipV(x, lo, hi) { lo = (lo == null ? -1 : lo); hi = (hi == null ? 1 : hi); return Math.max(lo, Math.min(hi, x)); }
  function _aggV(signals) {
    const v = signals.filter(s => s != null && !isNaN(s));
    if (!v.length) return { score: null, n: 0 };
    return { score: v.reduce((a, b) => a + b, 0) / v.length, n: v.length };
  }
  function _axisColor(s) {
    if (s == null) return '#6B7280';
    if (s >= 0.50) return '#10B981';
    if (s >= 0.15) return '#34D399';
    if (s > -0.15) return '#9CA3AF';
    if (s > -0.50) return '#F59E0B';
    return '#EF4444';
  }
  function _axisStatus(s) {
    if (s == null) return 'N/A';
    if (s >= 0.50) return 'Acheter';
    if (s >= 0.15) return 'Renforcer';
    if (s > -0.15) return 'Conserver';
    if (s > -0.50) return 'Alleger';
    return 'Vendre';
  }
  function _sigSmaCross(s20, s50) {
    if (s20 == null || s50 == null || s50 === 0) return null;
    return _clipV((s20 / s50 - 1) / 0.05);
  }
  function _sigAdxDi(a, dp, dm) {
    if (a == null || dp == null || dm == null) return null;
    const dir = dp > dm ? 1 : (dp < dm ? -1 : 0);
    return dir * _clipV((a - 20) / 30, 0, 1);
  }
  function _sigRsi(r) { return r == null ? null : _clipV((50 - r) / 20); }
  function _sigMacdHist(h, atrV, lc) {
    if (h == null) return null;
    let n;
    if (atrV != null && atrV > 0) n = Math.abs(h) / (0.5 * atrV);
    else if (lc != null && lc !== 0) n = Math.abs(h) / (0.005 * lc);
    else return null;
    return (h > 0 ? 1 : -1) * _clipV(n, 0, 1);
  }
  function _sigBbPctb(lc, up, lo) {
    if (lc == null || up == null || lo == null || up === lo) return null;
    const pb = (lc - lo) / (up - lo);
    return _clipV(2 * (0.5 - pb));
  }
  function _sigNatrRegime(serie, lookback) {
    lookback = lookback || 60;
    if (!serie || !serie.length) return null;
    const arr = serie.filter(v => v != null && !isNaN(v));
    if (arr.length < 10) return null;
    const cur = arr[arr.length - 1];
    const slc = arr.length >= lookback ? arr.slice(-lookback) : arr;
    const sorted = slc.slice().sort((a, b) => a - b);
    const ref = sorted[Math.floor(sorted.length / 2)];
    if (!(ref > 0) || !isFinite(cur)) return null;
    return -_clipV((cur / ref - 1) / 0.5);
  }
  function _sigObvSlope(serie, win) {
    win = win || 10;
    if (!serie || serie.length < win + 5) return null;
    const seg = serie.slice(-win);
    const mx = (win - 1) / 2;
    const my = seg.reduce((a, b) => a + b, 0) / win;
    let num = 0, den = 0;
    for (let i = 0; i < win; i++) { num += (i - mx) * (seg[i] - my); den += (i - mx) * (i - mx); }
    if (den === 0) return null;
    const slope = num / den;
    const refSeg = serie.slice(-win * 3);
    const ref = refSeg.reduce((a, b) => a + Math.abs(b), 0) / refSeg.length;
    if (!(ref > 0) || !isFinite(slope)) return null;
    return _clipV((slope / ref) / 0.05);
  }
  function _sigMfi(m) { return m == null ? null : _clipV((50 - m) / 20); }
  function _natrLocal(highs, lows, closes, period) {
    period = period || 14;
    const a = atr(highs, lows, closes, period);
    return a.map((v, i) => (v != null && closes[i]) ? (v / closes[i]) * 100 : null);
  }
  function _fmtScore(s) { return s == null ? '\u2014' : (s >= 0 ? '+' : '') + s.toFixed(2); }

  function computeVerdict() {
    const lc = last(CLO);
    const s20 = last(sma(CLO, 20));
    const s50 = last(sma(CLO, 50));
    const adxObj = adxDmi(H, L, CLO, 14);
    const adxV = last(adxObj.adx);
    const dpV = last(adxObj.plusDi);
    const dmV = last(adxObj.minusDi);
    const rsiV = last(rsi(CLO, 14));
    const macdObj = macd(CLO);
    const macdHist = last(macdObj.hist);
    const atrSer = atr(H, L, CLO, 14);
    const atrV = last(atrSer);
    const bb = bbands(CLO, 20, 2);
    const bbUp = last(bb.up), bbLo = last(bb.lo);
    const natrSer = _natrLocal(H, L, CLO, 14);
    const obvSer = obv(CLO, V);
    const mfiV = last(mfi(H, L, CLO, V, 14));

    const sT1 = _sigSmaCross(s20, s50);
    const sT2 = _sigAdxDi(adxV, dpV, dmV);
    const tend = _aggV([sT1, sT2]);
    const sM1 = _sigRsi(rsiV);
    const sM2 = _sigMacdHist(macdHist, atrV, lc);
    const mom = _aggV([sM1, sM2]);
    const sV1 = _sigBbPctb(lc, bbUp, bbLo);
    const sV2 = _sigNatrRegime(natrSer);
    const vol = _aggV([sV1, sV2]);
    const sU1 = _sigObvSlope(obvSer);
    const sU2 = _sigMfi(mfiV);
    const volu = _aggV([sU1, sU2]);
    const glob = _aggV([tend.score, mom.score, vol.score, volu.score]);

    // Valeurs réelles pour affichage (préférées aux z-scores normalisés)
    const natrV = last(natrSer);
    const bbPctB = (lc != null && bbUp != null && bbLo != null && bbUp !== bbLo)
      ? ((lc - bbLo) / (bbUp - bbLo)) : null;
    const fv = (x, d) => (x == null || isNaN(x)) ? '\u2014' : Number(x).toFixed(d == null ? 2 : d);
    const fvPct = (x, d) => (x == null || isNaN(x)) ? '\u2014' : Number(x).toFixed(d == null ? 1 : d) + '%';

    const axisCard = (titre, axe, details) => {
      const col = _axisColor(axe.score);
      const status = _axisStatus(axe.score);
      return '<div class="signal-card" style="border-left-color:' + col + ';">'
        + '<div class="signal-card-title">' + titre + ' <span class="text-muted small">(' + axe.n + '/2)</span></div>'
        + '<div class="signal-card-value">' + _fmtScore(axe.score) + '</div>'
        + '<div class="signal-card-status" style="color:' + col + ';">' + status + '</div>'
        + '<div class="text-muted" style="font-size:11px;margin-top:4px;line-height:1.5;">' + details + '</div>'
        + '</div>';
    };
    const cards = [
      axisCard('\ud83d\udcc8 Tendance', tend,
        'SMA20/50 : ' + fv(s20) + ' / ' + fv(s50)
        + ' \u00b7 ADX : ' + fv(adxV, 1)
        + ' (+DI ' + fv(dpV, 1) + ' / -DI ' + fv(dmV, 1) + ')'),
      axisCard('\ud83c\udfaf Momentum', mom,
        'RSI(14) : ' + fv(rsiV, 1)
        + ' \u00b7 MACD hist : ' + fv(macdHist, 3)),
      axisCard('\ud83c\udf2a\ufe0f Volatilite', vol,
        'BB %B : ' + fv(bbPctB)
        + ' \u00b7 NATR(14) : ' + fvPct(natrV)),
      axisCard('\ud83d\udce6 Volume', volu,
        'OBV : ' + fv(last(obvSer), 0)
        + ' \u00b7 MFI(14) : ' + fv(mfiV, 1)),
    ];

    return {
      score: glob.score,
      nAxes: glob.n,
      scorePct: glob.score == null ? null : Math.round((glob.score + 1) * 50 * 10) / 10,
      verdict: verdictMapping(glob.score),
      cards,
    };
  }

  function renderVerdict() {
    const v = computeVerdict();
    const { verdict, score, nAxes, scorePct, cards } = v;
    const gaugeVal = (score == null || isNaN(score)) ? 0 : score;
    const pctTxt = scorePct == null ? '\u2014' : scorePct;

    // Sticky header
    const sym = D.ticker || '';
    const nom = D.nom || '';
    setHTML('techStickyHeader', `
      <div class="tech-sticky-left">
        <span class="tech-sticky-ticker">${nom}</span>
        <span class="tech-sticky-symbol">${sym}</span>
      </div>
      <div class="tech-sticky-right" style="border-color:${verdict.color};">
        <span class="tech-sticky-emoji">${verdict.emoji}</span>
        <span class="tech-sticky-verdict" style="color:${verdict.color};">${verdict.label}</span>
        <span class="tech-sticky-score">score ${pctTxt}/100 \u00b7 ${nAxes}/4 axes</span>
      </div>
    `);

    // Cards (4 axes)
    setHTML('techVerdictCards', cards.join(''));

    // Gauge — bornes Sikafinance: -1 / -0.5 / -0.15 / +0.15 / +0.5 / +1
    const data = [{
      type: 'indicator',
      mode: 'gauge+number',
      value: gaugeVal,
      number: { valueformat: '.2f', font: { color: C.text, size: 28 } },
      gauge: {
        axis: {
          range: [-1, 1],
          tickvals: [-1, -0.5, -0.15, 0.15, 0.5, 1],
          ticktext: ['Vendre', 'Alleger', '', '', 'Renforcer', 'Acheter'],
          tickfont: { color: C.muted, size: 9 },
        },
        bar: { color: verdict.color, thickness: 0.25 },
        bgcolor: 'rgba(0,0,0,0)',
        borderwidth: 0,
        steps: [
          { range: [-1.0, -0.5], color: 'rgba(239,68,68,0.28)' },
          { range: [-0.5, -0.15], color: 'rgba(245,158,11,0.22)' },
          { range: [-0.15, 0.15], color: 'rgba(156,163,175,0.18)' },
          { range: [0.15, 0.5], color: 'rgba(52,211,153,0.22)' },
          { range: [0.5, 1.0], color: 'rgba(16,185,129,0.30)' },
        ],
        threshold: { line: { color: verdict.color, width: 4 }, thickness: 0.85, value: gaugeVal },
      },
      title: { text: `<b>${verdict.emoji} ${verdict.label}</b><br><span style="font-size:11px;color:${C.muted};">${pctTxt}/100 \u00b7 ${nAxes}/4 axes</span>`, font: { color: C.text, size: 14 } },
    }];
    Plotly.newPlot('techVerdictGauge', data, {
      paper_bgcolor: 'rgba(0,0,0,0)',
      font: { color: C.text },
      margin: { l: 10, r: 10, t: 60, b: 10 },
      height: 260,
    }, PLOTLY_CFG);
  }

  // ---------- Métriques helpers ----------
  function metricsRow(items) {
    return `<div class="metrics-row">${items.map(it =>
      `<div class="metric-tile"><div class="metric-label">${it.label}</div><div class="metric-value" style="${it.color ? 'color:' + it.color + ';' : ''}">${it.value}</div>${it.delta ? `<div class="metric-delta" style="color:${it.deltaColor || C.muted};">${it.delta}</div>` : ''}</div>`
    ).join('')}</div>`;
  }

  // ============================================================
  // SUBPLOTS — chacun trace cours en row1, indicateur en row2/3
  // ============================================================
  function tracePrice(rowIdx) {
    return {
      x: DATES, y: CLO, name: 'Cours', mode: 'lines',
      line: { color: C.blue, width: 1.8 },
      xaxis: rowIdx === 1 ? 'x' : `x${rowIdx}`,
      yaxis: rowIdx === 1 ? 'y' : `y${rowIdx}`,
    };
  }

  // ============================================================
  // 🎯 MOMENTUM
  // ============================================================
  function renderRSI() {
    const p = parseInt(el('ind_rsi_period').value);
    el('ind_rsi_period_lbl').textContent = p;
    const r = rsi(CLO, p);
    const traces = [
      tracePrice(1),
      { x: DATES, y: r, name: `RSI(${p})`, mode: 'lines', line: { color: C.purple, width: 1.6 }, xaxis: 'x2', yaxis: 'y2' },
    ];
    const layout = {
      ...baseLayout(2),
      grid: { rows: 2, columns: 1, pattern: 'independent', roworder: 'top to bottom' },
      yaxis: { gridcolor: C.grid, domain: [0.42, 1] },
      yaxis2: { gridcolor: C.grid, domain: [0, 0.38], range: [0, 100] },
      xaxis2: { gridcolor: C.grid, matches: 'x' },
      shapes: [
        { type: 'rect', xref: 'x2', yref: 'y2', x0: DATES[0], x1: DATES[DATES.length - 1], y0: 70, y1: 100, fillcolor: 'rgba(239,68,68,0.12)', line: { width: 0 } },
        { type: 'rect', xref: 'x2', yref: 'y2', x0: DATES[0], x1: DATES[DATES.length - 1], y0: 0, y1: 30, fillcolor: 'rgba(16,185,129,0.12)', line: { width: 0 } },
        { type: 'line', xref: 'x2', yref: 'y2', x0: DATES[0], x1: DATES[DATES.length - 1], y0: 70, y1: 70, line: { color: C.bear, dash: 'dot', width: 1 } },
        { type: 'line', xref: 'x2', yref: 'y2', x0: DATES[0], x1: DATES[DATES.length - 1], y0: 30, y1: 30, line: { color: C.bull, dash: 'dot', width: 1 } },
        { type: 'line', xref: 'x2', yref: 'y2', x0: DATES[0], x1: DATES[DATES.length - 1], y0: 50, y1: 50, line: { color: C.muted, dash: 'dot', width: 0.7 } },
      ],
      height: 680,
    };
    Plotly.react('chart_rsi', traces, applyPeriod(layout, traces), PLOTLY_CFG);
    const lr = last(r);
    const status = lr == null ? '—' : lr > 70 ? 'Suracheté' : lr < 30 ? 'Survendu' : 'Neutre';
    const col = lr == null ? C.muted : lr > 70 ? C.bear : lr < 30 ? C.bull : C.muted;
    setHTML('metrics_rsi', metricsRow([
      { label: 'RSI courant', value: lr == null ? '—' : lr.toFixed(2), color: col },
      { label: 'Statut', value: status, color: col },
      { label: 'Période', value: p },
    ]) + box(`<strong>Interprétation :</strong><br>• <span class="overbought">RSI &gt; 70</span> : suracheté<br>• <span class="oversold">RSI &lt; 30</span> : survendu<br>• <span class="neutral">30 ≤ RSI ≤ 70</span> : neutre`));
  }

  function renderStoch() {
    const k = +el('ind_stoch_k').value, d = +el('ind_stoch_d').value, sm = +el('ind_stoch_smooth').value;
    el('ind_stoch_k_lbl').textContent = k; el('ind_stoch_d_lbl').textContent = d; el('ind_stoch_smooth_lbl').textContent = sm;
    const st = stochastic(H, L, CLO, k, sm, d);
    const sr = stochRsi(CLO, 14, k, sm, d);
    const traces = [
      tracePrice(1),
      { x: DATES, y: st.k, name: '%K', mode: 'lines', line: { color: C.blue, width: 1.5 }, xaxis: 'x2', yaxis: 'y2' },
      { x: DATES, y: st.d, name: '%D', mode: 'lines', line: { color: C.orange, width: 1.5 }, xaxis: 'x2', yaxis: 'y2' },
      { x: DATES, y: sr.k, name: 'StochRSI %K', mode: 'lines', line: { color: C.purple, width: 1.3 }, xaxis: 'x3', yaxis: 'y3' },
      { x: DATES, y: sr.d, name: 'StochRSI %D', mode: 'lines', line: { color: C.bearLite, width: 1.3 }, xaxis: 'x3', yaxis: 'y3' },
    ];
    const refs = [
      { yref: 'y2', y: 80 }, { yref: 'y2', y: 20 },
      { yref: 'y3', y: 80 }, { yref: 'y3', y: 20 },
    ];
    const shapes = refs.map(r => ({
      type: 'line', xref: r.yref === 'y2' ? 'x2' : 'x3', yref: r.yref,
      x0: DATES[0], x1: DATES[DATES.length - 1], y0: r.y, y1: r.y,
      line: { color: r.y === 80 ? C.bear : C.bull, dash: 'dot', width: 1 },
    }));
    const layout = {
      ...baseLayout(3),
      grid: { rows: 3, columns: 1, pattern: 'independent', roworder: 'top to bottom' },
      yaxis: { gridcolor: C.grid, domain: [0.55, 1] },
      yaxis2: { gridcolor: C.grid, domain: [0.28, 0.50], range: [0, 100], title: { text: 'Stoch', font: { size: 10 } } },
      yaxis3: { gridcolor: C.grid, domain: [0, 0.22], range: [0, 100], title: { text: 'StochRSI', font: { size: 10 } } },
      xaxis2: { gridcolor: C.grid, matches: 'x' }, xaxis3: { gridcolor: C.grid, matches: 'x' },
      shapes, height: 820,
    };
    Plotly.react('chart_stoch', traces, applyPeriod(layout, traces), PLOTLY_CFG);
    const lk = last(st.k), ld = last(st.d);
    const stat = lk == null ? '—' : lk > 80 ? 'Suracheté' : lk < 20 ? 'Survendu' : 'Neutre';
    const col = lk == null ? C.muted : lk > 80 ? C.bear : lk < 20 ? C.bull : C.muted;
    setHTML('metrics_stoch', metricsRow([
      { label: '%K', value: lk == null ? '—' : lk.toFixed(2), color: col },
      { label: '%D', value: ld == null ? '—' : ld.toFixed(2) },
      { label: 'Statut', value: stat, color: col },
    ]) + box(`<strong>Interprétation :</strong><br>• <span class="overbought">&gt; 80</span> : suracheté · <span class="oversold">&lt; 20</span> : survendu<br>• Croisement %K/%D = signal de retournement`));
  }

  function renderCciWr() {
    const cp = +el('ind_cci_period').value, wp = +el('ind_wr_period').value;
    el('ind_cci_period_lbl').textContent = cp; el('ind_wr_period_lbl').textContent = wp;
    const cciArr = cci(H, L, CLO, cp);
    const wr = williamsR(H, L, CLO, wp);
    const traces = [
      { x: DATES, y: cciArr, name: `CCI(${cp})`, mode: 'lines', line: { color: C.orange, width: 1.5 }, xaxis: 'x', yaxis: 'y' },
      { x: DATES, y: wr, name: `Williams %R(${wp})`, mode: 'lines', line: { color: C.purple, width: 1.5 }, xaxis: 'x2', yaxis: 'y2' },
    ];
    const refs = [
      { yref: 'y', y: 100, color: C.bear }, { yref: 'y', y: -100, color: C.bull }, { yref: 'y', y: 0, color: C.muted },
      { yref: 'y2', y: -20, color: C.bear }, { yref: 'y2', y: -80, color: C.bull },
    ];
    const shapes = refs.map(r => ({
      type: 'line', xref: r.yref === 'y' ? 'x' : 'x2', yref: r.yref,
      x0: DATES[0], x1: DATES[DATES.length - 1], y0: r.y, y1: r.y,
      line: { color: r.color, dash: 'dot', width: 1 },
    }));
    const layout = {
      ...baseLayout(2),
      grid: { rows: 2, columns: 1, pattern: 'independent', roworder: 'top to bottom' },
      yaxis: { gridcolor: C.grid, domain: [0.52, 1], title: { text: 'CCI', font: { size: 10 } } },
      yaxis2: { gridcolor: C.grid, domain: [0, 0.48], title: { text: 'Williams %R', font: { size: 10 } }, range: [-100, 0] },
      xaxis2: { gridcolor: C.grid, matches: 'x' },
      shapes, height: 680,
    };
    Plotly.react('chart_cciwr', traces, applyPeriod(layout, traces), PLOTLY_CFG);
    const lc = last(cciArr), lw = last(wr);
    const cciStat = lc == null ? '—' : lc > 100 ? 'Suracheté' : lc < -100 ? 'Survendu' : 'Neutre';
    const cciCol = lc == null ? C.muted : lc > 100 ? C.bear : lc < -100 ? C.bull : C.muted;
    const wrStat = lw == null ? '—' : lw > -20 ? 'Suracheté' : lw < -80 ? 'Survendu' : 'Neutre';
    const wrCol = lw == null ? C.muted : lw > -20 ? C.bear : lw < -80 ? C.bull : C.muted;
    setHTML('metrics_cciwr', metricsRow([
      { label: 'CCI', value: lc == null ? '—' : lc.toFixed(1), color: cciCol },
      { label: 'CCI statut', value: cciStat, color: cciCol },
      { label: 'Williams %R', value: lw == null ? '—' : lw.toFixed(1), color: wrCol },
      { label: '%R statut', value: wrStat, color: wrCol },
    ]) + box(`<strong>Interprétation :</strong><br>• CCI : <span class="overbought">&gt; +100</span> suracheté · <span class="oversold">&lt; −100</span> survendu<br>• Williams %R : <span class="overbought">&gt; −20</span> suracheté · <span class="oversold">&lt; −80</span> survendu`));
  }

  function renderMomRoc() {
    const mp = +el('ind_mom_p').value, rp = +el('ind_roc_p').value, tp = +el('ind_trix_p').value;
    el('ind_mom_p_lbl').textContent = mp; el('ind_roc_p_lbl').textContent = rp; el('ind_trix_p_lbl').textContent = tp;
    const m = momentum(CLO, mp);
    const r = roc(CLO, rp);
    const t = trix(CLO, tp);
    const traces = [
      { x: DATES, y: m, name: `Mom(${mp})`, mode: 'lines', line: { color: C.blue, width: 1.4 }, xaxis: 'x', yaxis: 'y' },
      { x: DATES, y: r, name: `ROC(${rp})`, mode: 'lines', line: { color: C.orange, width: 1.4 }, xaxis: 'x2', yaxis: 'y2' },
      { x: DATES, y: t, name: `TRIX(${tp})`, mode: 'lines', line: { color: C.purple, width: 1.4 }, xaxis: 'x3', yaxis: 'y3' },
    ];
    const shapes = [
      { type: 'line', xref: 'x', yref: 'y', x0: DATES[0], x1: DATES[DATES.length - 1], y0: 0, y1: 0, line: { color: C.muted, dash: 'dot', width: 1 } },
      { type: 'line', xref: 'x2', yref: 'y2', x0: DATES[0], x1: DATES[DATES.length - 1], y0: 0, y1: 0, line: { color: C.muted, dash: 'dot', width: 1 } },
      { type: 'line', xref: 'x3', yref: 'y3', x0: DATES[0], x1: DATES[DATES.length - 1], y0: 0, y1: 0, line: { color: C.muted, dash: 'dot', width: 1 } },
    ];
    const layout = {
      ...baseLayout(3),
      grid: { rows: 3, columns: 1, pattern: 'independent', roworder: 'top to bottom' },
      yaxis: { gridcolor: C.grid, domain: [0.69, 1], title: { text: 'Momentum', font: { size: 10 } } },
      yaxis2: { gridcolor: C.grid, domain: [0.36, 0.65], title: { text: 'ROC %', font: { size: 10 } } },
      yaxis3: { gridcolor: C.grid, domain: [0, 0.32], title: { text: 'TRIX %', font: { size: 10 } } },
      xaxis2: { gridcolor: C.grid, matches: 'x' }, xaxis3: { gridcolor: C.grid, matches: 'x' },
      shapes, height: 820,
    };
    Plotly.react('chart_momroc', traces, applyPeriod(layout, traces), PLOTLY_CFG);
    setHTML('metrics_momroc', metricsRow([
      { label: 'Momentum', value: last(m) == null ? '—' : fmt(last(m), 2) },
      { label: 'ROC %', value: last(r) == null ? '—' : last(r).toFixed(2) },
      { label: 'TRIX %', value: last(t) == null ? '—' : last(t).toFixed(3) },
    ]) + box(`<strong>Interprétation :</strong><br>• Au-dessus de 0 = momentum haussier<br>• En-dessous de 0 = momentum baissier`));
  }

  // ============================================================
  // 📈 TENDANCE
  // ============================================================
  function renderSMA() {
    const ss = +el('ind_sma_s').value, sl = +el('ind_sma_l').value;
    const es = +el('ind_ema_s').value, el2 = +el('ind_ema_l').value;
    el('ind_sma_s_lbl').textContent = ss; el('ind_sma_l_lbl').textContent = sl;
    el('ind_ema_s_lbl').textContent = es; el('ind_ema_l_lbl').textContent = el2;
    const extras = Array.from(document.querySelectorAll('input[name="ind_sma_extra"]:checked')).map(x => x.value);
    const sm1 = sma(CLO, ss), sm2 = sma(CLO, sl);
    const em1 = ema(CLO, es), em2 = ema(CLO, el2);
    const traces = [
      { x: DATES, y: CLO, name: 'Cours', mode: 'lines', line: { color: C.blue, width: 1.8 } },
      { x: DATES, y: sm1, name: `SMA(${ss})`, mode: 'lines', line: { color: C.bullLite, width: 1.3 } },
      { x: DATES, y: sm2, name: `SMA(${sl})`, mode: 'lines', line: { color: C.bearLite, width: 1.3 } },
      { x: DATES, y: em1, name: `EMA(${es})`, mode: 'lines', line: { color: C.orange, width: 1.3, dash: 'dot' } },
      { x: DATES, y: em2, name: `EMA(${el2})`, mode: 'lines', line: { color: C.purple, width: 1.3, dash: 'dot' } },
    ];
    if (extras.includes('WMA')) traces.push({ x: DATES, y: wma(CLO, 20), name: 'WMA(20)', mode: 'lines', line: { color: '#0d9488', width: 1.2, dash: 'dash' } });
    if (extras.includes('DEMA')) traces.push({ x: DATES, y: dema(CLO, 20), name: 'DEMA(20)', mode: 'lines', line: { color: '#ec4899', width: 1.2, dash: 'dash' } });
    if (extras.includes('TEMA')) traces.push({ x: DATES, y: tema(CLO, 20), name: 'TEMA(20)', mode: 'lines', line: { color: '#22c55e', width: 1.2, dash: 'dash' } });
    const layout = { ...baseLayout(1), height: 540 };
    Plotly.react('chart_sma', traces, applyPeriod(layout, traces), PLOTLY_CFG);
    const ls1 = last(sm1), ls2 = last(sm2), le1 = last(em1), le2 = last(em2);
    const smaCross = (ls1 != null && ls2 != null) ? (ls1 > ls2 ? '🟢 Golden cross' : ls1 < ls2 ? '🔴 Death cross' : '⚪ Neutre') : '—';
    const emaCross = (le1 != null && le2 != null) ? (le1 > le2 ? '🟢 Golden cross' : le1 < le2 ? '🔴 Death cross' : '⚪ Neutre') : '—';
    setHTML('metrics_sma', metricsRow([
      { label: `SMA ${ss}`, value: ls1 == null ? '—' : fmt(ls1) },
      { label: `SMA ${sl}`, value: ls2 == null ? '—' : fmt(ls2) },
      { label: `EMA ${es}`, value: le1 == null ? '—' : fmt(le1) },
      { label: `EMA ${el2}`, value: le2 == null ? '—' : fmt(le2) },
    ]) + box(`<strong>Croisements :</strong><br>• SMA ${ss}/${sl} : ${smaCross}<br>• EMA ${es}/${el2} : ${emaCross}`));
  }

  function renderMACD() {
    const mode = document.querySelector('input[name="ind_macd_mode"]:checked').value;
    const fa = +el('ind_macd_fast').value, sl = +el('ind_macd_slow').value, sg = +el('ind_macd_sig').value;
    el('ind_macd_fast_lbl').textContent = fa; el('ind_macd_slow_lbl').textContent = sl; el('ind_macd_sig_lbl').textContent = sg;
    const m = mode === 'PPO' ? ppo(CLO, fa, sl, sg) : macd(CLO, fa, sl, sg);
    const traces = [
      tracePrice(1),
      { x: DATES, y: m.macd, name: mode, mode: 'lines', line: { color: C.blue, width: 1.6 }, xaxis: 'x2', yaxis: 'y2' },
      { x: DATES, y: m.signal, name: 'Signal', mode: 'lines', line: { color: C.orange, width: 1.4, dash: 'dot' }, xaxis: 'x2', yaxis: 'y2' },
      { x: DATES, y: m.hist, name: 'Histo', type: 'bar', xaxis: 'x2', yaxis: 'y2', marker: { color: m.hist.map(v => v == null ? C.muted : v >= 0 ? C.bull : C.bear) } },
    ];
    const layout = {
      ...baseLayout(2),
      grid: { rows: 2, columns: 1, pattern: 'independent', roworder: 'top to bottom' },
      yaxis: { gridcolor: C.grid, domain: [0.42, 1] },
      yaxis2: { gridcolor: C.grid, domain: [0, 0.38], title: { text: mode, font: { size: 10 } } },
      xaxis2: { gridcolor: C.grid, matches: 'x' },
      shapes: [{ type: 'line', xref: 'x2', yref: 'y2', x0: DATES[0], x1: DATES[DATES.length - 1], y0: 0, y1: 0, line: { color: C.muted, dash: 'dot', width: 1 } }],
      height: 680,
    };
    Plotly.react('chart_macd', traces, applyPeriod(layout, traces), PLOTLY_CFG);
    const lm = last(m.macd), ls = last(m.signal), lh = last(m.hist);
    const stat = (lm != null && ls != null) ? (lm > ls ? '🟢 Croisement haussier' : lm < ls ? '🔴 Croisement baissier' : '⚪ Neutre') : '—';
    const col = (lm != null && ls != null) ? (lm > ls ? C.bull : lm < ls ? C.bear : C.muted) : C.muted;
    setHTML('metrics_macd', metricsRow([
      { label: mode, value: lm == null ? '—' : lm.toFixed(3) },
      { label: 'Signal', value: ls == null ? '—' : ls.toFixed(3) },
      { label: 'Histo', value: lh == null ? '—' : lh.toFixed(3), color: lh == null ? C.muted : lh >= 0 ? C.bull : C.bear },
      { label: 'Statut', value: stat, color: col },
    ]) + box(`<strong>Interprétation :</strong><br>• Histogramme &gt; 0 : pression acheteuse<br>• Croisement ${mode} / Signal = signal d'entrée/sortie`));
  }

  function renderADX() {
    const p = +el('ind_adx_p').value;
    el('ind_adx_p_lbl').textContent = p;
    const a = adxDmi(H, L, CLO, p);
    const traces = [
      tracePrice(1),
      { x: DATES, y: a.adx, name: `ADX(${p})`, mode: 'lines', line: { color: C.blue, width: 1.8 }, xaxis: 'x2', yaxis: 'y2' },
      { x: DATES, y: a.plusDi, name: '+DI', mode: 'lines', line: { color: C.bull, width: 1.3 }, xaxis: 'x2', yaxis: 'y2' },
      { x: DATES, y: a.minusDi, name: '−DI', mode: 'lines', line: { color: C.bear, width: 1.3 }, xaxis: 'x2', yaxis: 'y2' },
    ];
    const shapes = [
      { type: 'line', xref: 'x2', yref: 'y2', x0: DATES[0], x1: DATES[DATES.length - 1], y0: 20, y1: 20, line: { color: C.muted, dash: 'dot', width: 1 } },
      { type: 'line', xref: 'x2', yref: 'y2', x0: DATES[0], x1: DATES[DATES.length - 1], y0: 40, y1: 40, line: { color: C.muted, dash: 'dot', width: 1 } },
    ];
    const layout = {
      ...baseLayout(2),
      grid: { rows: 2, columns: 1, pattern: 'independent', roworder: 'top to bottom' },
      yaxis: { gridcolor: C.grid, domain: [0.42, 1] },
      yaxis2: { gridcolor: C.grid, domain: [0, 0.38], title: { text: 'ADX/DMI', font: { size: 10 } } },
      xaxis2: { gridcolor: C.grid, matches: 'x' },
      shapes, height: 680,
    };
    Plotly.react('chart_adx', traces, applyPeriod(layout, traces), PLOTLY_CFG);
    const la = last(a.adx), lp = last(a.plusDi), lm = last(a.minusDi);
    const trend = la == null ? '—' : la > 40 ? 'Tendance forte' : la > 20 ? 'Tendance modérée' : 'Pas de tendance';
    const dir = (lp != null && lm != null) ? (lp > lm ? '🟢 Haussier' : '🔴 Baissier') : '—';
    setHTML('metrics_adx', metricsRow([
      { label: 'ADX', value: la == null ? '—' : la.toFixed(2) },
      { label: '+DI', value: lp == null ? '—' : lp.toFixed(2), color: C.bull },
      { label: '−DI', value: lm == null ? '—' : lm.toFixed(2), color: C.bear },
      { label: 'Direction', value: dir },
    ]) + box(`<strong>Interprétation :</strong><br>• ADX &gt; 25 = tendance forte · ADX &lt; 20 = absence de tendance<br>• +DI &gt; −DI = pression haussière. ${trend}`));
  }

  function renderSarIchi() {
    const af = +el('ind_sar_af').value, am = +el('ind_sar_max').value;
    const tk = +el('ind_ichi_tk').value, kj = +el('ind_ichi_kj').value, sb = +el('ind_ichi_sb').value;
    el('ind_sar_af_lbl').textContent = af.toFixed(2); el('ind_sar_max_lbl').textContent = am.toFixed(2);
    el('ind_ichi_tk_lbl').textContent = tk; el('ind_ichi_kj_lbl').textContent = kj; el('ind_ichi_sb_lbl').textContent = sb;
    const sar = parabolicSar(H, L, af, af, am);
    const ich = ichimoku(H, L, CLO, tk, kj, sb);
    const traces = [
      // Senkou A & B (cloud)
      { x: DATES, y: ich.senkouA, name: 'Senkou A', mode: 'lines', line: { color: 'rgba(16,185,129,0.6)', width: 1 } },
      { x: DATES, y: ich.senkouB, name: 'Senkou B', mode: 'lines', line: { color: 'rgba(239,68,68,0.6)', width: 1 }, fill: 'tonexty', fillcolor: C.cloud },
      { x: DATES, y: CLO, name: 'Cours', mode: 'lines', line: { color: C.blue, width: 1.8 } },
      { x: DATES, y: ich.tenkan, name: `Tenkan(${tk})`, mode: 'lines', line: { color: C.orange, width: 1.2 } },
      { x: DATES, y: ich.kijun, name: `Kijun(${kj})`, mode: 'lines', line: { color: C.purple, width: 1.2 } },
      { x: DATES, y: ich.chikou, name: 'Chikou', mode: 'lines', line: { color: C.muted, width: 1, dash: 'dot' } },
      { x: DATES, y: sar, name: 'SAR', mode: 'markers', marker: { symbol: 'circle-open', size: 5, color: C.bearLite } },
    ];
    const layout = { ...baseLayout(1), height: 540 };
    Plotly.react('chart_saric', traces, applyPeriod(layout, traces), PLOTLY_CFG);
    const lc = last(CLO), lsa = last(ich.senkouA), lsb = last(ich.senkouB), lt = last(ich.tenkan), lk = last(ich.kijun), lsar = last(sar);
    let cloudPos = '—', cloudCol = C.muted;
    if (lc != null && lsa != null && lsb != null) {
      const top = Math.max(lsa, lsb), bot = Math.min(lsa, lsb);
      if (lc > top) { cloudPos = '🟢 Au-dessus du nuage'; cloudCol = C.bull; }
      else if (lc < bot) { cloudPos = '🔴 Sous le nuage'; cloudCol = C.bear; }
      else { cloudPos = '⚪ Dans le nuage'; cloudCol = C.muted; }
    }
    const tkCross = (lt != null && lk != null) ? (lt > lk ? '🟢 Tenkan &gt; Kijun' : lt < lk ? '🔴 Tenkan &lt; Kijun' : '⚪ Égal') : '—';
    const sarSig = (lsar != null && lc != null) ? (lc > lsar ? '🟢 SAR sous le prix' : '🔴 SAR au-dessus') : '—';
    setHTML('metrics_saric', metricsRow([
      { label: 'SAR', value: lsar == null ? '—' : fmt(lsar) },
      { label: 'Position cloud', value: cloudPos, color: cloudCol },
      { label: 'Tenkan/Kijun', value: tkCross },
      { label: 'Signal SAR', value: sarSig },
    ]) + box(`<strong>Interprétation :</strong><br>• Prix au-dessus du nuage = tendance haussière<br>• Croisement Tenkan/Kijun = signal d'entrée<br>• SAR change de côté = renversement de tendance`));
  }

  // ============================================================
  // 🌪️ VOLATILITÉ
  // ============================================================
  function renderBB() {
    const p = +el('ind_bb_p').value, k = +el('ind_bb_k').value;
    el('ind_bb_p_lbl').textContent = p; el('ind_bb_k_lbl').textContent = k.toFixed(1);
    const b = bbands(CLO, p, k);
    const traces = [
      { x: DATES, y: b.up, name: 'Bande haute', mode: 'lines', line: { color: C.bear, width: 1, dash: 'dot' } },
      { x: DATES, y: b.lo, name: 'Bande basse', mode: 'lines', line: { color: C.bull, width: 1, dash: 'dot' }, fill: 'tonexty', fillcolor: C.bbFill },
      { x: DATES, y: b.mid, name: `SMA(${p})`, mode: 'lines', line: { color: C.muted, width: 1, dash: 'dash' } },
      { x: DATES, y: CLO, name: 'Cours', mode: 'lines', line: { color: C.blue, width: 1.8 } },
      { x: DATES, y: b.bw, name: 'Bandwidth %', mode: 'lines', line: { color: C.orange, width: 1.4 }, fill: 'tozeroy', fillcolor: 'rgba(245,158,11,0.12)', xaxis: 'x2', yaxis: 'y2' },
    ];
    const layout = {
      ...baseLayout(2),
      grid: { rows: 2, columns: 1, pattern: 'independent', roworder: 'top to bottom' },
      yaxis: { gridcolor: C.grid, domain: [0.32, 1] },
      yaxis2: { gridcolor: C.grid, domain: [0, 0.28], title: { text: 'Bandwidth %', font: { size: 10 } } },
      xaxis2: { gridcolor: C.grid, matches: 'x' },
      height: 680,
    };
    Plotly.react('chart_bb', traces, applyPeriod(layout, traces), PLOTLY_CFG);
    const lpb = last(b.pb), lbw = last(b.bw);
    const bws = b.bw.filter(isN).sort((a, b) => a - b);
    const pct = (lbw != null && bws.length) ? Math.round(100 * bws.findIndex(v => v >= lbw) / bws.length) : null;
    const pbStat = lpb == null ? '—' : lpb > 1 ? 'Cassure haute' : lpb < 0 ? 'Cassure basse' : lpb > 0.8 ? 'Proche bande haute' : lpb < 0.2 ? 'Proche bande basse' : 'Centre';
    const pbCol = lpb == null ? C.muted : lpb > 1 ? C.bear : lpb < 0 ? C.bull : C.muted;
    setHTML('metrics_bb', metricsRow([
      { label: '%B', value: lpb == null ? '—' : lpb.toFixed(3), color: pbCol },
      { label: 'Bandwidth %', value: lbw == null ? '—' : lbw.toFixed(2) },
      { label: 'Percentile BW', value: pct == null ? '—' : `${pct}%` },
      { label: 'Statut', value: pbStat, color: pbCol },
    ]) + box(`<strong>Interprétation :</strong><br>• <span class="overbought">%B &gt; 1</span> : prix au-dessus bande haute (suracheté)<br>• <span class="oversold">%B &lt; 0</span> : prix sous bande basse (survendu)<br>• Bandwidth faible = compression (cassure imminente)`));
  }

  function renderATR() {
    const p = +el('ind_atr_p').value;
    el('ind_atr_p_lbl').textContent = p;
    const a = atr(H, L, CLO, p);
    const aPct = a.map((v, i) => (v != null && CLO[i]) ? 100 * v / CLO[i] : null);
    const traces = [
      tracePrice(1),
      { x: DATES, y: a, name: `ATR(${p})`, mode: 'lines', line: { color: C.blue, width: 1.5 }, xaxis: 'x2', yaxis: 'y2' },
      { x: DATES, y: aPct, name: 'ATR %', mode: 'lines', line: { color: C.orange, width: 1.3, dash: 'dot' }, xaxis: 'x2', yaxis: 'y3' },
    ];
    const layout = {
      ...baseLayout(2),
      grid: { rows: 2, columns: 1, pattern: 'independent', roworder: 'top to bottom' },
      yaxis: { gridcolor: C.grid, domain: [0.42, 1] },
      yaxis2: { gridcolor: C.grid, domain: [0, 0.38], title: { text: 'ATR FCFA', font: { size: 10 } } },
      yaxis3: { gridcolor: C.grid, domain: [0, 0.38], overlaying: 'y2', side: 'right', title: { text: 'ATR %', font: { size: 10 } } },
      xaxis2: { gridcolor: C.grid, matches: 'x' },
      height: 680,
    };
    Plotly.react('chart_atr', traces, applyPeriod(layout, traces), PLOTLY_CFG);
    const la = last(a), lp = last(aPct), lc = last(CLO);
    const stop = (la != null && lc != null) ? lc - 2 * la : null;
    setHTML('metrics_atr', metricsRow([
      { label: 'ATR', value: la == null ? '—' : fmt(la, 2) },
      { label: 'ATR %', value: lp == null ? '—' : `${lp.toFixed(2)} %` },
      { label: 'Stop suggéré (−2·ATR)', value: stop == null ? '—' : fmt(stop), color: C.bear },
    ]) + box(`<strong>Interprétation :</strong><br>• ATR mesure la volatilité absolue (fourchette moyenne quotidienne)<br>• Stop-loss = cours actuel − 2 × ATR (protection contre le bruit)`));
  }

  // ============================================================
  // 📦 VOLUME
  // ============================================================
  function renderVolume() {
    const p = +el('ind_mfi_p').value;
    el('ind_mfi_p_lbl').textContent = p;
    const ob = obv(CLO, V);
    const ad_ = ad(H, L, CLO, V);
    const m = mfi(H, L, CLO, V, p);
    const traces = [
      { x: DATES, y: CLO, name: 'Cours', mode: 'lines', line: { color: C.blue, width: 1.8 } },
      { x: DATES, y: V, name: 'Volume', type: 'bar', marker: { color: C.volBar }, yaxis: 'y4' },
      { x: DATES, y: ob, name: 'OBV', mode: 'lines', line: { color: C.blue, width: 1.4 }, xaxis: 'x2', yaxis: 'y2' },
      { x: DATES, y: ad_, name: 'A/D', mode: 'lines', line: { color: C.orange, width: 1.3, dash: 'dot' }, xaxis: 'x2', yaxis: 'y2' },
      { x: DATES, y: m, name: `MFI(${p})`, mode: 'lines', line: { color: C.purple, width: 1.5 }, xaxis: 'x3', yaxis: 'y3' },
    ];
    const shapes = [
      { type: 'line', xref: 'x3', yref: 'y3', x0: DATES[0], x1: DATES[DATES.length - 1], y0: 80, y1: 80, line: { color: C.bear, dash: 'dot', width: 1 } },
      { type: 'line', xref: 'x3', yref: 'y3', x0: DATES[0], x1: DATES[DATES.length - 1], y0: 20, y1: 20, line: { color: C.bull, dash: 'dot', width: 1 } },
    ];
    const layout = {
      ...baseLayout(3),
      grid: { rows: 3, columns: 1, pattern: 'independent', roworder: 'top to bottom' },
      yaxis: { gridcolor: C.grid, domain: [0.55, 1] },
      yaxis4: { overlaying: 'y', side: 'right', showgrid: false, showticklabels: false },
      yaxis2: { gridcolor: C.grid, domain: [0.28, 0.50], title: { text: 'OBV / A-D', font: { size: 10 } } },
      yaxis3: { gridcolor: C.grid, domain: [0, 0.22], range: [0, 100], title: { text: 'MFI', font: { size: 10 } } },
      xaxis2: { gridcolor: C.grid, matches: 'x' }, xaxis3: { gridcolor: C.grid, matches: 'x' },
      shapes, height: 820,
    };
    Plotly.react('chart_volume', traces, applyPeriod(layout, traces), PLOTLY_CFG);
    const lo = last(ob), lm = last(m), lad = last(ad_);
    const mfiStat = lm == null ? '—' : lm > 80 ? 'Suracheté' : lm < 20 ? 'Survendu' : 'Neutre';
    const mfiCol = lm == null ? C.muted : lm > 80 ? C.bear : lm < 20 ? C.bull : C.muted;
    setHTML('metrics_volume', metricsRow([
      { label: 'OBV', value: lo == null ? '—' : fmt(lo) },
      { label: `MFI(${p})`, value: lm == null ? '—' : lm.toFixed(2), color: mfiCol },
      { label: 'MFI statut', value: mfiStat, color: mfiCol },
      { label: 'A/D', value: lad == null ? '—' : fmt(lad) },
    ]) + box(`<strong>Interprétation :</strong><br>• OBV ascendant = accumulation (volume confirme la hausse)<br>• <span class="overbought">MFI &gt; 80</span> suracheté · <span class="oversold">MFI &lt; 20</span> survendu<br>• A/D mesure la pression acheteuse/vendeuse cumulée`));
  }

  // ============================================================
  // Renderers map + dispatch sur changement
  // ============================================================
  const RENDERERS = {
    rsi: renderRSI, stoch: renderStoch, cciwr: renderCciWr, momroc: renderMomRoc,
    sma: renderSMA, macd: renderMACD, adx: renderADX, saric: renderSarIchi,
    bb: renderBB, atr: renderATR,
    volume: renderVolume,
  };

  function renderAll() {
    renderVerdict();
    Object.values(RENDERERS).forEach(fn => { try { fn(); } catch (e) { console.error('[indicators]', e); } });
  }

  // ---------- Init ----------
  function init() {
    if (typeof Plotly === 'undefined') {
      console.error('[indicators] Plotly non chargé');
      return;
    }
    // Période
    document.querySelectorAll('#techPeriodFilter [data-period]').forEach(b => {
      b.addEventListener('click', () => {
        document.querySelectorAll('#techPeriodFilter [data-period]').forEach(x => x.classList.remove('active'));
        b.classList.add('active');
        PERIOD = b.dataset.period;
        renderAll();
      });
    });
    // Sliders / inputs : re-render à chaque changement
    document.querySelectorAll('[data-ind-input]').forEach(inp => {
      const k = inp.dataset.indInput;
      const fn = RENDERERS[k];
      if (!fn) return;
      inp.addEventListener('input', fn);
      inp.addEventListener('change', fn);
    });
    // Premier rendu (déclenché à l'ouverture de l'onglet pour fiabilité Plotly)
    const indTab = document.getElementById('indicators-tab');
    let firstRender = false;
    const doFirst = () => { if (!firstRender) { firstRender = true; renderAll(); } };
    if (indTab) {
      indTab.addEventListener('shown.bs.tab', doFirst);
      // Si l'onglet est déjà actif (rare), on rend tout de suite
      if (document.getElementById('indicators')?.classList.contains('active')) doFirst();
    } else {
      doFirst();
    }

    // Resize Plotly à chaque changement de sous-onglet (familles + pills)
    // Indispensable car Plotly dessine en largeur 0 dans les onglets cachés
    function resizeVisible() {
      document.querySelectorAll('#indicators .tech-plot, #techVerdictGauge').forEach(div => {
        if (div.offsetParent !== null && div._fullLayout) {
          try { Plotly.Plots.resize(div); } catch (e) { /* ignore */ }
        }
      });
    }
    document.querySelectorAll('#indicators [data-bs-toggle="tab"], #indicators [data-bs-toggle="pill"]').forEach(t => {
      t.addEventListener('shown.bs.tab', () => {
        // Petit délai pour laisser le navigateur appliquer display:block
        setTimeout(resizeVisible, 50);
      });
    });
    window.addEventListener('resize', () => { setTimeout(resizeVisible, 100); });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
