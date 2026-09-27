from datetime import datetime

from .domain import (
    BatchValidationError,
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
    if isinstance(value, str):
        return value.strip()
    return value


def _record_birth(offspring, sire_id, dam_id, lookup):
    """Validate a whole litter and build archive payloads for each offspring.

    Either every item is valid (and summaries are returned) or a
    BatchValidationError is raised listing every conflicting cub.
    """
    errors = []
    summaries = []
    seen = {}
    today = datetime.now().date()
    for index, item in enumerate(offspring):
        if not isinstance(item, dict):
            errors.append({"index": index, "code": "invalid_entry", "reason": "幼崽资料必须是对象"})
            summaries.append(None)
            continue

        offspring_id = _clean(item.get("id"))
        name = _clean(item.get("name"))
        sex = _clean(item.get("sex"))
        birth_date = _clean(item.get("birth_date"))
        item_errors = []

        if not offspring_id:
            item_errors.append({"code": "missing_id", "reason": "缺少编号"})
        else:
            seen.setdefault(offspring_id, []).append(index)

        if not name:
            item_errors.append({"code": "missing_name", "reason": "缺少姓名"})
        if sex not in ("male", "female", "unknown"):
            item_errors.append({"code": "invalid_sex", "reason": "性别必须是 male、female 或 unknown"})

        parsed_date = None
        if not birth_date:
            item_errors.append({"code": "missing_birth_date", "reason": "缺少出生日期"})
        elif not isinstance(birth_date, str):
            item_errors.append({"code": "invalid_birth_date", "reason": "出生日期格式应为 YYYY-MM-DD"})
        else:
            try:
                parsed_date = datetime.strptime(birth_date, "%Y-%m-%d").date()
            except ValueError:
                item_errors.append({"code": "invalid_birth_date", "reason": "出生日期格式应为 YYYY-MM-DD"})
            else:
                if parsed_date > today:
                    item_errors.append({"code": "future_birth_date", "reason": "出生日期不能晚于今天"})

        for problem in item_errors:
            errors.append(dict(problem, index=index, offspring_id=offspring_id or None))
        summaries.append(
            {
                "id": offspring_id,
                "name": name,
                "sex": sex,
                "birth_date": birth_date,
                "sire_id": sire_id,
                "dam_id": dam_id,
            }
            if not item_errors
            else None
        )

    for offspring_id, indexes in seen.items():
        if len(indexes) > 1:
            for index in indexes:
                errors.append(
                    {
                        "index": index,
                        "offspring_id": offspring_id,
                        "code": "duplicate_in_batch",
                        "reason": "编号在本批次中重复",
                    }
                )
        elif _find_one(lookup, None, "id", offspring_id):
            errors.append(
                {
                    "index": indexes[0],
                    "offspring_id": offspring_id,
                    "code": "already_archived",
                    "reason": "该编号已有档案",
                }
            )

    if errors:
        raise BatchValidationError(
            "整批幼崽资料未通过校验，未建立任何档案", sorted(errors, key=lambda e: (e["index"], e["code"]))
        )
    return summaries


def _validate_pairing_completion(actor, entity, data, lookup):
    sire_id = entity["data"].get("sire_id")
    dam_id = entity["data"].get("dam_id")
    if not sire_id or not dam_id:
        raise ValidationError("approved pairing must carry sire_id and dam_id")
    offspring = data.get("offspring")
    summaries = _record_birth(offspring, sire_id, dam_id, lookup)
    return {"offspring": summaries, "offspring_ids": [item["id"] for item in summaries]}


CUSTOM_CREATE = {'animal': _validate_animal}
CUSTOM_TRANSITIONS = {
    ('pairing', 'approve'): _validate_pairing,
    ('pairing', 'complete'): _validate_pairing_completion,
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
    if lookup is None or value is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
