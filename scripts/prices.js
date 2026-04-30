/* =========================================================
   Daily Brief — Markets rendering
   Newspaper-style market table: header row + category divider rows
   + asset rows. One full-width container — no five-box gap.
   ========================================================= */

const $ = (sel, root = document) => root.querySelector(sel);

const CATEGORIES = [
  { id: 'crypto',      title: 'Crypto',         meta: 'Digital assets' },
  { id: 'commodities', title: 'Commodities',    meta: 'Stores of value' },
  { id: 'indices',     title: 'Indices & ETFs', meta: 'Broad market' },
  { id: 'ai_tech',     title: 'AI & Tech',      meta: 'Frontier compute' },
  { id: 'defense',     title: 'Defense',        meta: 'Geopolitics proxy' },
];

const escapeHTML = (str = '') => String(str)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

const fmtPrice = (val, currency) => {
  if (val == null || !Number.isFinite(val)) return '—';
  const cur = currency || 'USD';
  try {
    if (val >= 1000) {
      return new Intl.NumberFormat('en-US', {
        style: 'currency', currency: cur, maximumFractionDigits: 0,
      }).format(val);
    }
    if (val >= 1) {
      return new Intl.NumberFormat('en-US', {
        style: 'currency', currency: cur, maximumFractionDigits: 2,
      }).format(val);
    }
    return new Intl.NumberFormat('en-US', {
      style: 'currency', currency: cur, maximumFractionDigits: 4,
    }).format(val);
  } catch {
    return `${val.toFixed(2)} ${cur}`;
  }
};

const fmtPct = (val) => {
  if (val == null || !Number.isFinite(val)) return null;
  const sign = val > 0 ? '+' : '';
  return `${sign}${val.toFixed(2)}%`;
};

const signalLabel = (sig) => {
  switch (sig) {
    case 'bull':    return 'Bullish';
    case 'bear':    return 'Bearish';
    case 'neutral': return 'Neutral';
    default:        return 'No signal';
  }
};

export function renderMarkets(prices) {
  const table = $('#marketTable');
  if (!table) return;

  const head = `
    <div class="market-table__head" role="row">
      <span>Ticker</span>
      <span>Name</span>
      <span>Price</span>
      <span>Δ 24h</span>
      <span>Δ 1M</span>
      <span>Δ 3M</span>
      <span>Signal</span>
    </div>
  `;

  const body = CATEGORIES.map(cat => {
    const assets = prices?.categories?.[cat.id] || [];
    return renderCategoryBlock(cat, assets);
  }).join('');

  table.innerHTML = head + body;
}

function renderCategoryBlock(cat, assets) {
  const head = `
    <div class="market-table__cat" role="row" data-category="${cat.id}">
      <span class="market-table__cat-title">${escapeHTML(cat.title)}</span>
      <span class="market-table__cat-meta">${escapeHTML(cat.meta)}</span>
    </div>
  `;
  const rows = assets.length
    ? assets.map(renderRow).join('')
    : `<div class="market-row market-row--empty"><span>No data.</span></div>`;
  return head + rows;
}

function changeClassFor(val) {
  if (val == null) return 'market-row__change--flat';
  if (val > 0.05)  return 'market-row__change--up';
  if (val < -0.05) return 'market-row__change--down';
  return 'market-row__change--flat';
}

function changeCellHTML(val, extraClass = '') {
  const cls = `${changeClassFor(val)} ${extraClass}`.trim();
  return `<span class="market-row__change ${cls}">${fmtPct(val) || '—'}</span>`;
}

function renderRow(a) {
  const sig = a.signal || 'unknown';

  return `
    <div class="market-row" role="row" data-ticker="${escapeHTML(a.ticker || '')}">
      <span class="market-row__ticker">${escapeHTML(a.ticker || '—')}</span>
      <span class="market-row__name">${escapeHTML(a.name || '')}</span>
      <span class="market-row__price">${fmtPrice(a.price, a.currency)}</span>
      ${changeCellHTML(a.change_pct)}
      ${changeCellHTML(a.change_1m, 'market-row__change--1m')}
      ${changeCellHTML(a.change_3m, 'market-row__change--3m')}
      <span class="market-row__signal-wrap">
        <span class="signal signal--${sig}" title="${signalLabel(sig)}">${signalLabel(sig)}</span>
      </span>
      ${a.note ? `<p class="market-row__note">${escapeHTML(a.note)}</p>` : ''}
    </div>
  `;
}
