"""针渡修衣工时换版的输入契约及切换验收单。"""

from collections import Counter
from decimal import Decimal
import re

from .repair_migration import RepairMigration


SPEC = {
    "name": "针渡修衣·工时刻度换版",
    "summary": "在门店继续改工时的同时回填整数刻度，演练完工单归档与带回，核验新版读数、结算口径和未消费事件后签发切换结论。",
    "customer": "针渡修衣连锁结算组与门店调度组",
    "entity_labels": ["修衣工单", "分批回填", "切换验收"],
    "field_labels": {
        "batch_size": "每批工单数", "max_batches": "本次最多执行批次",
        "orders": "存量修衣工单", "events": "双写变更", "archive": "完工归档",
        "ticket": "工单号", "legacy_hours": "旧工时（小时，十进制字符串）",
        "revision": "版次", "done": "已完工", "after_batch": "交错批次（0 为回填前）",
    },
    "guidance": "每 15 分钟换 1 刻度。交错批次为 0 时先消费变更，其他值在对应批次取数后、写回前消费。已完工工单按批归档；归档取到的版次若被并发改写，该单留在在办库。归档后收到更高版次改时，工单带新工时回到在办库，归档副本保留。结算按单号只取一份当前工时。",
    "sample": {
        "batch_size": 2,
        "max_batches": 3,
        "orders": [
            {"ticket": "ZD-101", "legacy_hours": "1.25", "revision": 3, "done": False},
            {"ticket": "ZD-102", "legacy_hours": "0.75", "revision": 1, "done": True},
            {"ticket": "ZD-103", "legacy_hours": "2.00", "revision": 4, "done": True},
            {"ticket": "ZD-104", "legacy_hours": "0.10", "revision": 1, "done": True},
            {"ticket": "ZD-105", "legacy_hours": "0.00", "revision": 1, "done": True},
        ],
        "events": [
            {"ticket": "ZD-101", "legacy_hours": "1.75", "revision": 4, "after_batch": 1},
            {"ticket": "ZD-101", "legacy_hours": "1.25", "revision": 3, "after_batch": 1},
            {"ticket": "ZD-101", "legacy_hours": "1.75", "revision": 4, "after_batch": 1},
            {"ticket": "ZD-104", "legacy_hours": "0.50", "revision": 2, "after_batch": 2},
            {"ticket": "ZD-100", "legacy_hours": "0.25", "revision": 1, "after_batch": 2},
        ],
        "archive": {
            "batch_size": 2,
            "max_batches": 2,
            "events": [
                {"ticket": "ZD-102", "legacy_hours": "1.00", "revision": 2, "after_batch": 1},
                {"ticket": "ZD-103", "legacy_hours": "2.50", "revision": 5, "after_batch": 2},
            ],
        },
    },
    "expect": {
        "回填批次": 3, "归档批次": 2, "一致工单": 6, "回填写入": 3, "竞态拦截": 1,
        "归档移出": 3, "归档留办": 1, "归档带回": 1, "重试新增副本": 0,
        "结算工单": 6, "结算重复": 0, "未消费事件": 0, "切换门禁": "允许切换",
    },
}


def _require_integer(value, label, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"{label}须为 {lower} 至 {upper} 的整数")


def _check_row_core(row, location):
    ticket, hours = row["ticket"], row["legacy_hours"]
    if not isinstance(ticket, str) or re.fullmatch(r"[A-Z][A-Z0-9-]{0,31}", ticket) is None:
        raise ValueError(f"{location}工单号须以大写字母开头，仅含大写字母、数字或连字符，最多 32 位")
    if (not isinstance(hours, str) or re.fullmatch(r"(?:0|[1-9][0-9]{0,2})(?:\.[0-9]{1,6})?", hours) is None
            or Decimal(hours) > 168):
        raise ValueError(f"{location}旧工时须为 0 至 168 的十进制字符串，最多 6 位小数")
    _require_integer(row["revision"], f"{location}版次", 1, 2147483647)


def _check_orders(rows):
    if not isinstance(rows, list) or len(rows) > 5000:
        raise ValueError("存量工单须为最多 5000 项的数组")
    seen = set()
    for number, row in enumerate(rows, 1):
        location = f"存量工单第 {number} 行"
        if not isinstance(row, dict) or row.keys() != {"ticket", "legacy_hours", "revision", "done"}:
            raise ValueError(f"{location}字段缺失或未知")
        _check_row_core(row, location)
        if type(row["done"]) is not bool:
            raise ValueError(f"{location}已完工须为布尔值")
        if row["ticket"] in seen:
            raise ValueError(f"重复存量工单 {row['ticket']}")
        seen.add(row["ticket"])


def _check_events(rows, label):
    if not isinstance(rows, list) or len(rows) > 5000:
        raise ValueError(f"{label}须为最多 5000 项的数组")
    for number, row in enumerate(rows, 1):
        location = f"{label}第 {number} 行"
        if not isinstance(row, dict) or row.keys() != {"ticket", "legacy_hours", "revision", "after_batch"}:
            raise ValueError(f"{location}字段缺失或未知")
        _check_row_core(row, location)
        _require_integer(row["after_batch"], f"{location}交错批次", 0, 5000)


def validate(payload):
    if not isinstance(payload, dict) or payload.keys() != SPEC["sample"].keys():
        raise ValueError("请提供每批工单数、批次上限、存量工单、双写变更和完工归档")
    _require_integer(payload["batch_size"], "每批工单数", 1, 1000)
    _require_integer(payload["max_batches"], "批次上限", 0, 5000)
    _check_orders(payload["orders"])
    _check_events(payload["events"], "双写变更")
    archive = payload["archive"]
    if not isinstance(archive, dict) or archive.keys() != {"batch_size", "max_batches", "events"}:
        raise ValueError("完工归档须包含每批工单数、批次上限和归档变更")
    _require_integer(archive["batch_size"], "归档每批工单数", 1, 1000)
    _require_integer(archive["max_batches"], "归档批次上限", 0, 5000)
    _check_events(archive["events"], "归档变更")


