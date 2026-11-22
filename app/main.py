"""河段水质报告 · 浓度汇总 API。

业务规则
--------
- 只处理同一指标（analyte）、同一单位（unit）的读数。
- 样组按唯一编号登记后固定：提交读数总和、独立测量方差之和、
  各空白样的使用次数。每次测量只用一份空白样，
  因此空白样使用次数之和 = 测量总数。
- 空白样的扣减值与方差可另行登记、更正。
- 新汇总可凭样组号、已有汇总号或两者组合生成；重叠样组只计一次。
  引用旧汇总时展开为其样组集合，按**当前**空白样数据重算。
- 校正均值 = (读数总和 − Σ 空白扣减值 × 该空白总使用次数) / 测量总数
- 标准误   = sqrt(Σ 空白方差 × 该空白总使用次数² + 独立测量方差之和) / 测量总数
- 查询旧汇总时返回原结果与当时采用的空白样数据（快照）。
- 资料缺失（样组号 / 汇总号 / 空白样）时列出所缺编号，本次不生成汇总。

检出限复核
----------
旧汇总的读数合计可能把未检出读数代填为检出限，导致校正均值偏高。复核流程：
- 实验室按样组整批补录/更正（覆盖式替换）：未经空白扣减的检出读数之和，
  以及按空白样号 + 检出限登记的未检出次数；零未检出也须明确填报。
  同一空白样下的未检出次数合计不得超过该样组对该空白样的原使用次数；
  一组有误则整批不生效（原子提交）。
- 复核请求提交历史汇总号与同单位浓度限值。沿用该汇总留档的空白扣减快照，
  未检出读数按零至各自检出限计，求校正均值的上下界：
    下界 = (Σ 检出读数之和 + 0 − 空白扣减合计) / 测量总数
    上界 = (Σ 检出读数之和 + Σ 未检出次数×检出限 − 空白扣减合计) / 测量总数
- 判定：下界高于限值 → 超限；上界不高于限值 → 未超限；其余 → 无法判定。
- 缺少补录时列出样组，本次不生成复核记录。
- 复核以开始核算时已生效的补录为准（事务内快照），核算期间仍可完成整批更正，
  更正不影响已生成的复核记录。凭复核号查回原上下界、判定与核算依据。
"""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import (
    BaseModel,
    Field,
    NonNegativeInt,
    PositiveInt,
    model_validator,
)

from .db import connect, init_db


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 请求模型

class GroupCreate(BaseModel):
    """样组登记（登记后固定，不可修改）。"""

    group_id: str = Field(min_length=1, description="样组唯一编号")
    analyte: str = Field(min_length=1, description="指标，如 lead")
    unit: str = Field(min_length=1, description="单位，如 ug/L")
    reading_sum: float = Field(description="读数总和")
    variance_sum: float = Field(ge=0, description="独立测量方差之和")
    blank_usages: dict[str, PositiveInt] = Field(
        min_length=1, description="各空白样的使用次数：{空白样编号: 次数}"
    )


class BlankCreate(BaseModel):
    """空白样登记。"""

    blank_id: str = Field(min_length=1, description="空白样唯一编号")
    deduction: float = Field(description="扣减值")
    variance: float = Field(ge=0, description="方差")


class BlankUpdate(BaseModel):
    """空白样更正（扣减值与方差整体替换）。"""

    deduction: float = Field(description="扣减值")
    variance: float = Field(ge=0, description="方差")


class SummaryRequest(BaseModel):
    """生成汇总：凭样组号、已有汇总号或两者组合。"""

    group_ids: list[str] = Field(default_factory=list, description="样组编号列表")
    summary_ids: list[int] = Field(default_factory=list, description="已有汇总号列表")

    @model_validator(mode="after")
    def _need_at_least_one_input(self) -> "SummaryRequest":
        if not self.group_ids and not self.summary_ids:
            raise ValueError("group_ids 与 summary_ids 至少提供一项")
        return self


# ---------------------------------------------------------------- 检出限复核模型

class NonDetectEntry(BaseModel):
    """同一空白样、同一检出限下的未检出次数（零次也须明确填报）。"""

    blank_id: str = Field(min_length=1, description="空白样编号")
    detection_limit: float = Field(gt=0, description="检出限（必须为正）")
    count: NonNegativeInt = Field(description="未检出次数，零未检出也须明确填报为 0")


