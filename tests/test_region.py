import json
import os
import sys
import tempfile
import threading
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src import rules
from src.repository import Repository
from src.service import Service
from src.domain import DomainError


def _payload(primary, orgs):
    return {
        "primary_object_id": primary,
        "secondary_object_id": "DEB-9",
        "tca": "2026-09-28T12:00:00+00:00",
        "miss_distance_m": 120,
        "covariance_m": 100,
        "fuel_budget_m_s": 5,
        "track_age_hours": 1,
        "operating_organizations": orgs,
    }


class RegionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _make_legacy(self, created_by, orgs):
        """直接落一条 region=NULL 的历史数据，模拟回填前的存量记录。"""
        payload = _payload("SAT-%s-%s" % (created_by, uuid.uuid4().hex[:8]), orgs)
        conn = self.repo.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            stable_key = "%s|%s|%s" % (
                payload["primary_object_id"],
                payload["secondary_object_id"],
                payload["tca"],
            )
            cur = conn.execute(
                "INSERT INTO items(entity_type,stable_key,status,version,payload,region,created_by,created_role,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    rules.ENTITY_TYPE,
                    stable_key,
                    rules.INITIAL_STATUS,
                    1,
                    json.dumps(payload, ensure_ascii=False),
                    None,
                    created_by,
                    "analyst",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            item_id = cur.lastrowid
            conn.execute("COMMIT")
            return item_id
        finally:
            conn.close()

    # ---- 建单记辖区 ----
    def test_create_records_region(self):
        item = self.service.create_item(_payload("SAT-1", ["Org-A"]), "analyst-1", "analyst", region="east")
        self.assertEqual(item["region"], "east")

    def test_create_requires_region(self):
        with self.assertRaises(DomainError) as context:
            self.service.create_item(_payload("SAT-1", ["Org-A"]), "analyst-1", "analyst")
        self.assertEqual(context.exception.code, "region_required")

    # ---- 读取按辖区隔离 ----
    def test_list_confined_to_region(self):
        east = self.service.create_item(_payload("SAT-E", ["Org-A"]), "analyst-1", "analyst", region="east")
        west = self.service.create_item(_payload("SAT-W", ["Org-B"]), "analyst-2", "analyst", region="west")
        east_items = self.service.list_items(region="east", role="analyst")
        west_items = self.service.list_items(region="west", role="analyst")
        self.assertEqual([i["id"] for i in east_items], [east["id"]])
        self.assertEqual([i["id"] for i in west_items], [west["id"]])

    def test_get_other_region_denied(self):
        west = self.service.create_item(_payload("SAT-W", ["Org-B"]), "analyst-2", "analyst", region="west")
        with self.assertRaises(DomainError) as context:
            self.service.get_item(west["id"], region="east", role="analyst")
        self.assertEqual(context.exception.code, "region_mismatch")
        self.assertEqual(context.exception.status, 403)

    # ---- 修改按辖区隔离，且不留痕迹 ----
    def test_act_other_region_denied_without_trace(self):
        created = self.service.create_item(_payload("SAT-1", ["Org-A"]), "analyst-1", "analyst", region="east")
        item = self.service.get_item(created["id"], region="east", role="analyst")
        before_audit = len(item["audit"])
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "assess", {"hours_to_tca": 18}, "analyst-2", "analyst", item["version"], region="west")
        self.assertEqual(context.exception.code, "region_mismatch")
        # 越权挡回：审计链与动作表都不新增任何记录
        after = self.service.get_item(item["id"], region="east", role="analyst")
        self.assertEqual(len(after["audit"]), before_audit)
        self.assertEqual(after["version"], item["version"])

    def test_add_source_other_region_denied(self):
        item = self.service.create_item(_payload("SAT-1", ["Org-A"]), "analyst-1", "analyst", region="east")
        with self.assertRaises(DomainError) as context:
            self.service.add_source(
                item["id"],
                {"source_type": "radar", "external_id": "SRC-1", "observed_at": "2026-09-20T00:00:00+00:00", "miss_distance_m": 100, "covariance_m": 100},
                "analyst-2", "analyst", region="west",
            )
        self.assertEqual(context.exception.code, "region_mismatch")

    # ---- 历史数据回填 ----
    def test_backfill_by_creator_and_org(self):
        # 创建者在辖区表中 -> 回填
        by_creator = self._make_legacy("analyst-1", ["Unknown-Org"])
        # 组织在辖区表中 -> 回填
        by_org = self._make_legacy("ghost-user", ["Org-A"])
        # 两者都认不出 -> 挂起
        pending = self._make_legacy("ghost-user", ["Unknown-Org"])
        result = self.service.backfill_regions("regulator-1", "regulator")
        self.assertEqual(result["backfilled"], 2)
        self.assertEqual(result["pending_claim"], 1)
        self.assertEqual(self.repo.get_item(by_creator)["region"], "east")
        self.assertEqual(self.repo.get_item(by_org)["region"], "east")
        self.assertIsNone(self.repo.get_item(pending)["region"])

    def test_backfill_conflicting_signals_pending(self):
        # 创建者指向 east，组织指向 west，相互冲突 -> 挂起
        item_id = self._make_legacy("analyst-1", ["Org-B"])
        result = self.service.backfill_regions("regulator-1", "regulator")
        self.assertEqual(result["backfilled"], 0)
        self.assertEqual(result["pending_claim"], 1)
        self.assertIsNone(self.repo.get_item(item_id)["region"])

    def test_backfill_requires_regulator(self):
        self._make_legacy("analyst-1", ["Org-A"])
        with self.assertRaises(DomainError) as context:
            self.service.backfill_regions("analyst-1", "analyst")
        self.assertEqual(context.exception.code, "forbidden")

    # ---- 挂起认领 ----
    def test_pending_items_cannot_be_acted(self):
        item_id = self._make_legacy("ghost-user", ["Unknown-Org"])
        with self.assertRaises(DomainError) as context:
            self.service.act(item_id, "assess", {"hours_to_tca": 18}, "analyst-1", "analyst", 1, region="east")
        self.assertEqual(context.exception.code, "pending_claim")

    def test_regulator_claim(self):
        item_id = self._make_legacy("ghost-user", ["Unknown-Org"])
        result = self.service.claim_items([item_id], "regulator-1", "regulator", region="east")
        self.assertEqual(result["claimed"], [item_id])
        self.assertEqual(result["region"], "east")
        self.assertEqual(self.repo.get_item(item_id)["region"], "east")

    def test_claim_requires_regulator(self):
        item_id = self._make_legacy("ghost-user", ["Unknown-Org"])
        with self.assertRaises(DomainError) as context:
            self.service.claim_items([item_id], "analyst-1", "analyst", region="east")
        self.assertEqual(context.exception.code, "forbidden")

    def test_claim_requires_region(self):
        item_id = self._make_legacy("ghost-user", ["Unknown-Org"])
        with self.assertRaises(DomainError) as context:
            self.service.claim_items([item_id], "regulator-1", "regulator")
        self.assertEqual(context.exception.code, "region_required")

    def test_claim_nonexistent_item(self):
        with self.assertRaises(DomainError) as context:
            self.service.claim_items([9999], "regulator-1", "regulator", region="east")
        self.assertEqual(context.exception.code, "item_not_found")

    def test_pending_pool_visible_to_regulator(self):
        pending = self._make_legacy("ghost-user", ["Unknown-Org"])
        self._make_legacy("analyst-1", ["Org-A"])  # 这条会被回填，不在认领池
        self.service.backfill_regions("regulator-1", "regulator")
        pool = self.service.list_pending_claim("regulator-1", "regulator")
        self.assertEqual([i["id"] for i in pool], [pending])

    # ---- 认领并发：先到先得 ----
    def test_concurrent_claim_first_wins(self):
        ids = [self._make_legacy("ghost-user", ["Unknown-Org"]) for _ in range(3)]
        errors = []
        successes = []

        def claim(regulator):
            try:
                result = self.service.claim_items(ids, regulator, "regulator", region="east")
                successes.append(result)
            except DomainError as exc:
                errors.append(exc.code)

        t1 = threading.Thread(target=claim, args=("regulator-1",))
        t2 = threading.Thread(target=claim, args=("regulator-2",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(successes), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0], "claim_occupied")
        # 先到的成立：三条记录都归入 east
        for item_id in ids:
            self.assertEqual(self.repo.get_item(item_id)["region"], "east")


if __name__ == "__main__":
    unittest.main()
