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
  renderTable(tbody, data, 8, (p) => `
    <tr>
      <td><span class="pos-badge">${escapeHtml(p.position)}</span></td>
      <td class="cell-muted">${escapeHtml(p.club)}</td>
      <td class="cell-primary">${escapeHtml(p.name)} ${probabilityPill(p.probability)}</td>
      <td class="num">${formatMoney(p.price)}</td>
      <td class="num">${formatDelta(p.change, { hideZero: true })}</td>
      <td class="num cell-muted">${escapeHtml(p.demand)}</td>
      <td class="num">${escapeHtml(p.this_season_pts)}</td>
      <td class="num cell-muted">${escapeHtml(p.last_season_pts)}</td>
    </tr>
  `);
}

function renderTeamValuations(data) {
  const tbody = document.querySelector('#teamsTable tbody');
  renderTable(tbody, data, 8, (t) => {
    const pos = t.positions || {};
    return `
    <tr class="${t.is_me ? 'row-me' : ''}">
      <td class="cell-primary">${escapeHtml(t.team)}${t.is_me ? ' <span class="pill pill-me">You</span>' : ''}</td>
      <td class="num cell-muted">${escapeHtml(t.players)}</td>
      <td class="num cell-muted">${escapeHtml(pos.GK ?? '—')}</td>
      <td class="num cell-muted">${escapeHtml(pos.DEF ?? '—')}</td>
      <td class="num cell-muted">${escapeHtml(pos.MID ?? '—')}</td>
      <td class="num cell-muted">${escapeHtml(pos.FWD ?? '—')}</td>
      <td class="num">${formatMoney(t.total_value)}</td>
      <td class="num">${formatBalance(t.balance)}</td>
    </tr>
  `;
  });
}

function renderTeamPlayers(data) {
  const tbody = document.querySelector('#teamPlayersTable tbody');
  renderTable(tbody, data, 7, (p) => `
    <tr>
      <td><span class="pos-badge">${escapeHtml(p.position)}</span></td>
      <td class="cell-muted">${escapeHtml(p.club)}</td>
      <td class="cell-primary">${escapeHtml(p.name)} ${probabilityPill(p.probability)}</td>
      <td class="num">${formatMoney(p.price)}</td>
      <td class="num">${escapeHtml(p.this_season_pts)}</td>
      <td class="num cell-muted">${formatNumber(p.points_per_match)}</td>
      <td>${statusPill(p.status)}</td>
    </tr>
  `);
}

// ---------- App state & data loading ----------

const state = {
  dates: window.__availableDates || [],
  teams: window.__availableTeams || [],
  date: window.__selectedDate || null,
  team: window.__selectedTeam || null,
};

if (!state.date && state.dates.length) state.date = state.dates[0];
if (!state.team && state.teams.length) state.team = state.teams[0].team_id;

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
  if (!state.date) return;

  document.getElementById('currentDate').textContent = state.date;
  const teamMeta = state.teams.find((t) => t.team_id === state.team);
  document.getElementById('currentTeam').textContent = teamMeta ? teamMeta.team_name : 'All teams';

  let url = `/api/data?date=${encodeURIComponent(state.date)}`;
  if (state.team) url += `&team=${encodeURIComponent(state.team)}`;

  setLoading(true);
  showError(null);

  fetch(url)
    .then((response) => {
      if (!response.ok) throw new Error(`Server returned ${response.status}`);
      return response.json();
    })
    .then((data) => {
      if (data.error) throw new Error(data.error);
      renderStandings(data.standings);
      renderMarket(data.market);
      renderTeamValuations(data.teams);
      renderTeamPlayers(data.team_players);
    })
    .catch((err) => {
      showError(`Couldn't load data: ${err.message}`);
    })
    .finally(() => setLoading(false));
}

function navigateDate(offset) {
  if (!state.dates.length) return;
  const idx = state.dates.indexOf(state.date);
  let newIdx = (idx === -1 ? 0 : idx) + offset;
  newIdx = Math.max(0, Math.min(state.dates.length - 1, newIdx));
  state.date = state.dates[newIdx];
  if (window.__datePicker) window.__datePicker.setDate(state.date);
  loadData();
}

document.addEventListener('DOMContentLoaded', () => {
  const teamSelect = document.getElementById('teamSelect');
  state.teams.forEach((team) => {
    const option = document.createElement('option');
    option.value = team.team_id;
    option.textContent = team.team_name;
    if (team.team_id === state.team) option.selected = true;
    teamSelect.appendChild(option);
  });
  teamSelect.addEventListener('change', function () {
    state.team = this.value;
    loadData();
  });

  window.__datePicker = flatpickr('#datePicker', {
    dateFormat: 'Y-m-d',
    defaultDate: state.date,
    enable: state.dates.length ? state.dates : undefined,
    onChange: (_dates, dateStr) => {
      if (dateStr) {
        state.date = dateStr;
        loadData();
      }
    },
  });

  document.querySelector('.prev-date').addEventListener('click', () => navigateDate(-1));
  document.querySelector('.next-date').addEventListener('click', () => navigateDate(1));

  if (state.date) {
    loadData();
  } else {
    showError('No scraped data yet — run the scraper and migration first.');
  }
});
