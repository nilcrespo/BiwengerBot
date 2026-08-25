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

// Unlike probabilityPill (an inline annotation next to a name, where 0%
// is noise worth hiding), this is a dedicated table column — 0% is a real
// value there, not an absence of data, so it must render.
function startPctCell(probability) {
  if (probability === undefined || probability === null || probability === '') return '—';
  const pct = parseInt(probability, 10);
  if (Number.isNaN(pct)) return '—';
  let cls = 'prob-low';
  if (pct > 66) cls = 'prob-high';
  else if (pct > 33) cls = 'prob-mid';
  return `<span class="pill ${cls}">${escapeHtml(probability)}</span>`;
}

function scoreBadge(score) {
  if (score === undefined || score === null || score === '') return '—';
  const n = parseFloat(score);
  if (Number.isNaN(n)) return '—';
  let cls = 'prob-low';
  if (n >= 60) cls = 'prob-high';
  else if (n >= 35) cls = 'prob-mid';
  return `<span class="pill ${cls}">${n.toFixed(1)}</span>`;
}

function needBadge(need) {
  if (!need) return '';
  return `<span class="pill pill-me">${escapeHtml(need)}</span>`;
}

function rankBadge(pos) {
  const n = parseInt(pos, 10);
  const cls = n === 1 ? 'rank-1' : n === 2 ? 'rank-2' : n === 3 ? 'rank-3' : '';
  return `<span class="rank ${cls}">${escapeHtml(pos)}</span>`;
}

// ---------- Sorting ----------
// Every table is sortable by clicking any column header — data is kept
// in memory (tableState) so re-sorting never needs a refetch, and each
// header's data-sort attribute names the exact field to sort by
// (supports "a.b" for nested fields, e.g. teams' positions.GK).

function getByPath(obj, path) {
  return path.split('.').reduce((o, k) => (o === null || o === undefined ? undefined : o[k]), obj);
}

// Values arrive as raw JSON (numbers, null) or already-formatted strings
// ("83%", "Fit") depending on the field — strip the noise so both sort
// numerically when they're numeric at heart.
function parseSortValue(v) {
  if (v === null || v === undefined || v === '') return null;
  if (typeof v === 'number') return v;
  if (typeof v === 'boolean') return v ? 1 : 0;
  const stripped = String(v).replace(/[€,%]/g, '').trim();
  if (stripped !== '' && !Number.isNaN(Number(stripped))) return Number(stripped);
  return String(v).toLowerCase();
}

function sortData(data, key, dir) {
  const mult = dir === 'asc' ? 1 : -1;
  return [...data].sort((a, b) => {
    const av = parseSortValue(getByPath(a, key));
    const bv = parseSortValue(getByPath(b, key));
    if (av === null && bv === null) return 0;
    if (av === null) return 1;   // nulls always sort last, regardless of direction
    if (bv === null) return -1;
    if (av < bv) return -1 * mult;
    if (av > bv) return 1 * mult;
    return 0;
  });
}

const tableState = {};

function updateSortIndicators(tableId) {
  const st = tableState[tableId];
  document.querySelectorAll(`#${tableId} thead th[data-sort]`).forEach((th) => {
    th.classList.remove('sort-asc', 'sort-desc');
    if (th.dataset.sort === st.sortKey) th.classList.add(st.sortDir === 'asc' ? 'sort-asc' : 'sort-desc');
  });
}

function applySort(tableId) {
  const st = tableState[tableId];
  const sorted = st.sortKey ? sortData(st.data, st.sortKey, st.sortDir) : st.data;
  st.renderFn(sorted);
  updateSortIndicators(tableId);
}

// renderFn takes the (already sorted) data array and draws the tbody.
function registerSortable(tableId, renderFn, defaultKey, defaultDir = 'desc') {
  tableState[tableId] = { data: [], sortKey: defaultKey, sortDir: defaultDir, renderFn };
  document.querySelectorAll(`#${tableId} thead th[data-sort]`).forEach((th) => {
    th.addEventListener('click', () => {
      const st = tableState[tableId];
      const key = th.dataset.sort;
      st.sortDir = st.sortKey === key && st.sortDir === 'asc' ? 'desc' : (st.sortKey === key ? 'asc' : 'desc');
      st.sortKey = key;
      applySort(tableId);
    });
  });
}