def _report(settlement, archived, events, batches, races, archive_batches, archive_races):
    order_counts = Counter(row["status"] for row in settlement)
    event_counts = Counter(row["status"] for row in events)
    duplicates = len(settlement) - len({row["ticket"] for row in settlement})
    checks = [
        ("存在可验收工单", len(settlement), bool(settlement)),
        ("不可转换工单", order_counts["不可转换"], order_counts["不可转换"] == 0),
        ("待回填工单", order_counts["待回填"], order_counts["待回填"] == 0),
        ("影子版次不一致", order_counts["版次不一致"], order_counts["版次不一致"] == 0),
        ("影子刻度不一致", order_counts["刻度不一致"], order_counts["刻度不一致"] == 0),
        ("结算单号重复", duplicates, duplicates == 0),
        ("待重放事件", event_counts["待消费"], event_counts["待消费"] == 0),
        ("同版冲突事件", event_counts["同版冲突"], event_counts["同版冲突"] == 0),
    ]
    blockers = [label for label, _, passed in checks if not passed]
    verdict = "暂缓切换" if blockers else "允许切换"
    locations = Counter(row["location"] for row in settlement)
    summary = f"暂缓切换，需处理{'、'.join(blockers)}。" if blockers else (
        f"允许切换，{len(settlement)} 张工单各有一份当前工时"
        f"（在办 {locations['在办']} 张、归档 {locations['归档']} 张），变更队列已清空。"
    )
    current_archived = {(row["ticket"], row["revision"]) for row in settlement if row["location"] == "归档"}
    return {
        "summary": summary,
        "metrics": {
            "切换门禁": verdict,
            "回填批次": len(batches), "归档批次": len(archive_batches),
            "一致工单": order_counts["一致"],
            "回填写入": sum(batch["回填写入"] for batch in batches), "竞态拦截": len(races),
            "归档移出": sum(batch["归档移出"] for batch in archive_batches),
            "归档留办": len(archive_races), "归档带回": event_counts["带回已消费"],
            "重试新增副本": sum(batch["重试新增副本"] for batch in archive_batches),
            "结算工单": len(settlement), "结算重复": duplicates,
            "待回填工单": order_counts["待回填"], "不可转换工单": order_counts["不可转换"],
            "影子不一致": order_counts["版次不一致"] + order_counts["刻度不一致"],
            "未消费事件": event_counts["待消费"] + event_counts["同版冲突"],
        },
        "sections": [
            {"title": "切换门禁", "rows": [
                {"核对项": label, "工单或事件数": count, "判定": "通过" if passed else "阻断"}
                for label, count, passed in checks
            ]},
            {"title": "结算读数", "rows": [
                {"工单号": row["ticket"], "口径": row["location"], "旧工时（小时）": row["legacy_hours"],
                 "当前版次": row["revision"], "影子刻度": row["shadow_ticks"],
                 "影子版次": row["shadow_revision"], "判定": row["status"]}
                for row in settlement
            ]},
            {"title": "分批回填", "rows": batches},
            {"title": "过期快照写入凭据", "rows": races},
            {"title": "分批归档", "rows": archive_batches},
            {"title": "归档留办凭据", "rows": archive_races},
            {"title": "归档副本", "rows": [
                {"工单号": row["ticket"], "版次": row["revision"], "旧工时（小时）": row["legacy_hours"],
                 "影子刻度": row["shadow_ticks"], "影子版次": row["shadow_revision"],
                 "归档批次": row["archived_batch"],
                 "口径": "当前" if (row["ticket"], row["revision"]) in current_archived else "历史"}
                for row in archived
            ]},
            {"title": "双写事件消费", "rows": [
                {"事件序号": row["seq"], "阶段": row["phase"], "交错批次": row["after_batch"],
                 "工单号": row["ticket"], "提交工时（小时）": row["legacy_hours"], "提交版次": row["revision"],
                 "消费时工单版次": row["observed_revision"], "判定": row["status"]}
                for row in events
            ]},
        ],
    }


def solve(payload, engine):
    validate(payload)
    if engine.dialect.name != "postgresql":
        raise ValueError("工时迁移演练需要 PostgreSQL")
    migration = RepairMigration(engine)
    archive = payload["archive"]
    migration.install(payload["orders"], payload["events"], archive["events"])
    try:
        replay_phases = {row["after_batch"] for row in payload["events"]}
        if 0 in replay_phases:
            migration.replay("回填", 0)
        cursor, batches, races = "", [], []
        for number in range(1, payload["max_batches"] + 1):
            batch = migration.backfill(cursor, payload["batch_size"], number, number in replay_phases)
            if batch is None:
                break
            evidence, skipped = batch
            batches.append(evidence)
            races.extend(skipped)
            cursor = evidence["最后工单"]
        archive_phases = {row["after_batch"] for row in archive["events"]}
        if 0 in archive_phases:
            migration.replay("归档", 0)
        archive_cursor, archive_batches, archive_races = "", [], []
        for number in range(1, archive["max_batches"] + 1):
            batch = migration.archive_batch(archive_cursor, archive["batch_size"], number,
                                            number in archive_phases)
            if batch is None:
                break
            evidence, skipped = batch
            archive_batches.append(evidence)
            archive_races.extend(skipped)
            archive_cursor = evidence["最后工单"]
        settlement, archived, events = migration.read_gate()
        return _report(settlement, archived, events, batches, races, archive_batches, archive_races)
    finally:
        migration.close()
