# 本地电表读数采集与用电量统计服务

一个本地运行的服务：接收设备上报的**累计电表读数**，按时间整理并计算相邻读数之间的
用电增量，支持按设备 + 时间窗口查询用电量统计。内置模拟设备，可一键跑通
「上报 → 统计 → 查询」全流程。

- 语言/框架：Python 3.14 + FastAPI + SQLite（标准库 `sqlite3`，WAL 持久化）
- 并发模型：进程内按设备加锁 + SQLite 单连接串行写，保证并发安全与幂等
- 入口：`main.py`（HTTP 服务）、`scripts/run_demo.py`（一键模拟演示）

---

## 1. 读数语义

- 每条读数是设备计数器在某一时刻的**累计值（cumulative reading）**，物理上只增不减。
- 一段时间的用电量 = 相邻两次读数之差：`delta = cur - prev`。
- 时间戳接受 ISO8601（`...Z` 或带偏移量如 `+08:00`），内部统一规范化为 UTC 字符串。
  因此 `2026-10-07T10:00:00Z` 与 `2026-10-07T18:00:00+08:00` 被视为**同一时刻**。
- 每条读数在入库时就计算并固化它相对「上一条已接受读数」的增量段，统计时只需对窗口内
  的增量段求和 —— 结果只依赖已落库的数据，**同样输入必然得到稳定结果**。
- **增量段的归属口径**：每段增量 `[上一条读数时刻, 本条读数时刻]` 归属到段末的读数。
  统计窗口包含哪些读数行，就只累加这些行携带的段；窗口外的行不参与，口径确定、可复现。

设备注册时需要两个物理参数：

| 参数 | 含义 |
| --- | --- |
| `capacity` | 计数器满度（回绕阈值），例如电表显示 6 位整数 + 1 位小数可填 `999999.9` |
| `max_delta` | 相邻两次上报之间物理上**可能的最大增量**，用于识别异常跳变 |

### 增量段类型 `delta_flag`

| 标志 | 触发条件 | 增量处理 |
| --- | --- | --- |
| `FIRST` | 该设备首条读数，没有前值 | `delta = null`，不计电量 |
| `NORMAL` | `cur >= prev` 且 `cur-prev <= max_delta` | `delta = cur - prev`（读数不变时为 0） |
| `WRAPAROUND` | `cur < prev`，但 `(capacity-prev)+cur` 在 `[0, max_delta]` 内 | 判定为计数器回绕，`delta = (capacity-prev)+cur` |
| `RESET` | `cur < prev` 且按回绕计算出的增量不合理（为负或超 `max_delta`） | 判定为设备重启/清零，**接受读数但 `delta = null`**，该段不计电量；读数成为新基准 |

> 设计要点：读数变小有两种完全不同的成因——**计数器回绕**（可精确补算）与**设备重启/
> 清零**（跨过的用电量不可知）。只有当"按回绕补算"的结果落在物理上限内时才认定为回绕；
> 否则宁可把这段标记为 `RESET` 不计电量，也**绝不算出一个不合理的巨大或负增量**。

---

## 2. 异常处理规则

| 场景 | 处理 | HTTP | `status` / `code` |
| --- | --- | --- | --- |
| 完全相同的 `(设备, 时刻, 读数)` 再次上报 | 幂等返回首次结果，**不新增行、不重复计电量** | 200 | `DUPLICATE` |
| 同一时刻上报**不同读数** | 拒绝并留痕，只保留先到的一条 | 422 | `CONFLICT_SAME_TS` |
| 时间戳早于已存最新读数（**时间倒序**） | 拒绝并留痕（要求按时间递增上报） | 422 | `OUT_OF_ORDER` |
| 单调上涨但增量超过 `max_delta` | 拒绝并留痕（疑似异常跳变/丢帧） | 422 | `VALUE_TOO_LARGE` |
| 读数为负或非有限数字 | 拒绝 | 422 | `NEGATIVE_OR_INVALID` |
| 时间戳无法解析 / 窗口 `start>end` | 拒绝 | 422 | 请求校验错误 / `INVALID_WINDOW` |
| 计数器回绕 | 接受，按补算增量计入 | 200 | `ACCEPTED` + `WRAPAROUND` |
| 设备重启/清零 | 接受为新基准，该段不计电量 | 200 | `ACCEPTED` + `RESET` |
| 设备未注册 | 拒绝 | 404 | `DEVICE_NOT_FOUND` |
| 重复注册同名设备 | 拒绝 | 409 | `DEVICE_ALREADY_EXISTS` |

所有被拒绝的上报都会写入 `rejected_reports` 表（含原因 `reason` 与中文说明 `detail`），
可审计"为什么这条没计入"。

### 离线空档

设备离线期间没有任何读数上报。统计规则：

- 空档窗口内没有读数行 → 用电量 **0**，**不会凭空产生用电量**；
- 离线后第一条读数相对离线前最后一条的差值，作为这一整段的真实增量**只计一次**；
- 重复上报不会产生第二行，因此**永远不会重复计入**。

---

## 3. 并发、幂等与持久化保证

- **按设备串行化**：同一设备的「查重 → 取最新 → 计算 → 写入」在同一把设备锁内完成；
  不同设备使用不同锁，可真正并行。