class SupplementItem(BaseModel):
    """单个样组的检出限补录内容。"""

    group_id: str = Field(min_length=1, description="样组唯一编号")
    detected_reading_sum: float = Field(description="未经空白扣减的检出读数之和")
    non_detects: list[NonDetectEntry] = Field(
        min_length=1, description="按空白样号和检出限填未检出次数；须覆盖该样组全部空白样"
    )


class SupplementBatchRequest(BaseModel):
    """整批补录 / 更正：一组有误则整批不生效。"""

    supplements: list[SupplementItem] = Field(min_length=1, description="本批补录的样组列表")


class ReviewRequest(BaseModel):
    """发起检出限复核：历史汇总号 + 同单位浓度限值。"""

    summary_id: int = Field(description="历史汇总号")
    concentration_limit: float = Field(description="与汇总同单位的浓度限值")


# ---------------------------------------------------------------- 应用初始化

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="浓度汇总 API",
    version="1.1.0",
    description="河段水质报告浓度汇总：样组登记、空白样登记/更正、汇总生成与查询、检出限补录与复核。",
    lifespan=lifespan,
)


def get_db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


Db = Annotated[sqlite3.Connection, Depends(get_db)]


# ---------------------------------------------------------------- 读取辅助

def group_payload(db: sqlite3.Connection, group_id: str) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT * FROM sample_groups WHERE group_id = ?", (group_id,)
    ).fetchone()
    if row is None:
        return None
    usages = db.execute(
        "SELECT blank_id, usage_count FROM group_blank_usages "
        "WHERE group_id = ? ORDER BY blank_id",
        (group_id,),
    ).fetchall()
    return {
        "group_id": row["group_id"],
        "analyte": row["analyte"],
        "unit": row["unit"],
        "reading_sum": row["reading_sum"],
        "variance_sum": row["variance_sum"],
        "blank_usages": {r["blank_id"]: r["usage_count"] for r in usages},
        "measurement_count": sum(r["usage_count"] for r in usages),
        "created_at": row["created_at"],
    }


def blank_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "blank_id": row["blank_id"],
        "deduction": row["deduction"],
        "variance": row["variance"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def summary_payload(db: sqlite3.Connection, summary_id: int) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT * FROM summaries WHERE summary_id = ?", (summary_id,)
    ).fetchone()
    if row is None:
        return None
    group_ids = [
        r["group_id"]
        for r in db.execute(
            "SELECT group_id FROM summary_groups WHERE summary_id = ? ORDER BY group_id",
            (summary_id,),
        )
    ]
    blanks = [
        {
            "blank_id": r["blank_id"],
            "usage_count": r["usage_count"],
            "deduction": r["deduction"],
            "variance": r["variance"],
        }
        for r in db.execute(
            "SELECT blank_id, usage_count, deduction, variance "
            "FROM summary_blanks WHERE summary_id = ? ORDER BY blank_id",
            (summary_id,),
        )
    ]
    return {
        "summary_id": row["summary_id"],
        "analyte": row["analyte"],
        "unit": row["unit"],
        "group_ids": group_ids,
        "source_group_ids": json.loads(row["source_group_ids"]),
        "source_summary_ids": json.loads(row["source_summary_ids"]),
        "total_measurements": row["total_measurements"],
        "reading_sum": row["reading_sum"],
        "variance_sum": row["variance_sum"],
        "blank_deduction_total": row["blank_deduction_total"],
        "corrected_mean": row["corrected_mean"],
        "standard_error": row["standard_error"],
        # 生成该汇总时采用的空白样数据（快照，不随后续更正改变）
        "blanks": blanks,
        "created_at": row["created_at"],
    }


def missing_data_error(**missing: list) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "error": "missing_data",
            "message": "资料缺失，本次不生成汇总。",
            "missing": {k: v for k, v in missing.items() if v},
        },
    )


# ---------------------------------------------------------------- 检出限复核辅助

DETERMINATION_EXCEED = "exceed"                # 下界高于限值：超限
DETERMINATION_NOT_EXCEED = "not_exceed"        # 上界不高于限值：未超限
DETERMINATION_INDETERMINATE = "indeterminate"  # 其余：无法判定


