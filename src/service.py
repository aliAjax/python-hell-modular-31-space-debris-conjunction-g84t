from . import domain, rules
from .domain import DomainError


class Service:
    def __init__(self, repository):
        self.repository = repository

    # ---- 辖区校验 ----
    def _check_region(self, region):
        if rules.ENFORCE_REGION:
            return domain.require_region(region)
        return region

    def _can_cross_region(self, role):
        return role in rules.REGION_BYPASS_ROLES

    def _assert_item_region(self, item, region, role):
        """读取/修改前的辖区校验。

        必须在任何写入之前调用，越权直接挡回，不留痕迹。
        监管员可跨辖区（处理认领池）。
        """
        if not rules.ENFORCE_REGION or self._can_cross_region(role):
            return
        actor_region = self._check_region(region)
        if item["region"] is None:
            raise DomainError("pending_claim", "记录辖区待认领，暂不能处理", 409)
        if item["region"] != actor_region:
            raise DomainError("region_mismatch", "不能处理其他辖区的记录", 403)

    # ---- 建单 ----
    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        item_region = self._check_region(region)
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role, item_region
        )

    # ---- 来源记录 ----
    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        self._assert_item_region(item, region, role)
        normalized = domain.normalize_source(payload)
        result = self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )
        return result

    # ---- 动作 ----
    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS:
            self._assert_item_region(item, region, role)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id, region, role)

    # ---- 读取 ----
    def get_item(self, item_id, region=None, role=None):
        item = self.repository.get_item(item_id)
        if rules.ENFORCE_REGION and role is not None:
            self._assert_item_region(item, region, role)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        return item

    def list_items(self, status=None, region=None, role=None):
        if rules.ENFORCE_REGION and role is not None and not self._can_cross_region(role):
            region = self._check_region(region)
        return self.repository.list_items(status, region)

    def state(self, region=None, role=None):
        if rules.ENFORCE_REGION and role is not None and not self._can_cross_region(role):
            region = self._check_region(region)
        return self.repository.state_summary(region)

    # ---- 历史数据回填与认领 ----
    def backfill_regions(self, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.REGION_BYPASS_ROLES:
            raise DomainError("forbidden", "只有监管员可以回填辖区", 403)
        return self.repository.backfill_regions(rules.infer_region)

    def list_pending_claim(self, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.REGION_BYPASS_ROLES:
            raise DomainError("forbidden", "只有监管员可以查看认领池", 403)
        return self.repository.list_pending_claim()

    def claim_items(self, item_ids, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.REGION_BYPASS_ROLES:
            raise DomainError("forbidden", "只有监管员可以认领记录", 403)
        claim_region = self._check_region(region)
        return self.repository.claim_items(item_ids, claim_region, actor, role)
