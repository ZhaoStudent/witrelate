"""网络诽谤演练（原「反诈体验应用」）的离线验证（用假模型，不需要 API key）。

    ./.venv/bin/python -m tools.verify_experience

覆盖的是「接线 + 教学伦理」两类断言，尤其是几条**不能让它在线上失效**的性质：

  · 诱导方的 `traps` 自曝标记**绝不能**出现在给前端的公开状态里
    —— 泄漏了就等于当场揭穿，体验课作废；
  · 未揭示的会话**不允许**被判定为结束（教学伦理红线）；
  · 轮数上限必须真的拦得住（否则会一直烧模型额度）。
"""

from __future__ import annotations

import asyncio

from legal_acps.contracts import (
    FALLACY_EVIDENCE_KINDS,
    FALLACY_GLOSS,
    FALLACY_LAYER_EVIDENCE,
    FALLACY_LAYER_RHETORIC,
    FALLACY_LAYER_UNKNOWN,
    FALLACY_RHETORIC_KINDS,
    FallacyKind,
    fallacy_layer,
    group_fallacies,
)
from legal_acps.experience.session import (
    DEFAULT_DIFFICULTY,
    DIFFICULTY_ROUNDS,
    MAX_EXCHANGE_ROUNDS,
    REVEAL_MAX_STEPS,
    REVEAL_MAX_TOKENS,
    ExperienceSession,
    RevealRequired,
)
from legal_acps.experience.tool_scope import MAX_TOKENS_KEY, ONLY_TOOL_KEY
from legal_acps.llm import DEFAULT_MAX_TOKENS

_PASS = 0
_FAIL = 0

# ⚠️ 必须是**第一人称当事人**的场景：用户是被指控的那一方。
#    写成"网传某地超市……"这种第三人称传闻，就会把"当事人演练"验成"传闻鉴定"，
#    而这正是本应用与主链路的区别所在 —— 断言用的样例本身也必须是那个视角的。
DEMO_EVENT = (
    "有人在公司群里说我把内部资料卖给了竞争对手，还说有人看到我跟对方一起吃饭"
)


def section(name: str) -> None:
    print()
    print(f"── {name} " + "─" * max(0, 60 - len(name)))


def check(label: str, got: object, expect: object) -> None:
    global _PASS, _FAIL
    ok = got == expect
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
    print(f"  [{'通过' if ok else '失败'}] {label}：{got!r}（期望 {expect!r}）")


def check_true(label: str, got: bool) -> None:
    check(label, bool(got), True)


def _note_crash(label: str, exc: BaseException) -> None:
    """把"用例抛异常"记成一条 `[失败]` 行 —— 见 `async_cases()` 里的说明。"""
    global _FAIL
    _FAIL += 1
    print(f"  [失败] 用例 {label} 抛出未捕获异常（后面的用例继续跑）：{exc!r}")


async def case_happy_path() -> None:
    section("用例 A：开局 → 交锋 → 揭示")
    session = ExperienceSession(use_stub=True)

    state = await session.start(DEMO_EVENT)
    check_true("开局后拿到可判真假的命题", bool((state["case"] or {}).get("proposition")))
    check_true(
        "受理契约含事实清单",
        bool((state["case"] or {}).get("fact_sheet")),
    )
    check("开局后有 1 轮诱导方发言", state["pro_rounds"], 1)

    state = await session.reply("你说'朋友买过'，有没有具体时间和购物小票？")
    check("回复后诱导方轮数 = 2", state["pro_rounds"], 2)
    check_true("交锋记录含用户（反方）发言", any(t["role"] == "user" for t in state["turns"]))

    reveal = await session.reveal()
    check_true("揭示含话术条目", len(reveal.get("traps") or []) >= 1)
    # 话术必须**按层分好**再交给前端：分层口径只存在于 contracts 一处，
    # 让前端自己按 kind 判断就等于又抄一份口径（加一类必漏一处）。
    layers = [g.get("layer") for g in reveal.get("trap_groups") or []]
    check_true("揭示带服务端算好的分层分组", bool(layers))
    check_true(
        "分层名取自契约常量（不是前端/编排层自造的字符串）",
        all(name in (FALLACY_LAYER_EVIDENCE, FALLACY_LAYER_RHETORIC) for name in layers),
    )
    check_true("揭示含易被带偏环节", len(reveal.get("hot_spots") or []) >= 1)
    # ⚠️ hot_spots 与 solid_moves **成对**：只报"哪儿被带偏"会把当事人教成被动的。
    #    这条断言就是防止有人把 solid_moves 当成"锦上添花"删掉。
    check_true("揭示含当事人站住的地方（与 hot_spots 成对）", len(reveal.get("solid_moves") or []) >= 1)
    check_true("揭示含可执行建议", len(reveal.get("advice") or []) >= 1)
    check_true("揭示含通俗复盘", bool(reveal.get("summary")))
    check("揭示后 finished=True", session.state.finished, True)


async def case_traps_hidden() -> None:
    section("用例 B：★ traps 绝不外泄（外泄=当场揭穿）")
    session = ExperienceSession(use_stub=True)
    await session.start(DEMO_EVENT)
    await session.reply("这条消息的原始出处是哪？")

    public = session.state.to_dict()  # 默认即公开视图
    hidden = session.state.to_dict(include_hidden=True)

    def leak_count(view: dict) -> int:
        """**同一条**检测函数，分别去量两个**真实**输出。

        为什么必须这样写：早期这里的"反向对照"是另外**硬编码**造了一个带
        `traps` 的字典，再断言"我造的这个字典里确实有 traps" —— 它压根不经过
        `to_dict()`，**即使剥离逻辑被改坏也永远通过**（典型的空转探针，
        打着"反向对照"的旗号给出虚假的安全感）。

        现在的要求是：同一条检测在隐藏视图上必须报出 >0、在公开视图上必须是 0。
        只要 `to_dict()` 的过滤坏了，下面第一条或第三条立刻失败。
        """
        return len([t for t in view.get("turns") or [] if "traps" in t])

    check("公开视图里 traps 泄漏数 = 0", leak_count(public), 0)
    check_true("隐藏视图里确实有 traps（说明上面不是'从来就没有'）", leak_count(hidden) > 0)
    check_true(
        "反向对照：同一条检测能区分公开/隐藏视图（探针本身是活的）",
        leak_count(public) == 0 and leak_count(hidden) > 0,
    )


async def case_reveal_required() -> None:
    section("用例 C：★ 未揭示不允许结束（教学伦理红线）")
    session = ExperienceSession(use_stub=True)
    await session.start(DEMO_EVENT)

    try:
        session.assert_revealed()
        check("未揭示时 assert_revealed 应抛错", "没有抛错", "抛 RevealRequired")
    except RevealRequired:
        check("未揭示时 assert_revealed 抛 RevealRequired", True, True)

    await session.reveal()
    try:
        session.assert_revealed()
        check("揭示后 assert_revealed 通过", True, True)
    except RevealRequired:
        check("揭示后 assert_revealed 通过", False, True)

    # 已结束的会话不能再交锋
    try:
        await session.reply("再来一轮")
        check("已结束后 reply 应抛错", "没有抛错", "抛 RuntimeError")
    except RuntimeError:
        check("已结束后 reply 抛 RuntimeError", True, True)


async def case_round_cap() -> None:
    section("用例 D：轮数上限真的拦得住")
    session = ExperienceSession(use_stub=True)
    await session.start(DEMO_EVENT)
    noticed = False
    for i in range(MAX_EXCHANGE_ROUNDS + 3):
        state = await session.reply(f"第 {i + 1} 次反驳")
        if state.get("notice"):
            noticed = True
            break
    check_true("触顶后返回提示（不再继续烧额度）", noticed)
    check_true(
        f"诱导方轮数不超过上限 {MAX_EXCHANGE_ROUNDS}",
        session.state.pro_rounds <= MAX_EXCHANGE_ROUNDS,
    )


