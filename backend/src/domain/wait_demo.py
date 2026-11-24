"""九成等候的演示分片，覆盖乱序、缺片与旧批晚到。"""

from datetime import date


def _histogram(baseline, step, trucks):
    """把 ``trucks`` 辆车按等候秒数铺成直方图，返回 [秒数, 车数] 列表。"""
    return [[baseline + index * step, 1] for index in range(trucks)]


def demo_wait_days():
    """返回按市场日排列的封账单与分片，分片刻意乱序到达。"""
    return [
        {
            "market": "南桥", "date": date(2026, 11, 1).isoformat(), "batch_no": 1,
            "expected_shards": [1, 2, 3], "total_trucks": 90,
            "shards": [
                {"shard_no": 3, "buckets": _histogram(240, 6, 30)},
                {"shard_no": 1, "buckets": _histogram(30, 6, 30)},
                {"shard_no": 2, "buckets": _histogram(135, 6, 30)},
            ],
        },
        {
            "market": "西苑", "date": date(2026, 11, 2).isoformat(), "batch_no": 2,
            "expected_shards": [1, 2], "total_trucks": 60,
            "shards": [
                {"shard_no": 1, "buckets": _histogram(90, 10, 30)},
                {"shard_no": 2, "buckets": _histogram(300, 10, 30)},
            ],
        },
        {
            # 批号 5 先齐备并采用，随后批号 3 才补齐，演示旧批晚到不覆盖。
            "market": "东埠", "date": date(2026, 11, 3).isoformat(), "batch_no": 5,
            "expected_shards": [1], "total_trucks": 40,
            "shards": [{"shard_no": 1, "buckets": _histogram(120, 3, 40)}],
        },
        {
            "market": "东埠", "date": date(2026, 11, 3).isoformat(), "batch_no": 3,
            "expected_shards": [1, 2], "total_trucks": 50,
            "shards": [
                {"shard_no": 1, "buckets": _histogram(60, 4, 25)},
                {"shard_no": 2, "buckets": _histogram(360, 4, 25)},
            ],
        },
        {
            # 应到 3 片只到 2 片，保持待补齐，不产生采用结果。
            "market": "滨河", "date": date(2026, 11, 4).isoformat(), "batch_no": 1,
            "expected_shards": [1, 2, 3], "total_trucks": 75,
            "shards": [
                {"shard_no": 1, "buckets": _histogram(45, 5, 25)},
                {"shard_no": 2, "buckets": _histogram(180, 5, 25)},
            ],
        },
    ]