function setTableData(tableId, data) {
  tableState[tableId].data = data || [];
  applySort(tableId);
}

// Roster tables are a special case: one instance per team, created fresh
// each time an accordion row expands, so sort state is tracked per team
// id instead of per (static) table id, and re-sorting re-renders just
// that team's rows via delegated clicks rather than a registered table.
const rosterSortState = {};

function rosterTableHtml(teamId, players) {
  const st = rosterSortState[teamId] || { key: 'price', dir: 'desc' };
  const sorted = st.key ? sortData(players, st.key, st.dir) : players;
  return `<table class="roster-table" data-team-id="${escapeHtml(teamId)}">${renderRosterRows(sorted, st)}</table>`;
}

document.addEventListener('click', (e) => {
  const th = e.target.closest('.roster-table th[data-sort]');
  if (!th) return;
  const table = th.closest('table.roster-table');
  const teamId = table.dataset.teamId;
  const key = th.dataset.sort;
  const prev = rosterSortState[teamId] || { key: 'price', dir: 'desc' };
  const dir = prev.key === key && prev.dir === 'asc' ? 'desc' : (prev.key === key ? 'asc' : 'desc');
  rosterSortState[teamId] = { key, dir };
  const players = (state.rostersByTeamId || {})[teamId] || [];
  table.outerHTML = rosterTableHtml(teamId, players);
});

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

function sumBy(data, key) {
  return (data || []).reduce((total, row) => total + (parseFloat(row[key]) || 0), 0);
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
  if (data && data.length) {
    tbody.insertAdjacentHTML('beforeend', `
      <tr class="total-row">
        <td colspan="3" class="cell-primary">Total</td>
        <td class="num cell-primary">${formatMoney(sumBy(data, 'buy_price'))}</td>
        <td class="num cell-primary">${formatMoney(sumBy(data, 'current_price'))}</td>
        <td class="num">${formatDelta(sumBy(data, 'profit'))}</td>
      </tr>
    `);
  }
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
  if (data && data.length) {
    tbody.insertAdjacentHTML('beforeend', `
      <tr class="total-row">
        <td class="cell-primary">Total</td>
        <td class="num cell-primary">${formatMoney(sumBy(data, 'buy_price'))}</td>
        <td class="num cell-primary">${formatMoney(sumBy(data, 'sell_price'))}</td>
        <td class="num">${formatDelta(sumBy(data, 'profit'))}</td>
      </tr>
    `);
  }
}