async def case_contract_hygiene() -> None:
    section("用例 E：契约卫生")
    from legal_acps.experience.roles import DeceptionReveal, TrapMessage

    check_true("TrapMessage 含 message/traps", {"message", "traps"} <= set(TrapMessage.model_fields))
    check_true(
        "DeceptionReveal 含关键字段",
        {"traps", "hot_spots", "solid_moves", "advice", "summary"}
        <= set(DeceptionReveal.model_fields),
    )
    # ⚠️ 契约里**必须没有**轮次字段 —— 轮次是**服务端的账本**，模型无从填写。
    #    这是"记录字段绝不外授"的门禁：谁把字段加回去（那正是缺陷的入口），
    #    或在 session 里改回采纳模型值，这两条 + 用例 H 的行为断言会立刻报失败。
    check(
        "TrapMessage 不含 round 字段（轮次由服务端记账）",
        "round" in TrapMessage.model_fields,
        False,
    )
    check(
        "DeceptionReveal 不含 rounds 字段（轮数由服务端注入）",
        "rounds" in DeceptionReveal.model_fields,
        False,
    )

    # ── 话术枚举：两层，且**提示词清单必须由枚举派生** ──
    #
    # 这里原来是 `check("话术类型恰为四类", kinds, {...})`（**精确相等**）。两个毛病：
    #   · 新增一类话术时会报"多了"，而枚举本来就会长；
    #   · 更糟的是它只验集合，验不出**提示词里手写的那份清单**有没有同步 ——
    #     枚举里加了、提示词里没加 ⇒ 模型永远不会用新那一类，
    #     而这种不一致在离线完全看不出来（提示词是自由文本，没有 schema 可校）。
    # 现在改成四件事：①两层各自不许缺 ②每类都得有归层与 gloss
    #              ③stub 里的话术都能归层（否则前端分组会把它静默丢掉）
    #              ④诱导方提示词里必须真的出现每一类（证明它由枚举派生）
    kinds = {k.value for k in FallacyKind}
    check_true("证据层话术齐全", {k.value for k in FALLACY_EVIDENCE_KINDS} <= kinds)
    check_true("修辞层话术齐全", {k.value for k in FALLACY_RHETORIC_KINDS} <= kinds)
    check("每类话术都归了层", sorted(k for k in kinds if not fallacy_layer(k)), [])
    check("每类话术都有 gloss", sorted(FALLACY_GLOSS), sorted(FallacyKind))

    from legal_acps.experience.roles import DECEIVER_INSTRUCTIONS

    check(
        "诱导方提示词里出现每一类话术（= 清单由枚举派生，不是手写）",
        [k.value for k in FallacyKind if k.value not in DECEIVER_INSTRUCTIONS],
        [],
    )

    # stub 数据里的 kind 必须都能通过枚举校验（否则离线跑的是假绿）
    from legal_acps.stub_model import CANNED_ARGUMENTS

    bad: list[str] = []
    for tool in ("debate_trap_argument", "case_reveal"):
        for trap in CANNED_ARGUMENTS[tool].get("traps") or []:
            if trap["kind"] not in kinds:
                bad.append(f"{tool}:{trap['kind']}")
    check("stub 数据里的话术类型合法", bad, [])

    # 合法还不够 —— 归不了层的会在前端按层分组时**静默消失**（不报错，只是不显示）。
    # 这类缺陷最难发现：链路全绿，用户只是"少看到一条"。
    unlayered = sorted(
        {
            trap["kind"]
            for tool in ("debate_trap_argument", "case_reveal")
            for trap in CANNED_ARGUMENTS[tool].get("traps") or []
            if not fallacy_layer(trap["kind"])
        }
    )
    check("stub 里的话术都能归层（否则前端分组会丢掉）", unlayered, [])

    # ── 前端契约：轮数上限必须在界面上真的生效 ──
    # 这是**结构断言**（没有 JS 运行时，测不了行为），价值在于拦住"改回去"：
    # 早期 renderState() 把发送按钮禁用，随后各请求的 finally 又**无条件**放开，
    # 于是上限只写在提示文字里，按钮照样能点、后端每轮都要多跑一次模型。
    #
    # ⚠️ 这里刻意用**正向**判据（"finally 里必须是 atCap"），而不是早期的
    #    否定式（"全文不许出现 `disabled = false`"）—— 后者把"别处合法地重新启用
    #    按钮"也一并禁掉了（例如"再体验一次"重置界面时就需要置 false）。
    #    否定式复合断言会把无关的正当写法误判成缺陷。
    from pathlib import Path

    html = (
        Path(__file__).resolve().parent.parent
        / "legal_acps" / "experience" / "web" / "index.html"
    ).read_text(encoding="utf-8")
    check(
        "前端：发送按钮在 finally 里按 atCap 恢复（不再无条件放开）",
        "$('sendBtn').disabled = atCap" in html,
        True,
    )
    check("前端：声明了 atCap 上限状态并在 finally 里恢复它", "let atCap" in html, True)
    check(
        "前端：轮数上限判断在 renderState 里写回 atCap",
        "atCap = used >= max" in html,
        True,
    )

    # ── 前端契约：★ 揭示后必须真正结束会话（教学伦理红线②→①）──
    # 服务端 /close 在未揭示时返回 409，这条校验早就有；但前端**从不调用**它，
    # 于是红线只活在服务端校验里、产品路径永远走不到，会话也一直挂在 SESSIONS。
    # 这里断言前端确实实现了"揭示即收尾"。
    check(
        "前端：定义了 closeSession() 且 POST 到 /close",
        ("function closeSession" in html) and ("'/close'" in html),
        True,
    )
    check(
        "前端：揭示处理里 await closeSession()（揭示即收尾）",
        "await closeSession()" in html,
        True,
    )

    # ── 前端契约：揭示页必须**渲染** solid_moves ──
    # 契约里有字段、前端不渲染，等于这类内容从来没到过用户眼前（"接了但没用上"）。
    # 判据取两个：① 读的是 r.solid_moves；② 真的拼进了 revealBox。
    check(
        "前端：揭示页读取并渲染 solid_moves",
        ("r.solid_moves" in html) and ("renderReveal" in html),
        True,
    )
    # 分层渲染：前端必须**消费服务端给的 trap_groups**，而不是自己按 kind 判断。
    #
    # ⚠️ 判据不能只写 `"r.trap_groups" in html` —— 注释里也会出现这个字符串，
    #    那样"只把代码删掉、注释留着"就能骗过断言（一个能被注释满足的探针
    #    等于没有）。所以这里钉的是**真正的判断表达式**与**真正的渲染调用**。
    check(
        "前端：分层用服务端给的 trap_groups（不自己判 kind）",
        ("Array.isArray(r.trap_groups)" in html) and ("renderTrapGroup" in html),
        True,
    )

    # ── 前端契约：立题结论必须真的渲染出来 ──
    # 契约里有字段、服务端也返回了，但前端不渲染，等于这条信息从没到过用户眼前
    # （"接了但没用上"是比"没接"更难发现的一类缺陷）。
    check(
        "前端：定义 renderOpening 并在 renderState 里填充 #openingBox",
        ("function renderOpening" in html)
        and ("$('openingBox').innerHTML = renderOpening" in html),
        True,
    )
    # 拿不到就**如实说** —— 静默给一个"看起来正常"的结论，
    # 比直接说"没有"更危险：用户会以为这道关卡有人守着。
    check(
        "前端：裁判没给立题结论时如实显示「未给出」",
        "未给出</b>立题意见" in html,
        True,
    )
    # ── 前端契约：局中标记与难度都必须真的随请求发出去 ──
    # 只是"页面上有个下拉框 / 有个按钮"不算数：没带上去，它们就只是装饰。
    check(
        "前端：揭示请求带上当事人标的轮次",
        "suspected: [...suspects]" in html,
        True,
    )
    check(
        "前端：难度随开局请求发出",
        "difficulty: $('difficulty').value" in html,
        True,
    )

    # ── 前端契约：★ 每一处 api() 调用都必须显式声明 HTTP 方法 ──
    #
    # 病根（2026-10-01 实测）：`api(path, body)` 原先用
    # `body === undefined ? 'GET' : 'POST'` **推断**方法，于是
    # `api('/api/session/x/reveal')`（不带 body）被静默发成 GET。服务端那个端点
    # 只注册了 POST —— 实测 `GET .../reveal → 405`、`POST → 200`。
    # 结果：用户点「揭晓话术」只看到"揭示失败：请求失败 (405)"，
    # **整局演练的核心交付物根本走不到**。
    #
    # 它躲过了全部既有验证：HTTP 用例直接 `client.post(...)`，绕过这段 JS；
    # 前端断言全是"字符串在不在"，而**方法错不错，字符串看不出来**。
    # 所以这条断言查的不是某个字符串，而是**关系**：调用点与方法声明必须成对。
    import re

    def _strip_comments(src: str) -> str:
        """先剥掉注释与 HTML 注释，**再**拿去扫描。

        ⚠️ 这一步不是洁癖。注释里天然会引用代码写法 —— 本轮修的这两个缺陷，
        说明文字里就必须写出 `api(path, body)`、`$('composer').classList.add('hide')`
        这样的原文，而它们会被当成**真实调用点**：
          · `api(...)` 扫描会数到注释里的那几个，报出一堆假失败；
          · hide 配对会把注释里那次 `add` 也计入，让"已修好"的代码看起来仍是单向的。
        反过来更危险：**能靠注释满足的探针等于没有**（删掉代码只留注释照样通过）。
        所以凡是要在源码上做结构判断的探针，都必须先剥注释。
        """
        src = re.sub(r"<!--.*?-->", "", src, flags=re.S)  # HTML 注释
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)  # 块注释
        src = re.sub(r"(?<!:)//[^\n]*", "", src)  # 行注释（(?<!:) 放过 http://）
        return src

    code = _strip_comments(html)

    def _api_call_sites(src: str) -> list[str]:
        """取出所有 `api(...)` 调用（**排除它自己的函数定义**）。

        用括号配对取完整实参，而不是正则截一行 —— 调用可以跨行、可以嵌套对象字面量，
        按行截会得到半截字符串，判据就失效了。
        """
        sites: list[str] = []
        for m in re.finditer(r"\bapi\(", src):
            if src[max(0, m.start() - 9) : m.start()] == "function ":
                continue  # `async function api(` 是定义，不是调用
            i, depth = m.end(), 1
            while i < len(src) and depth:
                if src[i] == "(":
                    depth += 1
                elif src[i] == ")":
                    depth -= 1
                i += 1
            sites.append(src[m.start() : i])
        return sites

    sites = _api_call_sites(code)
    # 先证明探针是活的：正则抓不到调用点时下面的断言会"零缺陷通过"（空转）。
    check_true("前端：扫描到 >= 4 处 api() 调用（正则没失效）", len(sites) >= 4)
    check(
        "前端：每一处 api() 调用都显式声明 HTTP 方法（不靠 body 推断）",
        [s for s in sites if "method" not in s],
        [],
    )
    check(
        "前端：揭示调用显式走 POST（GET 会被服务端挡成 405）",
        any("'/reveal'" in s and "method: 'POST'" in s for s in sites),
        True,
    )

    # ── 前端契约：★ 显隐必须闭合（add('hide') 与 remove('hide') 配对）──
    #
    # 病根（2026-10-01 实测）：`renderReveal()` 会 `$('composer').classList.add('hide')`
    # 收起输入框，而 `resetToIntro()`（「再体验一次」走的就是它）**从来没有**
    # 把 composer 的 hide 摘掉。于是第二局 #chat 回来了、#composer 还藏着 ——
    # **输入框永远不出现，用户没法替自己辩白**。
    #
    # 这个缺陷当时被上面那个 405 掩盖着：揭示从没成功过，renderReveal 就没跑过。
    # 判据取**配对**而非"某处必须写某行"：只要某个元素被单向隐藏、没有恢复路径，
    # 就报失败。它同时能抓住"以后又加了一处只 hide 不 show"。
    def _hide_counts(src: str) -> tuple[dict[str, int], dict[str, int]]:
        adds: dict[str, int] = {}
        removes: dict[str, int] = {}
        for m in re.finditer(r"\$\('([^']+)'\)\.classList\.add\('hide'\)", src):
            adds[m.group(1)] = adds.get(m.group(1), 0) + 1
        for m in re.finditer(r"\$\('([^']+)'\)\.classList\.remove\('hide'\)", src):
            removes[m.group(1)] = removes.get(m.group(1), 0) + 1
        return adds, removes

    adds, removes = _hide_counts(code)
    check_true("前端：扫描到至少一处 hide 显隐操作（正则没失效）", bool(adds))
    check(
        "前端：每个被隐藏的元素都有恢复路径（显隐闭合）",
        sorted(el for el, n in adds.items() if n != removes.get(el, 0)),
        [],
    )

    # ── 资产覆盖：前端文件必须真的进**同步清单** ──
    # 病根（2026-10-01 实测）：`tools/file_fingerprint.PATTERNS` 原先只收
    # `legal_acps/**/*.py`，于是 81 个文件的同步清单里 **html 数为 0** ——
    # `experience/web/index.html` 从来没被同步过。本地一切正常（文件就在磁盘上），
    # 部署到服务器后 `GET /` 会因为找不到它直接失败。
    # 这条断言查的是**同一份 PATTERNS**（`collect()`），不是另抄一份清单 ——
    # 抄一份就必然再次不同步，那正是这类漏项的成因。
    from tools.file_fingerprint import collect

    root = Path(__file__).resolve().parent.parent
    manifest = {p.as_posix() for p in collect(root)}
    check(
        "前端 index.html 在同步清单内（否则部署到服务器即失效）",
        "legal_acps/experience/web/index.html" in manifest,
        True,
    )
    # 同一个坑的第二次：清单按**扩展名白名单**收文件（`*.py` / `*.html`），
    # 于是 2026-10-01 新增的 `run/verify_frontend_flow.mjs` 又落在白名单外 ——
    # 新加的文件再次不在清单里。这条断言把"资产类文件必须收进清单"钉成判据，
    # 而不是靠每次新增文件时记得改 PATTERNS。
    check(
        "前端行为验证脚本在同步清单内（run/*.mjs 曾被 *.py 白名单漏掉）",
        "run/verify_frontend_flow.mjs" in manifest,
        True,
    )
    # 同一个坑的**第三次**（2026-10-05，#27）：`("deploy", "**/*.service")` 收不到
    # `deploy/systemd/acps-case-round.timer`（仓库里第一个 `.timer`），而它正是服务器上
    # 排期用的那个单元 —— 不推 ⇒ `systemd-install.sh case-install` 装不了定时任务。
    # 同一次还查出 `deploy/systemd/acps.logrotate` 也不在清单里，而
    # `systemd-install.sh` 要 `install "$UNIT_SRC/acps.logrotate"` ——
    # 那句话在服务器上**从来就找不到源文件**。
    # 第 4 处：`docs/` 顶层的材料也不在清单里（`acps-case-round.service` 的注释
    # 点名了 `docs/上线操作手册.md`，而它一直是靠手工另外推的）。
    # 第 5 处（2026-10-07，#law-advisor）：`legal_acps/laws/*.jsonl`（法条库数据，
    # law-advisor 的知识本体）不在任何清单里 —— 漏推的后果是服务器上一个
    # **空库**：lookup 全空、建议书只剩「能力不足」，而本地一切全绿。
    # 第 6 处（2026-10-07 深夜）：`legal_acps/rulings/ground_truth.jsonl`（基准
    # 标注）也不在清单 —— 它在服务器被人工重标后，本地一直是旧版，下次从
    # 本地推送会把新标注**静默覆盖**。
    # ⇒ 判据不再"再补一条白名单"，而是**现场派生**：磁盘上这些资产目录里有哪个文件，
    #    清单里就必须有哪个。下次加任何扩展名都不会再漏。
    # ⚠️ 这里的目录清单是**刻意**写死的 —— 它是"哪些目录算交付资产"这个决定，
    #    不是"清单收了什么"；判据查的是「磁盘上有什么 ⊂ 清单」，与 `PATTERNS` 的
    #    扩展名白名单无关，所以换扩展名照样会响。
    asset_globs = (
        ("deploy", "**/*"),
        ("docs", "*.md"),
        ("legal_acps", "laws/**/*"),
        ("legal_acps", "rulings/**/*"),
    )
    missing_assets: list[str] = []
    for base, pat in asset_globs:
        for p in (root / base).glob(pat):
            if p.is_file():
                rel = p.relative_to(root).as_posix()
                if rel not in manifest:
                    missing_assets.append(rel)
    check(
        "deploy/ 与 docs/*.md 与 legal_acps/{laws,rulings}/ 下的每个文件都在同步清单内"
        "（按目录收，不再按扩展名白名单）",
        sorted(missing_assets),
        [],
    )


