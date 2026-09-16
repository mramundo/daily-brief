export const LOCALE = 'en-US';

export const $ = (sel, root = document) => root.querySelector(sel);

export const escapeHTML = (str = '') => String(str)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

const format = (iso, options) => {
  if (!iso) return '';
  try {
    return new Intl.DateTimeFormat(LOCALE, options).format(new Date(iso));
  } catch { return ''; }
};

export const fmtDateLong = (iso) =>
  format(iso, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });

export const fmtDateTime = (iso) =>
  format(iso, { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });

export const fmtDayMonth = (iso) => format(iso, { day: 'numeric', month: 'short' });
