# 浓度汇总 API

汇编河段水质报告用的浓度汇总服务。两份汇总可能收录同一组测量，不同样组也可能
共用空白样；本服务按样组登记原始读数汇总，按当前空白样数据生成校正结果，
并为每次汇总保存当时采用的空白样数据快照。

技术栈：FastAPI + SQLite（Python 标准库 `sqlite3`，无额外 ORM），支持 Docker Compose 启动。

## 业务规则

- **只处理同一指标、同一单位的读数**：一次汇总涉及的所有样组必须指标
  （`analyte`）与单位（`unit`）一致，否则返回 409。
- **样组登记后固定**：按唯一编号登记，提交读数总和、独立测量方差之和、
  各空白样的使用次数；不可修改，重复登记返回 409。
- **每次测量只用一份空白样**：因此一个样组（或一次汇总）的空白样使用次数
  之和即测量总数。
- **空白样可另行登记、更正**：扣减值与方差通过 `POST /blanks` 登记、
  `PUT /blanks/{id}` 更正。样组登记时不要求空白样已存在。
- **汇总生成**：凭样组号、已有汇总号或两者组合生成新汇总；重叠样组只计一次。
  引用旧汇总时展开为其样组集合，按**当前**空白样数据重新计算。
- **查询旧汇总**：返回原结果与当时采用的空白样数据（快照），不随后续更正改变。
- **资料缺失**：样组号 / 汇总号 / 空白样任一缺失时，返回 404 并列出所缺编号，
  本次不生成汇总。

### 检出限复核

旧汇总的读数合计可能把未检出读数代填为检出限，导致校正均值偏高。复核流程：

- **整批补录 / 更正**（`PUT /detection-limit/supplements`）：实验室对已登记样组
  整批补录或更正，每个样组填写**未经空白扣减的检出读数之和**，并按空白样号与
  检出限填报未检出次数。**零未检出也必须明确填报为 0**，且样组用到的每个空白样
  都要覆盖。同一空白样下的未检出次数合计不得超过该样组对它的原使用次数。
  补录为覆盖式替换；任一样组校验失败则**整批不生效**（原子提交）。
- **发起复核**（`POST /reviews`）：提交历史汇总号与同单位浓度限值。
  - 沿用该汇总留档的**空白扣减快照**（不受之后空白样更正影响）；
  - 未检出读数按零至各自检出限计，求校正均值的上下界；
  - **下界高于限值 → 超限（exceed）**；**上界不高于限值 → 未超限（not_exceed）**；
    其余 → **无法判定（indeterminate）**。
- **缺少补录**：复核收录的样组若有任一未补录，返回 404 并列出样组号，
  本次不生成复核记录。
- **核算时点**：复核以开始核算时已生效的补录为准（事务内一致性快照）；
  核算期间仍可完成整批更正，更正不影响已生成的复核记录。
- **凭复核号查回**（`GET /reviews/{id}`）：原上下界、判定与完整核算依据
  （各样组补录快照、未检出明细、沿用的空白扣减）。

## 计算公式

设汇总去重后收录若干样组，空白样 `b` 的总使用次数为 `n_b`（各样组使用次数之和），
测量总数 `N = Σ n_b`：

```
校正均值 = ( Σ 读数总和 − Σ_b 扣减值_b × n_b ) / N
总方差   = Σ_b 方差_b × n_b²  +  Σ 独立测量方差之和
标准误   = sqrt(总方差) / N
```

### 检出限复核的上下界

设复核收录的样组已全部补录：`D` 为各样组检出读数之和，未检出明细按
`(空白样, 检出限 L, 次数 k)` 登记，汇总留档的空白扣减合计为 `B`，
测量总数为 `N`。未检出读数按零至各自检出限计：

```
未检出读数下界合计 = 0
未检出读数上界合计 = Σ k × L
校正均值下界 = ( D + 0 − B ) / N
校正均值上界 = ( D + Σ k×L − B ) / N
```

判定（限值为 `C`）：

| 条件 | 判定 |
| --- | --- |
| 下界 > C | `exceed`（超限） |
| 上界 ≤ C | `not_exceed`（未超限） |
| 其余（下界 ≤ C < 上界） | `indeterminate`（无法判定） |