async def case_round_labels() -> None:
    section("用例 H：★ 轮次标签由服务端决定（模型不得改写记录）")
    # 病根：**记录里的字段不能交给"被记录的那一方"来写。**
    # 旧写法 `round=int(out.get("round") or nxt)` 直接采纳诱导方模型的输出参数：
    #   · 模型写 1        → 每一轮都显示"第 1 轮"（复盘时用户正是靠轮次回忆"第几轮被带偏"）；
    #   · 模型写 "第一轮"  → int() 抛 ValueError → 冒泡成 HTTP 400，用户以为是自己输错了；
    #   · 模型写 99/2.5/0 → 静默篡改 / 无声截断 / 因 falsy 而"偷偷"回落。
    #
    # ⚠️ 断言查 `pro_rounds`（计数）**抓不到**这件事 —— 计数一直是对的，
    #    错的是每一轮的 `round` **标签**。所以这里直接量标签。
    session = ExperienceSession(use_stub=True)

    # 让"诱导方"越权返回伪造轮次（模拟模型自作主张）。这段**真的穿过被测代码路径**，
    # 不是断言对象属性，也不是自己造一个字典来自证。
    orig_run = session._run

    async def forged_run(runtime, prompt, *, tool, what, max_steps=None, max_tokens=None):  # type: ignore[no-untyped-def]
        out = await orig_run(runtime, prompt, tool=tool, what=what, max_steps=max_steps, max_tokens=max_tokens)
        if not isinstance(out, dict):
            return out
        if tool == "debate.trap_argument":
            return dict(out) | {"round": 77}  # 模型谎报本轮轮次
        if tool == "case.reveal":
            return dict(out) | {"rounds": 99}  # 模型谎报总轮数
        return out

    session._run = forged_run  # type: ignore[method-assign]

    state = await session.start(DEMO_EVENT)
    for i in range(2):
        state = await session.reply(f"第 {i + 1} 次追问：消息的原始出处是哪？")

    pro_labels = [t["round"] for t in state["turns"] if t["role"] == "pro"]
    user_labels = [t["round"] for t in state["turns"] if t["role"] == "user"]
    check("诱导方轮次标签 = 服务端账本（模型谎报的 77 被忽略）", pro_labels, [1, 2, 3])
    check("用户轮次标签也来自服务端账本", user_labels, [1, 2])
    check("最后一个诱导方标签 == pro_rounds", pro_labels[-1], state["pro_rounds"])

    # ── 同源的第二处：揭示里的"本局共交锋 N 轮" ──
    # 与每轮的 `round` 完全同构 —— 旧契约 `DeceptionReveal.rounds` 也由裁判模型自报，
    # 上游塞的 99 必须在这里被服务端账本覆盖掉（否则复盘页显示的轮数就是假的）。
    reveal = await session.reveal()
    check("揭示的轮数 = 服务端账本（模型谎报的 99 被覆盖）", reveal["rounds"], 3)
    check("揭示轮数与 pro_rounds 一致", reveal["rounds"], session.state.pro_rounds)
    check(
        "to_dict 输出的 reveal 同样是服务端值",
        session.state.to_dict()["reveal"]["rounds"],
        3,
    )


