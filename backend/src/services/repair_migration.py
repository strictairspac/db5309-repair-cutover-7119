"""PostgreSQL transactions for one isolated repair-time migration rehearsal."""

import json
from uuid import uuid4

from sqlalchemy import text


class RepairMigration:
    def __init__(self, engine):
        self.engine = engine
        self.schema = f"gb5309_{uuid4().hex}"
        self.orders = f"{self.schema}.repair_orders"
        self.archive = f"{self.schema}.repair_order_archive"
        self.settlement = f"{self.schema}.settlement_current"
        self.events = f"{self.schema}.change_events"
        self.to_ticks = f"{self.schema}.to_ticks"

    def install(self, orders, events, archive_events):
        with self.engine.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA {self.schema}"))
            conn.execute(text(f"""
                CREATE TABLE {self.orders} (
                    ticket text COLLATE "C" PRIMARY KEY,
                    legacy_hours numeric NOT NULL CHECK (legacy_hours BETWEEN 0 AND 168),
                    revision integer NOT NULL CHECK (revision > 0),
                    done boolean NOT NULL DEFAULT false
                )
            """))
            if orders:
                conn.execute(text(f"""
                    INSERT INTO {self.orders} (ticket, legacy_hours, revision, done)
                    VALUES (:ticket, CAST(:legacy_hours AS numeric), :revision, :done)
                """), orders)
            conn.execute(text(f"""
                ALTER TABLE {self.orders}
                    ADD COLUMN shadow_ticks integer CHECK (shadow_ticks BETWEEN 0 AND 672),
                    ADD COLUMN shadow_revision integer CHECK (shadow_revision > 0),
                    ADD CHECK ((shadow_ticks IS NULL) = (shadow_revision IS NULL))
            """))
            # An exact quarter-hour conversion. Casting an inexact numeric to integer would round it.
            conn.execute(text(f"""
                CREATE FUNCTION {self.to_ticks}(hours numeric) RETURNS integer
                LANGUAGE sql IMMUTABLE STRICT AS $$
                    SELECT CASE WHEN mod(hours * 4, 1) = 0 THEN (hours * 4)::integer END
                $$
            """))
            conn.execute(text(f"""
                CREATE FUNCTION {self.schema}.dual_write() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                    NEW.shadow_ticks := {self.to_ticks}(NEW.legacy_hours);
                    NEW.shadow_revision := CASE WHEN NEW.shadow_ticks IS NOT NULL THEN NEW.revision END;
                    RETURN NEW;
                END;
                $$
            """))
            conn.execute(text(f"""
                CREATE TRIGGER dual_write
                BEFORE INSERT OR UPDATE OF legacy_hours, revision ON {self.orders}
                FOR EACH ROW EXECUTE FUNCTION {self.schema}.dual_write()
            """))
            # Archived copies are immutable history. The (ticket, revision) key makes a
            # redelivered archive batch a no-op instead of a duplicate same-version copy.
            conn.execute(text(f"""
                CREATE TABLE {self.archive} (
                    ticket text COLLATE "C" NOT NULL,
                    revision integer NOT NULL CHECK (revision > 0),
                    legacy_hours numeric NOT NULL CHECK (legacy_hours BETWEEN 0 AND 168),
                    shadow_ticks integer CHECK (shadow_ticks BETWEEN 0 AND 672),
                    shadow_revision integer CHECK (shadow_revision > 0),
                    archived_batch integer NOT NULL CHECK (archived_batch > 0),
                    PRIMARY KEY (ticket, revision),
                    CHECK ((shadow_ticks IS NULL) = (shadow_revision IS NULL))
                )
            """))
            # Settlement reads one current row per ticket: the active row wins, otherwise
            # the newest archived copy stands in for tickets that never came back.
            conn.execute(text(f"""
                CREATE VIEW {self.settlement} AS
                SELECT ticket, legacy_hours, revision, shadow_ticks, shadow_revision,
                       CAST('在办' AS text) AS location
                FROM {self.orders}
                UNION ALL
                SELECT kept.ticket, kept.legacy_hours, kept.revision, kept.shadow_ticks,
                       kept.shadow_revision, CAST('归档' AS text)
                FROM {self.archive} kept
                WHERE NOT EXISTS (SELECT 1 FROM {self.orders} live WHERE live.ticket = kept.ticket)
                  AND kept.revision = (SELECT max(newer.revision) FROM {self.archive} newer
                                       WHERE newer.ticket = kept.ticket)
            """))
            conn.execute(text(f"""
                CREATE TABLE {self.events} (
                    seq integer PRIMARY KEY,
                    phase text NOT NULL,
                    after_batch integer NOT NULL CHECK (after_batch >= 0),
                    ticket text NOT NULL,
                    legacy_hours numeric NOT NULL CHECK (legacy_hours BETWEEN 0 AND 168),
                    revision integer NOT NULL CHECK (revision > 0),
                    status text NOT NULL DEFAULT '待消费',
                    observed_revision integer
                )
            """))
            conn.execute(text(f"""
                CREATE INDEX change_events_pending ON {self.events} (phase, after_batch, seq)
                WHERE status = '待消费'
            """))
            rows = [{**event, "phase": "回填"} for event in events]
            rows += [{**event, "phase": "归档"} for event in archive_events]
            if rows:
                conn.execute(text(f"""
                    INSERT INTO {self.events} (seq, phase, after_batch, ticket, legacy_hours, revision)
                    VALUES (:seq, :phase, :after_batch, :ticket, CAST(:legacy_hours AS numeric), :revision)
                """), [{**row, "seq": seq} for seq, row in enumerate(rows, 1)])

    def replay(self, phase, batch_no):
        # The legacy write, the trigger's shadow write, and event acknowledgement commit together.
        with self.engine.begin() as conn:
            events = conn.execute(text(f"""
                SELECT seq, ticket, legacy_hours, revision
                FROM {self.events}
                WHERE phase = :phase AND after_batch = :batch AND status = '待消费'
                ORDER BY seq FOR UPDATE
            """), {"phase": phase, "batch": batch_no}).mappings().all()
            for event in events:
                revision, status = self.apply_event(conn, event)
                conn.execute(text(f"""
                    UPDATE {self.events} SET status = :status, observed_revision = :revision
                    WHERE seq = :seq
                """), {"status": status, "revision": revision, "seq": event["seq"]})

    def apply_event(self, conn, event):
        revision = conn.execute(text(f"""
            UPDATE {self.orders} SET legacy_hours = :legacy_hours, revision = :revision
            WHERE ticket = :ticket AND revision < :revision
            RETURNING revision
        """), event).scalar_one_or_none()
        if revision is not None:
            return revision, "双写已消费"
        current = conn.execute(text(f"""
            SELECT revision, legacy_hours = :legacy_hours AS same_hours FROM {self.orders}
            WHERE ticket = :ticket
        """), event).mappings().first()
        if current is not None:
            if current["revision"] > event["revision"]:
                return current["revision"], "过期已消费"
            return current["revision"], "重复已消费" if current["same_hours"] else "同版冲突"
        # The ticket left the active ledger. Only a strictly newer revision brings it back;
        # anything at or below the archived copy is consumed without touching either store.
        kept = conn.execute(text(f"""
            SELECT revision, legacy_hours = :legacy_hours AS same_hours FROM {self.archive}
            WHERE ticket = :ticket ORDER BY revision DESC LIMIT 1
        """), event).mappings().first()
        if kept is not None and event["revision"] <= kept["revision"]:
            if event["revision"] < kept["revision"]:
                return kept["revision"], "过期已消费"
            return kept["revision"], "重复已消费" if kept["same_hours"] else "同版冲突"
        revision = conn.execute(text(f"""
            INSERT INTO {self.orders} (ticket, legacy_hours, revision)
            VALUES (:ticket, :legacy_hours, :revision)
            ON CONFLICT (ticket) DO NOTHING
            RETURNING revision
        """), event).scalar_one()
        return revision, "带回已消费" if kept is not None else "双写已消费"

    def backfill(self, cursor, batch_size, batch_no, replay_due):
        with self.engine.connect().execution_options(isolation_level="READ COMMITTED") as conn, conn.begin():
            snapshot = conn.execute(text(f"""
                SELECT ticket, revision, {self.to_ticks}(legacy_hours) AS ticks,
                       shadow_revision IS NULL OR shadow_revision < revision AS needs_backfill
                FROM {self.orders}
                WHERE ticket > :cursor ORDER BY ticket LIMIT :batch_size
            """), {"cursor": cursor, "batch_size": batch_size}).mappings().all()
            if not snapshot:
                return None
            # No row locks are held by the snapshot. Replay commits on a separate connection.
            if replay_due:
                self.replay("回填", batch_no)
            candidates = [dict(row) for row in snapshot if row["ticks"] is not None and row["needs_backfill"]]
            written = self.write_snapshot(conn, candidates)
            races = self.race_evidence(conn, batch_no, [row for row in candidates if row["ticket"] not in written])
            return {
                "批次": batch_no,
                "起始工单": snapshot[0]["ticket"],
                "最后工单": snapshot[-1]["ticket"],
                "扫描工单": len(snapshot),
                "回填写入": len(written),
                "竞态拦截": len(races),
                "快照不可转换": sum(row["ticks"] is None for row in snapshot),
                "快照已同步": sum(row["ticks"] is not None and not row["needs_backfill"] for row in snapshot),
            }, races

    def write_snapshot(self, conn, candidates):
        if not candidates:
            return set()
        return set(conn.execute(text(f"""
            UPDATE {self.orders} AS target
            SET shadow_ticks = snapshot.ticks, shadow_revision = snapshot.revision
            FROM jsonb_to_recordset(CAST(:snapshot AS jsonb)) AS snapshot(ticket text, revision integer, ticks integer)
            WHERE target.ticket = snapshot.ticket AND target.revision = snapshot.revision
              AND (target.shadow_revision IS NULL OR target.shadow_revision < snapshot.revision)
            RETURNING target.ticket
        """), {"snapshot": json.dumps(candidates)}).scalars())

    def race_evidence(self, conn, batch_no, skipped):
        if not skipped:
            return []
        current = conn.execute(text(f"""
            SELECT ticket, revision, shadow_ticks, shadow_revision FROM {self.orders}
            WHERE ticket = ANY(CAST(:tickets AS text[]))
        """), {"tickets": [row["ticket"] for row in skipped]}).mappings()
        by_ticket = {row["ticket"]: row for row in current}
        return [{
            "批次": batch_no,
            "工单号": row["ticket"],
            "快照版次": row["revision"],
            "快照刻度": row["ticks"],
            "更新行数": 0,
            "当前版次": by_ticket[row["ticket"]]["revision"],
            "影子版次": by_ticket[row["ticket"]]["shadow_revision"],
            "保留刻度": by_ticket[row["ticket"]]["shadow_ticks"],
        } for row in skipped]

    def archive_batch(self, cursor, batch_size, batch_no, replay_due):
        with self.engine.connect().execution_options(isolation_level="READ COMMITTED") as conn, conn.begin():
            snapshot = [dict(row) for row in conn.execute(text(f"""
                SELECT ticket, revision FROM {self.orders}
                WHERE done AND ticket > :cursor ORDER BY ticket LIMIT :batch_size
            """), {"cursor": cursor, "batch_size": batch_size}).mappings()]
            if not snapshot:
                return None
            # Interleaved changes commit on a separate connection between read and move.
            if replay_due:
                self.replay("归档", batch_no)
            moved = self.move_snapshot(conn, snapshot, batch_no)
            kept = [row for row in snapshot if row["ticket"] not in moved]
            races = self.archive_race_evidence(conn, batch_no, kept)
            evidence = {
                "批次": batch_no,
                "起始工单": snapshot[0]["ticket"],
                "最后工单": snapshot[-1]["ticket"],
                "扫描工单": len(snapshot),
                "归档移出": len(moved),
                "留办拦截": len(races),
            }
        # The batch committed. An at-least-once redelivery of the same snapshot must not
        # move anything again or leave a second copy of an already archived version.
        evidence["重试新增副本"] = self.retry_archive(snapshot, batch_no)
        return evidence, races

    def move_snapshot(self, conn, snapshot, batch_no):
        if not snapshot:
            return set()
        # Each row moves only if its version is still the one the snapshot read; a
        # concurrent rewrite keeps the order in the active ledger untouched.
        return set(conn.execute(text(f"""
            WITH snapshot AS (
                SELECT ticket, revision
                FROM jsonb_to_recordset(CAST(:snapshot AS jsonb)) AS snapshot(ticket text, revision integer)
            ),
            moved AS (
                DELETE FROM {self.orders} AS target
                USING snapshot
                WHERE target.ticket = snapshot.ticket AND target.revision = snapshot.revision
                  AND target.done
                RETURNING target.ticket, target.legacy_hours, target.revision,
                          target.shadow_ticks, target.shadow_revision
            )
            INSERT INTO {self.archive} (ticket, revision, legacy_hours, shadow_ticks,
                                        shadow_revision, archived_batch)
            SELECT ticket, revision, legacy_hours, shadow_ticks, shadow_revision, :batch
            FROM moved
            ON CONFLICT (ticket, revision) DO NOTHING
            RETURNING ticket
        """), {"snapshot": json.dumps(snapshot), "batch": batch_no}).scalars())

    def retry_archive(self, snapshot, batch_no):
        with self.engine.begin() as conn:
            return len(self.move_snapshot(conn, snapshot, batch_no))

    def archive_race_evidence(self, conn, batch_no, kept):
        if not kept:
            return []
        current = conn.execute(text(f"""
            SELECT ticket, revision, shadow_ticks, shadow_revision FROM {self.orders}
            WHERE ticket = ANY(CAST(:tickets AS text[]))
        """), {"tickets": [row["ticket"] for row in kept]}).mappings()
        by_ticket = {row["ticket"]: row for row in current}
        return [{
            "批次": batch_no,
            "工单号": row["ticket"],
            "快照版次": row["revision"],
            "当前版次": by_ticket[row["ticket"]]["revision"] if row["ticket"] in by_ticket else None,
            "处置": "留在在办库",
        } for row in kept]

    def read_gate(self):
        # Check the settlement view, the archive and the event queue on one committed snapshot.
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn, conn.begin():
            settlement = conn.execute(text(f"""
                SELECT ticket, legacy_hours::text AS legacy_hours, revision, shadow_ticks,
                       shadow_revision, location,
                    CASE
                        WHEN mod(legacy_hours, 0.25) <> 0 THEN '不可转换'
                        WHEN shadow_ticks IS NULL THEN '待回填'
                        WHEN shadow_revision IS DISTINCT FROM revision THEN '版次不一致'
                        WHEN shadow_ticks * 0.25 <> legacy_hours THEN '刻度不一致'
                        ELSE '一致'
                    END AS status
                FROM {self.settlement} ORDER BY ticket
            """)).mappings().all()
            archived = conn.execute(text(f"""
                SELECT ticket, revision, legacy_hours::text AS legacy_hours, shadow_ticks,
                       shadow_revision, archived_batch
                FROM {self.archive} ORDER BY ticket, revision
            """)).mappings().all()
            events = conn.execute(text(f"""
                SELECT seq, phase, after_batch, ticket, legacy_hours::text AS legacy_hours,
                       revision, status, observed_revision
                FROM {self.events} ORDER BY seq
            """)).mappings().all()
            return settlement, archived, events

    def close(self):
        with self.engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {self.schema} CASCADE"))