def supplement_payload(db: sqlite3.Connection, group_id: str, status: str | None = None) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT * FROM dl_supplements WHERE group_id = ?", (group_id,)
    ).fetchone()
    if row is None:
        return None
    non_detects = [
        {
            "blank_id": r["blank_id"],
            "detection_limit": r["detection_limit"],
            "count": r["nd_count"],
        }
        for r in db.execute(
            "SELECT blank_id, detection_limit, nd_count "
            "FROM dl_supplement_non_detects "
            "WHERE group_id = ? ORDER BY blank_id, detection_limit",
            (group_id,),
        )
    ]
    payload = {
        "group_id": row["group_id"],
        "detected_reading_sum": row["detected_reading_sum"],
        "non_detects": non_detects,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if status is not None:
        payload["status"] = status
    return payload


def review_payload(db: sqlite3.Connection, review_id: int) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT * FROM reviews WHERE review_id = ?", (review_id,)
    ).fetchone()
    if row is None:
        return None
    groups = [
        {
            "group_id": r["group_id"],
            "detected_reading_sum": r["detected_reading_sum"],
            "non_detect_sum_low": r["non_detect_sum_low"],
            "non_detect_sum_high": r["non_detect_sum_high"],
            "non_detects": [
                {
                    "blank_id": nd["blank_id"],
                    "detection_limit": nd["detection_limit"],
                    "count": nd["nd_count"],
                }
                for nd in db.execute(
                    "SELECT blank_id, detection_limit, nd_count "
                    "FROM review_non_detects "
                    "WHERE review_id = ? AND group_id = ? "
                    "ORDER BY blank_id, detection_limit",
                    (review_id, r["group_id"]),
                )
            ],
        }
        for r in db.execute(
            "SELECT group_id, detected_reading_sum, non_detect_sum_low, non_detect_sum_high "
            "FROM review_groups WHERE review_id = ? ORDER BY group_id",
            (review_id,),
        )
    ]
    blanks = [
        {
            "blank_id": r["blank_id"],
            "usage_count": r["usage_count"],
            "deduction": r["deduction"],
        }
        for r in db.execute(
            "SELECT blank_id, usage_count, deduction "
            "FROM review_blanks WHERE review_id = ? ORDER BY blank_id",
            (review_id,),
        )
    ]
    return {
        "review_id": row["review_id"],
        "summary_id": row["summary_id"],
        "analyte": row["analyte"],
        "unit": row["unit"],
        "concentration_limit": row["concentration_limit"],
        "total_measurements": row["total_measurements"],
        "detected_reading_sum": row["detected_reading_sum"],
        "non_detect_sum_low": row["non_detect_sum_low"],
        "non_detect_sum_high": row["non_detect_sum_high"],
        "blank_deduction_total": row["blank_deduction_total"],
        "corrected_mean_lower": row["corrected_mean_lower"],
        "corrected_mean_upper": row["corrected_mean_upper"],
        "determination": row["determination"],
        "groups": groups,
        "blanks": blanks,
        "created_at": row["created_at"],
    }


def supplement_validation_error(issues: list[dict[str, Any]]) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "error": "supplement_validation_failed",
            "message": "补录校验未通过，整批不生效。",
            "issues": issues,
        },
    )


# ---------------------------------------------------------------- 基础接口

@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def root() -> dict[str, str]:
    return {"service": "浓度汇总 API", "docs": "/docs", "health": "/health"}


# ---------------------------------------------------------------- 样组

@app.post("/groups", status_code=201, tags=["groups"], summary="登记样组（登记后固定）")
def create_group(body: GroupCreate, db: Db) -> dict[str, Any]:
    exists = db.execute(
        "SELECT 1 FROM sample_groups WHERE group_id = ?", (body.group_id,)
    ).fetchone()
    if exists:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "group_exists",
                "message": f"样组 {body.group_id} 已登记；样组登记后固定，不可修改。",
            },
        )
    with db:
        db.execute(
            "INSERT INTO sample_groups "
            "(group_id, analyte, unit, reading_sum, variance_sum, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (body.group_id, body.analyte, body.unit,
             body.reading_sum, body.variance_sum, utcnow()),
        )
        db.executemany(
            "INSERT INTO group_blank_usages (group_id, blank_id, usage_count) "
            "VALUES (?, ?, ?)",
            [(body.group_id, b, c) for b, c in sorted(body.blank_usages.items())],
        )
    return group_payload(db, body.group_id)