async def case_missing_input() -> None:
    section("用例 F：空输入与非法调用")
    session = ExperienceSession(use_stub=True)
    try:
        await session.start("   ")
        check("空事件应被拒绝", "没有抛错", "抛 ValueError")
    except ValueError:
        check("空事件抛 ValueError", True, True)

    try:
        await session.reply("未开局")
        check("未开局就 reply 应被拒绝", "没有抛错", "抛 RuntimeError")
    except RuntimeError:
        check("未开局 reply 抛 RuntimeError", True, True)

    try:
        await session.reveal()
        check("未开局就 reveal 应被拒绝", "没有抛错", "抛 RuntimeError")
    except RuntimeError:
        check("未开局 reveal 抛 RuntimeError", True, True)


async def case_opening() -> None:
    section("用例 I：★ 裁判立题（此前只有承诺、没有实现）")
    # 病史：模块文档、段标题、JUDGE_INSTRUCTIONS 整整一段、连前端 loading 文案
    # 都写了立题，但**没有任何代码路径会调用裁判去立题** —— `_judge` 只在
    # `reveal()` 里用过一次。也就是说，「命题方向写反了当场指出」这道唯一的
    # 把关点一直是空的。所以这里的第一条断言不是"契约里有没有字段"，
    # 而是"开局时裁判到底有没有被调用"。
    session = ExperienceSession(use_stub=True)
    calls: list[str] = []
    orig_run = session._run

    async def spy(runtime, prompt, *, tool, what, max_steps=None, max_tokens=None):  # type: ignore[no-untyped-def]
        calls.append(what)
        return await orig_run(runtime, prompt, tool=tool, what=what, max_steps=max_steps, max_tokens=max_tokens)

    session._run = spy  # type: ignore[method-assign]
    state = await session.start(DEMO_EVENT)

    check("开局时裁判真的被调用（what=立题）", "立题" in calls, True)
    if "立题" in calls and "诱导方第1轮" in calls:
        check(
            "立题发生在诱导方开场之前",
            calls.index("立题") < calls.index("诱导方第1轮"),
            True,
        )

    opening = state.get("opening") or {}
    check_true("开局状态里带立题结论", bool(opening))
    check_true("立题结论含确定性守门的结果（checks）", isinstance(opening.get("checks"), list))
    judge = opening.get("judge") or {}
    check(
        "立题结论含裁判的语义判断（worth_debating/sufficiency/note）",
        sorted(k for k in ("worth_debating", "sufficiency", "note") if k in judge),
        ["note", "sufficiency", "worth_debating"],
    )

    # ── 能力与实现必须成对（关系断言）──
    # "裁判挂了立题能力"与"开局真的调它"是两件事，缺任何一件都会回到旧形态：
    # 前者缺 = 工具没暴露给模型；后者缺 = 能力挂着但没有任何路径走到它。
    from legal_acps.experience.roles import build_judge
    from legal_acps.stub_model import StubModel

    judge_agent = build_judge(StubModel())
    cap_ids = sorted(
        c.capability.id for c in (judge_agent.capabilities or []) if getattr(c, "capability", None)
    )
    check("裁判同时挂了「立题」与「揭示」两个能力", cap_ids, ["case.open", "case.reveal"])

    # ── 确定性守门：不依赖模型，因此可以逐条穷举 ──
    # 这是本环节能"在模型不听话时照样起作用"的原因，也是它能被离线验的原因。
    from legal_acps.experience.opening import inspect_case, inspect_proposition

    def codes(problems: list[dict]) -> list[str]:
        return sorted(p["code"] for p in problems)

    check("守门：正常命题不报问题", inspect_proposition("他收受了好处"), [])
    check(
        "守门：疑问句被拦（没法判真假）",
        codes(inspect_proposition("他是不是收受了好处？")),
        ["interrogative_proposition"],
    )
    check(
        "守门：价值判断被拦",
        codes(inspect_proposition("他这样做很恶劣")),
        ["value_judgment"],
    )
    # ★ 这一条是整个体验唯一的把关点：命题方向必须是「对方在指控他」。
    check(
        "★ 守门：命题方向写反（「他被冤枉」）被拦",
        codes(inspect_proposition("他被冤枉了")),
        ["direction_reversed"],
    )
    # 判据必须对**真实说法**有效，不能只对自己造的例句有效。
    # 「当事人被公司群里的人污蔑了」里没有子串「被污蔑」——"被"和动词之间
    # 隔着"公司群里的人"，裸词表在这里整个失效。这个缺陷正是用真实输入
    # 跑出来的（run/rehearse_experience.py），所以它必须有回归守卫。
    check(
        "★ 守门：方向写反的真实说法也拦得住（「被…污蔑」中间有别的字）",
        codes(inspect_proposition("当事人被公司群里的人污蔑了")),
        ["direction_reversed"],
    )
    check(
        "守门：正常命题不会被方向判据误伤",
        codes(inspect_proposition("当事人向竞争对手提供了公司内部资料")),
        [],
    )
    check("守门：空命题被拦", codes(inspect_proposition("")), ["empty_proposition"])
    check(
        "守门：只有「根本没法辩」的问题才算阻断级",
        [p["code"] for p in inspect_proposition("他是不是收受了好处？") if p["level"] == "block"],
        ["interrogative_proposition"],
    )
    check(
        "守门：事实清单为空时报警",
        codes(inspect_case({"proposition": "他收受了好处", "fact_sheet": {"items": []}})),
        ["thin_fact_sheet"],
    )

    # ── 立题失败不能拖垮开局，但也**不许假装成功** ──
    # 与受理环节不同：受理拿不到结论就必须抛错（没有命题根本没法辩），
    # 立题是"把关"，失败不做阻断 —— 但 judge 保持 None，前端显式显示"未给出"。
    session2 = ExperienceSession(use_stub=True)
    orig2 = session2._run

    async def drop_opening(runtime, prompt, *, tool, what, max_steps=None, max_tokens=None):  # type: ignore[no-untyped-def]
        if tool == "case.open":
            return None
        return await orig2(runtime, prompt, tool=tool, what=what, max_steps=max_steps, max_tokens=max_tokens)

    session2._run = drop_opening  # type: ignore[method-assign]
    state2 = await session2.start(DEMO_EVENT)
    check_true("裁判没给立题结论时，开局仍然继续", bool(state2.get("case")))
    check(
        "立题拿不到时 judge 保持 None（不假装成功）",
        (state2.get("opening") or {}).get("judge"),
        None,
    )
    check_true(
        "守门结果仍然在（模型哑火时这一层照常起作用）",
        isinstance((state2.get("opening") or {}).get("checks"), list),
    )


