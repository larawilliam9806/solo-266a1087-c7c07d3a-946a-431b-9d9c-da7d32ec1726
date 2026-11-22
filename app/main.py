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
"""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field, PositiveInt, model_validator

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


# ---------------------------------------------------------------- 应用初始化

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="浓度汇总 API",
    version="1.0.0",
    description="河段水质报告浓度汇总：样组登记、空白样登记/更正、汇总生成与查询。",
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
