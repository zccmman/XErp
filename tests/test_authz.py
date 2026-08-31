"""P1-03 TDD：Casbin 鉴权（RBAC + 账套 domain）与 Agent 自治额度。"""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from kernel.authz import (
    AuthzError,
    check_agent_quota,
    enforce,
    grant_ledger_role,
    revoke_ledger_role,
)
from kernel.db.base import Base
from kernel.db.models import Subject
from kernel.events import E
from kernel.seed import seed_demo_ledger


@pytest.fixture()
def ctx():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    s.commit()

    def mk(name: str, type_: str = "user", limit: str | None = None) -> Subject:
        sub = Subject(type=type_, display_name=name, autonomy_level=1,
                      daily_voucher_limit=Decimal(limit) if limit else None)
        s.add(sub)
        s.commit()
        return sub

    ids["mk"] = mk
    yield {"s": s, "ids": ids}
    s.close()


def test_grant_and_enforce_direct_policy(ctx):
    s, ids = ctx["s"], ctx["ids"]
    ls = ids["ledger_set_id"]
    acc = ids["mk"]("会计小王")
    with pytest.raises(AuthzError):
        enforce(s, actor_id=acc.id, ledger_set_id=ls, action="ledger:read")
    grant_ledger_role(s, ledger_set_id=ls, subject_id=acc.id, role="accountant")
    enforce(s, actor_id=acc.id, ledger_set_id=ls, action="ledger:read")
    enforce(s, actor_id=acc.id, ledger_set_id=ls, action="voucher:post")


def test_unauthorized_denied(ctx):
    import pytest as _pytest

    s, ids = ctx["s"], ctx["ids"]
    ls = ids["ledger_set_id"]
    stranger = ids["mk"]("陌生人")
    with _pytest.raises(AuthzError):
        enforce(s, actor_id=stranger.id, ledger_set_id=ls, action="voucher:create")


def test_roles_and_domain_isolation(ctx):
    s, ids = ctx["s"], ctx["ids"]
    ls1 = ids["ledger_set_id"]
    acc = ids["mk"]("会计小李")
    grant_ledger_role(s, ledger_set_id=ls1, subject_id=acc.id, role="accountant")
    # 账套内：会计可记账、不可审批
    enforce(s, actor_id=acc.id, ledger_set_id=ls1, action="voucher:create")
    enforce(s, actor_id=acc.id, ledger_set_id=ls1, action="voucher:push")
    enforce(s, actor_id=acc.id, ledger_set_id=ls1, action="ledger:read")
    with pytest.raises(AuthzError):
        enforce(s, actor_id=acc.id, ledger_set_id=ls1, action="voucher:approve")
    # 账套隔离：换账套即无权限
    ls2 = "0" * 32
    with pytest.raises(AuthzError):
        enforce(s, actor_id=acc.id, ledger_set_id=ls2, action="voucher:create")
    # 回收角色后权限消失
    revoke_ledger_role(s, ledger_set_id=ls1, subject_id=acc.id, role="accountant")
    with pytest.raises(AuthzError):
        enforce(s, actor_id=acc.id, ledger_set_id=ls1, action="voucher:create")


def test_admin_wildcard(ctx):
    s, ids = ctx["s"], ctx["ids"]
    ls = ids["ledger_set_id"]
    boss = ids["mk"]("老板")
    grant_ledger_role(s, ledger_set_id=ls, subject_id=boss.id, role="admin")
    for act in ("ledger:manage", "voucher:create", "voucher:approve",
                "voucher:cancel", "ledger:close", "ledger:read"):
        enforce(s, actor_id=boss.id, ledger_set_id=ls, action=act)


def test_agent_quota_exceeded(ctx):
    s, ids = ctx["s"], ctx["ids"]
    bot = ids["mk"](name="记账Agent", type_="agent", limit="500.00")
    from kernel.ledger import append_event

    def simulate(amount: str, occurred: object) -> None:
        append_event(
            s, ledger_set_id=ids["ledger_set_id"], event_type=E.VOUCHER_CREATED,
            aggregate_id=f"v-{amount}-{occurred}", payload={"total_debit": amount},
            actor={"type": "agent", "id": bot.id}, occurred_at=occurred,
        )
        s.commit()

    # 额度按「创建时刻的 UTC 日」统计：先模拟当日已用 400
    from kernel.db.models import utcnow as _utcnow

    simulate("400.00", _utcnow().replace(microsecond=0))
    with pytest.raises(AuthzError) as ei:
        check_agent_quota(s, actor_id=bot.id, voucher_amount=Decimal("101.00"))
    assert "自治额度不足" in str(ei.value)
    # 400 + 100 = 500 恰好不超
    check_agent_quota(s, actor_id=bot.id, voucher_amount=Decimal("100.00"))
    # 人类主体不受限
    human = ids["mk"](name="人类")
    check_agent_quota(s, actor_id=human.id, voucher_amount=Decimal("999999"))
    # 隔日事件不计入当日
    simulate("99999.00", _utcnow().replace(microsecond=0) - timedelta(days=1))
    check_agent_quota(s, actor_id=bot.id, voucher_amount=Decimal("100.00"))


def test_agent_without_limit_unrestricted(ctx):
    s, ids = ctx["s"], ctx["ids"]
    bot = ids["mk"](name="无限Agent", type_="agent", limit=None)
    check_agent_quota(s, actor_id=bot.id, voucher_amount=Decimal("99999999"))
