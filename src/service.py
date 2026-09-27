from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "pairing" and action == "complete":
            return self._register_litter(
                actor, entity, dict(data or {}), expected_version
            )
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def _register_litter(self, actor, pairing, data, expected_version):
        """Approve-time birth registration: build the whole litter atomically."""
        next_status, patch = self.rules.validate_transition(
            actor, pairing, "complete", data, self._lookup
        )
        if next_status != "completed":
            raise ConflictError("unexpected pairing status after validation")
        normalized = patch.pop("_offspring", [])
        sire_id = pairing["data"].get("sire_id")
        dam_id = pairing["data"].get("dam_id")
        offspring = [
            {
                "id": cub["id"],
                "data": {
                    "name": cub["name"],
                    "sex": cub["sex"],
                    "birth_date": cub["birth_date"],
                    "sire_id": sire_id,
                    "dam_id": dam_id,
                    "pairing_id": pairing["id"],
                },
            }
            for cub in normalized
        ]
        merged = dict(pairing["data"])
        merged.update(patch)
        expected = int(expected_version) if expected_version is not None else pairing["version"]
        try:
            updated, created = self.repository.complete_pairing_with_offspring(
                pairing["id"], expected, offspring, merged, actor.user_id
            )
        except ConflictError:
            raise
        for cub in created:
            self.audit.record(
                cub["id"],
                actor,
                "create",
                None,
                cub["status"],
                {"kind": "animal", "via": "birth_registration", "pairing_id": pairing["id"]},
            )
        self.audit.record(
            pairing["id"],
            actor,
            "complete",
            pairing["status"],
            updated["status"],
            {"offspring_ids": patch.get("offspring_ids", [])},
        )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def offspring_of(self, parent_id):
        parent = self.repository.get_entity(parent_id)
        if not parent:
            raise NotFoundError("entity not found: " + parent_id)
        field = "sire_id" if parent["data"].get("sex") == "male" else "dam_id"
        return self._lookup("animal", field, parent_id)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
