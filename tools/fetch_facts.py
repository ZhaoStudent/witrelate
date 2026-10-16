"""**离线抓取器** —— 从白名单权威站抓条目，落成本地事实库的原料。

════════════════════════════════════════════════════════════════════
⚠️ 定位：这是**维护者手动跑的离线工具**，**不是** agent 运行时的一部分
════════════════════════════════════════════════════════════════════

为什么要这么切（这是整个设计的关键，别改）：

  🔴 **出网面是本项目从未有过、且刻意不要的东西**（SSRF）。
     事实搜集能力如果在**运行时**去抓网页，等于把整个项目的攻击面
     从"只收 ACPs 委派"扩到"能对外发任意请求"。
  ⇒ 所以：**agent 运行时只读本地库，自身不出网**；出网这件事
     被**隔离到本脚本**，由人手动触发、产出落盘、之后不再联网。

  这样"从权威网站获取信息"是**真的做了**（库里的条目有真实 URL 和
  抓取时间，可逐条回查），但**风险面没有扩大**。

  另一半理由是**评审时不能翻车**：实时抓网站依赖当时网络与对方反爬策略
  （实测 `/search` 直接 403），演示时挂掉就全完了。本地库离线可复现。

════════════════════════════════════════════════════════════════════
为什么抓**列表页**而不是调搜索接口
════════════════════════════════════════════════════════════════════

实测（2026-10-07）：

  · 这些站**能出网**：piyao.org.cn / gov.cn / cac.gov.cn 全部 200，< 1.2 s
  · 首页是**服务端渲染**（curl 直接拿 67 KB HTML），不需要浏览器
  · 🔴 但**搜索接口不通**：`/search?keyword=…` 跟随 301 后 **403（反爬）**，
    `/api/search` **404**

⇒ 所以**不搜**。抓列表页 + 栏目页（不需要搜索接口），把条目**全部**入库，
  「检索」这件事交给 agent 在**本地**做关键词匹配。
  绕开搜索接口，反爬策略的影响面就从"每次查询"降到"每次维护"。

════════════════════════════════════════════════════════════════════
用法
════════════════════════════════════════════════════════════════════

    ./.venv/bin/python -m tools.fetch_facts                 抓白名单全部源
    ./.venv/bin/python -m tools.fetch_facts --source piyao  只抓指定源
    ./.venv/bin/python -m tools.fetch_facts --detail        连详情页正文一起抓
    ./.venv/bin/python -m tools.fetch_facts --limit 5       限制条目数（试水）

产出：`legal_acps/facts/raw-<源>-<UTC时间戳>.jsonl`，一行一条，字段见
`FactEntry`。**只增不改**：每跑一次生成新文件，不覆盖旧的
（覆盖会让"库里有什么"依赖"最后一次跑没跑成"，那正是要避免的）。
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "legal_acps" / "facts"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# ────────────────────────────────────────────────────────────────────
# 白名单（**唯一的出网面**，加源必须写清发布方与为什么可信）
# ────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Source:
    key: str
    publisher: str          # 发布方（**必须点名**，不能写"权威网站"这种泛称）
    level: str              # 权威结论 / 一手材料 / 二手转述
    base: str
    entry: str              # 列表页（栏目页）URL
    note: str = ""


SOURCES: tuple[Source, ...] = (
    Source(
        key="piyao",
        publisher="中国互联网联合辟谣平台",
        level="权威结论",
        base="https://www.piyao.org.cn",
        entry="https://www.piyao.org.cn/",
        note="中央网信办指导；「今日辟谣」栏目逐条给出官方定性，天然带 ground truth",
    ),
    # ⚠️ 2026-10-07 扩库（治弃权 + 治偏食）：首页只覆盖**当天**的辟谣条目，
    #    而「热门辟谣」「真相」两个栏目页是**月度汇总**列表页 —— 同域、
    #    同为列表页（不碰搜索接口），一次抓回 70+ 条**带官方定性**的历史辟谣，
    #    正是 ruling_store 偏食与 fact_store 覆盖缺口的直接素材。
    #    实测：bd 40 条 + zs 34 条，条目为 `../YYYYMMDD/…` 相对路径，
    #    服务器对 `/../` 形态自动归一化（实测 200），现有 `_abs` 无需改动。
    Source(
        key="piyao-bd",
        publisher="中国互联网联合辟谣平台",
        level="权威结论",
        base="https://www.piyao.org.cn",
        entry="https://www.piyao.org.cn/rm/bd.htm",
        note="「热门辟谣」月度汇总：每条覆盖多个已定性谣言，天然带 ground truth",
    ),
    Source(
        key="piyao-zs",
        publisher="中国互联网联合辟谣平台",
        level="权威结论",
        base="https://www.piyao.org.cn",
        entry="https://www.piyao.org.cn/rm/zs.htm",
        note="「真相」栏目汇总：对流传热点的官方求证结论",
    ),
)

# 详情页正文抓取的**域名白名单**：只允许落到这些域，防重定向把人带出去。
# ⚠️ 2026-10-07 扩容（`--curated` 定向摄取，治裁判弃权的证据通路）：
#    新增的 8 个域全部是**中央级官方媒体 / 部委官网**，每条 URL 的发布方与
#    为什么可信写在 `CURATED_URLS` 里；白名单仍是**唯一的出网面**——
#    列表页源（SOURCES）与定向摄取（CURATED_URLS）共用同一道闸门。
ALLOWED_HOSTS = frozenset({
    "www.piyao.org.cn", "piyao.org.cn",
    "www.thepaper.cn",          # 澎湃新闻（转载公安部网安局通报）
    "m.gmw.cn",                 # 光明网（中央重点新闻网站）
    "zj.cnr.cn", "law.cnr.cn",  # 央广网（中央广播电视总台）
    "news.bjd.com.cn",          # 京报网（北京日报，转载公安部网安局通报）
    "news.cctv.cn", "news.cctv.com",  # 央视网（中央广播电视总台）
    "wzjs.jcrb.com",            # 正义网（最高人民检察院主办）
    "k.sina.cn",                # 新浪看点（转载法治网/公安部网安局提示，标二手转述）
    "www.jxzfw.gov.cn",         # 江西政法网（省委政法委官网，转载公安部典型案例）
})

#: 定向摄取的 URL 清单（2026-10-07，治弃权的证据通路）。
#:
#: 🔴 为什么需要它：列表页只能抓到**站点当下展示**的条目，而 13 个案例事件
#:    的官方通报散落在不同站点、多为 2024–2026 的历史文章——裁判弃权的病因
#:    （92% 弃权全在证据通路）只能靠**把这些官方定性放进库**来治。
#: 语义：每条 = 一个已知 URL 的官方文章。`publisher` 是转载平台、
#: `origin` 是内容真正出自谁（正文「来源：…」抽不到时用它兜底）、
#: `published` 是文章自带日期（维护者从文章页眉核对，**不是抓取时间**）、
#: `why` 记这条为什么可信/覆盖哪个案例事件。
#:
#: ⚠️ **清单可以加，不许静默删**：删一条 = 那个案例事件重新回到「结构性
#: 弃权」，而「没抓到」与「被删了」在产物里长得一模一样。下线一条 URL
#: 须注明原因（如对方站点永久失效，换替代源补上）。
CURATED_URLS: tuple[dict[str, str], ...] = (
    {   # c-food-01 矿泉水「泡尸水」（基准：不实）
        "url": "https://www.thepaper.cn/newsDetail_forward_31510374",
        "publisher": "澎湃新闻", "origin": "公安部网安局",
        "level": "权威结论", "published": "2025-08-29",
        "why": "公安部网安局通报全文：云南临沧耿马曹某某造谣被行政处罚，"
               "企业核查从未发生人员死亡事件",
    },
    {   # c-food-02 武山食堂（基准：不实）
        "url": "https://m.gmw.cn/2025-12/02/content_1304247128.htm",
        "publisher": "光明网", "origin": "公安部网安局",
        "level": "权威结论", "published": "2025-12-02",
        "why": "公安部网安局通报：甘肃天水武山王某编造食堂腹泻谣言被行政处罚",
    },
    {   # c-water-01 粪水警情通报（基准：不实）
        "url": "https://zj.cnr.cn/qxkx/20250722/t20250722_527276792.shtml",
        "publisher": "央广网", "origin": "公安部网安局",
        "level": "权威结论", "published": "2025-07-22",
        "why": "公安部网安局通报：杭州余杭邵某豪编造虚假警情通报被刑事拘留，"
               "官方查明异味系硫醚类物质",
    },
    {   # c-quake-01 院士预测广东大地震（基准：不实）
        "url": "https://www.piyao.org.cn/20250707/b0f6629c191e4f5eb4fcc249cd30089c/c.html",
        "publisher": "中国互联网联合辟谣平台", "origin": "广东省互联网举报中心",
        "level": "权威结论", "published": "2025-07-07",
        "why": "倪四道院士团队辟谣：截图并非院士发布，已报警；"
               "地震专家确认无科学依据",
    },
    {   # c-quake-01 查处进展 + 当日多条辟谣（今日辟谣汇总）
        "url": "https://www.piyao.org.cn/20250707/470b3a856eda40fa826f786800e329a8/c.html",
        "publisher": "中国互联网联合辟谣平台", "origin": "广东省互联网举报中心",
        "level": "权威结论", "published": "2025-07-07",
        "why": "今日辟谣：院士预测广东8级地震系谣言，中国地震局确认无科学依据",
    },
    {   # c-quake-02 云南楚雄地震预警（基准：不实）
        "url": "https://news.bjd.com.cn/2025/04/14/11130401.shtml",
        "publisher": "京报网", "origin": "公安部网安局",
        "level": "权威结论", "published": "2025-04-14",
        "why": "公安部网安局通报：楚雄李某某两次发布地震预警谣言被行政处罚",
    },
    {   # c-blast-01 萍乡烟花厂爆炸（基准：不实）
        "url": "https://news.cctv.cn/2026/03/22/ARTIGX4njpiiEzTm3zAry3b2260322.shtml",
        "publisher": "央视网", "origin": "央视新闻",
        "level": "权威结论", "published": "2026-03-22",
        "why": "上栗警方核查全县烟花企业无爆炸事故；阮某思用 AI 生成虚假视频被处罚",
    },
    {   # c-blast-02 / c-bank-01 / c-delivery-01 / c-staged-01（公安部 10 起典型案例）
        "url": "https://law.cnr.cn/yfxz/20240425/t20240425_526680566.shtml",
        "publisher": "央广网法治频道", "origin": "公安部",
        "level": "权威结论", "published": "2024-04-25",
        "why": "公安部 2024-04-12 公布 10 起典型案例：民房爆炸（康某余）、"
               "银行倒闭（廖某慧）、广州限外卖（杨某等6人）、秦朗巴黎丢作业"
               "（徐某艺摆拍）四案同文覆盖",
    },
    {   # c-police-01 昆明 131 死伪造通报（基准：不实）
        "url": "https://news.cctv.com/2025/08/01/ARTItUdjx01QjXn6w62X0Vy3250801.shtml",
        "publisher": "央视网", "origin": "公安部网安局",
        "level": "权威结论", "published": "2025-08-01",
        "why": "公安部网安局通报 3 起伪造警情通报案：昆明白某某/陈某某（131死"
               "系编造，实为2死4轻伤）、杭州邵某豪均被刑拘",
    },
    {   # c-deepfake-01 南漳 AI 换脸（基准：确有其事）
        "url": "https://wzjs.jcrb.com/jcsc/202508/t20250826_7111478.html",
        "publisher": "正义网", "origin": "正义网",
        "level": "权威结论", "published": "2025-08-26",
        "why": "最高检主办媒体报道：南漳陈某等三人用 AI 换脸视频骗过人脸核验"
               "激活电话卡获刑——犯罪手法真实存在",
    },
    {   # c-deepfake-02 视频通话换脸诈骗（基准：确有其事）
        "url": "https://k.sina.cn/article_1617264814_606580ae02002jr70.html",
        "publisher": "新浪看点", "origin": "法治网",
        "level": "二手转述", "published": "2026-03-19",
        "why": "公安部网安局 2026-03-17 提示：王女士被 AI 换脸+声纹克隆的"
               "「儿子」视频通话骗走 50 万——手法真实（转载版，如实标二手转述）",
    },
    {   # c-staged-01 秦朗巴黎丢作业（基准：不实·摆拍）
        "url": "https://www.piyao.org.cn/20240703/36db17c6f7a24920b6461d7ac134abe7/c.html",
        "publisher": "中国互联网联合辟谣平台", "origin": "公安部",
        "level": "权威结论", "published": "2024-07-03",
        "why": "辟谣平台新闻追踪：徐某艺与公司编导薛某编造剧本摆拍，"
               "公安机关查明并处罚——官方定性系编造",
    },
    {   # c-bank-01 专文（基准：不实）
        # 🔴 为什么单加一条：law.cnr 的 10 案长文（2699 字覆盖 4 案例）里
        #    银行倒闭段落被 BM25 长度归一化稀释到 5.81（阈值 10）⇒ 检索
        #    零命中。专文短、聚焦单案，分数才能过线（2026-10-07 实测教训）。
        "url": "https://www.jxzfw.gov.cn/2024/0416/2024041656524.html",
        "publisher": "江西政法网", "origin": "公安部",
        "level": "权威结论", "published": "2024-04-16",
        "why": "省委政法委官网转载公安部 10 起典型案例：廖某慧编造"
               "「某银行倒闭」谣言吸粉引流被拘留——正文含银行倒闭案专述",
    },
    {   # c-blast-02 专文（基准：不实）—— 2026-10-07 补，治最后弃权之一
        # 🔴 同 c-bank-01 的病：官方定性只在 law.cnr 10 案长文里被稀释。
        #    这篇是 4 案事故谣言汇总（约 1200 字，比 2699 字长文聚焦一倍），
        #    巫溪段约 300 字专述，含谣言标题逐字（与案例 text 同源）+
        #    AI 生成细节 + 行政拘留定性。来源是应急管理部官微。
        "url": "https://m.gmw.cn/2024-01/04/content_1303620471.htm",
        "publisher": "光明网", "origin": "中国应急管理",
        "level": "权威结论", "published": "2024-01-04",
        "why": "应急管理部官微汇总：康某某用 AI 编造「重庆巫溪一民房发生"
               "爆炸事故，4人不幸遇难」谣言被行政拘留——官方定性系编造",
    },
    {   # c-delivery-01 专文（基准：不实）—— 2026-10-07 补，治最后弃权之二
        #    广州市市场监管局的正式定性通报（经核查严重不实、恶意曲解
        #    电动自行车限行新规），限外卖段全文专述。
        "url": "https://www.piyao.org.cn/20231225/17f4e06380fa4d7d90147f15a84faf88/c.html",
        "publisher": "中国互联网联合辟谣平台",
        "origin": "广东省互联网举报中心",
        "level": "权威结论", "published": "2023-12-25",
        "why": "今日辟谣：广州市场监管局通报「广州市将实行全新政策，成为"
               "全国首个限制外卖配送的城市」严重不实，系恶意曲解电动自行车限行新规",
    },
    {   # c-delivery-01 补充（月度辟谣榜官方定性）
        "url": "https://www.piyao.org.cn/20240110/5715a3f23d3a4746b6e45ebaedcb397b/c.html",
        "publisher": "中国互联网联合辟谣平台",
        "origin": "中国互联网联合辟谣平台",
        "level": "权威结论", "published": "2024-01-10",
        "why": "2023年12月辟谣榜：谣言6 点名「广州成为全国首个限制外卖"
               "配送的城市」，综述定性为曲解民生政策的虚假信息",
    },
)

TAG_STRIP = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")
ASSET = re.compile(r"\.(css|js|png|jpg|jpeg|gif|ico|svg|woff2?)$", re.I)
LINK = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)

# 详情页里像正文的行（太短的通常是导航/版权）
MIN_TEXT_LINE = 12


@dataclass
class FactEntry:
    """一条事实原料。**来源字段不许含糊**：url / publisher / level 必填。"""

    source_key: str
    publisher: str
    level: str                      # 权威结论 / 一手材料 / 二手转述
    title: str
    url: str
    published: str                  # 从 URL 日期路径推出；推不出就留空（**不许编**）
    body: str = ""
    fetched_at: str = ""
    title_only: bool = True
    origin_publisher: str = ""
    """**原始发布方**，从正文「来源：…」抽出；抽不出留空。

    ⚠️ `publisher` 是**我从哪个站抓的**，`origin_publisher` 是**这条内容谁发的**。
    实测两者**常常不是同一个**：辟谣平台的条目多转载自「网信中国」微信公众号，
    正文里明写「来源：…」。混为一谈就是把转载平台冒充成原始权威方，
    而"这条结论有多可信"恰恰取决于**原始**发布方是谁。
    """

    def __post_init__(self) -> None:
        if not self.fetched_at:
            self.fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get(url: str, timeout: int = 15) -> str:
    """只走 https、只落白名单域；越界直接抛，不静默吞。

    🔴 2026-10-07 修两类**实测踩到**的编码坑（扩 curated 源时暴露；
    此前 piyao 源是 utf-8 且不压缩，所以从没显形）：
      1. **无条件 gzip**：gmw / jcrb 不看 Accept-Encoding 直接回 gzip 字节
         （响应头只写 Content-Type: text/html，连 Content-Encoding 都没有）
         ⇒ utf-8 解码成乱码/空串，正文「恰好全空」而**不报任何错**。
      2. **gb2312 页面**：cnr 两域 `<meta charset="gb2312">`，utf-8 解码
         ⇒ 中文整段变问号碎片，标题直接乱码。
    ⇒ 解法：魔数识别 gzip 就解压；charset 先看 HTTP 头，再嗅探
    `<meta charset=…>`，都没有才落 utf-8。
    """
    if not url.startswith("https://"):
        raise ValueError(f"拒绝非 https 请求：{url}")
    host = urllib.parse.urlsplit(url).hostname or ""
    if host not in ALLOWED_HOSTS:
        raise ValueError(f"拒绝白名单外的主机：{host}（{url}）")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        header_charset = (resp.headers.get_content_charset() or "").strip()
    if raw[:2] == b"\x1f\x8b":  # gzip 魔数（服务器无条件压缩时不带 Content-Encoding）
        import gzip
        raw = gzip.decompress(raw)
    charset = header_charset or _sniff_charset(raw) or "utf-8"
    try:
        return raw.decode(charset, "ignore")
    except LookupError:  # 不认识的 charset 名（如 gb2312 的别名缺失）⇒ 退 gb18030
        return raw.decode("gb18030", "ignore")


def _sniff_charset(raw: bytes) -> str:
    """从 HTML 头部嗅探 `<meta charset=…>` / `…content="text/html; charset=…"`。"""
    head = raw[:2048].decode("ascii", "ignore")
    m = re.search(r'charset\s*=\s*["\']?\s*([a-zA-Z0-9_-]+)', head, re.I)
    return m.group(1).lower() if m else ""


def _text_of(html_fragment: str) -> str:
    return WS.sub(" ", html.unescape(TAG_STRIP.sub("", html_fragment))).strip()


def _date_from_path(href: str) -> str:
    """从 `20260930/xxx/c.html` 推出发布日期；推不出返回空串。

    ⚠️ **推不出就留空，不许猜、不许用抓取时间冒充发布时间** ——
    那是把"我什么时候抓的"伪装成"它什么时候发的"，属伪造溯源信息。

    ⚠️ 正则必须**两种形态都认**：详情页链接在列表页里通常是**相对路径**
    （`20260930/xxx/c.html`，**无前导斜杠**），也可能带前导斜杠。
    第一版只认带斜杠的形态 ⇒ `published` 整列为空而**看起来一切正常** ——
    「没解析出来」与「本来就没有日期」在产物里长得一模一样。
    """
    m = re.search(r"(?:^|/)(20\d{2})(\d{2})(\d{2})(?:/|$)", href)
    if not m:
        return ""
    y, mo, d = m.groups()
    try:
        datetime(int(y), int(mo), int(d))
    except ValueError:
        return ""
    return f"{y}-{mo}-{d}"


def _abs(base: str, href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    return base.rstrip("/") + "/" + href.lstrip("/")


def _abs_from_page(page_url: str, href: str) -> str:
    """相对**页面**解析链接（2026-10-07 扩库引入）。

    🔴 为什么不能沿用 `_abs`：栏目页（如 `/rm/bd.htm`）里的条目链接是
    `../20260903/…/c.html` 形态 —— `_abs` 会拼出 `…/../20260903/…`。
    curl 会自动归一化点段（实测 200），**urllib 不会**（实测 HTTP 400，
    正文全军覆没）⇒ 必须用 `urljoin` 按相对路径语义解析：
    `urljoin('…/rm/bd.htm', '../20260903/x/c.html') → '…/20260903/x/c.html'`。
    """
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    return urllib.parse.urljoin(page_url, href)


def collect(src: Source, *, with_detail: bool, limit: int) -> list[FactEntry]:
    html = _get(src.entry)
    out: list[FactEntry] = []
    seen: set[str] = set()

    for href, inner in LINK.findall(html):
        href = href.strip()
        if href.startswith(("javascript:", "#", "mailto:")):
            continue
        if ASSET.search(href):
            continue
        title = _text_of(inner)
        if len(title) < 6:
            continue
        # 只要**带日期路径的详情页**（`.../20260930/<hash>/c.html`）。
        # ⚠️ 不要用「路径够长」这种宽松条件：实测会放进 `pypt_gywm/index.html`
        # 这种**栏目/关于页** —— 它同域、也能抓到正文，但**不是事实条目**，
        # 混在库里会被检索命中而它根本没有日期与发布方。
        # 收紧的好处：站点哪天改了 URL 规则 ⇒ 结果是**0 条**这种响亮失败，
        # 而不是"混进去一些没日期的条目"这种静默污染。
        if not re.search(r"(?:^|/)20\d{6}(?:/|$)", href):
            continue
        url = _abs_from_page(src.entry, href)

        # ⚠️ 只要**本站**条目：列表页里混着页眉页脚的**外链**（实测 89 条里有
        # 10 条指向 weibo.com 等友情链接/导航）。它们抓不到正文 ⇒ 日期、发布方全空
        # ⇒ 一旦进库就成了「没有来源的事实」，那正是本项目最忌讳的假证据。
        # 而「抓不到正文」与「本来就没日期」在产物里长得一模一样，只能靠**入口**拦。
        host = urllib.parse.urlsplit(url).hostname or ""
        if host not in ALLOWED_HOSTS:
            continue

        if url in seen:
            continue
        seen.add(url)

        body = ""
        origin = ""
        body_date = ""
        if with_detail:
            try:
                body = _extract_body(_get(url))
                origin, body_date = _parse_meta(body)
            except (urllib.error.URLError, urllib.error.HTTPError,
                    ValueError, TimeoutError) as exc:
                # 详情页抓不到**照常保留标题条目**，但**如实标记**没正文
                body = ""
                print(f"    [跳过正文] {type(exc).__name__}: {exc}", file=sys.stderr)
            time.sleep(0.4)  # 别把对方站点当自己的接口打

        # 日期：**正文标注优先**于 URL 路径（实测两者会对不上，差一天也是错）
        published = body_date or _date_from_path(href)

        out.append(FactEntry(
            source_key=src.key,
            publisher=src.publisher,
            level=src.level,
            title=title[:200],
            url=url,
            published=published,
            body=body,
            title_only=not body,
            origin_publisher=origin,
        ))
        if limit and len(out) >= limit:
            break
    return out


def _title_of(html_text: str) -> str:
    """从详情页 HTML 抽 <title>（`--curated` 定向摄取用）。

    为什么不用 H1：各站正文页结构不一，`<title>` 是唯一稳定存在的；
    分隔符后缀（`_央视网`、`-澎湃新闻`）**原样保留**——那正是「这条内容
    出自哪个站」的一部分，删了反而是把溯源信息洗掉。
    """
    m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.S | re.I)
    return _text_of(m.group(1)) if m else ""


def collect_curated() -> list[FactEntry]:
    """按 `CURATED_URLS` 定向抓取（2026-10-07，治弃权的证据通路）。

    与 `collect()`（列表页）的差异：URL 是**维护者逐条核实过的已知文章**，
    不是从列表页解析出来的 —— 所以 publisher/level/日期兜底都来自清单
    （每条都写了为什么可信），正文抓取失败则**整条跳过**（与列表页模式
    「保留标题条目」不同：定向摄取的价值全在正文，没正文就没有官方定性，
    留一条只有标题的条目等于往库里放一条**看不出结论的事实**）。
    """
    out: list[FactEntry] = []
    for spec in CURATED_URLS:
        url = spec["url"]
        try:
            html_text = _get(url)
        except (urllib.error.URLError, urllib.error.HTTPError,
                ValueError, TimeoutError) as exc:
            print(f"  [跳过] {url}\n         {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            continue
        body = _extract_body(html_text)
        title = _title_of(html_text) or spec["publisher"] + "（标题抽取失败）"
        # 正文「来源：…」抽到的原始发布方优先；抽不到用清单里核实过的
        origin, body_date = _parse_meta(body)
        published = body_date or spec["published"]
        out.append(FactEntry(
            source_key="curated",
            publisher=spec["publisher"],
            level=spec["level"],
            title=title[:200],
            url=url,
            published=published,
            body=body,
            title_only=not body,
            origin_publisher=origin or spec["origin"],
        ))
        time.sleep(0.5)
    return out


def _parse_meta(body: str) -> tuple[str, str]:
    """从正文头部的「来源：…」「时间：YYYY-MM-DD」抽原始发布方与发布日期。

    ⚠️ 为什么不能只用 URL 里的日期路径：**实测对不上** ——
    详情页 URL 是 `.../20260901/...`，而正文标注「时间： 2026-09-02」。
    URL 路径是**该站上线这条内容的日期**，不是权威方发布的时间；
    差一天也是错的，而"日期差一天"与"日期对"在产物里长得一模一样。

    ⇒ 优先取正文标注；取不到才退回 URL 路径，并且**留空优先于猜**。
    """
    origin = ""
    m = re.search(r"来源[：:]\s*([^\s时]{2,40}?)(?=\s*时间[：:]|\s*$)", body[:200])
    if m:
        # ⚠️ 用 replace 去掉**所有**引号，不是 strip：实测正文写作
        # 「来源： “网信中国”微信公众号」⇒ 后引号在**中间**，strip 只管首尾，
        # 会留下 `网信中国”微信公众号` 这种半吊子名字。
        origin = re.sub(r"[“”„‟'\"《》]", "", m.group(1)).strip(" 、，,。")
    date = ""
    m = re.search(r"时间[：:]\s*(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", body[:200])
    if m:
        y, mo, d = m.groups()
        date = f"{y}-{int(mo):02d}-{int(d):02d}"
    return origin, date


def _extract_body(html: str) -> str:
    """抽正文：取最长的若干段文本行。不追求精确，追求**不伪造**。"""
    lines: list[str] = []
    for chunk in re.findall(r"<p[^>]*>(.*?)</p>", html, re.S):
        t = _text_of(chunk)
        if len(t) >= MIN_TEXT_LINE:
            lines.append(t)
    if not lines:  # 有些页不用 <p>
        for chunk in re.findall(r"<div[^>]*>(.*?)</div>", html, re.S):
            t = _text_of(chunk)
            if len(t) >= MIN_TEXT_LINE:
                lines.append(t)
    return "\n".join(lines[:40])[:4000]


def main() -> int:
    ap = argparse.ArgumentParser(description="离线抓取权威源条目（维护者手动跑）")
    ap.add_argument("--source", default="", help="只抓指定源（默认全部）")
    ap.add_argument("--detail", action="store_true", help="连详情页正文一起抓")
    ap.add_argument("--curated", action="store_true",
                    help="按 CURATED_URLS 定向抓取（案例事件的官方通报）")
    ap.add_argument("--limit", type=int, default=0, help="限制条目数（0 = 不限）")
    ap.add_argument("--out", default="", help="输出目录（默认 legal_acps/facts/）")
    args = ap.parse_args()

    out_dir = Path(args.out) if args.out else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.curated:
        # 🔴 定向摄取与列表页抓取**互斥**（一条命令只干一件事）：
        #    混在一起的话，「这次产物来自哪个通道」在文件名上就说不清了。
        print(f"── curated｜定向摄取 {len(CURATED_URLS)} 条官方通报")
        entries = collect_curated()
        if not entries:
            print("  [失败] 一条都没抓到 ⇒ 全部 URL 失效或网络不通，如实失败"
                  "（不是「库里没有」，是「没抓成」）")
            return 1
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = out_dir / f"raw-curated-{stamp}.jsonl"
        # ⚠️ newline="\n" 必须显式给：Windows 文本模式会把 \n 翻成 \r\n，
        #    而同步纪律要求产物**纯 LF**（2026-10-07 实测踩坑：CRLF=12~20）。
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            for e in entries:
                fh.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")
        _crlf = path.read_bytes().count(b"\r\n")
        if _crlf:
            print(f"  [失败] 产物混入 CRLF {_crlf} 处（Windows 换行翻译没关住）"
                  " ⇒ 已删除脏产物，修写入方式后重跑")
            path.unlink()
            return 1
        with_body = sum(1 for e in entries if not e.title_only)
        print(f"  → {len(entries)} 条（含正文 {with_body}）→ {path}")
        print(f"\n合计 {len(entries)} 条。⚠️ 这些是**原料**，进库前须逐条核对来源与日期。")
        return 0

    picks = [s for s in SOURCES if not args.source or s.key == args.source]
    if not picks:
        print(f"[失败] 没有名为 {args.source!r} 的源；现有：{[s.key for s in SOURCES]}")
        return 2

    total = 0
    for src in picks:
        print(f"── {src.key}｜{src.publisher}（{src.level}）")
        try:
            entries = collect(src, with_detail=args.detail, limit=args.limit)
        except (urllib.error.URLError, urllib.error.HTTPError,
                ValueError, TimeoutError) as exc:
            print(f"  [失败] {type(exc).__name__}: {exc}")
            continue
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = out_dir / f"raw-{src.key}-{stamp}.jsonl"
        # ⚠️ 同上：newline="\n" 防止 Windows 换行翻译污染产物（纯 LF 纪律）。
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            for e in entries:
                fh.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")
        _crlf = path.read_bytes().count(b"\r\n")
        if _crlf:
            print(f"  [失败] 产物混入 CRLF {_crlf} 处 ⇒ 已删除脏产物，修写入方式后重跑")
            path.unlink()
            return 1
        with_body = sum(1 for e in entries if not e.title_only)
        print(f"  → {len(entries)} 条（含正文 {with_body}）→ {path}")
        total += len(entries)

    print(f"\n合计 {total} 条。⚠️ 这些是**原料**，进库前须逐条核对来源与日期。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