## 启动

### Docker Compose（推荐）

```bash
docker compose up --build
```

服务监听 `http://localhost:8000`，交互式文档见 `http://localhost:8000/docs`。
SQLite 数据保存在命名卷 `app-data`（容器内 `/data/app.db`）。

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload          # 默认数据库 ./data/app.db，可用 DB_PATH 覆盖
```

### 测试

```bash
pip install -r requirements-dev.txt
python3 tests/test_api.py              # 或 pytest tests/
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/groups` | 登记样组（登记后固定；重复编号 409） |
| GET | `/groups/{id}` / `/groups` | 查询 / 列出样组 |
| POST | `/blanks` | 登记空白样（重复编号 409） |
| PUT | `/blanks/{id}` | 更正空白样的扣减值与方差 |
| GET | `/blanks/{id}` / `/blanks` | 查询 / 列出空白样当前数据 |
| POST | `/summaries` | 生成新汇总（样组号、汇总号或两者组合） |
| GET | `/summaries/{id}` / `/summaries` | 查询旧汇总（原结果 + 当时空白样数据） |
| PUT | `/detection-limit/supplements` | 整批补录 / 更正检出限数据（覆盖式替换，原子校验） |
| GET | `/detection-limit/supplements/{id}` / `/detection-limit/supplements` | 查询 / 列出检出限补录 |
| POST | `/reviews` | 发起检出限复核（历史汇总号 + 同单位浓度限值） |
| GET | `/reviews/{id}` / `/reviews` | 凭复核号查回原上下界、判定与核算依据 |
| GET | `/health` | 健康检查 |

### 登记样组

```bash
curl -X POST localhost:8000/groups -H 'Content-Type: application/json' -d '{
  "group_id": "SG-1", "analyte": "cod", "unit": "mg/L",
  "reading_sum": 105.0, "variance_sum": 4.0,
  "blank_usages": {"BLK-A": 10}
}'
```

### 登记 / 更正空白样

```bash
curl -X POST localhost:8000/blanks -H 'Content-Type: application/json' -d \
  '{"blank_id": "BLK-A", "deduction": 0.5, "variance": 0.01}'

curl -X PUT localhost:8000/blanks/BLK-A -H 'Content-Type: application/json' -d \
  '{"deduction": 0.8, "variance": 0.02}'
```

### 生成汇总

```bash
# 仅凭样组号
curl -X POST localhost:8000/summaries -H 'Content-Type: application/json' -d \
  '{"group_ids": ["SG-1", "SG-2"]}'

# 凭已有汇总号 + 新样组继续合并（按当前空白样数据重算，重叠样组只计一次）
curl -X POST localhost:8000/summaries -H 'Content-Type: application/json' -d \
  '{"summary_ids": [1], "group_ids": ["SG-3"]}'
```

响应示例：

```json
{
  "summary_id": 1,
  "analyte": "cod", "unit": "mg/L",
  "group_ids": ["SG-1"],
  "source_group_ids": ["SG-1"], "source_summary_ids": [],
  "total_measurements": 10,
  "reading_sum": 105.0, "variance_sum": 4.0,
  "blank_deduction_total": 5.0,
  "corrected_mean": 10.0,
  "standard_error": 0.223606797749979,
  "blanks": [
    {"blank_id": "BLK-A", "usage_count": 10, "deduction": 0.5, "variance": 0.01}
  ],
  "created_at": "2026-11-21T23:12:23+00:00"
}
```

其中 `blanks` 为生成该汇总时采用的空白样数据快照；之后更正空白样不影响
`GET /summaries/{id}` 的返回，但凭该汇总号继续合并时会按当前数据重算。

### 整批补录 / 更正检出限数据

```bash
curl -X PUT localhost:8000/detection-limit/supplements \
  -H 'Content-Type: application/json' -d '{
  "supplements": [
    {"group_id": "SG-1", "detected_reading_sum": 80.0, "non_detects": [
      {"blank_id": "BLK-A", "detection_limit": 3.0, "count": 2},
      {"blank_id": "BLK-A", "detection_limit": 1.0, "count": 0}
    ]},
    {"group_id": "SG-2", "detected_reading_sum": 40.0, "non_detects": [
      {"blank_id": "BLK-A", "detection_limit": 4.0, "count": 3},
      {"blank_id": "BLK-B", "detection_limit": 2.0, "count": 0}
    ]}
  ]
}'
```

- 每个样组须覆盖其登记的全部空白样，零未检出显式填 `"count": 0`；
- 同一批再次提交即为覆盖式更正，返回中 `status` 为 `created` 或 `updated`；
- 任一样组有误（空白样未覆盖 / 未使用的空白样 / 次数超用量 / 组合重复等）
  返回 422 并逐条列出 `issues`，整批不生效。

### 发起复核

```bash
curl -X POST localhost:8000/reviews -H 'Content-Type: application/json' -d \
  '{"summary_id": 1, "concentration_limit": 5.0}'
