"""完工修衣单归档演练的验收测试。

需要可创建 schema 的 PostgreSQL 16 测试库：

    GB5309_DATABASE_URL=postgresql+psycopg://app@127.0.0.1:55444/gb5309_test \
        PYTHONPATH=. python3 -m unittest discover -s tests -v
"""

import copy
import os
import unittest

from sqlalchemy import create_engine

from src.services.domain import SPEC, solve, validate

DATABASE_URL = os.getenv("GB5309_DATABASE_URL")


def section(report, title):
    return next(rows for rows in report["sections"] if rows["title"] == title)["rows"]


def settlement_row(report, ticket):
    return next(row for row in section(report, "结算读数") if row["工单号"] == ticket)


def event_row(report, ticket):
    return next(row for row in section(report, "双写事件消费") if row["工单号"] == ticket)


def sample_payload(**overrides):
    payload = copy.deepcopy(SPEC["sample"])
    payload.update(overrides)
    return payload


@unittest.skipUnless(DATABASE_URL, "需要 GB5309_DATABASE_URL 指向可创建 schema 的 PostgreSQL 测试库")
class SampleDrillTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(DATABASE_URL)
        cls.report = solve(SPEC["sample"], cls.engine)

    def test_metrics_match_expect(self):
        for key, value in SPEC["expect"].items():
            self.assertEqual(self.report["metrics"][key], value, key)

    def test_repeated_run_produces_identical_evidence(self):
        self.assertEqual(solve(SPEC["sample"], self.engine), self.report)

    def test_archive_race_keeps_updated_order_active(self):
        kept = section(self.report, "归档留办凭据")
        self.assertEqual([(row["工单号"], row["快照版次"], row["当前版次"]) for row in kept],
                         [("ZD-102", 1, 2)])
        current = settlement_row(self.report, "ZD-102")
        self.assertEqual((current["口径"], current["旧工时（小时）"], current["影子刻度"]),
                         ("在办", "1.00", 4))

    def test_restored_order_returns_active_and_copy_is_kept(self):
        self.assertEqual(event_row(self.report, "ZD-103")["判定"], "带回已消费")
        current = settlement_row(self.report, "ZD-103")
        self.assertEqual((current["口径"], current["旧工时（小时）"], current["影子刻度"]),
                         ("在办", "2.50", 10))
        copies = [row for row in section(self.report, "归档副本") if row["工单号"] == "ZD-103"]
        self.assertEqual([(row["版次"], row["口径"]) for row in copies], [(4, "历史")])

    def test_settlement_returns_exactly_one_current_row_per_ticket(self):
        rows = section(self.report, "结算读数")
        tickets = [row["工单号"] for row in rows]
        self.assertEqual(len(tickets), len(set(tickets)))
        self.assertEqual(self.report["metrics"]["结算重复"], 0)
        self.assertEqual(set(tickets), {"ZD-100", "ZD-101", "ZD-102", "ZD-103", "ZD-104", "ZD-105"})
        self.assertEqual(settlement_row(self.report, "ZD-104")["口径"], "归档")
        self.assertEqual(settlement_row(self.report, "ZD-105")["口径"], "归档")

    def test_archive_retry_leaves_no_duplicate_copies(self):
        batches = section(self.report, "分批归档")
        self.assertEqual([row["重试新增副本"] for row in batches], [0, 0])
        copies = section(self.report, "归档副本")
        keys = [(row["工单号"], row["版次"]) for row in copies]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(self.report["metrics"]["重试新增副本"], 0)


