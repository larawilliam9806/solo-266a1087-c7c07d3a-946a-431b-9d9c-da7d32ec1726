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

    # ============================================================
    # 检出限复核：补录 / 整批更正 / 上下界判定 / 快照留档
    # ============================================================
    # 基线：汇总 1 = G1(B1×10) + G2(B1×5,B2×5)，N=20
    # 汇总 1 留档空白扣减：B1=2.0（旧值快照）, B2=1.0 → 扣减合计 35
    # 补录设计：
    #   G1: 检出和 80；B1 未检出 2 次（检出限 3），其余 B1 8 次填 0
    #   G2: 检出和 40；B1 未检出 3 次（检出限 4），B2 未检出 1 次（检出限 2），其余填 0
    # 检出和合计 120；未检出高值 = 2*3 + 3*4 + 1*2 = 20；扣减 35
    # 下界 = (120 - 35)/20 = 4.25，上界 = (120 + 20 - 35)/20 = 5.25

    # ---- 缺少补录时发起复核：列出样组，不生成复核记录 ----
    reviews_before = len(client.get("/reviews").json())
    r = client.post("/reviews", json={"summary_id": 1, "concentration_limit": 5.0})
    check(r.status_code == 404, f"review without supplements -> 404: {r.text}")
    check(r.json()["detail"]["missing"] == {"group_ids": ["G1", "G2"]},
          "missing supplements list groups")
    check(len(client.get("/reviews").json()) == reviews_before, "no review created")

    # ---- 整批校验失败（多类问题一次列出，整批不生效）----
    bad_batch = {"supplements": [
        # 未覆盖 G1 使用的全部空白（G1 只用 B1，这里又报了未使用的 B2），且 B1 超用量
        {"group_id": "G1", "detected_reading_sum": 80.0, "non_detects": [
            {"blank_id": "B1", "detection_limit": 3.0, "count": 11},
            {"blank_id": "B2", "detection_limit": 2.0, "count": 0}]},
        # G2 漏报 B2
        {"group_id": "G2", "detected_reading_sum": 40.0, "non_detects": [
            {"blank_id": "B1", "detection_limit": 4.0, "count": 3}]},
    ]}
    r = client.put("/detection-limit/supplements", json=bad_batch)
    check(r.status_code == 422, f"invalid batch -> 422: {r.text}")
    reasons = {(i["group_id"], i["reason"]) for i in r.json()["detail"]["issues"]}
    check(("G1", "non_detect_count_exceeds_usage") in reasons, "G1 over usage reported")
    check(("G1", "blank_not_used_by_group") in reasons, "G1 unknown blank reported")
    check(("G2", "blank_missing_from_report") in reasons, "G2 uncovered blank reported")
    check(client.get("/detection-limit/supplements/G1").status_code == 404,
          "atomic batch: nothing written on validation failure")

    # ---- 未知样组：404 列出编号 ----
    r = client.put("/detection-limit/supplements", json={"supplements": [
        {"group_id": "GX", "detected_reading_sum": 1.0, "non_detects": [
            {"blank_id": "B1", "detection_limit": 1.0, "count": 0}]}]})
    check(r.status_code == 404 and r.json()["detail"]["missing"] == {"group_ids": ["GX"]},
          "unknown group in supplement batch -> 404")

    # ---- 批内样组重复 / 同 (空白, 检出限) 重复填报 ----
    r = client.put("/detection-limit/supplements", json={"supplements": [
        {"group_id": "G1", "detected_reading_sum": 1.0, "non_detects": [
            {"blank_id": "B1", "detection_limit": 1.0, "count": 0}]},
        {"group_id": "G1", "detected_reading_sum": 2.0, "non_detects": [
            {"blank_id": "B1", "detection_limit": 1.0, "count": 0}]}]})
    check(r.status_code == 422 and r.json()["detail"]["issues"][0]["reason"]
          == "duplicate_group_in_batch", "duplicate group in batch -> 422")
    r = client.put("/detection-limit/supplements", json={"supplements": [
        {"group_id": "G1", "detected_reading_sum": 80.0, "non_detects": [
            {"blank_id": "B1", "detection_limit": 3.0, "count": 1},
            {"blank_id": "B1", "detection_limit": 3.0, "count": 1}]}]})
    check(r.status_code == 422 and r.json()["detail"]["issues"][0]["reason"]
          == "duplicate_non_detect_entry", "duplicate non-detect entry -> 422")

    # ---- 入参校验：检出限非正、负数次数、空批次 / 空未检出清单 -> 422 ----
    r = client.put("/detection-limit/supplements", json={"supplements": [
        {"group_id": "G1", "detected_reading_sum": 80.0, "non_detects": [
            {"blank_id": "B1", "detection_limit": 0.0, "count": 0}]}]})
    check(r.status_code == 422, "non-positive detection limit -> 422")
    r = client.put("/detection-limit/supplements", json={"supplements": [
        {"group_id": "G1", "detected_reading_sum": 80.0, "non_detects": [
            {"blank_id": "B1", "detection_limit": 3.0, "count": -1}]}]})
    check(r.status_code == 422, "negative count -> 422")
    r = client.put("/detection-limit/supplements", json={"supplements": []})
    check(r.status_code == 422, "empty batch -> 422")
    r = client.put("/detection-limit/supplements", json={"supplements": [
        {"group_id": "G1", "detected_reading_sum": 80.0, "non_detects": []}]})
    check(r.status_code == 422, "empty non_detects -> 422")

    # ---- 合法整批补录（零未检出也明确填报）----
    good_batch = {"supplements": [
        {"group_id": "G1", "detected_reading_sum": 80.0, "non_detects": [
            {"blank_id": "B1", "detection_limit": 3.0, "count": 2},
            {"blank_id": "B1", "detection_limit": 1.0, "count": 0}]},
        {"group_id": "G2", "detected_reading_sum": 40.0, "non_detects": [
            {"blank_id": "B1", "detection_limit": 4.0, "count": 3},
            {"blank_id": "B2", "detection_limit": 2.0, "count": 1},
            {"blank_id": "B2", "detection_limit": 1.0, "count": 0}]}]}
    r = client.put("/detection-limit/supplements", json=good_batch)
    check(r.status_code == 200, f"create supplements: {r.text}")
    statuses = {s["group_id"]: s["status"] for s in r.json()["supplements"]}
    check(statuses == {"G1": "created", "G2": "created"}, "first batch status=created")
    r = client.get("/detection-limit/supplements/G1")
    check(r.status_code == 200, "get supplement")
    g1_sup = r.json()
    check(approx(g1_sup["detected_reading_sum"], 80.0), "supplement detected sum stored")
    check([(e["blank_id"], e["detection_limit"], e["count"]) for e in g1_sup["non_detects"]]
          == [("B1", 1.0, 0), ("B1", 3.0, 2)], "zero non-detect explicitly stored & sorted")
    check(len(client.get("/detection-limit/supplements").json()) == 2, "list supplements")

    # ---- 复核判定：下界 4.25、上界 5.25 ----
    # 限值 4.0 < 下界 → 超限
    r = client.post("/reviews", json={"summary_id": 1, "concentration_limit": 4.0})
    check(r.status_code == 201, f"create review exceed: {r.text}")
    rv = r.json()
    check(rv["determination"] == "exceed", "lower > limit -> exceed")
    check(approx(rv["corrected_mean_lower"], 4.25), "review lower bound")
    check(approx(rv["corrected_mean_upper"], 5.25), "review upper bound")
    check(approx(rv["detected_reading_sum"], 120.0), "review detected total")
    check(approx(rv["non_detect_sum_high"], 20.0) and approx(rv["non_detect_sum_low"], 0.0),
          "review non-detect sums")
    # 沿用汇总 1 留档的空白扣减（B1=2.0 旧值，而非当前 4.0）
    check(approx(rv["blank_deduction_total"], 35.0), "review uses archived blank deduction")
    rb = {b["blank_id"]: b for b in rv["blanks"]}
    check(approx(rb["B1"]["deduction"], 2.0) and rb["B1"]["usage_count"] == 15,
          "review blank snapshot from summary")
    check(rv["total_measurements"] == 20 and rv["unit"] == "ug/L", "review N and unit")
    review_id = rv["review_id"]

    # 限值 6.0 >= 上界 → 未超限
    r = client.post("/reviews", json={"summary_id": 1, "concentration_limit": 6.0})
    check(r.status_code == 201 and r.json()["determination"] == "not_exceed",
          "upper <= limit -> not_exceed")

    # 限值 5.0 落在区间内（4.25 <= 5.0 < 5.25）→ 无法判定
    r = client.post("/reviews", json={"summary_id": 1, "concentration_limit": 5.0})
    check(r.status_code == 201 and r.json()["determination"] == "indeterminate",
          "limit inside interval -> indeterminate")

    # 边界：限值 = 上界 5.25，上界不高于限值 → 未超限
    r = client.post("/reviews", json={"summary_id": 1, "concentration_limit": 5.25})
    check(r.json()["determination"] == "not_exceed", "limit == upper -> not_exceed")
    # 边界：限值 = 下界 4.25，下界不高于限值 → 无法判定
    r = client.post("/reviews", json={"summary_id": 1, "concentration_limit": 4.25})
    check(r.json()["determination"] == "indeterminate", "limit == lower -> indeterminate")

    # ---- 凭复核号查回原上下界、判定与核算依据 ----
    r = client.get(f"/reviews/{review_id}")
    check(r.status_code == 200, "get review by id")
    got = r.json()
    check(got["determination"] == "exceed"
          and approx(got["corrected_mean_lower"], 4.25)
          and approx(got["corrected_mean_upper"], 5.25), "review stored bounds & determination")
    rg = {g["group_id"]: g for g in got["groups"]}
    check(approx(rg["G1"]["non_detect_sum_high"], 6.0), "G1 basis nd high = 2*3")
    check(approx(rg["G2"]["non_detect_sum_high"], 14.0), "G2 basis nd high = 3*4+1*2")
    check([(e["blank_id"], e["detection_limit"], e["count"]) for e in rg["G2"]["non_detects"]]
          == [("B1", 4.0, 3), ("B2", 1.0, 0), ("B2", 2.0, 1)], "G2 non-detect basis snapshotted")
    check(client.get("/reviews/999").status_code == 404, "missing review -> 404")
    check(client.post("/reviews", json={"summary_id": 999, "concentration_limit": 1.0}).status_code
          == 404, "review unknown summary -> 404")

    # ---- 整批更正：复核以开始核算时已生效的补录为准，旧复核不变 ----
    corrected_batch = {"supplements": [
        {"group_id": "G1", "detected_reading_sum": 70.0, "non_detects": [
            {"blank_id": "B1", "detection_limit": 3.0, "count": 1}]},
        {"group_id": "G2", "detected_reading_sum": 40.0, "non_detects": [
            {"blank_id": "B1", "detection_limit": 4.0, "count": 3},
            {"blank_id": "B2", "detection_limit": 2.0, "count": 1},
            {"blank_id": "B2", "detection_limit": 1.0, "count": 0}]}]}
    r = client.put("/detection-limit/supplements", json=corrected_batch)
    check(r.status_code == 200, "corrected batch accepted")
    statuses = {s["group_id"]: s["status"] for s in r.json()["supplements"]}
    check(statuses == {"G1": "updated", "G2": "updated"}, "second batch status=updated")
    # 覆盖式替换：G1 旧条目（检出限 1.0 的 0 次条目）已删除
    g1_now = client.get("/detection-limit/supplements/G1").json()
    check(approx(g1_now["detected_reading_sum"], 70.0), "G1 detected sum replaced")
    check([(e["detection_limit"], e["count"]) for e in g1_now["non_detects"]]
          == [(3.0, 1)], "G1 non-detects fully replaced")

    # 旧复核记录原样保留（核算依据快照）
    old_review = client.get(f"/reviews/{review_id}").json()
    check(approx(old_review["detected_reading_sum"], 120.0)
          and old_review["determination"] == "exceed", "old review unaffected by correction")
    old_g1 = {g["group_id"]: g for g in old_review["groups"]}["G1"]
    check(approx(old_g1["detected_reading_sum"], 80.0)
          and approx(old_g1["non_detect_sum_high"], 6.0), "old review group basis retained")

    # 新复核按更正后的补录：检出和 110，未检出高值 = 1*3 + 3*4 + 1*2 = 17
    # 下界 = (110-35)/20 = 3.75，上界 = (110+17-35)/20 = 4.6
    r = client.post("/reviews", json={"summary_id": 1, "concentration_limit": 5.0})
    check(r.status_code == 201, f"review after correction: {r.text}")
    rv2 = r.json()
    check(approx(rv2["corrected_mean_lower"], 3.75), "new review lower after correction")
    check(approx(rv2["corrected_mean_upper"], 4.6), "new review upper after correction")
    check(rv2["determination"] == "not_exceed", "corrected data -> not_exceed at limit 5")
    check(approx(rv2["blank_deduction_total"], 35.0), "still uses archived deduction")
    # 成功创建的复核共 6 条：超限、未超限、无法判定、两个边界判定、更正后各 1 条
    check(len(client.get("/reviews").json()) == reviews_before + 6, "all reviews retained")

    print("ALL TESTS PASSED")


def test_api():
    run()


if __name__ == "__main__":
    run()
