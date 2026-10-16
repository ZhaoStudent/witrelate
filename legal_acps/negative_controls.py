"""系统性负控集 —— 喂**无害热点**，看管线会不会硬造问题（#102 拍板，2026-10-13）。

存在理由
--------
快车道、复研判、线索受理这条链回答的都是「这个传闻是不是谣言」。但一个只会
说「是谣言」的系统没有答辩能力——被问「你们会不会把无害内容硬造成问题」时，
唯一站得住的回答是：**负控集里每一条无害热点，管线都没有替它编造问题**。

负控测的是**假阳方向**（与 gt 校准的假阴方向互补）：

  · **判道层**（离线可测）：无害热点走 `decide_lane` 必须落辩论道——它没有
    对齐的官方证伪材料（`official_confirms` 只覆盖证伪向），把它送进快车道
    等于「官方材料否定了这件事」的错位暗示。🔴 邻近域话题（医保目录、寒潮
    预警、流感疫苗——辟谣平台**常有同域谣言**）是这条断言的真正压力位：
    ≥4 字连续片段撞进 ±120 字定性窗口，就会把一件真事错配到另一件事的
    证伪材料上。
  · **受理闸门层**（离线可测）：无害文本必须**干净通过** `validate_lead`
    ——闸门是防污染的，不是扩音器；把无辜文本拦下与放进定性词同样是事故。
  · **模型层**（真机，另见手册）：判官对无害热点的正确行为是「确有其事 /
    无可核查断言」的如实结论——离线测不了模型，负控集为其备好输入。

纪律（与 `case_library` 同源）
----
  · 每条**真实、无害、可公开查证**：`source` 记明官方例行发布/公开报道的
    性质——负控要当答辩素材用，出处含糊的负控比没有负控更糟。
  · `why_harmless` 写明无害依据（人核，不得省略）：扩集时一条一条过，
    不许「看着都无害」整批抄入。
  · **维度现场派生**：判据按本表数据现算维度清单与覆盖数（每维度 ≥2 条），
    不另抄一份清单——同一事实散在两处必漏（本项目老账）。
  · 扩集（用户已拍板「之后会扩大」）：加一条 = 加一个 `NegativeControl`，
    判据零改动；新维度自动进覆盖断言。**禁止**把已知会触发的谣言文本
    塞进来（那是正对照的事，正对照由判据从事实库现场派生，不写死）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NegativeControl:
    """一条无害热点负控。**六个字段都不可省**，理由见模块头。"""

    #: 稳定 ID（台账/判据输出里指名道姓用）。改了它等于换了一条负控。
    id: str
    #: 系统性维度（判据现场派生维度清单；同维度 ≥2 条）。
    dimension: str
    #: 一句话标题（人看的）。
    title: str
    #: **喂给判道入口的事件文本**（与谣言事件同形态：一句自然陈述）。
    text: str
    #: 出处（官方例行发布/公开报道的性质描述——负控要能当答辩素材引用）。
    source: str
    #: 为什么无害（人核依据，逐条写明）。
    why_harmless: str


CONTROLS: list[NegativeControl] = [
    # ── 航天科技（与辟谣域零交集的远域对照）──────────────────────────
    NegativeControl(
        id="nc-space-01", dimension="航天科技", title="神舟发射进驻空间站",
        text="神舟载人飞船发射圆满成功，三名航天员顺利进驻空间站开展科学实验",
        source="中国载人航天工程办公室例行公开发布（任务全程公开报道）",
        why_harmless="官方正面通报的既成事实，无任何待核查的可疑断言",
    ),
    NegativeControl(
        id="nc-space-02", dimension="航天科技", title="嫦娥采样返回",
        text="嫦娥探月任务返回器携带月壤样品在预定区域安全着陆",
        source="国家航天局例行公开发布",
        why_harmless="官方正面通报的既成事实",
    ),
    # ── 大型活动（正面盛事；「取消/延期」类谣言的同域压力位）──────────
    NegativeControl(
        id="nc-event-01", dimension="大型活动", title="冬奥会开幕式举行",
        text="北京冬奥会开幕式在国家体育场举行，各国代表团陆续入场亮相",
        source="北京冬奥组委公开发布及全球媒体现场报道",
        why_harmless="已发生的正面盛事，「冬奥会取消」类谣言已被事实本身否定",
    ),
    NegativeControl(
        id="nc-event-02", dimension="大型活动", title="亚运会收官",
        text="杭州亚运会各项赛事收官，中国体育代表团金牌数位列榜首",
        source="杭州亚组委公开发布及赛事公开成绩公报",
        why_harmless="公开赛事的既成成绩",
    ),
    # ── 民生政策（近域压力位：辟谣平台常有医保/养老金类谣言）──────────
    NegativeControl(
        id="nc-policy-01", dimension="民生政策", title="医保目录新增罕见病用药",
        text="国家医保局公布新版药品目录，多款罕见病用药纳入报销范围",
        source="国家医保局官网例行发布（目录调整公开文件）",
        why_harmless="官方政策发布；同域存在「医保停用」类谣言，正是对齐筛子"
                     "最该分清的近邻",
    ),
    NegativeControl(
        id="nc-policy-02", dimension="民生政策", title="基础养老金上调",
        text="多地陆续上调城乡居民基础养老金标准，惠及数千万领取人员",
        source="各省市人社部门例行公开发布",
        why_harmless="官方正面政策；同域存在「养老金停发」类谣言",
    ),
    NegativeControl(
        id="nc-policy-03", dimension="民生政策", title="身份证异地办理",
        text="居民身份证异地换领补领政策全面实施，群众可在居住地就近办理",
        source="公安部治安管理局公开发布",
        why_harmless="官方便民政策，无受害方、无可疑断言",
    ),
    # ── 天气预警（近域压力位：「假预警/停课」类谣言常引用真预警词汇）──
    NegativeControl(
        id="nc-weather-01", dimension="天气预警", title="寒潮蓝色预警发布",
        text="中央气象台发布寒潮蓝色预警，中东部地区将出现明显降温和大风天气",
        source="中央气象台例行预警发布（官方渠道可查）",
        why_harmless="真预警本身；「假寒潮预警致停课」类谣言常复用同一词汇，"
                     "负控必须分得清真预警与假预警传闻",
    ),
    NegativeControl(
        id="nc-weather-02", dimension="天气预警", title="台风防御部署",
        text="台风登陆前沿海各地组织渔船回港避风，加固港口设施",
        source="应急管理部及地方防汛部门例行发布",
        why_harmless="常规防灾部署通报",
    ),
    # ── 交通出行（近域压力位：地铁施工/停运类谣言在本项目事实库内有案底）──
    NegativeControl(
        id="nc-transit-01", dimension="交通出行", title="地铁新线开通",
        text="城市轨道交通新线路开通初期运营，沿线多个车站同步投入使用",
        source="各地地铁集团例行开通公告",
        why_harmless="官方开通公告；事实库存有「地铁施工致停水」类谣言——"
                     "「地铁」两字重合但事件无关，对齐筛子必须分得开",
    ),
    NegativeControl(
        id="nc-transit-02", dimension="交通出行", title="新列车运行图实行",
        text="全国铁路实行新的列车运行图，部分线路旅行时间进一步压缩",
        source="国铁集团例行调图公告",
        why_harmless="官方运行图调整公告",
    ),
    NegativeControl(
        id="nc-transit-03", dimension="交通出行", title="春节高速免费通行",
        text="春节假期小客车通行高速公路继续实施免费政策，出行高峰集中在除夕前后",
        source="交通运输部例行发布（重大节假日免通政策公开文件）",
        why_harmless="年度例行政策，公开可查",
    ),
    # ── 教育考试（近域压力位：「高考推迟/停课」类谣言高发）────────────
    NegativeControl(
        id="nc-edu-01", dimension="教育考试", title="高考时间安排公布",
        text="教育部公布当年普通高考时间安排，考试将于六月初如期举行",
        source="教育部例行公开发布（年度考试安排）",
        why_harmless="官方例行安排；同域存在「高考推迟」类谣言，正是近邻压力位",
    ),
    NegativeControl(
        id="nc-edu-02", dimension="教育考试", title="高校招生简章公布",
        text="多所高校公布强基计划招生简章，考生统一在官方平台报名",
        source="各高校本科招生网例行公开发布",
        why_harmless="官方招生安排，公开可查",
    ),
    # ── 食品药品正面（最高压近域：食品辟谣是辟谣平台第一大类）──────────
    NegativeControl(
        id="nc-food-01", dimension="食品药品正面", title="食品抽检合格率公布",
        text="市场监管部门公布食品安全监督抽检结果，总体合格率保持在较高水平",
        source="市场监管总局例行抽检通报（按季度公开发布）",
        why_harmless="官方正面抽检通报；同域谣言密度最高，「食品安全」四字"
                     "常出现在辟谣文里——对齐筛子最硬的一块试金石",
    ),
    NegativeControl(
        id="nc-food-02", dimension="食品药品正面", title="致敏物质标注新标准",
        text="新修订的预包装食品标签标准实施，致敏物质信息须明确标注",
        source="国家卫生健康委、市场监管总局联合公开发布",
        why_harmless="官方标准发布实施",
    ),
    NegativeControl(
        id="nc-food-03", dimension="食品药品正面", title="流感疫苗接种季",
        text="流感疫苗接种季到来，疾控部门建议老年人和儿童优先接种",
        source="中国疾控中心例行健康提示（每年秋冬季公开发布）",
        why_harmless="官方健康建议；同域存在「疫苗致病」类谣言，近邻压力位",
    ),
    # ── 证实向旧闻（官方早已定论的正面事实）──────────────────────────
    NegativeControl(
        id="nc-true-01", dimension="证实向旧闻", title="国产大飞机商业首航",
        text="国产大型客机投入商业运营，首条航线顺利开航",
        source="中国商飞公开发布及民航主管部门批准文件",
        why_harmless="官方早已定论的正面事实",
    ),
    NegativeControl(
        id="nc-true-02", dimension="证实向旧闻", title="港珠澳大桥通车",
        text="港珠澳大桥正式通车运营，三地口岸实现同步通关",
        source="粤港澳大湾区建设领导小组办公室公开发布",
        why_harmless="官方早已定论的正面事实",
    ),
    # ── 科普常识（无可疑断言的背景知识）─────────────────────────────
    NegativeControl(
        id="nc-sci-01", dimension="科普常识", title="流星雨观测预报",
        text="天文部门发布流星雨观测预报，最佳观测时段在深夜无光害地区",
        source="天文台及天文普及机构例行发布",
        why_harmless="天文科普预报，无可核查的社会性断言",
    ),
    NegativeControl(
        id="nc-sci-02", dimension="科普常识", title="图书借阅服务升级",
        text="公共图书馆延长开放时间并增设自助借还设备，读者办证免费",
        source="各地公共图书馆例行服务公告",
        why_harmless="公共服务信息",
    ),
    # ── 体育文娱（正面赛绩/作品）────────────────────────────────────
    NegativeControl(
        id="nc-sport-01", dimension="体育文娱", title="乒乓球世锦赛夺冠",
        text="中国乒乓球队夺得世界锦标赛团体冠军，成功实现卫冕",
        source="国际乒联公开赛事成绩",
        why_harmless="公开赛事的既成成绩",
    ),
    NegativeControl(
        id="nc-sport-02", dimension="体育文娱", title="动画电影票房纪录",
        text="国产动画电影暑期档上映，票房刷新同类型影片历史纪录",
        source="公开票房统计数据（灯塔/猫眼等专业平台每日公布）",
        why_harmless="公开票房数据；「偷票房」指控类传闻与本案无关",
    ),
    # ── 消费生活（正面惠民信息）─────────────────────────────────────
    NegativeControl(
        id="nc-comm-01", dimension="消费生活", title="转账手续费下调",
        text="多家银行下调手机银行转账手续费标准，跨行转账更加便捷",
        source="各商业银行官网收费公示（价格调整须公开披露）",
        why_harmless="银行收费公示的正面调整",
    ),
    NegativeControl(
        id="nc-comm-02", dimension="消费生活", title="文旅消费券发放",
        text="新一轮文旅消费券发放活动启动，市民可通过官方平台申领",
        source="地方商务及文旅部门例行公开发布",
        why_harmless="官方惠民活动；同域存在「假消费券链接」类提醒——"
                     "本条说的是官方活动本身，近邻压力位",
    ),
]


def by_dimension() -> dict[str, list[NegativeControl]]:
    """按维度分组（判据现场派生维度清单的唯一来源——不另抄一份）。"""
    out: dict[str, list[NegativeControl]] = {}
    for c in CONTROLS:
        out.setdefault(c.dimension, []).append(c)
    return out