async def case_difficulty_and_suspects() -> None:
    section("用例 J：难度阶梯 + 当事人自己标的对照")
    for level, rounds in DIFFICULTY_ROUNDS.items():
        session = ExperienceSession(use_stub=True, difficulty=level)
        state = await session.start(DEMO_EVENT)
        check(f"难度「{level}」→ 本局上限 {rounds} 轮", state["max_rounds"], rounds)

    # 认不出的难度回落默认档：一个笔误不该让整局开不了，也不该被静默
    # 当成"用户选了轻松" —— 回落是回落，别装作用户选过。
    session = ExperienceSession(use_stub=True, difficulty="专家级")
    check(
        "认不出的难度回落默认档（不崩）",
        (await session.start(DEMO_EVENT))["max_rounds"],
        DIFFICULTY_ROUNDS[DEFAULT_DIFFICULTY],
    )

    # 上限必须**真的按难度生效**，而不只是显示一个数字
    session = ExperienceSession(use_stub=True, difficulty="轻松")
    await session.start(DEMO_EVENT)
    noticed = False
    for i in range(6):
        state = await session.reply(f"第 {i + 1} 次回应")
        if state.get("notice"):
            noticed = True
            break
    check(
        "轻松档在 3 轮后真的触顶（难度不只是个显示值）",
        (noticed, session.state.pro_rounds),
        (True, DIFFICULTY_ROUNDS["轻松"]),
    )

    # ── 当事人自己的判断 vs 真实话术 ──
    session = ExperienceSession(use_stub=True)
    await session.start(DEMO_EVENT)
    await session.reply("请先把原始出处拿出来")
    report = (await session.reveal(suspected=[1, 2]))["suspect_report"]
    check("标了且确实用了话术的轮次 → 命中", report["hit"], [1, 2])
    check("真值 = 真的用了话术的指控轮", report["truth"], [1, 2])
    check("没有漏标", report["missed"], [])
    check("没有多疑", report["false_alarm"], [])

    # 伪造"某一轮其实没用话术"，验证**多疑**那一侧也报出来 ——
    # 只报命中率会把"见谁都怀疑"教成好习惯，而这个应用教的是分寸。
    session = ExperienceSession(use_stub=True)
    orig = session._run

    async def no_traps(runtime, prompt, *, tool, what, max_steps=None, max_tokens=None):  # type: ignore[no-untyped-def]
        out = await orig(runtime, prompt, tool=tool, what=what, max_steps=max_steps, max_tokens=max_tokens)
        if tool == "debate.trap_argument" and what == "诱导方第2轮":
            return dict(out or {}) | {"traps": []}
        return out

    session._run = no_traps  # type: ignore[method-assign]
    await session.start(DEMO_EVENT)
    await session.reply("回应")
    report = (await session.reveal(suspected=[1, 2]))["suspect_report"]
    check("标了其实没用话术的那一轮 → 计入多疑", report["false_alarm"], [2])
    check("真值只含真的用了话术的轮", report["truth"], [1])

    # 一个都没标时，漏掉的要如实报出来（而不是静默显示"全对"）
    session = ExperienceSession(use_stub=True)
    await session.start(DEMO_EVENT)
    report = (await session.reveal())["suspect_report"]
    check("一个轮次都没标 → marked 为空", report["marked"], [])
    check("但漏掉的轮次照样报出来", report["missed"], [1])

    # 重复点击 / 脏值不能让"标了 3 次同一轮"看起来像"标了 3 轮"
    session = ExperienceSession(use_stub=True)
    await session.start(DEMO_EVENT)
    report = (await session.reveal(suspected=[1, 1, "x", 1]))["suspect_report"]
    check("重复与非法标记被规整", report["marked"], [1])


