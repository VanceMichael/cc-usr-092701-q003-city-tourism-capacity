# 城市文旅活动容量调度

面向假期文旅值班调度的容量调度服务：统一管理活动场次、场地容量、票种、
团队预约与现场核验，把安全调整、取消、转场、补偿留在同一条场次时间线上，
并支持逐名额的分配解释。

## 解决的问题

- 演出、游园、古建活动、京郊节庆共用一张日程，但容量与团队数据集中管理。
- **并发售票/退款不超卖**：全部写操作在 `BEGIN IMMEDIATE` 串行事务内完成，
  容量检查与占座原子发生；退款释放的名额在同一事务内按候补顺序递补，
  不会被重复卖出。
- **团队整体处理**：一个团队的多张预约是不可分割单元，剩余名额放不下整个
  团队时整体候补或整体移出，不会让部分成员越过安全上限。
- **迟到回执不重开**：场次结束/取消即终态，迟到支付回执、补录核验一律拒绝；
  结束时未支付的占座自动取消并退款。
- **崩溃恢复**：退款（`pending`）与候补（`waiting`）全部落库，重启后
  `recover` 继续未完成的退款与候补递补。
- **权限隔离**：景区工作人员只接触本景区场次名单并核验；旅行社只能修改
  本社团队；安全调整、取消、转场、补偿、跨区域调配、免费名额授权仅调度员。
- **可解释**：`timeline` 给出场次的完整决策时间线；`explain` 逐名额说明
  分配结果、触发的规则码（如 `safety_capacity_reduced`）与游客最终安排。

## 参与方（角色）

| 角色 | 能力 |
| --- | --- |
| `dispatcher` 文旅调度员 | 安全调整、取消、转场（含跨区域）、补偿决定、免费名额授权、全部只读视图 |
| `venue_staff` 景区工作人员 | 仅本景区场次名单、现场核验（携带 `X-Venue-Id`） |
| `agency` 旅行社 | 本社团队的预约、退款、候补、同区域转场（携带 `X-Agency-Id`） |
| `visitor_service` 游客服务人员 | 散客售票、退款、候补 |

## 主要接口

```
POST /venues /activities /sessions /ticket-types /teams
POST /sessions/{id}/sell            售票（团队整体，容量不足可候补）
POST /sessions/{id}/safety          安全容量调整（超员团队整体移出）
POST /sessions/{id}/cancel          取消场次（统一退款、关闭候补）
POST /sessions/{id}/free-grants     免费/授权名额发放
GET  /sessions/{id}/roster          本场次名单（景区工作人员限本景区）
GET  /sessions/{id}/timeline        场次决策时间线
GET  /sessions/{id}/explain         运营解释视图（逐名额+规则码+时间线）
POST /bookings/{id}/confirm-payment 支付回执（已关闭场次拒绝重开）
POST /bookings/{id}/refund          退款并同事务递补候补
POST /bookings/{id}/transfer        整体转场（跨区域需调度员）
POST /bookings/{id}/compensation    补偿决定（refund / transfer / voucher）
POST /bookings/{id}/admit           现场核验入场
POST /recover                       恢复未完成退款与候补（启动时自动执行）
```

鉴权通过请求头：`X-Actor-Role`、`X-Actor-Name`，旅行社加 `X-Agency-Id`，
景区工作人员加 `X-Venue-Id`。

## 运行

```bash
python3 -m src.city_tourism_capacity.__main__ seed --db tourism.db
python3 -m src.city_tourism_capacity.__main__ serve --db tourism.db --port 8080
```

## 测试

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
```

测试覆盖：并发售票/退款不超卖（含 40 个真实 HTTP 并发请求）、团队整体
候补与降容移出、旅行社/景区权限隔离、免费名额授权、跨区域授权、迟到回执
拒绝、结束场次不可重开、崩溃后退款与候补恢复、时间线与逐名额解释。

## 领域资料

`fixtures/context.json` 保存领域参与方、事实与约束的演示资料，由
`src/city_tourism_capacity/context.py` 读取与校验：

```bash
python3 -m src.city_tourism_capacity.context fixtures/context.json
```

数据均为演示用虚构内容。
