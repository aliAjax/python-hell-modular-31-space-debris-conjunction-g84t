import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError, NotFoundError


def make_payload(primary, secondary, tca, organizations=None):
    return {
        "primary_object_id": primary,
        "secondary_object_id": secondary,
        "tca": tca,
        "miss_distance_m": 80,
        "covariance_m": 100,
        "fuel_budget_m_s": 4,
        "track_age_hours": 1,
        "operating_organizations": organizations or [],
    }


class RegionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.seq = 0

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _create(self, region="R-1", actor="analyst-1", organizations=None):
        self.seq += 1
        payload = make_payload(
            "SAT-%d" % self.seq,
            "DEB-%d" % self.seq,
            "2026-10-0%dT12:00:00+00:00" % (self.seq % 9 + 1),
            organizations,
        )
        return self.service.create_item(payload, actor, "analyst", region)

    def _legacy(self, creator, organizations=None):
        self.seq += 1
        payload = make_payload(
            "LEGACY-SAT-%d" % self.seq,
            "LEGACY-DEB-%d" % self.seq,
            "2026-09-0%dT06:00:00+00:00" % (self.seq % 9 + 1),
            organizations,
        )
        stable_key = "%s|%s|%s" % tuple(
            sorted([payload["primary_object_id"], payload["secondary_object_id"]]) + [payload["tca"]]
        )
        return self.repo.create_item(
            "space_conjunction", stable_key, "pending", payload, creator, "analyst", None
        )

    def test_create_records_region_and_scopes_reads(self):
        item = self._create("R-1")
        self.assertEqual(item["region"], "R-1")
        fetched = self.service.get_item(item["id"], "analyst-1", "analyst", "R-1")
        self.assertEqual(fetched["id"], item["id"])
        self.assertFalse(fetched["suspended"])

    def test_cross_region_get_and_act_look_like_missing(self):
        item = self._create("R-1")
        for args in [
            lambda: self.service.get_item(item["id"], "a2", "analyst", "R-2"),
            lambda: self.service.act(item["id"], "assess", {"hours_to_tca": 3}, "a2", "analyst", 1, "R-2"),
            lambda: self.service.add_source(item["id"], {
                "source_type": "radar", "external_id": "X-1", "observed_at": "2026-10-01T00:00:00Z",
                "miss_distance_m": 70, "covariance_m": 90,
            }, "a2", "analyst", "R-2"),
        ]:
            with self.assertRaises(NotFoundError) as context:
                args()
            self.assertEqual(context.exception.code, "item_not_found")
            self.assertEqual(context.exception.status, 404)
        with self.assertRaises(NotFoundError):
            self.service.get_item(99999, "a2", "analyst", "R-2")

    def test_cross_region_denial_leaves_no_trace(self):
        item = self._create("R-1")
        before = self.repo.audit_trail(item["id"])
        with self.assertRaises(NotFoundError):
            self.service.act(item["id"], "assess", {"hours_to_tca": 3}, "a2", "analyst", 1, "R-2")
        with self.assertRaises(NotFoundError):
            self.service.get_item(item["id"], "a2", "analyst", "R-2")
        after = self.repo.audit_trail(item["id"])
        self.assertEqual([event["id"] for event in before], [event["id"] for event in after])

    def test_missing_region_identity_rejected(self):
        with self.assertRaises(DomainError) as context:
            self.service.create_item(make_payload("A", "B", "2026-10-02T00:00:00Z"), "a", "analyst")
        self.assertEqual(context.exception.code, "region_required")
        self.assertEqual(context.exception.status, 401)
        with self.assertRaises(DomainError):
            self.service.list_items("a", "analyst")
        with self.assertRaises(DomainError):
            self.service.get_item(1, "a", "analyst")

    def test_list_and_state_are_region_scoped(self):
        first = self._create("R-1")
        second = self._create("R-2")
        r1_ids = [item["id"] for item in self.service.list_items("a", "analyst", "R-1")]
        r2_ids = [item["id"] for item in self.service.list_items("a", "analyst", "R-2")]
        self.assertEqual(r1_ids, [first["id"]])
        self.assertEqual(r2_ids, [second["id"]])
        state = self.service.state("a", "analyst", "R-1")
        self.assertEqual(state["counts"], {"pending": 1})
        self.assertEqual([item["id"] for item in state["items"]], [first["id"]])

    def test_backfill_by_creator(self):
        legacy = self._legacy("legacy-analyst")
        self.repo.upsert_directory("user", "legacy-analyst", "R-7")
        summary = self.repo.backfill_regions()
        self.assertEqual(summary["assigned"], [legacy["id"]])
        self.assertEqual(summary["suspended"], [])
        self.assertEqual(self.repo.get_item(legacy["id"])["region"], "R-7")
        events = [e["event_type"] for e in self.repo.audit_trail(legacy["id"])]
        self.assertIn("region_backfilled", events)

    def test_backfill_by_organization(self):
        legacy = self._legacy("unknown-user", ["Org-A", "Org-B"])
        self.repo.upsert_directory("org", "Org-A", "R-8")
        self.repo.upsert_directory("org", "Org-B", "R-8")
        summary = self.repo.backfill_regions()
        self.assertEqual(summary["assigned"], [legacy["id"]])
        self.assertEqual(self.repo.get_item(legacy["id"])["region"], "R-8")

    def test_unresolved_backfill_stays_suspended(self):
        conflicted = self._legacy("unknown-user", ["Org-A", "Org-B"])
        unknown = self._legacy("nobody")
        self.repo.upsert_directory("org", "Org-A", "R-8")
        self.repo.upsert_directory("org", "Org-B", "R-9")
        summary = self.repo.backfill_regions()
        self.assertEqual(summary["assigned"], [])
        self.assertEqual(sorted(summary["suspended"]), sorted([conflicted["id"], unknown["id"]]))
        self.assertIsNone(self.repo.get_item(conflicted["id"])["region"])
        r1_ids = [item["id"] for item in self.service.list_items("a", "analyst", "R-1")]
        self.assertNotIn(conflicted["id"], r1_ids)
        pending = self.service.pending_claims("reg-1", "regulator", "R-9")
        self.assertEqual(sorted(item["id"] for item in pending), sorted([conflicted["id"], unknown["id"]]))
        self.assertTrue(all(item["suspended"] for item in pending))

    def test_suspended_item_is_frozen_until_claimed(self):
        legacy = self._legacy("nobody")
        with self.assertRaises(NotFoundError):
            self.service.act(legacy["id"], "assess", {"hours_to_tca": 3}, "a", "analyst", 1, "R-1")
        with self.assertRaises(NotFoundError):
            self.service.get_item(legacy["id"], "a", "analyst", "R-1")
        regulator_view = self.service.get_item(legacy["id"], "reg-1", "regulator", "R-9")
        self.assertTrue(regulator_view["suspended"])
        with self.assertRaises(NotFoundError):
            self.service.act(legacy["id"], "assess", {"hours_to_tca": 3}, "reg-1", "regulator", 1, "R-9")

    def test_claim_assigns_region_and_workflow_resumes(self):
        legacy = self._legacy("nobody")
        result = self.service.claim({"item_ids": [legacy["id"]]}, "reg-1", "regulator", "R-9")
        self.assertEqual(result, {"claimed": [legacy["id"]], "region": "R-9"})
        self.assertEqual(self.repo.get_item(legacy["id"])["region"], "R-9")
        events = [e["event_type"] for e in self.repo.audit_trail(legacy["id"])]
        self.assertIn("region_claimed", events)
        item = self.service.act(legacy["id"], "assess", {"hours_to_tca": 3}, "a9", "analyst", 1, "R-9")
        self.assertEqual(item["status"], "assessed")
        self.assertEqual(self.service.pending_claims("reg-1", "regulator", "R-9"), [])

    def test_second_claim_sees_already_claimed(self):
        first = self._legacy("nobody")
        second = self._legacy("nobody")
        self.service.claim({"item_ids": [first["id"], second["id"]]}, "reg-1", "regulator", "R-9")
        with self.assertRaises(ConflictError) as context:
            self.service.claim({"item_ids": [first["id"], second["id"]]}, "reg-2", "regulator", "R-10")
        self.assertEqual(context.exception.code, "already_claimed")
        self.assertEqual(context.exception.status, 409)
        with self.assertRaises(ConflictError):
            self.service.claim({"item_ids": [first["id"], 99999]}, "reg-2", "regulator", "R-10")

    def test_concurrent_claims_single_winner(self):
        ids = [self._legacy("nobody")["id"] for _ in range(3)]
        outcomes = []

        def claim(name, region):
            try:
                self.service.claim({"item_ids": ids}, name, "regulator", region)
                outcomes.append((name, "ok"))
            except ConflictError:
                outcomes.append((name, "conflict"))

        threads = [
            threading.Thread(target=claim, args=("reg-1", "R-9")),
            threading.Thread(target=claim, args=("reg-2", "R-10")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(result for _, result in outcomes), ["conflict", "ok"])
        winner = [name for name, result in outcomes if result == "ok"][0]
        expected_region = "R-9" if winner == "reg-1" else "R-10"
        for item_id in ids:
            self.assertEqual(self.repo.get_item(item_id)["region"], expected_region)

    def test_claim_requires_regulator_and_valid_ids(self):
        legacy = self._legacy("nobody")
        with self.assertRaises(DomainError) as context:
            self.service.claim({"item_ids": [legacy["id"]]}, "a", "analyst", "R-1")
        self.assertEqual(context.exception.status, 403)
        with self.assertRaises(DomainError):
            self.service.pending_claims("a", "coordinator", "R-1")
        for bad in [{}, {"item_ids": []}, {"item_ids": ["x"]}, {"item_ids": [0]}]:
            with self.assertRaises(DomainError) as context:
                self.service.claim(bad, "reg-1", "regulator", "R-9")
            self.assertEqual(context.exception.code, "invalid_item_ids")

    def test_register_directory_triggers_backfill(self):
        legacy = self._legacy("legacy-analyst")
        with self.assertRaises(DomainError) as context:
            self.service.register_directory(
                {"subject_type": "user", "subject": "legacy-analyst", "region": "R-7"},
                "a", "analyst", "R-1",
            )
        self.assertEqual(context.exception.status, 403)
        result = self.service.register_directory(
            {"subject_type": "user", "subject": "legacy-analyst", "region": "R-7"},
            "reg-1", "regulator", "R-9",
        )
        self.assertEqual(result["backfill"]["assigned"], [legacy["id"]])
        self.assertEqual(self.repo.get_item(legacy["id"])["region"], "R-7")


if __name__ == "__main__":
    unittest.main()