def case_group_fallacies() -> None:
    section("用例 K：★ 话术分层只有一处实现（接线时挖出真缺陷）")
    # 病史：`contracts.group_fallacies` 此前是**没人调用的死代码** ——
    # 编排层自己另写了一份。接线时才发现它其实**接不上**：
    # 旧实现是 `getattr(hit, "kind", hit)`，只认模型对象的 `.kind`，
    # 而编排层手上是 `model_dump` 过的 **dict** → 取不到 kind →
    # `fallacy_layer()` 返回空串 → **每一条都被静默归到"未归类"**。
    # 不报错、链路全绿，用户只是看到所有话术挤在同一个组下面。
    hits = [{"kind": "诉诸情绪"}, {"kind": "断章取义"}]
    grouped = group_fallacies(hits)
    check(
        "dict 形态能被正确分层（旧实现会全部落到未归类）",
        [name for name, items in grouped.items() if items],
        ["证据层", "修辞层"],
    )
    check("分层顺序固定（前端按此渲染）", list(grouped), ["证据层", "修辞层", "未归类"])
    check(
        "认不出层的话术显式成组、不丢条目",
        len(group_fallacies([{"kind": "查无此层"}])[FALLACY_LAYER_UNKNOWN]),
        1,
    )

    from legal_acps.experience.session import _group_traps

    check(
        "编排层输出与 contracts 的口径一致",
        [g["layer"] for g in _group_traps(hits)],
        ["证据层", "修辞层"],
    )
    check(
        "空层不输出给前端（前端不必为此写判断）",
        [g["layer"] for g in _group_traps([{"kind": "诉诸情绪"}])],
        ["修辞层"],
    )

    # ★ **关系断言**：编排层必须真的**引用** contracts 的口径，而不是又写一份。
    #   判据不是"输出长得像"，而是"改了 contracts 的定义，编排层的输出跟着变" ——
    #   自己写一份的实现不会跟着变（那正是它过去当死代码时的状态）。
    import legal_acps.contracts as C

    saved = dict(C.FALLACY_LAYER_OF)
    try:
        C.FALLACY_LAYER_OF[FallacyKind.EMOTIONAL_APPEAL] = "临时层"
        followed = [g["layer"] for g in _group_traps([{"kind": "诉诸情绪"}])]
    finally:
        C.FALLACY_LAYER_OF.clear()
        C.FALLACY_LAYER_OF.update(saved)
    check("改了 contracts 的层定义，编排层跟着变（= 真的在复用）", followed, ["临时层"])
    check(
        "恢复后台账正常（上面那次改动没有留下副作用）",
        [g["layer"] for g in _group_traps([{"kind": "诉诸情绪"}])],
        ["修辞层"],
    )


