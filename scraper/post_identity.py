"""Stable identity for a scraped league-forum post.

Shared by the scraper (to tell new posts from already-saved ones while
scrolling the feed) and the money ledger (money_left.compute_balances,
to collapse duplicate copies of one real post that are already sitting
in unique_posts.json from before a given volatile field was recognized).
Kept dependency-free so the ledger can import it without Playwright.
"""
import json
import re

# Matches every date-like token Biwenger's forum feed renders, both
# while a post is recent (relative — "Ara mateix"/"just now", "Fa 2
# hores"/"2 hours ago", "Ahir"/"yesterday") AND once it's aged into a
# fixed Catalan date ("15 d'ag.", "8 de set. 2025"). A single post's
# own timestamp text changes across ALL of these forms over its life —
# confirmed live: the identical "Fitxat per" block first seen as
# "Fa 3 hores" was seen again, unchanged otherwise, as "Ahir" the next
# day. Stripping only the relative forms (an earlier version of this
# fix) caught same-day re-scrapes but not the eventual settle into an
# absolute date, which itself then read as a "new" post — verified
# against all ~500 distinct date-like tokens actually present in
# unique_posts.json to confirm this doesn't also match real content
# (player names, amounts, team names never matched).
# Known manager renames — confirmed via league_standings.csv history that
# the two names never coexist as separate teams across the whole season,
# only ever one or the other (same team_id, same roster size/value
# trajectory, just a different display name after some date). Map old
# name -> current canonical name; add a new entry here if another rename
# is ever caught the same way (a real transaction duplicate-counted
# because a team's name differs between two scrapes of the same post).
TEAM_NAME_ALIASES = {
    'FerranGoaT': 'FreeJulian',
}

# The round-result ("Fi de Jornada") posts render their date lowercase
# and with the reaction count glued onto the same token (" fa 6 dies 1",
# " ahir") — the same real post was saved 7 times before this matched.
_DATE_TOKEN_RE = re.compile(
    r"^(Ara mateix|[Aa]vui|[Aa]hir|[Dd]emà"
    r"|[Ff]a (\d+|una?) \S+"
    r"|\d{1,2} d['’]?e?\.?\s?\S+\.?(\s\d{4})?)(\s-?\d+)?$"
)
# A bare integer, standing alone with no unit/currency/word attached, is a
# reaction/like count on that post (or on each item within a batch post) —
# never real transaction content, which is always tagged ("X licitacions",
# "€X", a name, a position code). Confirmed at scale across the real
# dataset: every one of 670 bare-integer tokens found immediately follows a
# "Venut per X€" / "Per X€" / "X licitacions" token, never appears as
# content on its own. Like a date string, this field re-renders as people
# react to a post after it's first scraped, so hashing it double/triple-
# counted real sales exactly like the timestamp did (caught live: the same
# real Bartra sale, scraped on two days with reaction counts '5' and '0',
# produced two different identities and was recorded as two separate
# sales in realized_trades). It can go negative too (a net score, not a
# raw count) — first regex version only matched bare positive digits and
# missed a real duplicate as a result: the same Mantilla/Hernan Krezzpo
# transfer, scraped once with a trailing '-2' and once with '0', produced
# two distinct identities and was double-counted in realized_trades.
_BARE_INT_RE = re.compile(r"^-?\d+$")
_POSITION_TOKENS = {"PT", "DF", "MC", "DV", "E", "DL", "F", "/", ""}

def _post_identity(post_data):
    """A stable identity for a scraped post, for deduplication across
    scrapes. Hashing the whole post text seems safe but isn't: the same
    real post's timestamp text changes every time it's re-scraped until
    it finally settles (see _DATE_TOKEN_RE), and a trailing reaction
    count changes too as people react to a post after it's first seen
    (see _BARE_INT_RE) — either was silently treated as turning one real
    post into a new one. This double/triple-counted real transactions in
    the money ledger, confirmed live against multiple teams' balances not
    matching reality. Both are dropped from the identity entirely rather
    than partially: the transaction content (type, team, player, exact
    euro amount) is already a near-unique key on its own, so the risk of
    two genuinely different transactions colliding is far smaller than
    the risk of missing another still-unrecognized volatile field down
    the line.

    A THIRD volatile field, of a completely different kind: a manager's
    team NAME itself can change mid-season (a rename), and Biwenger
    re-renders old posts with whatever name is current at scrape time —
    confirmed live, two "Inici de joc" (season-start budget credit)
    posts were byte-identical except one read "FreeJulian" where the
    other read "FerranGoaT", the same team before/after a rename. That's
    not a token this function can generically recognize and strip (it's
    an arbitrary string, not a date or a number) — but there is exactly
    one Inici de joc event per season ever, by construction, so any post
    containing it collapses to one constant identity regardless of which
    team names happen to appear in it, rather than trying to strip just
    the renamed token.
    """
    if any(isinstance(x, str) and x.strip() == 'Inici de joc' for x in post_data):
        return '"__SEASON_START_CREDIT__"'
    stable = [
        x for x in post_data
        if not (isinstance(x, str) and (_DATE_TOKEN_RE.match(x.strip()) or _BARE_INT_RE.match(x.strip())))
    ]
    # The Inici de joc case above is one symptom of a bigger issue: a
    # rename can hit ANY post, not just that one, and a batch post
    # ("MERCAT DE FITXATGES" etc.) bundles many teams' real transactions
    # together — a rename on ONE of them makes the whole post's text
    # differ, duplicate-counting every OTHER real transaction bundled
    # alongside it too, including ones that have nothing to do with the
    # renamed team. Confirmed via league_standings.csv history: FerranGoaT
    # and FreeJulian never coexist as two teams, only ever one or the
    # other — the same manager, renamed mid-season. Normalizing known
    # aliases to one canonical name before hashing fixes every post this
    # touches at once, not just the one that happened to be caught live.
    for old_name, canonical in TEAM_NAME_ALIASES.items():
        stable = [x.replace(old_name, canonical) if isinstance(x, str) else x for x in stable]
    # FOURTH volatile field: on 2026-09-22 Biwenger changed how a player row
    # renders inside a post. Before: "Yeremay","DV","/","MC","Yeremay",
    # "Fitxat per"; after: "DV","/","MC","Yeremay","Fitxat per" (name no
    # longer repeated, position badges moved). Every already-saved post
    # stopped matching its own re-scrape, so the scroll never "caught up"
    # and walked the whole feed back to 2024 (600+ "new" posts per run,
    # which also duplicated them in the ledger and made the league page
    # fail to load afterwards). Position badges and repeated tokens carry
    # no transaction content, so drop them: the identity is now the ordered
    # first occurrence of every other token, identical under both layouts.
    seen_tokens = set()
    canonical_tokens = []
    for x in stable:
        if isinstance(x, str) and x.strip() in _POSITION_TOKENS:
            continue
        key = x.strip() if isinstance(x, str) else x
        if key in seen_tokens:
            continue
        seen_tokens.add(key)
        canonical_tokens.append(x)
    return json.dumps(canonical_tokens, ensure_ascii=False)
