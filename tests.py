"""测试说明：主链路 / 数据缺失 / 接口失败 / 合规边界。

运行：python3 tests.py
"""
import json
import subprocess
import sys

sys.path.insert(0, ".")
from app.adapters import DataHealth, fetch_records  # noqa: E402
from app.pipeline import atomize, build_dashboard, resolve_events  # noqa: E402
from app.model import ClaimType, EventStatus, SourceTier  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def t1_main_link():
    """主链路：mock 数据 -> 归并 -> 仲裁。"""
    print("\n[1] 主链路（mock 全链路）")
    recs, h = fetch_records()
    d = build_dashboard(recs, h)
    check("10条原始记录全部原子化", d["stats"]["atom_count"] == 10)
    # 10 条记录 = 4 个事件：
    #   ①收购传闻(含否认) ②算力订单 ③固态电池传闻 ④增持公告(含更正)
    # 增持与收购同标的但属不同事项，被主题锚点正确切开（防止过合并）。
    check("归并为4个事件（增持/收购已正确切开）", d["stats"]["event_count"] == 4,
          f"got {d['stats']['event_count']}")
    check("检测到传闻vs官方否认冲突", d["stats"]["conflict_count"] >= 1)

    statuses = {e["status"] for e in d["events"]}
    check("覆盖 DENIED/CONFIRMED/RUMORED/CORRECTED 状态",
          {"DENIED", "CONFIRMED", "RUMORED", "CORRECTED"} <= statuses, str(statuses))

    ev = next(e for e in d["events"] if e["status"] == "DENIED")
    check("事件标题取最初来源叙事（传闻）", "收购" in ev["title"] or "白酒" in ev["title"], ev["title"])
    check("归并依据可解释", "归并依据" in ev["resolve_basis"])
    check("每条证据保留原始记录ID", all(a["raw_id"].startswith("mock:") for a in ev["atoms"]))
    check("否认事件归并了传闻+回应+转载", ev["atom_count"] >= 3, str(ev["atom_count"]))

    # 子线索仲裁：增持事件含更正记录，应判定为 CORRECTED 而非被其他线索污染
    inc = next((e for e in d["events"] if "增持" in e["title"]), None)
    check("增持事件状态为已更正（子线索仲裁生效）",
          inc is not None and inc["status"] == "CORRECTED",
          inc["status"] if inc else "未找到")

    all_atoms = [a for e in d["events"] for a in e["atoms"]]
    corrected = [a for a in all_atoms if a["polarity"] == "correct"]
    check("更正记录标记[发布后被修改]", bool(corrected) and corrected[0]["was_edited"])

    w = [a["weight"] for a in all_atoms]
    check("来源权重正确（官方1.0 > 传闻0.06）", max(w) == 1.0 and min(w) < 0.1)

    inst = {i["code"] for i in d["instruments"]}
    check("标的索引包含三个关注标的", {"600519.SH", "300033.SZ", "002594.SZ"} <= inst)


def t2_missing_data():
    """数据缺失/降级：不得静默生成正常结论。"""
    print("\n[2] 数据缺失与降级")
    h = DataHealth(mode="mock")
    h.warn("模拟：接口返回空列表（可能为服务降级）")
    d = build_dashboard([], h)
    check("空数据时 health.degraded=True", d["health"]["degraded"] is True)
    check("空数据时携带警告文案", "降级" in "".join(d["health"]["warnings"]))
    check("空数据时不伪造事件", d["stats"]["event_count"] == 0)

    h2 = DataHealth(mode="mock")
    h2.truncated = True
    h2.warn("was_truncated=true，结果被上游截断")
    d2 = build_dashboard([], h2)
    check("截断标志透传到 UI 层", d2["health"]["truncated"] is True)


def t3_api_failure():
    """接口失败：real 模式 + 无效 key，必须显式失败。"""
    print("\n[3] 接口失败（real 模式 + 无效 key）")
    code = "import os;os.environ['EVIDENT_DATA_MODE']='real';os.environ['FUYAO_API_KEY']='bad';" \
           "from app.adapters import fetch_records;r,h=fetch_records();" \
           "assert len(r)==0 and h.ok is False and h.degraded, (len(r),h.ok);" \
           "print('FAIL-LOUD-OK')"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=".")
    check("real模式+坏key 显式失败不伪造", "FAIL-LOUD-OK" in out.stdout, out.stderr[-200:])
    code2 = "import os;os.environ['EVIDENT_DATA_MODE']='auto';os.environ['FUYAO_API_KEY']='';" \
            "from app.adapters import fetch_records;r,h=fetch_records();" \
            "assert h.mode=='mock' and h.degraded;print('FALLBACK-OK')"
    out2 = subprocess.run([sys.executable, "-c", code2], capture_output=True, text=True, cwd=".")
    check("auto模式无key自动降级并警告", "FALLBACK-OK" in out2.stdout, out2.stderr[-200:])


def t4_compliance():
    """合规边界。"""
    print("\n[4] 合规边界")
    recs, _ = fetch_records()
    atoms = atomize(recs)
    d = build_dashboard(recs, DataHealth(mode="mock"))
    blob = json.dumps(d, ensure_ascii=False)
    for banned in ["建议买入", "建议卖出", "必涨", "必跌", "保证收益"]:
        check(f"输出不含违禁表述「{banned}」", banned not in blob)
    check("证据分类覆盖事实/观点/传闻", {"fact", "opinion", "rumor"} <= {a.claim_type.value for a in atoms})
    check("来源层级覆盖官方到传闻", {SourceTier.OFFICIAL.value, SourceTier.UNATTRIBUTED.value}
          <= {a.source_tier.value for a in atoms})
    check("所有结论可回溯原始ID", all(a.raw_id for a in atoms))


if __name__ == "__main__":
    t1_main_link()
    t2_missing_data()
    t3_api_failure()
    t4_compliance()
    print(f"\n========== 结果：{PASS} 通过 / {FAIL} 失败 ==========")
    sys.exit(1 if FAIL else 0)
