// ---------- Formatting helpers ----------

function formatMoney(num) {
  if (num === undefined || num === null || num === '') return '—';
  const n = parseFloat(num);
  if (Number.isNaN(n)) return '—';
  return '€' + n.toLocaleString('en-US', { maximumFractionDigits: 0 });
}

function formatNumber(num) {
  if (num === undefined || num === null || num === '') return '—';
  const n = parseFloat(num);
  if (Number.isNaN(n)) return '—';
  return n.toLocaleString('en-US', { maximumFractionDigits: 1 });
}

// A delta (day-over-day price change) — sign is the whole point, so show
// an explicit +/-.
function formatDelta(num, { hideZero = false } = {}) {
  if (num === undefined || num === null || num === '') return '—';
  const n = parseFloat(num);
  if (Number.isNaN(n)) return '—';
  if (n === 0) return hideZero ? '<span class="cell-muted">—</span>' : '€0';
  const cls = n > 0 ? 'money-pos' : 'money-neg';
  const sign = n > 0 ? '+' : '−';
  const abs = Math.abs(n).toLocaleString('en-US', { maximumFractionDigits: 0 });
  return `<span class="${cls}">${sign}€${abs}</span>`;
}

// An absolute balance — color communicates sign (red = in the red), but
// no +/- prefix since it isn't a delta.
function formatBalance(num) {
  if (num === undefined || num === null || num === '') return '—';
  const n = parseFloat(num);
  if (Number.isNaN(n)) return '—';
  const cls = n >= 0 ? 'money-pos' : 'money-neg';
  const sign = n < 0 ? '−' : '';
  const abs = Math.abs(n).toLocaleString('en-US', { maximumFractionDigits: 0 });
  return `<span class="${cls}">${sign}€${abs}</span>`;
}

