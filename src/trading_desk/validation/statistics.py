from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Sequence

from trading_desk.validation.metrics import TradeRecord, as_decimal, profit_factor

EULER_GAMMA = 0.5772156649015329


def _norm_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _norm_ppf(probability: float) -> float:
    if probability <= 0.0:
        return -math.inf
    if probability >= 1.0:
        return math.inf
    low, high = -1.0, 1.0
    while _norm_cdf(low) > probability:
        low *= 2.0
    while _norm_cdf(high) < probability:
        high *= 2.0
    for _ in range(80):
        mid = 0.5 * (low + high)
        if _norm_cdf(mid) < probability:
            low = mid
        else:
            high = mid
    return 0.5 * (low + high)


def interpolated_percentile(values: Sequence[Decimal], probability: float) -> Decimal:
    if not values:
        return Decimal("0")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * probability
    left = math.floor(index)
    right = math.ceil(index)
    if left == right:
        return ordered[left]
    weight = Decimal(str(index - left))
    return ordered[left] * (Decimal("1") - weight) + ordered[right] * weight


def trade_entry_moving_block_lower_bound(
    trades: Sequence[TradeRecord],
    *,
    block_days: int,
    resamples: int,
    seed: int,
    confidence: float,
    algorithm: str,
    preserve: Sequence[str],
) -> Decimal:
    if algorithm != "trade-entry-moving-block-v1":
        raise ValueError(f"unknown bootstrap algorithm: {algorithm}")
    required = {"whole_trades", "cross_symbol_entry_clusters"}
    if set(preserve) != required:
        raise ValueError("bootstrap must preserve whole trades and entry clusters")
    if not trades:
        return Decimal("0")
    clusters: dict[datetime, list[TradeRecord]] = defaultdict(list)
    for trade in trades:
        clusters[trade.entry_time].append(trade)
    by_day: dict[object, list[list[TradeRecord]]] = defaultdict(list)
    for entry_time in sorted(clusters):
        by_day[entry_time.date()].append(clusters[entry_time])
    first = min(by_day)
    last = max(by_day)
    n_days = (last - first).days + 1
    all_days = [first + timedelta(days=offset) for offset in range(n_days)]

    def block_at(start_day, length: int) -> list[TradeRecord]:
        collected: list[TradeRecord] = []
        for offset in range(length):
            day = start_day + timedelta(days=offset)
            for cluster in by_day.get(day, []):
                collected.extend(cluster)
        return collected

    if n_days <= block_days:
        blocks = [list(trades)]
    else:
        blocks = [block_at(day, block_days) for day in all_days[: n_days - block_days + 1]]
    draws = max(1, math.ceil(n_days / block_days))
    rng = random.Random(seed)
    samples: list[Decimal] = []
    for _ in range(resamples):
        chosen: list[TradeRecord] = []
        for _draw in range(draws):
            chosen.extend(blocks[rng.randrange(len(blocks))])
        samples.append(profit_factor([row.net_pnl for row in chosen]))
    alpha = 1.0 - float(confidence)
    return interpolated_percentile(samples, alpha)


def _moments(returns: Sequence[float]) -> tuple[float, float, float, float, float, int] | None:
    series = [float(item) for item in returns]
    n = len(series)
    if n < 2:
        return None
    mean = statistics.fmean(series)
    stdev = statistics.stdev(series)
    if stdev == 0.0:
        sharpe = math.inf if mean > 0 else (-math.inf if mean < 0 else 0.0)
        return sharpe, mean, stdev, 0.0, 0.0, n
    skew = sum((item - mean) ** 3 for item in series) / n / stdev**3
    kurtosis = sum((item - mean) ** 4 for item in series) / n / stdev**4
    return mean / stdev, mean, stdev, skew, kurtosis, n


def _psr_from_moments(
    sharpe: float,
    skew: float,
    kurtosis: float,
    n: int,
    benchmark: float,
) -> float:
    if math.isinf(sharpe):
        return 1.0 if sharpe > benchmark else 0.0
    variance_term = 1.0 - skew * sharpe + ((kurtosis - 1.0) / 4.0) * sharpe * sharpe
    denom = math.sqrt(max(variance_term, 1e-18))
    z_score = (sharpe - benchmark) * math.sqrt(n - 1) / denom
    return _norm_cdf(z_score)


def probabilistic_sharpe_ratio(returns: Sequence[float], benchmark_sharpe: float) -> float:
    moments = _moments(returns)
    if moments is None:
        return 0.0
    sharpe, _mean, _sd, skew, kurtosis, n = moments
    return _psr_from_moments(sharpe, skew, kurtosis, n, float(benchmark_sharpe))


def _pearson(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) < 2:
        return 0.0
    mean_l = statistics.fmean(left)
    mean_r = statistics.fmean(right)
    num = sum((a - mean_l) * (b - mean_r) for a, b in zip(left, right))
    den_l = math.sqrt(sum((a - mean_l) ** 2 for a in left))
    den_r = math.sqrt(sum((b - mean_r) ** 2 for b in right))
    if den_l == 0.0 or den_r == 0.0:
        return 0.0
    return num / (den_l * den_r)


def mean_pairwise_correlation(
    daily_series: Sequence[Sequence[tuple[datetime, Decimal]]],
) -> float:
    if len(daily_series) < 2:
        return 0.0
    dates = sorted({stamp for series in daily_series for stamp, _value in series})
    if len(dates) < 2:
        return 0.0
    aligned: list[list[float]] = []
    for series in daily_series:
        lookup = {stamp: float(as_decimal(value)) for stamp, value in series}
        aligned.append([lookup.get(stamp, 0.0) for stamp in dates])
    pairs: list[float] = []
    for i, left in enumerate(aligned):
        for right in aligned[i + 1 :]:
            pairs.append(_pearson(left, right))
    if not pairs:
        return 0.0
    return sum(pairs) / len(pairs)


def deflated_sharpe_ratio(
    weekly_returns: Sequence[float],
    *,
    trial_count: int,
    daily_series: Sequence[Sequence[tuple[datetime, Decimal]]],
    correlation_treatment: str,
) -> float:
    if correlation_treatment != "daily-psr-dsr-v1":
        raise ValueError("unknown DSR correlation treatment")
    moments = _moments(weekly_returns)
    if moments is None:
        return 0.0
    sharpe, _mean, _sd, skew, kurtosis, n = moments
    trials = max(int(trial_count), 1)
    rho = mean_pairwise_correlation(daily_series)
    n_eff = trials / (1.0 + (trials - 1.0) * max(rho, 0.0))
    if n_eff <= 1.0 or n < 2:
        sr0 = 0.0
    else:
        variance = (1.0 - skew * sharpe + ((kurtosis - 1.0) / 4.0) * sharpe * sharpe) / (n - 1)
        variance = max(float(variance), 0.0)
        z1 = _norm_ppf(1.0 - 1.0 / n_eff)
        z2 = _norm_ppf(1.0 - 1.0 / (n_eff * math.e))
        sr0 = math.sqrt(variance) * ((1.0 - EULER_GAMMA) * z1 + EULER_GAMMA * z2)
    return _psr_from_moments(sharpe, skew, kurtosis, n, sr0)
