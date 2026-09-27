import re
from datetime import date, datetime, timedelta

from .domain import (
    BatchValidationError,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_animal(actor, data, lookup):
    if data.get("sex") not in ("male", "female", "unknown"):
        raise ValidationError("sex must be male, female or unknown")


def inbreeding_coefficient(sire, dam):
    if not sire or not dam:
        return 1.0
    sire_id = sire.get("id")
    dam_id = dam.get("id")
    if sire_id is None or dam_id is None:
        return 0.0
    if sire_id == dam_id:
        return 0.5
    if sire.get("sire_id") == dam_id or dam.get("sire_id") == sire_id:
        return 0.25
    return 0.0


def _validate_pairing(actor, entity, data, lookup):
    sire = _find_one(lookup, "animal", "id", data.get("sire_id"))
    dam = _find_one(lookup, "animal", "id", data.get("dam_id"))
    if not sire or not dam:
        raise ValidationError("pairing requires two existing animals")
    if sire["status"] != "active" or dam["status"] != "active":
        raise ValidationError("pairing animals must be active")
    if inbreeding_coefficient(sire["data"], dam["data"]) > 0.125:
        raise ValidationError("pairing exceeds inbreeding threshold")
    return {"approved_by": actor.user_id}


def _clean(value):
    return str(value).strip() if isinstance(value, str) else value


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_birth_date(value):
    if not isinstance(value, str) or not _DATE_RE.match(value):
        return "birth_date must be YYYY-MM-DD"
    try:
        born = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return "birth_date is not a real calendar date"
    if born > date.today():
        return "birth_date cannot be in the future"
    return None


def _validate_complete_pairing(actor, entity, data, lookup):
    sire = _find_one(lookup, "animal", "id", entity["data"].get("sire_id"))
    dam = _find_one(lookup, "animal", "id", entity["data"].get("dam_id"))
    if not sire or not dam:
        raise ValidationError("pairing parents are missing from the studbook")
    if sire["data"].get("sex") != "male":
        raise ValidationError("recorded sire is not a male animal")
    if dam["data"].get("sex") != "female":
        raise ValidationError("recorded dam is not a female animal")

    offspring = data.get("offspring")
    if not isinstance(offspring, list) or not offspring:
        raise ValidationError("offspring must be a non-empty list")

    seen_ids = {}
    for cub in offspring:
        cub_id = _clean(cub.get("id")) if isinstance(cub, dict) else None
        if cub_id:
            seen_ids[cub_id] = seen_ids.get(cub_id, 0) + 1

    conflicts = []
    normalized = []
    for index, cub in enumerate(offspring):
        reasons = []
        if not isinstance(cub, dict):
            conflicts.append({"index": index, "reasons": ["offspring entry must be an object"]})
            continue
        cub_id = _clean(cub.get("id"))
        name = _clean(cub.get("name"))
        sex = _clean(cub.get("sex"))
        birth_date = _clean(cub.get("birth_date"))
        if not cub_id:
            reasons.append("missing required field: id")
        else:
            if seen_ids.get(cub_id, 0) > 1:
                reasons.append("offspring id is duplicated within this batch")
            existing = _find_one(lookup, "animal", "id", cub_id)
            if existing:
                reasons.append("an animal record already exists for this id")
        if not name:
            reasons.append("missing required field: name")
        if sex not in ("male", "female"):
            reasons.append("sex must be male or female")
        birth_problem = _validate_birth_date(birth_date)
        if birth_problem:
            reasons.append(birth_problem)
        if reasons:
            conflicts.append({"index": index, "id": cub_id, "reasons": reasons})
        else:
            normalized.append(
                {
                    "id": cub_id,
                    "name": name,
                    "sex": sex,
                    "birth_date": birth_date,
                }
            )

    if conflicts:
        raise BatchValidationError(
            "%d of %d offspring failed validation; no records were created"
            % (len(conflicts), len(offspring)),
            conflicts=conflicts,
        )

    return {
        "offspring_ids": [cub["id"] for cub in normalized],
        "_offspring": normalized,
    }


CUSTOM_CREATE = {'animal': _validate_animal}
CUSTOM_TRANSITIONS = {
    ('pairing', 'approve'): _validate_pairing,
    ('pairing', 'complete'): _validate_complete_pairing,
}


class RuleEngine:
    ALIASES = {'animals': 'animal', 'pairings': 'pairing', 'transfers': 'transfer'}
    INITIAL_STATUS = {'animal': 'active', 'pairing': 'proposed', 'transfer': 'planned'}
    TRANSITIONS = {'animal': {'mark_deceased': (('active',), 'deceased'), 'quarantine_animal': (('active',), 'quarantined'), 'release_quarantine': (('quarantined',), 'active')}, 'pairing': {'approve': (('proposed',), 'approved'), 'reject': (('proposed',), 'rejected'), 'complete': (('approved',), 'completed')}, 'transfer': {'authorize': (('planned',), 'authorized'), 'ship': (('authorized',), 'in_transit'), 'arrive': (('in_transit',), 'completed')}}
    CREATE_REQUIRED = {'animal': ('name', 'sex'), 'pairing': ('proposed_by',), 'transfer': ('animal_id', 'from_institution', 'to_institution')}
    ACTION_REQUIRED = {('animal', 'mark_deceased'): ('cause',), ('animal', 'quarantine_animal'): ('reason',), ('pairing', 'approve'): ('sire_id', 'dam_id', 'approvals'), ('pairing', 'reject'): ('reason',), ('pairing', 'complete'): ('offspring',), ('transfer', 'authorize'): ('permit_id',), ('transfer', 'ship'): ('transport_id',), ('transfer', 'arrive'): ('arrival_date',)}
    CREATE_ROLES = {'animal': ('admin', 'registrar'), 'pairing': ('admin', 'coordinator'), 'transfer': ('admin', 'registrar')}
    ROLE_ACTIONS = {'mark_deceased': ('admin', 'veterinarian'), 'quarantine_animal': ('admin', 'veterinarian'), 'release_quarantine': ('admin', 'veterinarian'), 'approve': ('admin', 'coordinator'), 'reject': ('admin', 'coordinator'), 'complete': ('admin', 'coordinator'), 'authorize': ('admin', 'registrar'), 'ship': ('admin', 'registrar'), 'arrive': ('admin', 'registrar')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
