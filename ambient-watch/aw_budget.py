"""Token/USD budget — Claude Tag spend-limit parity.

Meters every ambient LLM call (fed by Hermes' post_api_request hook, which
carries token usage + model + session), converts to USD via a model price
table, and drives a decline-not-truncate gate:

    ok        under all caps — proceed
    alert75   crossed 75% of a period cap — warn ops, still proceed
    alert95   crossed 95% — warn ops, still proceed
    exceeded  a cap is reached — DECLINE (never truncate), like Claude Tag

Caps are enforced per-channel/day, global/day, and global/month, whichever
is tightest. State lives in the plugin's own sqlite ledger.
"""

from __future__ import annotations

import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS budget_usage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel    TEXT NOT NULL,
    model      TEXT,
    usd        REAL NOT NULL,
    prompt     INTEGER NOT NULL,
    completion INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_budget_chan_time ON budget_usage (channel, created_at);
CREATE TABLE IF NOT EXISTS budget_alerts (
    scope      TEXT NOT NULL,   -- "<channel>|daily" | "global|daily" | "global|monthly"
    threshold  REAL NOT NULL,
    period_key TEXT NOT NULL,   -- day or month bucket, so alerts re-arm each period
    PRIMARY KEY (scope, threshold, period_key)
);
"""

DAY = 86400
# Fallback price for an unpriced model ($/1M in, $/1M out). Deliberately not
# zero: an unpriced model must still consume budget, or it is a spend hole.
_DEFAULT_PRICE = (5.0, 15.0)


class Budget:
    def __init__(self, store, cfg: dict):
        self._store = store
        self._db = store._db
        self._lock = store._lock
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)
        self.daily_global = float(cfg.get("daily_usd_global", 0) or 0)
        self.daily_channel = float(cfg.get("daily_usd_per_channel", 0) or 0)
        self.monthly_global = float(cfg.get("monthly_usd_global", 0) or 0)
        self.thresholds = tuple(sorted(cfg.get("alert_thresholds", (0.75, 0.95))))
        self.prices = dict(cfg.get("prices", {}))

    # -- pricing ----------------------------------------------------------
    def _price(self, model: str):
        return self.prices.get(model, _DEFAULT_PRICE)

    def usd_for(self, model: str, prompt: int, completion: int) -> float:
        pin, pout = self._price(model)
        return (prompt / 1_000_000) * pin + (completion / 1_000_000) * pout

    # -- recording --------------------------------------------------------
    def record_usage(self, channel, model, prompt, completion, now=None) -> float:
        now = time.time() if now is None else now
        usd = self.usd_for(model or "", int(prompt or 0), int(completion or 0))
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO budget_usage (channel, model, usd, prompt, completion, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (channel, model, usd, int(prompt or 0), int(completion or 0), now),
            )
        return usd

    # -- rollups ----------------------------------------------------------
    def spent_usd_channel(self, channel, since) -> float:
        with self._lock:
            cur = self._db.execute(
                "SELECT COALESCE(SUM(usd),0) s FROM budget_usage"
                " WHERE channel=? AND created_at>?",
                (channel, since),
            )
            return cur.fetchone()["s"]

    def spent_usd_global(self, since) -> float:
        with self._lock:
            cur = self._db.execute(
                "SELECT COALESCE(SUM(usd),0) s FROM budget_usage WHERE created_at>?",
                (since,),
            )
            return cur.fetchone()["s"]

    # -- decision ---------------------------------------------------------
    def _ratios(self, channel, now):
        """Return (max_ratio, breached_scopes) across every configured cap."""
        day_start = now - DAY
        month_start = now - 30 * DAY
        checks = []
        if self.daily_channel > 0:
            checks.append((self.spent_usd_channel(channel, day_start) / self.daily_channel,
                           f"{channel}|daily"))
        if self.daily_global > 0:
            checks.append((self.spent_usd_global(day_start) / self.daily_global,
                           "global|daily"))
        if self.monthly_global > 0:
            checks.append((self.spent_usd_global(month_start) / self.monthly_global,
                           "global|monthly"))
        return checks

    def decision(self, channel, now=None) -> str:
        now = time.time() if now is None else now
        checks = self._ratios(channel, now)
        if not checks:
            # No cap configured anywhere. Returning "ok" here would be
            # fail-OPEN: a detector bug plus an unconfigured budget is an
            # unbounded bill, on a schedule, with no human in the loop. The
            # gate treats this verdict as a decline.
            return "unconfigured"
        top = max(r for r, _ in checks)
        if top >= 1.0:
            return "exceeded"
        if top >= 0.95:
            return "alert95"
        if top >= 0.75:
            return "alert75"
        return "ok"

    def report(self, channels, now=None) -> dict:
        """Operator-facing rollup: spend and cap ratios per scope."""
        now = time.time() if now is None else now
        day_start = now - DAY
        month_start = now - 30 * DAY
        return {
            "daily_global": (self.spent_usd_global(day_start), self.daily_global),
            "monthly_global": (self.spent_usd_global(month_start), self.monthly_global),
            "daily_channel": {
                c: (self.spent_usd_channel(c, day_start), self.daily_channel)
                for c in sorted(channels or ())
            },
        }

    # -- alerts (fire once per threshold per period) ----------------------
    def _period_key(self, scope, now) -> str:
        bucket = int(now // (30 * DAY)) if scope.endswith("monthly") else int(now // DAY)
        return str(bucket)

    def take_config_alert(self, now=None) -> bool:
        """True at most once a day while NO cap is configured.

        An unconfigured budget declines every candidate, so ambient goes
        completely silent — the exact failure mode ("a broken gate looks like
        a quiet week") the breadcrumb log exists to fight. This makes it audible
        in the ops channel too, without turning a misconfiguration into 96
        messages a day.
        """
        now = time.time() if now is None else now
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO budget_alerts (scope, threshold, period_key)"
                " VALUES ('global|config', 0.0, ?)",
                (str(int(now // DAY)),),
            )
            return bool(cur.rowcount)

    def take_pending_alert(self, channel, now=None):
        """Return the highest newly-crossed threshold to announce, or None."""
        now = time.time() if now is None else now
        fired = None
        for ratio, scope in self._ratios(channel, now):
            for thr in self.thresholds:
                if ratio >= thr:
                    pk = self._period_key(scope, now)
                    with self._lock, self._db:
                        cur = self._db.execute(
                            "INSERT OR IGNORE INTO budget_alerts (scope, threshold, period_key)"
                            " VALUES (?,?,?)",
                            (scope, thr, pk),
                        )
                        if cur.rowcount:  # newly inserted -> not yet announced
                            if fired is None or thr > fired:
                                fired = thr
        return fired