@unittest.skipUnless(DATABASE_URL, "需要 GB5309_DATABASE_URL 指向可创建 schema 的 PostgreSQL 测试库")
class ArchivedOrderEventTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(DATABASE_URL)

    def drill(self, archive_events):
        payload = sample_payload(
            orders=[
                {"ticket": "ZD-201", "legacy_hours": "0.50", "revision": 3, "done": True},
                {"ticket": "ZD-202", "legacy_hours": "1.00", "revision": 1, "done": True},
            ],
            events=[],
            archive={"batch_size": 1, "max_batches": 2, "events": archive_events},
        )
        return solve(payload, self.engine)

    def test_stale_event_does_not_resurrect_archived_order(self):
        report = self.drill([{"ticket": "ZD-201", "legacy_hours": "0.25", "revision": 2, "after_batch": 2}])
        event = event_row(report, "ZD-201")
        self.assertEqual((event["判定"], event["消费时工单版次"]), ("过期已消费", 3))
        current = settlement_row(report, "ZD-201")
        self.assertEqual((current["口径"], current["旧工时（小时）"], current["当前版次"]),
                         ("归档", "0.50", 3))
        self.assertEqual(report["metrics"]["切换门禁"], "允许切换")

    def test_same_revision_same_hours_is_duplicate(self):
        report = self.drill([{"ticket": "ZD-201", "legacy_hours": "0.50", "revision": 3, "after_batch": 2}])
        self.assertEqual(event_row(report, "ZD-201")["判定"], "重复已消费")
        self.assertEqual(report["metrics"]["切换门禁"], "允许切换")

    def test_same_revision_different_hours_is_conflict(self):
        report = self.drill([{"ticket": "ZD-201", "legacy_hours": "0.75", "revision": 3, "after_batch": 2}])
        self.assertEqual(event_row(report, "ZD-201")["判定"], "同版冲突")
        self.assertEqual(report["metrics"]["切换门禁"], "暂缓切换")
        self.assertEqual(report["metrics"]["未消费事件"], 1)

    def test_pending_archive_event_blocks_the_gate(self):
        report = self.drill([{"ticket": "ZD-202", "legacy_hours": "1.25", "revision": 2, "after_batch": 5}])
        self.assertEqual(event_row(report, "ZD-202")["判定"], "待消费")
        self.assertEqual(report["metrics"]["切换门禁"], "暂缓切换")

    def test_higher_revision_restores_then_newer_archive_copy_wins(self):
        report = self.drill([
            {"ticket": "ZD-201", "legacy_hours": "0.75", "revision": 4, "after_batch": 2},
            {"ticket": "ZD-201", "legacy_hours": "0.50", "revision": 3, "after_batch": 2},
        ])
        current = settlement_row(report, "ZD-201")
        self.assertEqual((current["口径"], current["旧工时（小时）"], current["当前版次"]),
                         ("在办", "0.75", 4))
        statuses = [row["判定"] for row in section(report, "双写事件消费") if row["工单号"] == "ZD-201"]
        self.assertEqual(statuses, ["带回已消费", "过期已消费"])
        copies = [row for row in section(report, "归档副本") if row["工单号"] == "ZD-201"]
        self.assertEqual([(row["版次"], row["口径"]) for row in copies], [(3, "历史")])
        self.assertEqual(report["metrics"]["结算重复"], 0)


class PayloadValidationTest(unittest.TestCase):
    def test_missing_archive_is_rejected(self):
        payload = sample_payload()
        del payload["archive"]
        with self.assertRaisesRegex(ValueError, "完工归档"):
            validate(payload)

    def test_done_must_be_boolean(self):
        payload = sample_payload()
        payload["orders"][0]["done"] = "true"
        with self.assertRaisesRegex(ValueError, "已完工须为布尔值"):
            validate(payload)

    def test_archive_shape_is_checked(self):
        payload = sample_payload()
        payload["archive"] = {"batch_size": 2, "max_batches": 2}
        with self.assertRaisesRegex(ValueError, "完工归档须包含"):
            validate(payload)
        payload = sample_payload()
        payload["archive"]["batch_size"] = 0
        with self.assertRaisesRegex(ValueError, "归档每批工单数"):
            validate(payload)
        payload = sample_payload()
        payload["archive"]["events"][0]["after_batch"] = -1
        with self.assertRaisesRegex(ValueError, "交错批次"):
            validate(payload)

    def test_sample_payload_is_valid(self):
        validate(SPEC["sample"])


if __name__ == "__main__":
    unittest.main()
