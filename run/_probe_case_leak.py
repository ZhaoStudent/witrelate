"""临时探针：体检 13 案例库 text 的「定性/处罚泄漏」面（只报告，不改）。"""
import re

from legal_acps.case_library import CASES

# 与 derive_gt_cases 同源的定性/处罚模式（口径一致，不另写一套）
FALSE_PAT = re.compile(
    r"不属实|情况不实|信息不实|.{0,6}系谣言|属谣言|为不实信息|不实信息|虚假信息|"
    r"系编造|编造.{0,8}谣言|为谣言|是谣言|严重不实|系自导自演|自导自演|摆拍|辟谣"
)
PUNISH_PAT = re.compile(r"处罚|拘留|罚款|行政.{0,4}处罚|依法.{0,6}查处|刑事|判刑|采取刑事")

print(f"案例库 {len(CASES)} 条体检：text 是否把「官方定性/处罚结果」喂给了裁判\n")
leak = 0
for c in CASES:
    falsy = sorted(set(FALSE_PAT.findall(c.text)))
    punish = sorted(set(PUNISH_PAT.findall(c.text)))
    if falsy or punish:
        leak += 1
        print(f"[{c.id}] {c.title[:28]}")
        if falsy:
            print(f"    定性词: {falsy}")
        if punish:
            print(f"    处罚词: {punish}")
print(f"\n共 {leak}/{len(CASES)} 条 text 带定性或处罚字样")