async def case_tool_scope() -> None:
    section("用例 L：★ 每个动作只把目标工具交给模型（真机缺陷的离线判据）")
    # 病史（2026-10-01 夜，真机跑出来的）：裁判同时挂着 `case.open` 与 `case.reveal`
    # 两条能力，两条**同时出现在**那一轮的 `request.tools` 里，由模型自己挑。
    # 真模型会挑错 —— 同一个输入连跑三局，揭示分别用了 5 步 / 3 步 / **一步都不调**：
    # 第三局直接以 completed 收尾，`_run` 取不到结论 → 揭示抛错 → **整局报废**，
    # 而用户已经聊完 5 轮。
    #
    # ⚠️ 这个缺陷**离线原本无论如何都发现不了**：假模型对 `request.tools` 里
    #    **每个**它认识的工具都返回调用（见 stub_model.complete），
    #    所以"手里同时有两个工具"在假模型那里不产生任何岔子，它不会挑错。
    #    ⇒ 判据必须落在"模型这一轮**看到**了哪些工具"，而不是"结果对不对"。
    from types import SimpleNamespace

    from legal_acps.experience.tool_scope import ToolScopeHooks

    class _Req:
        def __init__(self, names):
            self.tools = [SimpleNamespace(name=n) for n in names]

    class _Ctx:
        def __init__(self, only="", extra=None):
            metadata = dict(extra or {})
            if only:
                metadata[ONLY_TOOL_KEY] = only
            self.config = SimpleNamespace(metadata=metadata)

    async def exposed(only, present):
        """跑一次 Hook，返回模型最终**看到**的工具名（走真实 handler 路径）。"""
        seen = []

        async def handler(req):
            seen.append([getattr(t, "name", "?") for t in (getattr(req, "tools", None) or [])])
            return "ok"

        await ToolScopeHooks().wrap_model_call(_Ctx(only), _Req(present), handler)
        return seen[0]

    async def _noop(req):
        return "ok"

    both = ["case_open", "case_reveal"]
    check("指定 case_open → 只暴露它", await exposed("case_open", both), ["case_open"])
    check("指定 case_reveal → 只暴露它", await exposed("case_reveal", both), ["case_reveal"])
    check("没指定 → **不动**请求（不是自动只留第一个）", await exposed("", both), both)
    check(
        "目标工具不在本轮列表里 → **不清空**（清空=把「必须交结论」偷换成「只能输出文本」）",
        await exposed("case_reveal", ["debate_trap_argument"]),
        ["debate_trap_argument"],
    )

    # ── token 预算必须**真的写进请求** ──
    # ⚠️ 这一条补的是"参数接对了 ≠ 被用上了"：`_run` 把 `MAX_TOKENS_KEY` 放进
    #    metadata 有断言管着，但 Hook 收下之后**有没有真的改请求**，
    #    离线和真机都看不见（改不动也不会报错，只是继续拿空输出）。
    settings = {"temperature": 0.0}
    await ToolScopeHooks().wrap_model_call(
        _Ctx(extra={MAX_TOKENS_KEY: 16384}), SimpleNamespace(tools=[], settings=settings), _noop
    )
    check("Hook 真的把 token 预算写进了本次请求", settings.get("max_tokens"), 16384)
    untouched = {"temperature": 0.0}
    await ToolScopeHooks().wrap_model_call(
        _Ctx(), SimpleNamespace(tools=[], settings=untouched), _noop
    )
    check("没指定时不替调用方设默认值（settings 原样放行）", untouched, {"temperature": 0.0})

    # ── 端到端：整局跑下来，模型**每一次**请求都只看到一个工具 ──
    import legal_acps.stub_model as stub_mod

    real_stub = stub_mod.StubModel
    seen_per_request = []

    class _Recording(real_stub):
        async def complete(self, request):
            seen_per_request.append(
                [getattr(t, "name", "?") for t in (getattr(request, "tools", None) or [])]
            )
            return await super().complete(request)

    stub_mod.StubModel = _Recording
    try:
        session = ExperienceSession(use_stub=True)
        await session.start(DEMO_EVENT)
        await session.reply("请先出示原始出处。")
        await session.reveal()
    finally:
        stub_mod.StubModel = real_stub

    check_true("整局发生了多次模型请求（否则下面的断言是空转）", len(seen_per_request) >= 6)
    check(
        "每一次请求都只暴露一个工具（旧形态下裁判那几次是两个）",
        sorted({len(names) for names in seen_per_request}),
        [1],
    )
    check(
        "四个动作的目标工具各就各位（受理 / 立题 / 诱导方 / 揭示）",
        sorted({names[0] for names in seen_per_request if names}),
        ["case_accept", "case_open", "case_reveal", "debate_trap_argument"],
    )

    # ── 重试与诊断：一次偶发不调工具，不该让用户丢掉整局 ──
    class _FakeRun:
        def __init__(self, status, iterations, observations, final_text="", reasoning=""):
            self.status = status
            self.iterations = iterations
            self.observations = observations
            # ⚠️ 这两项是**诊断的决定性字段**：真机那次揭示故障就是靠
            #    `iterations=1` + 输出为空，才分清"模型用文本回答了"还是
            #    "思考吃光 token、输出为空"（后者才是真因）。
            self.final_text = final_text
            self.reasoning_text = reasoning

    class _FakeRuntime:
        """只实现 `_run` 用到的那**一个**方法 —— 断言的是被测代码的判断，不是假 runtime 的行为。"""

        def __init__(self, responses):
            self._responses = list(responses)
            self.calls = []

        async def run(self, prompt, config):
            self.calls.append(config)
            return self._responses.pop(0)

    retry_session = ExperienceSession(use_stub=True)
    ok_result = {"worth_debating": True}
    rt = _FakeRuntime(
        [
            _FakeRun("completed", 6, []),  # 第一次：一步都不调就收尾了
            _FakeRun(
                "completed",
                2,
                [{"tool": "case_open", "capabilityId": "case.open", "result": ok_result}],
            ),
        ]
    )
    got = await retry_session._run(rt, "prompt", tool="case.open", what="立题", max_steps=6)
    check("第一次没拿到结论 → 会重试一次", len(rt.calls), 2)
    check("重试时放宽了预算（6 → 12）", [c.max_steps for c in rt.calls], [6, 12])
    check("重试后取到了结论", got, ok_result)
    check(
        "每一轮都带上了「本轮只暴露目标工具」的指令",
        [c.metadata.get(ONLY_TOOL_KEY) for c in rt.calls],
        ["case_open", "case_open"],
    )

    # ── token 预算：**空输出**的真因（思考过程把 token 吃光）──
    # 判据是"这次请求真的带了更大的 max_tokens"，不是"常量等于多少"。
    rt_tok = _FakeRuntime([_FakeRun("completed", 2, [{"tool": "case_reveal", "result": {}}])])
    await retry_session._run(
        rt_tok, "p", tool="case.reveal", what="揭示", max_steps=12, max_tokens=16384
    )
    check(
        "指定了 token 预算 → 写进本轮 metadata（Hook 据此覆盖请求）",
        rt_tok.calls[0].metadata.get(MAX_TOKENS_KEY),
        16384,
    )
    rt_no = _FakeRuntime([_FakeRun("completed", 2, [{"tool": "case_reveal", "result": {}}])])
    await retry_session._run(rt_no, "p", tool="case.reveal", what="揭示", max_steps=12)
    check(
        "没指定 token 预算 → 不写进 metadata（沿用 provider 默认）",
        MAX_TOKENS_KEY in rt_no.calls[0].metadata,
        False,
    )

    dead = _FakeRuntime(
        [
            _FakeRun("completed", 1, [], reasoning="x" * 8000),
            _FakeRun("completed", 1, [{"tool": "case_reveal"}]),  # 调了，但没给结果
        ]
    )
    check(
        "两次都拿不到就如实返回 None（不编一个结论出来）",
        await retry_session._run(
            dead, "p", tool="case.reveal", what="揭示", max_steps=12, max_tokens=8192
        ),
        None,
    )
    diag = retry_session.last_run or {}
    check("诊断记下了目标工具", diag.get("tool"), "case_reveal")
    check(
        "诊断记下了每次尝试的状态 / 迭代数 / 预算（步数与 token 都翻倍重试）",
        [
            (a["status"], a["iterations"], a["max_steps"], a["max_tokens"])
            for a in diag.get("attempts") or []
        ],
        [("completed", 1, 12, 8192), ("completed", 1, 24, 16384)],
    )
    check(
        "诊断记下了**输出为空**这件事（这是分清「用文本回答」与「思考吃光 token」的判据）",
        [(a["final_text_len"], a["reasoning_len"]) for a in diag.get("attempts") or []],
        [(0, 8000), (0, 0)],
    )
    check(
        "诊断记下了这轮**调过**的工具（第二次调了但没给结果）",
        [a["tools_seen"] for a in diag.get("attempts") or []],
        [[], ["case_reveal"]],
    )

    # ── 揭示的**两项**预算都必须单独给（它是上下文最长的一环）──
    budgets = {}
    session4 = ExperienceSession(use_stub=True)
    orig4 = session4._run

    async def watch(runtime, prompt, *, tool, what, max_steps=None, max_tokens=None):  # type: ignore[no-untyped-def]
        budgets[what] = (max_steps, max_tokens)
        return await orig4(runtime, prompt, tool=tool, what=what, max_steps=max_steps, max_tokens=max_tokens)

    session4._run = watch  # type: ignore[method-assign]
    await session4.start(DEMO_EVENT)
    await session4.reveal()
    check("普通环节走默认预算（步数与 token 都不特别指定）", budgets.get("立题"), (None, None))
    check(
        "揭示拿到自己的、更宽的步数与 token 预算",
        budgets.get("揭示"),
        (REVEAL_MAX_STEPS, REVEAL_MAX_TOKENS),
    )
    # ⚠️ 这一条**必须落在"揭示实际交付的值"上**，不能写成常量自比
    #    （`REVEAL_MAX_TOKENS > DEFAULT_MAX_TOKENS`）—— 那是"参数接对了"，
    #    不是"被用上了"：把 `max_tokens=REVEAL_MAX_TOKENS` 从调用处删掉，
    #    常量自比照样成立，而揭示又回到 8192 被思考吃光。
    #    元验证（run/meta_verify_tool_scope.py 探针 ⑧）就是这么把它逮出来的：
    #    第一版常量自比报"空转"。判据取**揭示真正收到的那个数**。
    delivered_tokens = (budgets.get("揭示") or (None, None))[1]
    check_true(
        "揭示的 token 预算确实比默认宽（否则「单独给」没有意义）",
        isinstance(delivered_tokens, int) and delivered_tokens > DEFAULT_MAX_TOKENS,
    )

    # ── 揭示真失败时：**降级而不是报废整局**，但必须显式标注 ──
    # 病史：旧行为是 `raise RuntimeError` —— 用户已经聊完 5 轮，
    # 整局在最后一步报废。降级保留服务端自己算得出来的那两部分。
    session3 = ExperienceSession(use_stub=True)
    await session3.start(DEMO_EVENT)
    session3._judge = _FakeRuntime(
        [_FakeRun("completed", 1, []), _FakeRun("completed", 1, [])]
    )
    degraded = await session3.reveal()
    check("裁判交不回结论时**不抛错**（用户已经聊完，整局不该报废）", degraded.get("judge_ok"), False)
    check(
        "降级时三节需要裁判判断的内容**空着**，不拿像模像样的话填上",
        [degraded.get("hot_spots"), degraded.get("solid_moves"), degraded.get("advice")],
        [[], [], []],
    )
    check_true(
        "降级文案说明了原因（不是一句无意义的占位）",
        "裁判" in (degraded.get("summary") or "")
        and "没有内容" in (degraded.get("summary") or ""),
    )
    check_true(
        "降级页仍然给出**真实的话术清单**（用诱导方逐轮自曝的那份，不依赖裁判）",
        len(degraded.get("trap_groups") or []) > 0,
    )
    check_true(
        "降级页仍然给出当事人自己的对照（服务端算，不依赖裁判）",
        isinstance(degraded.get("suspect_report"), dict),
    )
    check_true("降级也算「已揭示」（教学闭环不断）", session3.state.finished)

    # 正常时 judge_ok 必须是 True —— 否则"降级"会变成一个安静的常态
    ok_session = ExperienceSession(use_stub=True)
    await ok_session.start(DEMO_EVENT)
    await ok_session.reply("请先出示原始出处。")
    ok_reveal = await ok_session.reveal()
    check("裁判正常交回时 judge_ok=True", ok_reveal.get("judge_ok"), True)
    check_true(
        "正常时三节内容都在（证明降级分支没有把正常路径也掏空）",
        bool(ok_reveal.get("hot_spots"))
        and bool(ok_reveal.get("solid_moves"))
        and bool(ok_reveal.get("advice")),
    )


