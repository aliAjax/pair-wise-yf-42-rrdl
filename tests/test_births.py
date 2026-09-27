import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.domain import Actor, BatchValidationError, PermissionDenied
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class BirthRegistrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.sire = self.service.create(
            self.admin, "animal", {"name": "公兽", "sex": "male"}
        )
        self.dam = self.service.create(
            self.admin, "animal", {"name": "母兽", "sex": "female"}
        )
        self.pairing = self.service.create(
            self.admin, "pairing", {"proposed_by": "coordinator"}
        )
        self.service.transition(
            self.admin,
            self.pairing["id"],
            "approve",
            {"sire_id": self.sire["id"], "dam_id": self.dam["id"], "approvals": ["vet-1"]},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _complete(self, offspring, actor=None):
        return self.service.transition(
            actor or self.admin,
            self.pairing["id"],
            "complete",
            {"offspring": offspring},
        )

    def _cub(self, index, **overrides):
        item = {
            "id": "cub-%d" % index,
            "name": "幼崽%d" % index,
            "sex": "male" if index % 2 else "female",
            "birth_date": "2026-06-01",
        }
        item.update(overrides)
        return item

    def test_success_creates_archives_with_parents(self):
        updated = self._complete([self._cub(1), self._cub(2, sex="female")])
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(updated["data"]["offspring_ids"], ["cub-1", "cub-2"])
        for index in (1, 2):
            cub = self.service.get("cub-%d" % index)
            self.assertEqual(cub["status"], "active")
            self.assertEqual(cub["data"]["sire_id"], self.sire["id"])
            self.assertEqual(cub["data"]["dam_id"], self.dam["id"])
            self.assertEqual(cub["data"]["birth_date"], "2026-06-01")

    def test_litter_query_by_parents_returns_whole_litter(self):
        other_dam = self.service.create(
            self.admin, "animal", {"name": "其他母兽", "sex": "female"}
        )
        other_pairing = self.service.create(
            self.admin, "pairing", {"proposed_by": "coordinator"}
        )
        self.service.transition(
            self.admin,
            other_pairing["id"],
            "approve",
            {"sire_id": self.sire["id"], "dam_id": other_dam["id"], "approvals": ["vet-1"]},
        )
        self.service.transition(
            self.admin,
            other_pairing["id"],
            "complete",
            {"offspring": [self._cub(9)]},
        )
        self._complete([self._cub(1), self._cub(2)])

        by_sire = self.service.list("animal", filters={"sire_id": self.sire["id"]})
        self.assertEqual({item["id"] for item in by_sire}, {"cub-1", "cub-2", "cub-9"})
        litter = self.service.list(
            "animal", filters={"sire_id": self.sire["id"], "dam_id": self.dam["id"]}
        )
        self.assertEqual({item["id"] for item in litter}, {"cub-1", "cub-2"})
        self.assertEqual(
            self.service.list("animal", filters={"dam_id": other_dam["id"]})[0]["id"],
            "cub-9",
        )

    def test_duplicate_id_in_batch_rejects_everything_and_marks_rows(self):
        with self.assertRaises(BatchValidationError) as caught:
            self._complete([self._cub(1), self._cub(1, sex="female")])
        indexes = {error["index"] for error in caught.exception.errors}
        self.assertEqual(indexes, {0, 1})
        self.assertTrue(
            all(error["code"] == "duplicate_in_batch" for error in caught.exception.errors)
        )
        self._assert_nothing_landed()

    def test_existing_archive_id_rejects_batch(self):
        with self.assertRaises(BatchValidationError) as caught:
            self._complete([self._cub(1, id=self.sire["id"])])
        self.assertEqual(caught.exception.errors[0]["code"], "already_archived")
        self.assertEqual(caught.exception.errors[0]["index"], 0)
        self._assert_nothing_landed()

    def test_invalid_cub_data_rejects_batch_and_keeps_pairing_approved(self):
        bad_birth = (date.today() + timedelta(days=2)).isoformat()
        with self.assertRaises(BatchValidationError) as caught:
            self._complete(
                [
                    self._cub(1),
                    self._cub(
                        2,
                        name="  ",
                        sex="x",
                        birth_date="not-a-date",
                    ),
                    self._cub(3, birth_date=bad_birth),
                ]
            )
        by_index = {}
        for error in caught.exception.errors:
            by_index.setdefault(error["index"], set()).add(error["code"])
        self.assertEqual(by_index[1], {"invalid_sex", "missing_name", "invalid_birth_date"})
        self.assertEqual(by_index[2], {"future_birth_date"})
        self.assertNotIn(0, by_index)
        self._assert_nothing_landed()

    def test_empty_litter_rejected(self):
        from src.domain import ValidationError

        with self.assertRaises(ValidationError):
            self._complete([])
        self._assert_nothing_landed()

    def test_cannot_complete_twice(self):
        self._complete([self._cub(1)])
        from src.domain import InvalidTransition

        with self.assertRaises(InvalidTransition):
            self._complete([self._cub(2)])

    def test_role_not_allowed(self):
        with self.assertRaises(PermissionDenied):
            self._complete([self._cub(1)], actor=Actor("viewer", "viewer"))
        self._assert_nothing_landed()

    def test_audit_records_pairing_and_every_archive(self):
        self._complete([self._cub(1), self._cub(2)])
        pairing_log = self.service.audit_log(self.pairing["id"])
        self.assertEqual(
            [(item["action"], item["to_status"]) for item in pairing_log],
            [("create", "proposed"), ("approve", "approved"), ("complete", "completed")],
        )
        for index in (1, 2):
            log = self.service.audit_log("cub-%d" % index)
            self.assertEqual(len(log), 1)
            self.assertEqual(log[0]["action"], "birth_register")
            self.assertEqual(log[0]["detail"]["pairing_id"], self.pairing["id"])

    def _assert_nothing_landed(self):
        self.assertEqual(self.service.get(self.pairing["id"])["status"], "approved")
        self.assertIsNone(self.service.repository.get_entity("cub-1"))
        pairing = self.service.get(self.pairing["id"])
        self.assertNotIn("offspring", pairing["data"])
        self.assertNotIn("offspring_ids", pairing["data"])


if __name__ == "__main__":
    unittest.main()
