/* =========================================================
   Daily Brief — News rendering
   Hero (lead story) + 4-card flat grid for the 4 remaining briefs.
   ========================================================= */

const $ = (sel, root = document) => root.querySelector(sel);

const CATEGORY_LABELS = {
  politics:  'Politics & Geopolitics',
  finance:   'Finance',
  conflicts: 'Conflicts',
  science:   'Science',
  resources: 'Resources',
};

const fmtRelative = (iso) => {
  if (!iso) return '';
  const d = new Date(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60)        return 'just now';
  if (diff < 3600)      return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400)     return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)}d ago`;
  try {
    return new Intl.DateTimeFormat('en-US', { day: '2-digit', month: 'short' }).format(d);
  } catch { return ''; }
};

const fmtDateLong = (iso) => {
  if (!iso) return '';
  try {
    return new Intl.DateTimeFormat('en-US', {
      weekday: 'long', day: 'numeric', month: 'long', year: 'numeric',
    }).format(new Date(iso));
  } catch { return ''; }
};

const escapeHTML = (str = '') => String(str)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

const labelFor = (cat) => CATEGORY_LABELS[cat] || (cat ? cat.replace(/^\w/, c => c.toUpperCase()) : 'Top story');

export function renderHero(news) {
  const hero = news?.hero;
  const headlineEl = $('#heroHeadline');
  const summaryEl = $('#heroSummary');
  const linkEl = $('#heroLink');
  const sourceEl = $('#heroSource');
  const dateEl = $('#heroDate');
  const catEl = $('#heroCategory');

  if (!hero) {
    if (headlineEl) headlineEl.textContent = 'No top story available right now.';
    if (summaryEl) summaryEl.textContent = 'Run the news fetcher or wait for the next scheduled refresh.';
    if (catEl) catEl.textContent = '—';
    return;
  }

  if (headlineEl) headlineEl.textContent = hero.title || '—';
  if (summaryEl) summaryEl.textContent = hero.summary || '';
  if (linkEl && hero.url) {
    linkEl.href = hero.url;
    linkEl.hidden = false;
  }
  if (sourceEl) {
    const parts = [];
    if (hero.source) parts.push(`<strong>${escapeHTML(hero.source)}</strong>`);
    if (hero.published_at) parts.push(escapeHTML(fmtRelative(hero.published_at)));
    sourceEl.innerHTML = parts.join(' &middot; ');
  }
  if (dateEl) {
    const today = news?.updated || hero.published_at;
    dateEl.textContent = fmtDateLong(today) || 'Today';
  }
  if (catEl) catEl.textContent = labelFor(hero.category);
}

export function renderNews(news) {
  const grid = $('#newsGrid');
  if (!grid) return;

  const items = (news?.items || []).slice(0, 4);

  if (!items.length) {
    grid.innerHTML = `<p class="news-empty">No additional briefs available right now.</p>`;
    return;
  }

  grid.innerHTML = items.map((it, i) => renderCard(it, i + 2)).join('');
}

function renderCard(it, rank) {
  const url = it.url || '#';
  const rel = fmtRelative(it.published_at);
  const cat = labelFor(it.category);
  return `
    <article class="news-card" style="animation-delay: ${rank * 40}ms">
      <a class="news-card__link" href="${escapeHTML(url)}" target="_blank" rel="noopener">
        <header class="news-card__head">
          <span class="news-card__tag">${escapeHTML(cat)}</span>
          <span class="news-card__rank">№ ${rank}</span>
        </header>
        <h3 class="news-card__title">${escapeHTML(it.title || 'Untitled')}</h3>
        ${it.summary ? `<p class="news-card__lead">${escapeHTML(it.summary)}</p>` : ''}
        <footer class="news-card__meta">
          <span class="news-card__source">${escapeHTML(it.source || '—')}</span>
          ${rel ? `<span class="news-card__sep" aria-hidden="true">·</span><span>${escapeHTML(rel)}</span>` : ''}
        </footer>
      </a>
    </article>
  `;
}