@app.get("/groups/{group_id}", tags=["groups"], summary="查询样组")
def get_group(group_id: str, db: Db) -> dict[str, Any]:
    payload = group_payload(db, group_id)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "group_not_found", "message": f"样组 {group_id} 不存在。"},
        )
    return payload


@app.get("/groups", tags=["groups"], summary="列出全部样组")
def list_groups(db: Db) -> list[dict[str, Any]]:
    ids = [
        r["group_id"]
        for r in db.execute("SELECT group_id FROM sample_groups ORDER BY group_id")
    ]
    return [group_payload(db, g) for g in ids]


# ---------------------------------------------------------------- 空白样

@app.post("/blanks", status_code=201, tags=["blanks"], summary="登记空白样")
def create_blank(body: BlankCreate, db: Db) -> dict[str, Any]:
    exists = db.execute(
        "SELECT 1 FROM blanks WHERE blank_id = ?", (body.blank_id,)
    ).fetchone()
    if exists:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "blank_exists",
                "message": f"空白样 {body.blank_id} 已登记；如需更正请使用 PUT /blanks/{body.blank_id}。",
            },
        )
    now = utcnow()
    with db:
        db.execute(
            "INSERT INTO blanks (blank_id, deduction, variance, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (body.blank_id, body.deduction, body.variance, now, now),
        )
    row = db.execute("SELECT * FROM blanks WHERE blank_id = ?", (body.blank_id,)).fetchone()
    return blank_payload(row)


@app.put("/blanks/{blank_id}", tags=["blanks"], summary="更正空白样的扣减值与方差")
def update_blank(blank_id: str, body: BlankUpdate, db: Db) -> dict[str, Any]:
    row = db.execute("SELECT * FROM blanks WHERE blank_id = ?", (blank_id,)).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "blank_not_found", "message": f"空白样 {blank_id} 尚未登记。"},
        )
    with db:
        db.execute(
            "UPDATE blanks SET deduction = ?, variance = ?, updated_at = ? "
            "WHERE blank_id = ?",
            (body.deduction, body.variance, utcnow(), blank_id),
        )
    row = db.execute("SELECT * FROM blanks WHERE blank_id = ?", (blank_id,)).fetchone()
    return blank_payload(row)


@app.get("/blanks/{blank_id}", tags=["blanks"], summary="查询空白样当前数据")
def get_blank(blank_id: str, db: Db) -> dict[str, Any]:
    row = db.execute("SELECT * FROM blanks WHERE blank_id = ?", (blank_id,)).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "blank_not_found", "message": f"空白样 {blank_id} 尚未登记。"},
        )
    return blank_payload(row)


@app.get("/blanks", tags=["blanks"], summary="列出全部空白样")
def list_blanks(db: Db) -> list[dict[str, Any]]:
    rows = db.execute("SELECT * FROM blanks ORDER BY blank_id").fetchall()
    return [blank_payload(r) for r in rows]


# ---------------------------------------------------------------- 汇总

