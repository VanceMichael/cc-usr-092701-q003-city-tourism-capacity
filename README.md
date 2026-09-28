# 城市文旅活动容量调度

假期文旅活动（演出、游园、古建活动、京郊节庆）共用一张日程，承办方分散、
临时加场/限流/入境团队安排互相不同步。本项目提供一个容量调度服务，统一管理
**活动场次、场地容量、票种、团队预约与现场核验**，并把每一次安全调整、取消、
转场、补偿决定留在同一条时间线上。

当前版本：SQLite 持久化的纯 Python 服务（无第三方依赖），可直接作为领域内核
嵌入 Web/RPC 层；`src/city_tourism_capacity/context.py` 的领域资料读取与
校验能力保持不变。

## 参与方与权限

| 角色 | 能做什么 | 规则 |
| --- | --- | --- |
| 文旅值班调度员 | 排期、安全容量调整、取消/结束场次、授权免费名额与跨区调配、补偿决定、全量视图 | S17 |
| 景区工作人员 | **只接触本场次名单**、本场次现场核验 | S15/S19/S18/S20 |
| 旅行社 | **只能修改本社代码下的团队**，整团预约/退款/申请转场 | S16/S01 |
| 游客服务人员 | 只读协助，无修改权 | S16 |

## 核心不变量

1. **容量硬上限（S05）**：任意时刻 `confirmed 预约合计 ≤ 当前 safe_capacity`。
   名额不是加减计数器，而是以"已确认预约"的事实重算，退款释放的名额天然不会
   被重复卖出。
2. **整团原子（S01）**：一个团队只有一条预约，qty=团队人数；确认/候补/转场/
   退款都作用于整团，不允许部分成员越过安全上限。
3. **终态冻结（S04/S12）**：场次 `finished`/`cancelled` 后，迟到的退款/转场
   回执一律拒绝并留痕，前一天已结束的场次不会被重开。
4. **退款幂等（S09）**：`UNIQUE(reservation_id, reason)` + 客户端幂等键，
   同一预约同一原因只退一次。
5. **授权留痕（S17）**：免费名额签发、跨区域调配必须持有调度员授权单。

## 并发与崩溃恢复

- SQLite WAL + 每个写事务 `BEGIN IMMEDIATE`，写操作在数据库层串行化；
  "读余量 → 判定 → 写状态"在同一事务内完成，应用层加锁退避重试。
  多线程并发测试：100 抢 50 张恰好 50 确认 50 候补；25 并发退款 + 50 并发
  补位，释放的每个名额只被卖出一次。
- 退款 outbox（`refunds.status='pending'`）与候补队列
  （`reservations.status='waitlisted'`）均持久化；服务重启后调用
  `resume_pending()` 继续处理，幂等不重（S08）。
- 候补按创建时间 **FIFO**：队首整团放不下就继续等，不会跳过整团拆散确认（S07）。

## 规则目录

所有拒绝与自动决策都带规则编号（S01–S20），完整文案见
[`src/city_tourism_capacity/rules.py`](src/city_tourism_capacity/rules.py)。
运营在解释视图里看到的每条限制都能回溯到该目录。

## 主要 API（`CapacityService`）

```python
from src.city_tourism_capacity.service import CapacityService
from src.city_tourism_capacity.store import Store
from src.city_tourism_capacity.models import Actor, TicketKind

svc = CapacityService(Store("data/tourism.db"))   # 或 ":memory:"

# 排期与票种
svc.create_venue("v1", "景山公园", "西城区")
svc.create_session("s1", "v1", "中秋国风夜演", "演出",
                   "2026-09-30T19:30", "2026-09-30T21:30", 200,
                   actor=Actor.dispatcher("lin"))
svc.add_ticket_type("t1", "s1", "观演票", TicketKind.PAID, 200, price=12800)

# 整团预约（散客 team_id=None；idem_key 支持中断重投）
svc.register_team("g1", "CITS", "赵领队", 12, "13800000008")
svc.book(Actor.agency("a1", "CITS"), "s1", "t1", 12,
         visitor_name="赵领队", team_id="g1", idem_key="order-1")

# 限流（最晚确认者先挤出；有偏好转场偏好的整团先尝试原子转场）
svc.adjust_safe_capacity(Actor.dispatcher("lin"), "s1", 180, "疏散通道收窄")

# 授权：免费名额 / 跨区域调配
svc.grant_authorization(Actor.dispatcher("lin"), "a1",
                         "free_quota", "s1", {"purpose": "入境团队"})

# 退款、取消、转场、补偿、核验
svc.request_refund(actor, reservation_id, "行程变更", idem_key="rf-1")
svc.cancel_session(Actor.dispatcher("lin"), "s1", "暴雨红色预警")
svc.reroll_reservation(Actor.agency("a1", "CITS"), reservation_id, "s2")
svc.decide_compensation(Actor.dispatcher("lin"), reservation_id, "voucher", 5000, "暴雨补偿")
svc.check_in(Actor.site_staff("wu", "s1"), reservation_id)

# 服务（重）启动后继续处理挂起退款与候补
svc.resume_pending()

# 运营解释视图：每个名额为何分配、哪条规则限制、最终安排
svc.explain_session("s1")
svc.timeline("s1")          # 统一时间线
svc.session_roster(Actor.site_staff("wu", "s1"), "s1")  # 本场名单
```

## 代码结构

```
src/city_tourism_capacity/
├── context.py   # 领域资料读取/校验（原有）
├── rules.py     # S01–S20 规则目录、PolicyError（每条拒绝带规则编号）
├── models.py    # 角色、场次/预约/退款状态、时间线类型、Actor
├── store.py     # SQLite 表结构、WAL、BEGIN IMMEDIATE 串行写事务
├── service.py   # 调度服务：售票/候补/限流/取消/转场/退款/核验/恢复/解释
└── demo.py      # 端到端情景演示
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

- `tests/test_context.py`：领域资料校验（原有）
- `tests/test_service.py`：整团原子、权限隔离、限流挤出与 FIFO 递补、
  取消/转场/补偿、免费名额授权、跨区授权、迟到回执不重开、崩溃恢复、
  解释视图
- `tests/test_concurrency.py`：多连接多线程并发售票不超卖、并发退款+
  补位名额不重复卖出、退款幂等

## 端到端演示

```bash
python3 -m src.city_tourism_capacity.demo
```

串联：多类型活动排期 → 入境团队免费名额授权 → 满场候补 → 临时限流 →
模拟进程中断与恢复 → 候补整团顶位 → 越权操作被拒 → 跨区转场授权 →
隔日迟到回执被拒 → 统一时间线与 `explain_session` 解释视图。

## 领域资料命令行检查（原有能力）

```bash
python3 -m src.city_tourism_capacity.context fixtures/context.json
python3 -m compileall -q src tests
```
