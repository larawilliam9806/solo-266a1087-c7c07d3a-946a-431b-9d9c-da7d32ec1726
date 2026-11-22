"""浓度汇总 API 的端到端测试。

可用 pytest 运行，也可直接执行：python3 tests/test_api.py
覆盖：样组登记/固定、空白样登记/更正、组合汇总、重叠去重、
旧汇总快照查询、按当前空白数据重算、缺失资料列出编号、指标/单位一致性。
"""
import math
import os
import sys
import tempfile

# 必须在导入应用之前指定独立数据库
_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.remove(_db_path)
os.environ["DB_PATH"] = _db_path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

# 直接建表；另用上下文管理器验证 lifespan 也会初始化
init_db()
client = TestClient(app)


def check(cond, msg):
    assert cond, msg


def approx(a, b, tol=1e-9):
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


def run():
    # ---- lifespan 初始化 ----
    with TestClient(app) as c:
        check(c.get("/health").status_code == 200, "lifespan startup")

    # ---- 基础 ----
    r = client.get("/health")
    check(r.status_code == 200 and r.json()["status"] == "ok", "health")

    # ---- 空白样登记 ----
    r = client.post("/blanks", json={"blank_id": "B1", "deduction": 2.0, "variance": 0.04})
    check(r.status_code == 201, f"register B1: {r.text}")
    r = client.post("/blanks", json={"blank_id": "B2", "deduction": 1.0, "variance": 0.09})
    check(r.status_code == 201, f"register B2: {r.text}")
    r = client.post("/blanks", json={"blank_id": "B1", "deduction": 9.9, "variance": 0.1})
    check(r.status_code == 409, "duplicate blank -> 409")

    # ---- 样组登记（登记后固定）----
    g1 = {"group_id": "G1", "analyte": "lead", "unit": "ug/L",
          "reading_sum": 100.0, "variance_sum": 5.0, "blank_usages": {"B1": 10}}
    g2 = {"group_id": "G2", "analyte": "lead", "unit": "ug/L",
          "reading_sum": 50.0, "variance_sum": 3.0, "blank_usages": {"B1": 5, "B2": 5}}
    r = client.post("/groups", json=g1)
    check(r.status_code == 201, f"register G1: {r.text}")
    check(r.json()["measurement_count"] == 10, "G1 measurement_count")
    r = client.post("/groups", json=g2)
    check(r.status_code == 201, f"register G2: {r.text}")
    r = client.post("/groups", json=g1)
    check(r.status_code == 409, "duplicate group -> 409")

    # ---- 生成汇总 S1 = G1 + G2 ----
    # N=20, 读数和=150, 空白扣减=2*15+1*5=35, 校正均值=115/20=5.75
    # V = 0.04*15^2 + 0.09*5^2 + 8 = 19.25, SE = sqrt(19.25)/20
    r = client.post("/summaries", json={"group_ids": ["G1", "G2"]})
    check(r.status_code == 201, f"create S1: {r.text}")
    s1 = r.json()
    check(s1["summary_id"] == 1, "S1 id")
    check(s1["analyte"] == "lead" and s1["unit"] == "ug/L", "S1 analyte/unit")
    check(s1["total_measurements"] == 20, "S1 N")
    check(approx(s1["reading_sum"], 150.0), "S1 reading_sum")
    check(approx(s1["blank_deduction_total"], 35.0), "S1 deduction")
    check(approx(s1["corrected_mean"], 5.75), "S1 mean")
    check(approx(s1["standard_error"], math.sqrt(19.25) / 20), "S1 SE")
    check(s1["group_ids"] == ["G1", "G2"], "S1 groups")
    blanks = {b["blank_id"]: b for b in s1["blanks"]}
    check(blanks["B1"]["usage_count"] == 15 and approx(blanks["B1"]["deduction"], 2.0), "S1 B1 snapshot")
    check(blanks["B2"]["usage_count"] == 5 and approx(blanks["B2"]["variance"], 0.09), "S1 B2 snapshot")

    # ---- 更正空白样 B1 扣减值 2.0 -> 4.0 ----
    r = client.put("/blanks/B1", json={"deduction": 4.0, "variance": 0.04})
    check(r.status_code == 200 and approx(r.json()["deduction"], 4.0), "correct B1")
    r = client.put("/blanks/NOPE", json={"deduction": 0.0, "variance": 0.0})
    check(r.status_code == 404, "correct missing blank -> 404")

    # ---- 查旧汇总：返回原结果与当时空白样数据 ----
    r = client.get("/summaries/1")
    check(r.status_code == 200, "get S1")
    old = r.json()
    check(approx(old["corrected_mean"], 5.75), "S1 keeps original mean")
    old_b1 = {b["blank_id"]: b for b in old["blanks"]}["B1"]
    check(approx(old_b1["deduction"], 2.0), "S1 keeps original blank data")

    # ---- 凭旧汇总号继续合并：按当前空白样数据重算 ----
    # 扣减=4*15+1*5=65, 均值=(150-65)/20=4.25, V 不变
    r = client.post("/summaries", json={"summary_ids": [1]})
    check(r.status_code == 201, f"create S2: {r.text}")
    s2 = r.json()
    check(approx(s2["corrected_mean"], 4.25), "S2 recomputed with current blanks")
    check(approx(s2["standard_error"], math.sqrt(19.25) / 20), "S2 SE")
    check(approx({b["blank_id"]: b for b in s2["blanks"]}["B1"]["deduction"], 4.0),
          "S2 snapshot uses current B1")

    # ---- 重叠样组只计一次：G1 + S1(=G1,G2) 仍为 {G1,G2} ----
    r = client.post("/summaries", json={"group_ids": ["G1"], "summary_ids": [1]})
    check(r.status_code == 201, f"create S3: {r.text}")
    s3 = r.json()
    check(s3["group_ids"] == ["G1", "G2"], "overlap deduped")
    check(s3["total_measurements"] == 20 and approx(s3["corrected_mean"], 4.25),
          "overlap counted once")

    # ---- 样组号 + 汇总号组合，引入新样组 G5 ----
    g5 = {"group_id": "G5", "analyte": "lead", "unit": "ug/L",
          "reading_sum": 30.0, "variance_sum": 1.0, "blank_usages": {"B2": 5}}
    r = client.post("/groups", json=g5)
    check(r.status_code == 201, "register G5")
    # N=25, 读数和=180, 扣减=4*15+1*10=70, 均值=110/25=4.4
    # V = 0.04*15^2 + 0.09*10^2 + 9 = 27, SE = sqrt(27)/25
    r = client.post("/summaries", json={"group_ids": ["G5"], "summary_ids": [2]})
    check(r.status_code == 201, f"create S4: {r.text}")
    s4 = r.json()
    check(s4["group_ids"] == ["G1", "G2", "G5"], "S4 groups")
    check(s4["total_measurements"] == 25, "S4 N")
    check(approx(s4["corrected_mean"], 4.4), "S4 mean")
    check(approx(s4["standard_error"], math.sqrt(27) / 25), "S4 SE")

    # ---- 更正空白样方差后，旧汇总不变、新汇总重算 ----
    r = client.put("/blanks/B2", json={"deduction": 1.0, "variance": 0.16})
    check(r.status_code == 200, "correct B2 variance")
    r = client.get(f"/summaries/{s4['summary_id']}")
    check(approx(r.json()["standard_error"], math.sqrt(27) / 25), "S4 keeps original SE")
    check(approx({b["blank_id"]: b for b in r.json()["blanks"]}["B2"]["variance"], 0.09),
          "S4 keeps original B2 variance")
    # V = 0.04*15^2 + 0.16*10^2 + 9 = 34
    r = client.post("/summaries", json={"summary_ids": [s4["summary_id"]]})
    check(r.status_code == 201, "create S5")
    check(approx(r.json()["standard_error"], math.sqrt(34) / 25), "S5 recomputed SE")
    check(approx(r.json()["corrected_mean"], 4.4), "S5 mean unchanged")

    # ---- 资料缺失：列出所缺编号，不生成汇总 ----
    before = len(client.get("/summaries").json())
    r = client.post("/summaries", json={"group_ids": ["G1", "GX"]})
    check(r.status_code == 404, "missing group -> 404")
    check(r.json()["detail"]["missing"] == {"group_ids": ["GX"]}, "missing group listed")
    r = client.post("/summaries", json={"summary_ids": [999]})
    check(r.status_code == 404 and r.json()["detail"]["missing"] == {"summary_ids": [999]},
          "missing summary listed")
    # 样组引用了未登记的空白样 B9
    r = client.post("/groups", json={"group_id": "G9", "analyte": "lead", "unit": "ug/L",
                                     "reading_sum": 10.0, "variance_sum": 0.5,
                                     "blank_usages": {"B9": 3}})
    check(r.status_code == 201, "register G9 referencing unregistered blank")
    r = client.post("/summaries", json={"group_ids": ["G9"]})
    check(r.status_code == 404 and r.json()["detail"]["missing"] == {"blank_ids": ["B9"]},
          "missing blank listed")
    check(len(client.get("/summaries").json()) == before, "no summary created on missing data")

    # ---- 只处理同一指标、同一单位 ----
    r = client.post("/groups", json={"group_id": "G3", "analyte": "lead", "unit": "mg/L",
                                     "reading_sum": 1.0, "variance_sum": 0.1,
                                     "blank_usages": {"B1": 2}})
    check(r.status_code == 201, "register G3 (different unit)")
    r = client.post("/summaries", json={"group_ids": ["G1", "G3"]})
    check(r.status_code == 409, "unit mismatch -> 409")
    combos = r.json()["detail"]["combinations"]
    check(len(combos) == 2, "mismatch details list combinations")

    # ---- 入参校验 ----
    r = client.post("/summaries", json={})
    check(r.status_code == 422, "empty request -> 422")
    r = client.post("/groups", json={"group_id": "BAD", "analyte": "x", "unit": "y",
                                     "reading_sum": 1.0, "variance_sum": -1.0,
                                     "blank_usages": {"B1": 1}})
    check(r.status_code == 422, "negative variance -> 422")
    r = client.post("/groups", json={"group_id": "BAD2", "analyte": "x", "unit": "y",
                                     "reading_sum": 1.0, "variance_sum": 0.0,
                                     "blank_usages": {}})
    check(r.status_code == 422, "empty blank_usages -> 422")

    # ---- 查询接口 ----
    check(client.get("/groups/G1").status_code == 200, "get group")
    check(client.get("/groups/GX").status_code == 404, "get missing group")
    check(client.get("/blanks/B1").status_code == 200, "get blank")
    check(client.get("/summaries/999").status_code == 404, "get missing summary")
    check(len(client.get("/groups").json()) == 5, "list groups")
    check(len(client.get("/blanks").json()) == 2, "list blanks")

    print("ALL TESTS PASSED")


def test_api():
    run()


if __name__ == "__main__":
    run()