async def async_cases() -> None:
    # ⚠️ 每个用例都包一层：**用例崩了也要报出来并继续跑后面的**。
    #
    #    病史（2026-10-01 夜，写元验证时撞上的）：一个退回形态让某个用例抛了
    #    未捕获异常，脚本就停在那一行 —— 后面的用例一条都不跑，
    #    而报告看起来只是"少打了几行"（`tail` 一截就看不见）。
    #    更糟的是元验证：它按 `[失败]` 行判断"这条断言有没有活着"，
    #    而崩溃产生的是 traceback、不是 `[失败]` 行 ⇒ 探针被误判成"空转"。
    #    把异常显式变成一条 `[失败]` 并继续，两个问题一起解决。
    for label, case in (
        ("A 主流程", case_happy_path),
        ("B traps 不外泄", case_traps_hidden),
        ("C 必须揭示", case_reveal_required),
        ("D 轮数上限", case_round_cap),
        ("E 契约卫生", case_contract_hygiene),
        ("F 空输入与非法调用", case_missing_input),
        ("H 轮次标签", case_round_labels),
        ("I 裁判立题", case_opening),
        ("J 难度与当事人标记", case_difficulty_and_suspects),
        ("L 本轮工具收敛", case_tool_scope),
    ):
        try:
            await case()
        except Exception as exc:  # noqa: BLE001
            _note_crash(label, exc)


def http_case() -> None:
    """HTTP 层 —— 前端实际走的就是这条路径（含 422/404/409 这些边角）。"""
    section("用例 G：HTTP 层")
    from fastapi.testclient import TestClient

    from legal_acps.experience.server import create_app

    with TestClient(create_app(use_stub=True)) as client:
        r = client.get("/")
        check("GET / 返回页面", r.status_code, 200)
        check_true("页面含标题", "网络诽谤演练" in r.text)

        r = client.post("/api/session", json={"event": DEMO_EVENT})
        check("POST /api/session", r.status_code, 200)
        data = r.json()
        sid = data.get("session_id", "")
        check_true("返回 session_id", bool(sid))
        # ★ 公开响应的**原文**里也不能出现 traps
        check("开局响应体不含 traps", "traps" in r.text, False)

        r = client.post(f"/api/session/{sid}/close")
        check("未揭示就关闭会话被拒（409）", r.status_code, 409)

        r = client.post(f"/api/session/{sid}/reply", json={"text": "有原始出处吗？"})
        check("POST reply", r.status_code, 200)
        check("reply 响应体不含 traps", "traps" in r.text, False)

        r = client.post(f"/api/session/{sid}/reveal", json={"suspected": [1]})
        check("POST reveal（带当事人自己的标记）", r.status_code, 200)
        check_true("揭示响应含话术汇总", bool(r.json().get("reveal", {}).get("traps")))
        check_true(
            "揭示响应含「你标的准不准」的对照",
            bool(r.json().get("reveal", {}).get("suspect_report")),
        )

        r = client.post(f"/api/session/{sid}/close")
        check("揭示后可以正常关闭", r.status_code, 200)

        # ★ 关闭必须真的把会话从服务端移除 —— 否则前端的"揭示即收尾"
        #   只是发了个请求、服务端还留着会话，红线依旧没真正落地。
        r = client.get(f"/api/session/{sid}")
        check("关闭后会话真的被移除（再查 404）", r.status_code, 404)

        r = client.post("/api/session/deadbeef/reply", json={"text": "x"})
        check("不存在的会话返回 404", r.status_code, 404)

        r = client.post("/api/session", json={"event": ""})
        check("空事件返回 422（参数校验）", r.status_code, 422)

        # ⚠️ 纯空白（"   "）能通过 min_length=1，必须由业务层挡成 4xx。
        #    早期 create_session / reply 只捕 RuntimeError → ValueError 逃逸
        #    → **HTTP 500**。这条断言就是那次缺陷的回归守卫。
        r = client.post("/api/session", json={"event": "   "})
        check("纯空白事件返回 400（而不是未处理的 500）", r.status_code, 400)

        # ── 难度的口径：可选值由 `DIFFICULTY_ROUNDS` 派生，不是写死一份 Literal ──
        r = client.post("/api/session", json={"event": DEMO_EVENT, "difficulty": "专家级"})
        check("未知难度返回 422（口径来自 DIFFICULTY_ROUNDS）", r.status_code, 422)

        r = client.post("/api/session", json={"event": DEMO_EVENT, "difficulty": "困难"})
        check("困难档可以正常开局", r.status_code, 200)
        check("困难档 max_rounds 随难度变化", r.json().get("max_rounds"), DIFFICULTY_ROUNDS["困难"])
        check_true("开局响应里带立题结论", bool(r.json().get("opening")))

        # 揭示的 body 是**可选**的：少一份对照只是少一点教学价值，
        # 而拒绝揭示是"功能不可用" —— 两者不对等，不能因为没带字段就不给揭示。
        sid3 = r.json().get("session_id", "")
        r = client.post(f"/api/session/{sid3}/reveal")
        check("揭示不传 body 也能用（可选 body 的用意）", r.status_code, 200)

        r = client.post("/api/session", json={"event": DEMO_EVENT})
        sid2 = r.json().get("session_id", "")
        check_true("为空白回复用例另开了一个会话", bool(sid2))
        r = client.post(f"/api/session/{sid2}/reply", json={"text": "   "})
        check("纯空白回复返回 400（而不是未处理的 500）", r.status_code, 400)


def main() -> int:
    print("网络诽谤演练 · 离线验证（假模型）")
    asyncio.run(async_cases())
    for label, case in (("K 话术分层", case_group_fallacies), ("G HTTP 层", http_case)):
        try:
            case()
        except Exception as exc:  # noqa: BLE001
            _note_crash(label, exc)

    print()
    if _FAIL:
        print(f"❌ {_FAIL} 项失败 / {_PASS + _FAIL} 项断言")
        return 1
    print(f"✅ 全部符合预期：{_PASS} 项断言通过。")
    print("  · 诱导方的 traps 只在隐藏视图里，公开视图与 HTTP 响应都拿不到 —— 不会当场剧透")
    print("  · 未揭示的会话无法被判为结束，也无法被关闭 —— 教学闭环有硬保证")
    print("  · 揭示里 hot_spots 与 solid_moves 成对出现 —— 不会把当事人教成被动的")
    print("  · 轮数上限、空输入、非法时序都被挡住")
    print("  · 裁判在开局真的被调用立题，且命题方向写反会被确定性守门拦下")
    print("  · 话术分层只有 contracts 一处实现，编排层改了口径会跟着变（不是各写一份）")
    print("  · 每个动作只把**它自己要调的那个工具**交给模型 —— 裁判不会在两个工具之间挑错")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