function escapeHtml(str) {
  if (str === undefined || str === null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function statusPill(status) {
  if (!status) return '';
  const s = status.toLowerCase();
  let cls = 'pill-neutral';
  if (s.includes('fit') || s.includes('en forma')) cls = 'pill-fit';
  else if (s.includes('injur') || s.includes('lesion')) cls = 'pill-injured';
  else if (s.includes('doubt') || s.includes('dubte')) cls = 'pill-doubt';
  // Status can be a full sentence with injury detail ("Injured. ACL tear.
  // Estimated return: Early October.") — show just the short label as the
  // pill and put the full text in a tooltip instead of blowing out the row.
  const shortLabel = status.split('.')[0].trim();
  return `<span class="pill ${cls}" title="${escapeHtml(status)}">${escapeHtml(shortLabel)}</span>`;
}

function probabilityPill(probability) {
  if (!probability || probability === '0%') return '';
  const pct = parseInt(probability, 10);
  let cls = 'prob-low';
  if (pct > 66) cls = 'prob-high';
  else if (pct > 33) cls = 'prob-mid';
  return `<span class="pill ${cls}">${escapeHtml(probability)}</span>`;
}

function rankBadge(pos) {
  const n = parseInt(pos, 10);
  const cls = n === 1 ? 'rank-1' : n === 2 ? 'rank-2' : n === 3 ? 'rank-3' : '';
  return `<span class="rank ${cls}">${escapeHtml(pos)}</span>`;
}

// ---------- Table rendering ----------

function renderTable(tbodyEl, data, colCount, rowFn) {
  if (!data || data.length === 0) {
    tbodyEl.innerHTML = `<tr class="empty-row"><td colspan="${colCount}">No data available</td></tr>`;
    return;
  }
  tbodyEl.innerHTML = data.map(rowFn).join('');
}

function renderStandings(data) {
  const tbody = document.querySelector('#standingsTable tbody');
  renderTable(tbody, data, 3, (team) => `
    <tr class="${team.is_me ? 'row-me' : ''}">
      <td>${rankBadge(team.pos)}</td>
      <td class="cell-primary">${escapeHtml(team.team)}${team.is_me ? ' <span class="pill pill-me">You</span>' : ''}</td>
      <td class="num">${escapeHtml(team.points)}</td>
    </tr>
  `);
}

function renderMarket(data) {
  const tbody = document.querySelector('#marketTable tbody');
  renderTable(tbody, data, 9, (p) => `
    <tr>
      <td><span class="pos-badge">${escapeHtml(p.position)}</span></td>
      <td class="cell-muted">${escapeHtml(p.club)}</td>
      <td class="cell-primary">${escapeHtml(p.name)} ${probabilityPill(p.probability)}</td>
      <td class="num">${formatMoney(p.price)}</td>
      <td class="num">${formatDelta(p.change, { hideZero: true })}</td>
      <td>${statusPill(p.status)}</td>
      <td class="num">${escapeHtml(p.this_season_pts)}</td>
      <td class="num cell-muted">${escapeHtml(p.recent_pts)}</td>
      <td class="num cell-muted">${escapeHtml(p.last_season_pts)}</td>
    </tr>
  `);
}

function renderHoldings(data) {
  const tbody = document.querySelector('#holdingsTable tbody');
  renderTable(tbody, data, 6, (p) => `
    <tr>
      <td><span class="pos-badge">${escapeHtml(p.position)}</span></td>
      <td class="cell-muted">${escapeHtml(p.club)}</td>
      <td class="cell-primary">${escapeHtml(p.player)}</td>
      <td class="num">${formatMoney(p.buy_price)}</td>
      <td class="num">${formatMoney(p.current_price)}</td>
      <td class="num">${formatDelta(p.profit)}</td>
    </tr>
  `);
}

function renderSales(data) {
  const tbody = document.querySelector('#salesTable tbody');
  renderTable(tbody, data, 4, (p) => `
    <tr>
      <td class="cell-primary">${escapeHtml(p.player)}</td>
      <td class="num">${formatMoney(p.buy_price)}</td>
      <td class="num">${formatMoney(p.sell_price)}</td>
      <td class="num">${formatDelta(p.profit)}</td>
    </tr>
  `);
}

// Team valuations doubles as the roster browser: click a team row to
// expand its squad inline instead of a separate dropdown-driven section.
function renderRosterRows(players) {
  if (!players || players.length === 0) {
    return `<tr><td colspan="8" class="roster-empty">No roster data</td></tr>`;
  }
  const head = `
    <tr class="roster-head">
      <th>Pos</th><th>Club</th><th>Name</th>
      <th class="num">Price</th><th class="num">Change</th><th class="num">Points</th>
      <th class="num">Pts/match</th><th>Status</th>
    </tr>`;
  const rows = players.map((p) => `
    <tr>
      <td><span class="pos-badge">${escapeHtml(p.position)}</span></td>
      <td class="cell-muted">${escapeHtml(p.club)}</td>
      <td class="cell-primary">${escapeHtml(p.name)} ${probabilityPill(p.probability)}</td>
      <td class="num">${formatMoney(p.price)}</td>
      <td class="num">${formatDelta(p.change, { hideZero: true })}</td>
      <td class="num">${escapeHtml(p.this_season_pts)}</td>
      <td class="num cell-muted">${formatNumber(p.points_per_match)}</td>
      <td>${statusPill(p.status)}</td>
    </tr>
  `).join('');
  return head + rows;
}

function renderTeamValuations(teams, rostersByTeamId) {
  const tbody = document.querySelector('#teamsTable tbody');
  if (!teams || teams.length === 0) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="9">No data available</td></tr>`;
    return;
  }
  tbody.innerHTML = teams.map((t) => {
    const pos = t.positions || {};
    const roster = rostersByTeamId[t.team_id] || [];
    return `
    <tr class="team-row ${t.is_me ? 'row-me' : ''}" data-team-id="${escapeHtml(t.team_id)}">
      <td class="cell-primary"><span class="expand-chevron">▸</span> ${escapeHtml(t.team)}${t.is_me ? ' <span class="pill pill-me">You</span>' : ''}</td>
      <td class="num cell-muted">${escapeHtml(t.players)}</td>
      <td class="num cell-muted">${escapeHtml(pos.GK ?? '—')}</td>
      <td class="num cell-muted">${escapeHtml(pos.DEF ?? '—')}</td>
      <td class="num cell-muted">${escapeHtml(pos.MID ?? '—')}</td>
      <td class="num cell-muted">${escapeHtml(pos.FWD ?? '—')}</td>
      <td class="num">${formatMoney(t.total_value)}</td>
      <td class="num">${formatDelta(t.value_change, { hideZero: true })}</td>
      <td class="num">${formatBalance(t.balance)}</td>
    </tr>
    <tr class="roster-detail" hidden>
      <td colspan="9">
        <table class="roster-table">${renderRosterRows(roster)}</table>
      </td>
    </tr>
  `;
  }).join('');

  tbody.querySelectorAll('tr.team-row').forEach((row) => {
    row.addEventListener('click', () => {
      const detail = row.nextElementSibling;
      const collapsing = !detail.hidden;
      detail.hidden = collapsing;
      row.classList.toggle('expanded', !collapsing);
    });
  });
}

function groupRostersByTeam(teamPlayers) {
  const grouped = {};
  for (const p of teamPlayers || []) {
    (grouped[p.team_id] = grouped[p.team_id] || []).push(p);
  }
  return grouped;
}

// ---------- App state & data loading ----------
// Always shows the most recent scrape — no date navigation. A picker over
// static daily snapshots wasn't actually useful without trend context;
// that's better served by real trend charts later than by flipping
// between raw snapshots.

const state = {
  date: window.__selectedDate || (window.__availableDates || [])[0] || null,
};

function setLoading(isLoading) {
  document.body.classList.toggle('is-loading', isLoading);
}

function showError(message) {
  const el = document.getElementById('errorBanner');
  if (!message) {
    el.style.display = 'none';
    el.textContent = '';
    return;
  }
  el.style.display = 'block';
  el.textContent = message;
}

function loadData() {
  if (!state.date) {
    showError('No scraped data yet — run the scraper and migration first.');
    return;
  }

  document.getElementById('currentDate').textContent = state.date;

  setLoading(true);
  showError(null);

  fetch(`/api/data?date=${encodeURIComponent(state.date)}`)
    .then((response) => {
      if (!response.ok) throw new Error(`Server returned ${response.status}`);
      return response.json();
    })
    .then((data) => {
      if (data.error) throw new Error(data.error);
      renderStandings(data.standings);
      renderMarket(data.market);
      renderTeamValuations(data.teams, groupRostersByTeam(data.team_players));
      renderHoldings(data.my_holdings);
      renderSales(data.my_sales);
    })
    .catch((err) => {
      showError(`Couldn't load data: ${err.message}`);
    })
    .finally(() => setLoading(false));
}

// ---------- Tabs ----------

function activateTab(name) {
  document.querySelectorAll('.tab').forEach((btn) => {
    const isActive = btn.dataset.tab === name;
    btn.classList.toggle('active', isActive);
    btn.setAttribute('aria-selected', String(isActive));
  });
  document.querySelectorAll('.tab-panel').forEach((panel) => {
    panel.hidden = panel.dataset.tabPanel !== name;
  });
  if (history.replaceState) history.replaceState(null, '', `#${name}`);
}

function initTabs() {
  const tabNames = Array.from(document.querySelectorAll('.tab')).map((b) => b.dataset.tab);
  const fromHash = window.location.hash.replace('#', '');
  activateTab(tabNames.includes(fromHash) ? fromHash : tabNames[0]);

  document.querySelectorAll('.tab').forEach((btn) => {
    btn.addEventListener('click', () => activateTab(btn.dataset.tab));
  });
}

document.addEventListener('DOMContentLoaded', () => {
  initTabs();
  loadData();
});
