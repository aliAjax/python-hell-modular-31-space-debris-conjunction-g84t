from . import domain, rules
from .domain import DomainError, NotFoundError


class Service:
    def __init__(self, repository):
        self.repository = repository

    def _require_identity(self, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)

    def _require_region(self, region):
        if not region:
            raise DomainError("region_required", "需要辖区身份", 401)

    def _visible_item(self, item_id, role, region):
        item = self.repository.get_item(item_id)
        if item["region"] is not None and item["region"] == region:
            return item
        if role == "regulator" and item["region"] is None:
            return item
        raise NotFoundError("item_not_found", "业务实体不存在")

    def _writable_item(self, item_id, region):
        item = self.repository.get_item(item_id)
        if item["region"] is None or item["region"] != region:
            raise NotFoundError("item_not_found", "业务实体不存在")
        return item

    def create_item(self, payload, actor, role, region=None):
        self._require_identity(actor, role)
        self._require_region(region)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role, region
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        self._require_identity(actor, role)
        self._require_region(region)
        self._writable_item(item_id, region)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        normalized = domain.normalize_source(payload)
        if normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        return self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        self._require_identity(actor, role)
        self._require_region(region)
        item = self._writable_item(item_id, region)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id, actor, role, region)

    def get_item(self, item_id, actor, role, region=None):
        self._require_identity(actor, role)
        self._require_region(region)
        item = self._visible_item(item_id, role, region)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        item["suspended"] = item["region"] is None
        return item

    def list_items(self, actor, role, region=None, status=None):
        self._require_identity(actor, role)
        self._require_region(region)
        items = self.repository.list_items(status=status, region=region)
        for item in items:
            item["suspended"] = False
        return items

    def pending_claims(self, actor, role, region=None):
        self._require_identity(actor, role)
        self._require_region(region)
        if role != "regulator":
            raise DomainError("forbidden", "只有监管员可以查看待认领记录", 403)
        items = self.repository.list_items(unassigned=True)
        for item in items:
            item["suspended"] = True
        return items

    def claim(self, payload, actor, role, region=None):
        self._require_identity(actor, role)
        self._require_region(region)
        if role != "regulator":
            raise DomainError("forbidden", "只有监管员可以认领挂起记录", 403)
        item_ids = domain.normalize_claim(payload)
        claimed = self.repository.claim_items(item_ids, region, actor, role)
        return {"claimed": claimed, "region": region}

    def register_directory(self, payload, actor, role, region=None):
        self._require_identity(actor, role)
        self._require_region(region)
        if role != "regulator":
            raise DomainError("forbidden", "只有监管员可以维护辖区目录", 403)
        entry = domain.normalize_directory(payload)
        self.repository.upsert_directory(**entry)
        backfill = self.repository.backfill_regions()
        return {"directory": entry, "backfill": backfill}

    def state(self, actor, role, region=None):
        self._require_identity(actor, role)
        self._require_region(region)
        return self.repository.state_summary(region)