- **数据库约束兜底**：`readings` 主键为 `(device_id, ts)`，同一时刻不可能插入两行。
- **并发幂等**：同一读数被多个请求同时提交时，恰好 1 个 `ACCEPTED`，其余全部 `DUPLICATE`，
  统计电量不翻倍。
- **持久化**：SQLite `WAL` + `synchronous=FULL`，提交即落盘；服务重启后重新打开同一库文件，
  已计入的数据不丢失；重启后重放旧上报仍命中 `DUPLICATE`，不会重复计入。

---

## 4. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/devices` | 注册设备：`{"device_id","capacity","max_delta"}` |
| `GET`  | `/devices` | 设备列表 |
| `POST` | `/readings` | 上报读数：`{"device_id","timestamp","value"}` |
| `GET`  | `/devices/{id}/readings?start=&end=` | 读数明细（时间范围可选） |
| `GET`  | `/devices/{id}/statistics?start=&end=` | 时间窗口用电量统计 |
| `GET`  | `/health` | 健康检查 |

上报成功响应示例：

```json
{
  "device_id": "meter-A",
  "timestamp": "2026-10-07T16:00:00.000000Z",
  "value": 20.0,
  "status": "ACCEPTED",
  "delta_flag": "WRAPAROUND",
  "delta": 30.0,
  "reason": null
}
```

统计响应示例：

```json
{
  "device_id": "meter-A",
  "window_start": "2026-10-07T00:00:00.000000Z",
  "window_end": "2026-10-08T00:00:00.000000Z",
  "energy": 955.0,
  "segment_count": 6,
  "first_reading_ts": "2026-10-07T10:00:00.000000Z",
  "last_reading_ts": "2026-10-07T18:00:00.000000Z",
  "reset_gaps": 1
}
```

---

## 5. 运行与验证

环境同步（首次）：

```bash
uv sync
```

### 5.1 启动 HTTP 服务

```bash
uv run python main.py
# 自定义：METER_DB=/path/to/meter.db PORT=8000 uv run python main.py
```

手动验证：

```bash
curl -s -XPOST localhost:8000/devices -H 'Content-Type: application/json' \
  -d '{"device_id":"m1","capacity":1000,"max_delta":900}'
curl -s -XPOST localhost:8000/readings -H 'Content-Type: application/json' \
  -d '{"device_id":"m1","timestamp":"2026-10-07T10:00:00Z","value":990}'
curl -s -XPOST localhost:8000/readings -H 'Content-Type: application/json' \
  -d '{"device_id":"m1","timestamp":"2026-10-07T11:00:00Z","value":20}'   # 回绕 +30
curl -s "localhost:8000/devices/m1/statistics?start=2026-10-07T00:00:00Z&end=2026-10-08T00:00:00Z"
```

### 5.2 一键模拟演示（推荐先跑这个）

```bash
uv run python scripts/run_demo.py
```

脚本会在临时端口启动服务，用两台模拟设备跑通完整流程，覆盖：
正常计量、读数不变（0 增量）、**离线空档**、**计数器回绕**、**设备重启 RESET**、
**重复上报幂等**、**时间倒序被拒**、多设备上报、窗口统计与明细查询。

### 5.3 单元测试

```bash
uv run pytest                 # 简洁结果
uv run pytest -s | tee logs/test-run.log   # 打印业务分组 + 每个用例的中文说明与结果
```

日志按业务分组，可直接看出覆盖了哪些业务、每个业务有哪些用例以及结果。
当前共 **8 大业务 / 38 个用例，全部通过**：

| 测试文件 | 覆盖业务 |
| --- | --- |
| `tests/test_01_normal.py` | 相邻增量、0 增量、窗口统计、结果稳定、时区等价 |
| `tests/test_02_wraparound.py` | **计数回绕**：基本回绕、满度归零边界、计入统计、回绕 vs 重启判别 |
| `tests/test_03_reset.py` | **设备重启/清零**：不计电量、重启后基准延续、多次重启不为负 |
| `tests/test_04_rejections.py` | **时间倒序**、同时刻冲突、超上限、负数、非法时间戳、拒绝留痕 |
| `tests/test_05_duplicate.py` | **重复上报**幂等、HTTP 200、多次统计稳定 |
| `tests/test_06_concurrency.py` | **并发**：同读数 20 线程并发、乱序一致性、多设备并行、幂等 vs 冲突 |
| `tests/test_07_persistence.py` | **服务重启**：数据不丢、重放不重复、基准延续、文件落盘 |
| `tests/test_08_gaps_http.py` | **离线空档**不产生电量、空窗口、空档+重复、HTTP 端到端 |

---

## 6. 代码结构

```
app/
  models.py     # 数据模型与状态/原因枚举
  errors.py     # 业务异常（带错误码与 HTTP 状态）
  timeutil.py   # ISO8601 解析与 UTC 规范化（幂等去重的基础）
  storage.py    # SQLite 持久化（设备/读数/拒绝留痕）+ 按设备锁
  service.py    # 核心业务规则：回绕/重启判别、拒绝规则、窗口统计
  api.py        # FastAPI 路由与统一错误响应
  simulator.py  # 模拟设备上报（HTTP 客户端）
scripts/run_demo.py  # 一键起服务并跑模拟流程
tests/                # 按业务分组的 38 个用例
main.py               # 服务入口
```
