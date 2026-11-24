"""分片等候直方图的九成秒数与封账对账。

门岗分片只上报「等候秒数 → 车辆数」的计数，不展开单车记录。
这里只做纯计算与输入归一化，便于单独测试，也避免在服务层重复口径。
"""

from datetime import date
from itertools import islice

MAX_WAIT_SECONDS = 7 * 24 * 60 * 60
MAX_TRUCKS = 10 ** 12
MAX_SHARD_NO = 10 ** 9
MAX_BUCKETS_PER_SHARD = 100_000


def _integer(value, label, minimum, maximum):
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}须为整数。")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"{label}须为整数。")
        value = int(value)
    if not minimum <= value <= maximum:
        raise ValueError(f"{label}须为 {minimum}–{maximum} 的整数。")
    return value


def normalize_market(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 300:
        raise ValueError("市场名称须为 1–300 个字符。")
    return value.strip()


def normalize_date(value):
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            parsed = None
        if parsed is not None and value == parsed.isoformat():
            return value
    raise ValueError("观测日期须使用有效的 YYYY-MM-DD 日期。")


def normalize_batch_no(value):
    return _integer(value, "封账单批号", 1, MAX_SHARD_NO)


def normalize_shard_no(value):
    return _integer(value, "片号", 1, MAX_SHARD_NO)


def normalize_total(value):
    return _integer(value, "封账总车数", 1, MAX_TRUCKS)


def normalize_shards(values):
    """应到片号：非空、去重后升序保存。"""
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError("应到片号至少需要一个片号。")
    if len(values) > 10_000:
        raise ValueError("应到片号最多 10000 个。")
    result = [normalize_shard_no(value) for value in values]
    if len(set(result)) != len(result):
        raise ValueError("应到片号不能重复。")
    return sorted(result)


def normalize_buckets(raw):
    """把分片计数归一化为按秒数升序的 ``[[秒数, 车数], ...]``。

    支持三种写法：``{"12": 3}``、``[[12, 3]]`` 与
    ``[{"seconds": 12, "count": 3}]``。同一秒数重复上报时合并计数。
    """
    if isinstance(raw, dict):
        entries = list(raw.items())
    elif isinstance(raw, (list, tuple)):
        entries = []
        for item in raw:
            if isinstance(item, dict):
                if "seconds" not in item or "count" not in item:
                    raise ValueError("每条等候计数须包含 seconds 与 count。")
                entries.append((item["seconds"], item["count"]))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                entries.append((item[0], item[1]))
            else:
                raise ValueError("每条等候计数须为「秒数, 车数」。")
    else:
        raise ValueError("分片等候计数须为对象或数组。")
    if len(entries) > MAX_BUCKETS_PER_SHARD:
        raise ValueError(f"单个分片最多 {MAX_BUCKETS_PER_SHARD} 个秒数档位。")

    merged = {}
    for seconds, count in entries:
        seconds = _integer(seconds, "等候秒数", 0, MAX_WAIT_SECONDS)
        count = _integer(count, "等候车数", 0, MAX_TRUCKS)
        merged[seconds] = merged.get(seconds, 0) + count
        if merged[seconds] > MAX_TRUCKS:
            raise ValueError("单个分片的车辆数合计超过允许上限。")

    buckets = [[seconds, count] for seconds, count in sorted(merged.items()) if count > 0]
    return buckets, sum(count for _, count in buckets)


def merge_histograms(histograms, chunk_size=512):
    """分批合并分片直方图。

    内存只与秒数档位数有关，不随车辆数或分片数线性展开；
    每批最多读取 ``chunk_size`` 个分片，避免一次性载入全部原始计数。
    """
    totals = {}
    iterator = iter(histograms)
    while True:
        chunk = list(islice(iterator, chunk_size))
        if not chunk:
            break
        for buckets in chunk:
            for seconds, count in buckets:
                totals[seconds] = totals.get(seconds, 0) + count
    return sorted(totals.items())


def reconcile(expected_shards, received_shards, declared_total, observed_total):
    """按应到片号与封账总车数对账。分片到达顺序不影响结果。"""
    expected = set(expected_shards)
    received = set(received_shards)
    missing = sorted(expected - received)
    unexpected = sorted(received - expected)
    total_matches = observed_total == declared_total
    reasons = []
    if missing:
        reasons.append("缺片 " + "、".join(str(number) for number in missing))
    if unexpected:
        reasons.append("封账单外片号 " + "、".join(str(number) for number in unexpected))
    if not total_matches:
        reasons.append(f"总车数不符：分片合计 {observed_total}，封账 {declared_total}")
    return {
        "expected_shards": sorted(expected),
        "received_shards": sorted(received),
        "missing_shards": missing,
        "unexpected_shards": unexpected,
        "declared_trucks": declared_total,
        "observed_trucks": observed_total,
        "total_matches": total_matches,
        "complete": not missing and not unexpected and total_matches,
        "pending_reason": "；".join(reasons) if reasons else None,
    }


def percentile_detail(histogram, total_trucks, numerator=9, denominator=10):
    """取累计首次达到指定比例的秒数（默认九成）。

    一次遍历累计车辆数，首次达到 ceil(比例 × 总车数) 的档位秒数即结果；
    使用整数比较避免浮点误差，也不展开单车。输入须按秒数升序且不重复。
    """
    if isinstance(total_trucks, bool) or not isinstance(total_trucks, int) or total_trucks <= 0:
        raise ValueError("总车数须为正整数。")
    if not (1 <= numerator <= denominator):
        raise ValueError("累计比例须在 0–1 之间。")

    threshold = (total_trucks * numerator + denominator - 1) // denominator
    cumulative, observed, previous, result, hit = 0, 0, None, None, 0
    for seconds, count in histogram:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("等候车数须为非负整数。")
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
            raise ValueError("等候秒数须为非负整数。")
        if previous is not None and seconds <= previous:
            raise ValueError("等候直方图须按秒数升序且不重复。")
        previous = seconds
        observed += count
        cumulative += count
        if result is None and cumulative >= threshold:
            result, hit = seconds, cumulative
    if observed != total_trucks:
        raise ValueError("分片累计车辆数与封账总车数不一致。")
    if result is None:
        raise ValueError("直方图无法达到指定累计比例。")
    return {
        "seconds": result,
        "cumulative": hit,
        "threshold": threshold,
        "total_trucks": total_trucks,
    }


def p90_seconds(histogram, total_trucks):
    """九成车辆等候不超过的秒数。"""
    return percentile_detail(histogram, total_trucks)["seconds"]