@app.post("/summaries", status_code=201, tags=["summaries"], summary="生成新汇总")
def create_summary(body: SummaryRequest, db: Db) -> dict[str, Any]:
    """凭样组号、已有汇总号或两者组合生成汇总。

    - 重叠样组只计一次；
    - 引用旧汇总时展开为其样组集合，按**当前**空白样数据重算；
    - 资料缺失时返回所缺编号（404），本次不生成汇总；
    - 指标 / 单位不一致时返回 409。
    """
    # 1) 校验已有汇总号
    summary_ids = list(dict.fromkeys(body.summary_ids))
    missing_summary_ids = [
        s for s in summary_ids
        if not db.execute("SELECT 1 FROM summaries WHERE summary_id = ?", (s,)).fetchone()
    ]

    # 2) 校验直接给出的样组号
    direct_group_ids = list(dict.fromkeys(body.group_ids))
    missing_group_ids = [
        g for g in direct_group_ids
        if not db.execute("SELECT 1 FROM sample_groups WHERE group_id = ?", (g,)).fetchone()
    ]

    if missing_summary_ids or missing_group_ids:
        raise missing_data_error(
            summary_ids=missing_summary_ids, group_ids=missing_group_ids
        )

    # 3) 展开旧汇总为其样组集合，与直接样组取并集 —— 重叠样组只计一次
    group_ids: set[str] = set(direct_group_ids)
    for sid in summary_ids:
        rows = db.execute(
            "SELECT group_id FROM summary_groups WHERE summary_id = ?", (sid,)
        ).fetchall()
        group_ids.update(r["group_id"] for r in rows)

    groups = [group_payload(db, g) for g in sorted(group_ids)]

    # 4) 只处理同一指标、同一单位的读数
    combos: dict[tuple[str, str], list[str]] = {}
    for g in groups:
        combos.setdefault((g["analyte"], g["unit"]), []).append(g["group_id"])
    if len(combos) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "inconsistent_analyte_or_unit",
                "message": "只能汇总同一指标、同一单位的读数。",
                "combinations": [
                    {"analyte": a, "unit": u, "group_ids": ids}
                    for (a, u), ids in sorted(combos.items())
                ],
            },
        )
    (analyte, unit), _ = next(iter(combos.items()))

    # 5) 汇总各空白样的总使用次数
    usage: dict[str, int] = {}
    for g in groups:
        for b, c in g["blank_usages"].items():
            usage[b] = usage.get(b, 0) + c

    # 6) 取当前空白样资料；缺失则列出所缺编号，本次不生成汇总
    blanks: dict[str, sqlite3.Row] = {}
    missing_blank_ids: list[str] = []
    for b in sorted(usage):
        row = db.execute(
            "SELECT blank_id, deduction, variance FROM blanks WHERE blank_id = ?", (b,)
        ).fetchone()
        if row is None:
            missing_blank_ids.append(b)
        else:
            blanks[b] = row
    if missing_blank_ids:
        raise missing_data_error(blank_ids=missing_blank_ids)

    # 7) 计算
    reading_sum = math.fsum(g["reading_sum"] for g in groups)
    variance_sum = math.fsum(g["variance_sum"] for g in groups)
    total_measurements = sum(usage.values())  # 每次测量只用一份空白样
    if total_measurements <= 0:
        raise HTTPException(
            status_code=422,
            detail={"error": "no_measurements", "message": "测量总数为零，无法汇总。"},
        )
    blank_deduction_total = math.fsum(
        blanks[b]["deduction"] * n for b, n in usage.items()
    )
    corrected_mean = (reading_sum - blank_deduction_total) / total_measurements
    total_variance = variance_sum + math.fsum(
        blanks[b]["variance"] * n * n for b, n in usage.items()
    )
    standard_error = math.sqrt(total_variance) / total_measurements

    # 8) 写入汇总及当时采用的空白样数据快照
    with db:
        cur = db.execute(
            "INSERT INTO summaries "
            "(analyte, unit, total_measurements, reading_sum, variance_sum, "
            " blank_deduction_total, corrected_mean, standard_error, "
            " source_group_ids, source_summary_ids, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                analyte, unit, total_measurements, reading_sum, variance_sum,
                blank_deduction_total, corrected_mean, standard_error,
                json.dumps(direct_group_ids), json.dumps(summary_ids), utcnow(),
            ),
        )
        summary_id = cur.lastrowid
        db.executemany(
            "INSERT INTO summary_groups (summary_id, group_id) VALUES (?, ?)",
            [(summary_id, g) for g in sorted(group_ids)],
        )
        db.executemany(
            "INSERT INTO summary_blanks "
            "(summary_id, blank_id, usage_count, deduction, variance) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (summary_id, b, usage[b], blanks[b]["deduction"], blanks[b]["variance"])
                for b in sorted(usage)
            ],
        )
    return summary_payload(db, summary_id)


@app.get("/summaries/{summary_id}", tags=["summaries"], summary="查询旧汇总（原结果与当时空白样数据）")
def get_summary(summary_id: int, db: Db) -> dict[str, Any]:
    payload = summary_payload(db, summary_id)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "summary_not_found", "message": f"汇总 {summary_id} 不存在。"},
        )
    return payload


@app.get("/summaries", tags=["summaries"], summary="列出全部汇总")
def list_summaries(db: Db) -> list[dict[str, Any]]:
    ids = [
        r["summary_id"]
        for r in db.execute("SELECT summary_id FROM summaries ORDER BY summary_id")
    ]
    return [summary_payload(db, s) for s in ids]