// round_scores.position is Biwenger's own small-int code (see
// scraper.py's BIWENGER_POSITIONS), not the "Forward"/"Defender" text
// every other table already has — this table is the one place in the
// dashboard that needs to resolve it.
const ROUND_SCORE_POSITIONS = { 1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD', 5: 'Coach' };

function roundScorePointsCell(p) {
  if (p.points === null || p.points === undefined) {
    // Matches the site's own notation: "?" while the fixture hasn't
    // kicked off, "–" once it's finished without him (a real DNP).
    return p.game_status === 'finished' ? '–' : '?';
  }
  return escapeHtml(p.points);
}

function renderRoundScores(data) {
  const tbody = document.querySelector('#roundScoresTable tbody');
  renderTable(tbody, data, 7, (p) => `
    <tr>
      <td>${escapeHtml(p.round_name)}</td>
      <td class="cell-muted">${escapeHtml(p.team)}</td>
      <td class="cell-primary">${escapeHtml(p.player)}</td>
      <td><span class="pos-badge">${escapeHtml(ROUND_SCORE_POSITIONS[p.position] || p.position)}</span></td>
      <td class="cell-muted">${escapeHtml(p.club)}</td>
      <td>${escapeHtml(p.lineup_slot)}</td>
      <td class="num">${roundScorePointsCell(p)}</td>
    </tr>
  `);
}

// Not a generic sortable table like the rest of the dashboard — a
// starting lineup has a natural order (goalkeeper first, then outfield
// by role) that a click-to-sort column header would only scramble, and
// there's exactly one row per squad slot rather than an open-ended list.
function renderBestEleven(payload) {
  const tbody = document.querySelector('#bestElevenTable tbody');
  const meta = document.getElementById('bestElevenMeta');
  const starters = (payload && payload.starters) || [];
  if (!starters.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="5">Not enough squad data to fill a valid formation</td></tr>';
    meta.textContent = 'Highest-scoring valid formation from your own squad';
    return;
  }
  meta.textContent = `Formation ${payload.formation} — ${formatNumber(payload.total_expected_points)} expected pts this round`;
  tbody.innerHTML = starters.map((p) => `
    <tr>
      <td><span class="pos-badge">${escapeHtml(p.position)}</span></td>
      <td class="cell-muted">${escapeHtml(p.club)}</td>
      <td class="cell-primary">${escapeHtml(p.player)}${p.is_captain ? ' <span class="pill pill-me">C</span>' : ''}</td>
      <td class="num">${escapeHtml(p.start_pct)}%</td>
      <td class="num">${formatNumber(p.expected_value)}</td>
    </tr>
  `).join('');
}

function affordabilityCell(p) {
  const shortfall = parseFloat(p.shortfall);
  if (!shortfall || shortfall <= 0) {
    return '<span class="pill prob-high">✅ In balance</span>';
  }
  if (p.funding_leaves_squad_thin) {
    const title = `Short ${formatMoney(shortfall)} — covering it would require selling enough players `
      + `(${p.funding_players_sold}, worth ${p.funding_points_given_up.toFixed(0)} pts of output) to leave the squad `
      + `unable to field a full lineup. Already factored into the score above.`;
    return `<span class="pill prob-low" title="${escapeHtml(title)}">⚠️ Would gut the squad</span>`;
  }
  const funded = p.funded_without_hard_choices;
  const cls = funded ? 'prob-mid' : 'prob-low';
  const label = funded ? '⚠️ Needs a sale' : '⚠️ Short even after sells';
  const costNote = p.funding_players_sold
    ? ` (${p.funding_players_sold} player${p.funding_players_sold === 1 ? '' : 's'}, ~${p.funding_points_given_up.toFixed(0)} pts of output given up — already factored into the score)`
    : '';
  const title = p.funding_plan
    ? `Short ${formatMoney(shortfall)} of balance — would need to sell: ${p.funding_plan}${costNote}`
    : `Short ${formatMoney(shortfall)} of balance — no sell candidates available to cover it today`;
  return `<span class="pill ${cls}" title="${escapeHtml(title)}">${label}</span>`;
}

// Biwenger's market is a first-price sealed-bid auction (winner pays
// exactly what they bid — confirmed on 9/9 real transactions), so "the
// average price" is the wrong number to show: every euro bid above the
// true minimum needed to win is pure waste. Show the minimum bid for a
// coin-flip chance instead. On a cheap listing this can round to the
// same figure as the 90%-safe number — show one number rather than
// "€X – €X" in that case.
function formatBidRange(p) {
  const win50 = formatMoney(p.win_bid_50);
  const win90 = formatMoney(p.win_bid_90);
  return win50 === win90 ? win50 : `${win50} (safe: ${win90})`;
}

function renderBuyRecommendations(data) {
  const tbody = document.querySelector('#buyTable tbody');
  renderTable(tbody, data, 9, (p) => {
    const bucketNote = p.bucket_avg_bids
      ? `${p.bucket_sample} historical signings in the ${p.bid_bucket} range drew ${parseFloat(p.bucket_avg_bids).toFixed(1)} bids on average`
      : 'no historical signings in this price range yet';
    const change = parseFloat(p.change);
    const momentumNote = change > 0
      ? ` + today's own +${formatMoney(change)} rise (a fast riser tends to keep rising and draw more competition)`
      : ' — but the price is flat or falling, so that average competition doesn\'t really apply right now; only a small cushion above market price is added';
    const rivalNote = p.competitor_count
      ? `\n${p.competitor_count} rival(s) could afford him and have room at his position`
        + (p.top_competitors ? `, most likely ${p.top_competitors}` : '')
        + ` → ~${p.expected_bidders} bidders expected.`
      : '\nNo rival looks able to afford him out of the squad-value credit line we can see.';
    const calibNote = p.bid_calibration_samples
      ? ` Each competing bidder is worth ~${(parseFloat(p.markup_per_bidder) * 100).toFixed(1)}% over asking, from ${p.bid_calibration_samples} real auction(s).`
      : ' No real auction data captured yet — using the default per-bidder premium.';
    const bidTitle = `${formatMoney(p.win_bid_50)} for roughly a coin-flip chance of winning, `
      + `${formatMoney(p.win_bid_90)} to be pretty confident.\n`
      + `${bucketNote}${momentumNote}${rivalNote}${calibNote}`;
    return `
    <tr>
      <td><span class="pos-badge">${escapeHtml(p.position)}</span></td>
      <td class="cell-muted">${escapeHtml(p.club)}</td>
      <td class="cell-primary">${escapeHtml(p.name)}</td>
      <td class="num">${formatMoney(p.price)}</td>
      <td class="num">${startPctCell(p.probability)}</td>
      <td class="num">${scoreBadge(p.score)}</td>
      <td>${needBadge(p.squad_need)}</td>
      <td class="num" title="${escapeHtml(bidTitle)}">${formatBidRange(p)}</td>
      <td>${affordabilityCell(p)}</td>
    </tr>
  `;
  });
}

function offerCell(p) {
  if (!p.offer_price) return '<span class="cell-muted">—</span>';
  const premium = parseFloat(p.offer_premium_pct);
  let cls = 'prob-mid';
  let title = 'Roughly fair value — no strong reason to rush or wait';
  if (p.offer_is_generous) {
    cls = 'prob-high';
    title = `Beats current market price by ${premium.toFixed(1)}% — worth grabbing even if you weren't already planning to sell`;
  } else if (p.offer_is_lowball) {
    cls = 'prob-low';
    title = `${Math.abs(premium).toFixed(1)}% below current market price — consider waiting for a better one instead of taking this`;
  }
  return `<span class="pill ${cls}" title="${escapeHtml(title)}">${formatMoney(p.offer_price)}</span>`;
}

function renderSellRecommendations(data) {
  const tbody = document.querySelector('#sellTable tbody');
  renderTable(tbody, data, 9, (p) => `
    <tr>
      <td><span class="pos-badge">${escapeHtml(p.position)}</span></td>
      <td class="cell-muted">${escapeHtml(p.club)}</td>
      <td class="cell-primary">${escapeHtml(p.player)}</td>
      <td class="num">${formatMoney(p.current_price)}</td>
      <td class="num">${p.profit === null || p.profit === undefined ? '<span class="cell-muted">—</span>' : formatDelta(p.profit)}</td>
      <td class="num">${offerCell(p)}</td>
      <td class="num">${startPctCell(p.probability)}</td>
      <td>${statusPill(p.status)}</td>
      <td class="num">${scoreBadge(p.score)}</td>
    </tr>
  `);

  // The offers feature is easy to mistake for broken/missing when it's
  // just quiet — say outright whether any live offers exist right now
  // instead of leaving the whole Offer column blank with no explanation.
  const offerCount = (data || []).filter((p) => p.offer_price).length;
  const meta = document.getElementById('sellMeta');
  if (meta) {
    meta.textContent = offerCount > 0
      ? `Your roster, ranked by bench risk and banked profit — ${offerCount} live offer${offerCount === 1 ? '' : 's'} right now`
      : 'Your roster, ranked by bench risk and banked profit — no pending offers on your players right now';
  }
}

// Team valuations doubles as the roster browser: click a team row to
// expand its squad inline instead of a separate dropdown-driven section.
function renderRosterRows(players, sortState) {
  const sortCls = (key) => (sortState && sortState.key === key ? `sort-${sortState.dir}` : '');
  const head = `
    <tr class="roster-head">
      <th data-sort="position" class="${sortCls('position')}">Pos</th>
      <th data-sort="club" class="${sortCls('club')}">Club</th>
      <th data-sort="name" class="${sortCls('name')}">Name</th>
      <th class="num ${sortCls('price')}" data-sort="price">Price</th>
      <th class="num ${sortCls('change')}" data-sort="change">Change</th>
      <th class="num ${sortCls('this_season_pts')}" data-sort="this_season_pts">Points</th>
      <th class="num ${sortCls('points_per_match')}" data-sort="points_per_match">Pts/match</th>
      <th data-sort="status" class="${sortCls('status')}">Status</th>
    </tr>`;
  if (!players || players.length === 0) {
    return head + `<tr><td colspan="8" class="roster-empty">No roster data</td></tr>`;
  }
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
    tbody.innerHTML = `<tr class="empty-row"><td colspan="10">No data available</td></tr>`;
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
      <td class="num cell-muted">${formatMoney(t.max_bid)}</td>
    </tr>
    <tr class="roster-detail" hidden>
      <td colspan="10">
        ${rosterTableHtml(t.team_id, roster)}
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

// renderTeamValuations needs rostersByTeamId as a second argument, unlike
// every other sortable table's single-arg renderFn(data) — this wrapper
// closes over the current state so registerSortable's uniform signature
// still works for it.
function renderTeamsWrapper(sortedTeams) {
  renderTeamValuations(sortedTeams, state.rostersByTeamId || {});
}

// ---------- App state & data loading ----------
// Always shows the most recent scrape — no date navigation. A picker over
// static daily snapshots wasn't actually useful without trend context;
// that's better served by real trend charts later than by flipping
// between raw snapshots.

const state = {
  date: window.__selectedDate || (window.__availableDates || [])[0] || null,
  rostersByTeamId: {},
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

  // On the static (GitHub Pages) export there's no Flask backend to hit
  // — the daily Action freezes today's snapshot to this file instead.
  // window.__staticMode is only set in that exported page.
  const dataUrl = window.__staticMode
    ? 'data.json'
    : `/api/data?date=${encodeURIComponent(state.date)}`;

  fetch(dataUrl)
    .then((response) => {
      if (!response.ok) throw new Error(`Server returned ${response.status}`);
      return response.json();
    })
    .then((data) => {
      if (data.error) throw new Error(data.error);
      state.rostersByTeamId = groupRostersByTeam(data.team_players);
      setTableData('standingsTable', data.standings);
      setTableData('marketTable', data.market);
      setTableData('teamsTable', data.teams);
      setTableData('holdingsTable', data.my_holdings);
      setTableData('salesTable', data.my_sales);
      setTableData('buyTable', data.buy_recommendations);
      setTableData('sellTable', data.sell_recommendations);
      setTableData('roundScoresTable', data.round_scores);
      renderBestEleven(data.best_eleven);
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

function initSortableTables() {
  registerSortable('standingsTable', renderStandings, 'pos', 'asc');
  // Explicit default: biggest value gains first, so the market tab opens
  // on "what's rising" rather than an arbitrary price ordering.
  registerSortable('marketTable', renderMarket, 'change', 'desc');
  registerSortable('teamsTable', renderTeamsWrapper, 'total_value', 'desc');
  registerSortable('holdingsTable', renderHoldings, 'profit', 'desc');
  registerSortable('salesTable', renderSales, 'profit', 'desc');
  registerSortable('buyTable', renderBuyRecommendations, 'score', 'desc');
  registerSortable('sellTable', renderSellRecommendations, 'score', 'desc');
  // Free agents get a row too (any La Liga player with a match report),
  // not just owned squad players — defaulting to top scorers first
  // surfaces something interesting on load instead of an alphabetical
  // wall dominated by team=null rows.
  registerSortable('roundScoresTable', renderRoundScores, 'points', 'desc');
}

document.addEventListener('DOMContentLoaded', () => {
  initTabs();
  initSortableTables();
  loadData();
});