```

响应示例（含完整核算依据）：

```json
{
  "review_id": 1,
  "summary_id": 1,
  "analyte": "lead", "unit": "ug/L",
  "concentration_limit": 5.0,
  "total_measurements": 20,
  "detected_reading_sum": 120.0,
  "non_detect_sum_low": 0.0,
  "non_detect_sum_high": 20.0,
  "blank_deduction_total": 35.0,
  "corrected_mean_lower": 4.25,
  "corrected_mean_upper": 5.25,
  "determination": "indeterminate",
  "groups": [
    {"group_id": "SG-1", "detected_reading_sum": 80.0,
     "non_detect_sum_low": 0.0, "non_detect_sum_high": 6.0,
     "non_detects": [
       {"blank_id": "BLK-A", "detection_limit": 3.0, "count": 2}
     ]}
  ],
  "blanks": [
    {"blank_id": "BLK-A", "usage_count": 15, "deduction": 2.0}
  ],
  "created_at": "2026-11-22T10:00:00+00:00"
}
```

`groups` 为开始核算时已生效的补录快照，`blanks` 为沿用自历史汇总的空白扣减
快照；之后整批更正补录不会改变本复核记录，`GET /reviews/{id}` 原样查回。

### 错误返回

- **404 资料缺失**（不生成汇总）：

```json
{"detail": {"error": "missing_data", "message": "资料缺失，本次不生成汇总。",
            "missing": {"group_ids": ["SG-9"], "blank_ids": ["BLK-X"]}}}
```

- **409 指标 / 单位不一致**：

```json
{"detail": {"error": "inconsistent_analyte_or_unit",
            "message": "只能汇总同一指标、同一单位的读数。",
            "combinations": [{"analyte": "lead", "unit": "ug/L", "group_ids": ["G1"]},
                             {"analyte": "lead", "unit": "mg/L", "group_ids": ["G3"]}]}}
```

- **409 重复登记**（样组 / 空白样编号已存在）；**422 入参校验失败**
  （如方差为负、空白样使用次数为空、汇总请求两项皆空）。

- **404 缺少补录**（发起复核时不生成复核记录）：

```json
{"detail": {"error": "missing_data",
            "message": "样组缺少检出限补录，本次不生成复核记录。",
            "missing": {"group_ids": ["SG-2"]}}}
```

- **422 补录校验未通过**（整批不生效，逐条列出问题样组与原因）：

```json
{"detail": {"error": "supplement_validation_failed",
            "message": "补录校验未通过，整批不生效。",
            "issues": [
              {"group_id": "SG-1", "reason": "non_detect_count_exceeds_usage",
               "message": "同一空白样下的未检出次数合计超过该样组对它的原使用次数。",
               "entries": [{"blank_id": "BLK-A", "non_detect_count": 11, "usage_count": 10}]}
            ]}}
```

## 项目结构

```
app/
  main.py        FastAPI 应用与全部接口
  db.py          SQLite 连接与建表
tests/
  test_api.py    端到端测试（覆盖全部业务规则）
Dockerfile
docker-compose.yml
requirements.txt
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `data/app.db`（容器内 `/data/app.db`） | SQLite 数据库文件路径 |