# ================================================================ 检出限补录

@app.put(
    "/detection-limit/supplements",
    tags=["detection-limit"],
    summary="整批补录 / 更正检出限数据（覆盖式替换，一组有误则整批不生效）",
)
def upsert_supplements(body: SupplementBatchRequest, db: Db) -> dict[str, Any]:
    # 1) 同一批内样组号不得重复
    seen: set[str] = set()
    duplicated: list[str] = []
    for item in body.supplements:
        if item.group_id in seen and item.group_id not in duplicated:
            duplicated.append(item.group_id)
        seen.add(item.group_id)
    if duplicated:
        raise supplement_validation_error(
            [
                {
                    "group_id": g,
                    "reason": "duplicate_group_in_batch",
                    "message": f"样组 {g} 在本批补录中重复出现。",
                }
                for g in duplicated
            ]
        )

    # 2) 样组必须已登记；缺失则列出编号，本次不生效
    missing_group_ids = [
        item.group_id
        for item in body.supplements
        if db.execute(
            "SELECT 1 FROM sample_groups WHERE group_id = ?", (item.group_id,)
        ).fetchone()
        is None
    ]
    if missing_group_ids:
        raise missing_data_error(group_ids=missing_group_ids)

    # 3) 逐样组校验（全部通过才写入，保证整批原子）
    issues: list[dict[str, Any]] = []
    for item in body.supplements:
        usage = {
            r["blank_id"]: r["usage_count"]
            for r in db.execute(
                "SELECT blank_id, usage_count FROM group_blank_usages WHERE group_id = ?",
                (item.group_id,),
            )
        }

        # 同一 (空白样, 检出限) 组合不得重复登记
        key_seen: set[tuple[str, float]] = set()
        duplicate_entries: list[dict[str, Any]] = []
        for e in item.non_detects:
            key = (e.blank_id, e.detection_limit)
            if key in key_seen:
                duplicate_entries.append(
                    {"blank_id": e.blank_id, "detection_limit": e.detection_limit}
                )
            key_seen.add(key)
        if duplicate_entries:
            issues.append(
                {
                    "group_id": item.group_id,
                    "reason": "duplicate_non_detect_entry",
                    "message": "同一空白样与检出限的组合在该样组中重复填报。",
                    "entries": duplicate_entries,
                }
            )

        # 按空白样汇总未检出次数
        nd_by_blank: dict[str, int] = {}
        for e in item.non_detects:
            nd_by_blank[e.blank_id] = nd_by_blank.get(e.blank_id, 0) + e.count

        # 不得填报样组未使用的空白样
        unknown_blanks = sorted(b for b in nd_by_blank if b not in usage)
        if unknown_blanks:
            issues.append(
                {
                    "group_id": item.group_id,
                    "reason": "blank_not_used_by_group",
                    "message": "填报了该样组未使用的空白样。",
                    "blank_ids": unknown_blanks,
                }
            )

        # 样组用到的每个空白样都须明确填报（零未检出也须填 0）
        uncovered_blanks = sorted(set(usage) - set(nd_by_blank))
        if uncovered_blanks:
            issues.append(
                {
                    "group_id": item.group_id,
                    "reason": "blank_missing_from_report",
                    "message": "该样组使用的空白样未全部填报未检出次数（零未检出也须明确填报）。",
                    "blank_ids": uncovered_blanks,
                }
            )

        # 同一空白样下未检出次数合计不得超过原使用次数
        exceeded = [
            {
                "blank_id": b,
                "non_detect_count": nd_by_blank[b],
                "usage_count": usage[b],
            }
            for b in sorted(nd_by_blank)
            if b in usage and nd_by_blank[b] > usage[b]
        ]
        if exceeded:
            issues.append(
                {
                    "group_id": item.group_id,
                    "reason": "non_detect_count_exceeds_usage",
                    "message": "同一空白样下的未检出次数合计超过该样组对它的原使用次数。",
                    "entries": exceeded,
                }
            )

    if issues:
        raise supplement_validation_error(issues)

    # 4) 整批覆盖式写入（已登记的样组整体替换，未登记的新建）
    now = utcnow()
    statuses = {
        item.group_id: (
            "updated"
            if db.execute(
                "SELECT 1 FROM dl_supplements WHERE group_id = ?", (item.group_id,)
            ).fetchone()
            else "created"
        )
        for item in body.supplements
    }
    with db:
        for item in body.supplements:
            if statuses[item.group_id] == "updated":
                db.execute(
                    "UPDATE dl_supplements SET detected_reading_sum = ?, updated_at = ? "
                    "WHERE group_id = ?",
                    (item.detected_reading_sum, now, item.group_id),
                )
                db.execute(
                    "DELETE FROM dl_supplement_non_detects WHERE group_id = ?",
                    (item.group_id,),
                )
            else:
                db.execute(
                    "INSERT INTO dl_supplements "
                    "(group_id, detected_reading_sum, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (item.group_id, item.detected_reading_sum, now, now),
                )
            db.executemany(
                "INSERT INTO dl_supplement_non_detects "
                "(group_id, blank_id, detection_limit, nd_count) VALUES (?, ?, ?, ?)",
                [
                    (item.group_id, e.blank_id, e.detection_limit, e.count)
                    for e in sorted(
                        item.non_detects, key=lambda e: (e.blank_id, e.detection_limit)
                    )
                ],
            )

    return {
        "supplements": [
            supplement_payload(db, item.group_id, status=statuses[item.group_id])
            for item in body.supplements
        ]
    }


@app.get(
    "/detection-limit/supplements/{group_id}",
    tags=["detection-limit"],
    summary="查询单样组的检出限补录",
)
def get_supplement(group_id: str, db: Db) -> dict[str, Any]:
    payload = supplement_payload(db, group_id)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "supplement_not_found",
                "message": f"样组 {group_id} 尚无检出限补录。",
            },
        )
    return payload


@app.get(
    "/detection-limit/supplements",
    tags=["detection-limit"],
    summary="列出全部检出限补录",
)
def list_supplements(db: Db) -> list[dict[str, Any]]:
    ids = [
        r["group_id"]
        for r in db.execute(
            "SELECT group_id FROM dl_supplements ORDER BY group_id"
        )
    ]
    return [supplement_payload(db, g) for g in ids]


# ================================================================ 检出限复核

@app.post("/reviews", status_code=201, tags=["detection-limit"], summary="发起检出限复核")
def create_review(body: ReviewRequest, db: Db) -> dict[str, Any]:
    """凭历史汇总号与同单位浓度限值发起复核。

    - 沿用该汇总留档的空白扣减（summary_blanks 快照）；
    - 以开始核算时已生效的补录为准：事务内读取，核算期间的整批更正不影响本次复核；
    - 未检出读数按零至各自检出限计，求校正均值上下界并判定；
    - 缺少补录时列出样组（404），本次不生成复核记录。
    """
    summary = db.execute(
        "SELECT * FROM summaries WHERE summary_id = ?", (body.summary_id,)
    ).fetchone()
    if summary is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "summary_not_found",
                "message": f"汇总 {body.summary_id} 不存在。",
            },
        )

    # 显式开启事务：核算期间读取到的全部补录构成一致性快照
    db.execute("BEGIN")
    try:
        group_ids = [
            r["group_id"]
            for r in db.execute(
                "SELECT group_id FROM summary_groups WHERE summary_id = ? ORDER BY group_id",
                (body.summary_id,),
            )
        ]

        missing_group_ids = [
            g
            for g in group_ids
            if db.execute(
                "SELECT 1 FROM dl_supplements WHERE group_id = ?", (g,)
            ).fetchone()
            is None
        ]
        if missing_group_ids:
            db.execute("ROLLBACK")
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "missing_data",
                    "message": "样组缺少检出限补录，本次不生成复核记录。",
                    "missing": {"group_ids": missing_group_ids},
                },
            )

        # 快照读取每个样组的补录
        snapshot_groups: list[dict[str, Any]] = []
        detected_parts: list[float] = []
        nd_high_parts: list[float] = []
        for g in group_ids:
            sup = db.execute(
                "SELECT * FROM dl_supplements WHERE group_id = ?", (g,)
            ).fetchone()
            nd_rows = db.execute(
                "SELECT blank_id, detection_limit, nd_count "
                "FROM dl_supplement_non_detects "
                "WHERE group_id = ? ORDER BY blank_id, detection_limit",
                (g,),
            ).fetchall()
            nd_high = math.fsum(
                r["nd_count"] * r["detection_limit"] for r in nd_rows
            )
            snapshot_groups.append(
                {
                    "group_id": g,
                    "detected_reading_sum": sup["detected_reading_sum"],
                    "non_detects": [
                        {
                            "blank_id": r["blank_id"],
                            "detection_limit": r["detection_limit"],
                            "count": r["nd_count"],
                        }
                        for r in nd_rows
                    ],
                    "non_detect_sum_low": 0.0,
                    "non_detect_sum_high": nd_high,
                }
            )
            detected_parts.append(sup["detected_reading_sum"])
            nd_high_parts.append(nd_high)

        # 沿用历史汇总留档的空白扣减快照
        blank_rows = db.execute(
            "SELECT blank_id, usage_count, deduction "
            "FROM summary_blanks WHERE summary_id = ? ORDER BY blank_id",
            (body.summary_id,),
        ).fetchall()

        total_measurements = summary["total_measurements"]
        detected_reading_sum = math.fsum(detected_parts)
        non_detect_sum_low = 0.0
        non_detect_sum_high = math.fsum(nd_high_parts)
        blank_deduction_total = math.fsum(
            r["deduction"] * r["usage_count"] for r in blank_rows
        )

        # 未检出读数按零至各自检出限计 → 校正均值上下界
        corrected_mean_lower = (
            detected_reading_sum + non_detect_sum_low - blank_deduction_total
        ) / total_measurements
        corrected_mean_upper = (
            detected_reading_sum + non_detect_sum_high - blank_deduction_total
        ) / total_measurements

        limit = body.concentration_limit
        if corrected_mean_lower > limit:
            determination = DETERMINATION_EXCEED
        elif corrected_mean_upper <= limit:
            determination = DETERMINATION_NOT_EXCEED
        else:
            determination = DETERMINATION_INDETERMINATE

        now = utcnow()
        cur = db.execute(
            "INSERT INTO reviews "
            "(summary_id, analyte, unit, concentration_limit, total_measurements, "
            " detected_reading_sum, non_detect_sum_low, non_detect_sum_high, "
            " blank_deduction_total, corrected_mean_lower, corrected_mean_upper, "
            " determination, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                body.summary_id,
                summary["analyte"],
                summary["unit"],
                limit,
                total_measurements,
                detected_reading_sum,
                non_detect_sum_low,
                non_detect_sum_high,
                blank_deduction_total,
                corrected_mean_lower,
                corrected_mean_upper,
                determination,
                now,
            ),
        )
        review_id = cur.lastrowid
        db.executemany(
            "INSERT INTO review_groups "
            "(review_id, group_id, detected_reading_sum, non_detect_sum_low, non_detect_sum_high) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    review_id,
                    g["group_id"],
                    g["detected_reading_sum"],
                    g["non_detect_sum_low"],
                    g["non_detect_sum_high"],
                )
                for g in snapshot_groups
            ],
        )
        db.executemany(
            "INSERT INTO review_non_detects "
            "(review_id, group_id, blank_id, detection_limit, nd_count) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (review_id, g["group_id"], nd["blank_id"], nd["detection_limit"], nd["count"])
                for g in snapshot_groups
                for nd in g["non_detects"]
            ],
        )
        db.executemany(
            "INSERT INTO review_blanks (review_id, blank_id, usage_count, deduction) "
            "VALUES (?, ?, ?, ?)",
            [
                (review_id, r["blank_id"], r["usage_count"], r["deduction"])
                for r in blank_rows
            ],
        )
        db.execute("COMMIT")
    except HTTPException:
        raise
    except Exception:
        db.execute("ROLLBACK")
        raise

    return review_payload(db, review_id)


@app.get("/reviews/{review_id}", tags=["detection-limit"], summary="凭复核号查回原上下界、判定与核算依据")
def get_review(review_id: int, db: Db) -> dict[str, Any]:
    payload = review_payload(db, review_id)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "review_not_found", "message": f"复核 {review_id} 不存在。"},
        )
    return payload


@app.get("/reviews", tags=["detection-limit"], summary="列出全部复核记录")
def list_reviews(db: Db) -> list[dict[str, Any]]:
    ids = [
        r["review_id"]
        for r in db.execute("SELECT review_id FROM reviews ORDER BY review_id")
    ]
    return [review_payload(db, r) for r in ids]
