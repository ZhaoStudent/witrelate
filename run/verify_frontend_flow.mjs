// 前端**行为**验证：真跑 index.html 里的脚本，断言它发出去的 HTTP 方法。
//
//     node run/verify_frontend_flow.mjs
//
// 为什么需要这一层（而不是再加几条字符串断言）：
//
//   2026-10-01 实测的致命缺陷是 —— 揭示按钮调 `api('.../reveal')`（不带 body），
//   而 `api()` 当时用"有没有 body"**推断**方法，于是实际发的是 **GET**；
//   服务端那个端点只注册了 POST，实测 `GET → 405`。
//   **整局演练的核心交付物根本走不到**，而全部离线验证是绿的：
//     · HTTP 用例直接 `client.post(...)` —— 绕过了这段 JS；
//     · 前端断言只查"字符串在不在" —— 方法错不错，字符串看不出来。
//
//   同类问题还有：`renderReveal()` 藏了 `#composer`，`resetToIntro()` 从不恢复，
//   于是「再体验一次」后输入框永久消失。它当时被上面那个 405 掩盖（揭示从没成功过）。
//
//   这两个缺陷的共同点是：**只有在脚本真的跑起来时才看得见**。所以这里用一个
//   最小 DOM/网络桩把脚本跑起来，直接量"发出去的是什么方法、点完之后界面是什么状态"。
//
// 输出：每项 [通过]/[失败]；有失败则退出码 1。

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const HTML_PATH = join(ROOT, 'legal_acps', 'experience', 'web', 'index.html');

let PASS = 0;
let FAIL = 0;

function check(label, got, expect) {
  const ok = JSON.stringify(got) === JSON.stringify(expect);
  ok ? PASS++ : FAIL++;
  console.log(
    `  [${ok ? '通过' : '失败'}] ${label}：${JSON.stringify(got)}（期望 ${JSON.stringify(expect)}）`
  );
}

function section(name) {
  console.log();
  console.log(`── ${name} ${'─'.repeat(Math.max(0, 58 - name.length))}`);
}

// ── 最小 DOM 桩 ────────────────────────────────────────────────────────
class ClassList {
  constructor() {
    this.set = new Set();
  }
  add(...names) {
    names.forEach((n) => this.set.add(n));
  }
  remove(...names) {
    names.forEach((n) => this.set.delete(n));
  }
  toggle(name, force) {
    if (force === undefined) {
      this.set.has(name) ? this.set.delete(name) : this.set.add(name);
    } else {
      force ? this.set.add(name) : this.set.delete(name);
    }
  }
  contains(name) {
    return this.set.has(name);
  }
}

class El {
  constructor(id) {
    this.id = id;
    this.classList = new ClassList();
    this._html = '';
    this.textContent = '';
    this.value = '';
    this.disabled = false;
    this.onclick = null;
    this.dataset = {};
    this.className = '';
    this.children = [];
  }
  // ⚠️ `innerHTML` 做成访问器并**顺带解析出标记按钮**，不是洁癖：
  //    `renderTurns()` 把「这句有问题」按钮拼在 innerHTML 字符串里，
  //    stub 若不解析，`querySelectorAll('button.mark')` 就永远返回空 ——
  //    那样"点了标记 → 揭示时真的带上去了"这条断言就是**空转**的
  //    （把标记功能整个删掉，它照样通过）。
  set innerHTML(value) {
    this._html = String(value ?? '');
    this.children = parseMarkButtons(this._html);
  }
  get innerHTML() {
    return this._html;
  }
  addEventListener() {}
  focus() {}
  scrollIntoView() {}
  appendChild(child) {
    this.children.push(child);
  }
  querySelectorAll(selector) {
    if (selector !== 'button.mark') return [];
    // 递归找 —— 标记按钮挂在**每条消息的** innerHTML 里，
    // 而 `#msgs` 的直接子元素是消息容器，不递归就一个都找不到。
    const out = [];
    const walk = (el) => {
      for (const child of el.children) {
        if (String(child.className || '').split(/\s+/).includes('mark')) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
  click() {
    return this.onclick ? this.onclick() : undefined;
  }
}

/** 从 innerHTML 字符串里认出 `<button class="mark…" data-round="N">…</button>`。 */
function parseMarkButtons(html) {
  const out = [];
  const re = /<button class="([^"]*)" data-round="(\d+)"[^>]*>([^<]*)<\/button>/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    const btn = new El('btn-mark-' + m[2]);
    btn.className = m[1];
    btn.dataset.round = m[2];
    btn.textContent = m[3];
    out.push(btn);
  }
  return out;
}

function makeDom() {
  const byId = new Map();
  return {
    getById(id) {
      if (!byId.has(id)) byId.set(id, new El(id));
      return byId.get(id);
    },
    document: {
      getElementById: (id) => byId.get(id) || byId.set(id, new El(id)).get(id),
      createElement: () => new El('#created'),
      querySelectorAll: () => [],
    },
    byId,
  };
}

// ── 假网络：记录每次请求的方法与路径 ────────────────────────────────────
// `opts.degraded = true` 时让 `/reveal` 返回**降级**形态（服务端在裁判交不回
// 结论时的样子）：`judge_ok: false` + 三节空缺 + 一段说明。用例 C 用它验证
// 前端**真的**把提示顶到最上面，而不是只把字段塞进 DOM 里没人看得见。
const DEGRADED_SUMMARY =
  '这一局裁判没有交回复盘（模型未提交结论），所以本页只显示服务端自己算得出来的部分。「最容易带偏你的地方」「你站得住的地方」「下次怎么防」三节没有内容。';

function makeFetch(calls, opts = {}) {
  const degraded = opts.degraded === true;
  return (path, opts2 = {}) => {
    const method = String(opts2.method || 'GET').toUpperCase();
    calls.push({ path, method, body: opts2.body });
    const base = {
      case: { proposition: '他收受了好处' },
      opening: {
        checks: [],
        judge: {
          worth_debating: true,
          sufficiency: '勉强',
          gaps: [],
          note: '这一局要辩的是：他有没有收受好处。',
        },
      },
      turns: [{ round: 1, role: 'pro', text: '……' }],
      pro_rounds: 1,
      max_rounds: 5,
      reveal: null,
    };
    let data = base;
    if (path.endsWith('/reveal')) {
      data = {
        ...base,
        reveal: {
          judge_ok: !degraded,
          rounds: 2,
          traps: [{ kind: '诉诸情绪', excerpt: '你还替自己辩解？', why: '用情绪替代论证' }],
          trap_groups: [
            {
              layer: '修辞层',
              traps: [{ kind: '诉诸情绪', excerpt: '你还替自己辩解？', why: '用情绪替代论证' }],
            },
          ],
          hot_spots: degraded ? [] : ['你被"你还替自己辩解"带偏了'],
          solid_moves: degraded ? [] : ['你追问了原始出处'],
          advice: degraded ? [] : ['先找原始出处'],
          summary: degraded ? DEGRADED_SUMMARY : '被话术带动是常态，认得出来就不会被当枪使。',
          suspect_report: {
            marked: [1],
            hit: [1],
            false_alarm: [],
            missed: [],
            truth: [1],
          },
        },
      };
    } else if (path.endsWith('/close')) {
      data = { closed: true };
    } else if (path === '/api/session') {
      data = { session_id: 'sid-test', ...base };
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: async () => data,
    });
  };
}

// ── 装载脚本 ───────────────────────────────────────────────────────────
function loadSandbox(opts = {}) {
  const html = readFileSync(HTML_PATH, 'utf8');
  const m = html.match(/<script>([\s\S]*?)<\/script>/);
  if (!m) throw new Error('index.html 里找不到 <script> 块');

  const calls = [];
  const dom = makeDom();
  const notices = [];
  const sandbox = {
    document: dom.document,
    fetch: makeFetch(calls, opts),
    alert: (msg) => notices.push(String(msg)),
    console: { warn: () => {}, log: () => {}, error: () => {} },
  };
  vm.createContext(sandbox);
  vm.runInContext(m[1], sandbox, { filename: 'index.html#script' });
  return { sandbox, calls, dom, notices };
}

async function main() {
  console.log('网络诽谤演练 · 前端行为验证（真跑 index.html 的脚本）');
  const { sandbox, calls, dom, notices } = loadSandbox();
  const $ = (id) => dom.document.getElementById(id);

  section('用例 A：完整一局（开局 → 辩白 → 标记 → 揭示 → 收尾）');
  $('event').value = '有人在公司群里说我把内部资料卖给了竞争对手';
  $('difficulty').value = '困难'; // 真页面里这是 <select> 的当前选项
  await $('startBtn').click();

  const startCall = calls.find((c) => c.path === '/api/session');
  check('开局请求走 POST', startCall && startCall.method, 'POST');
  check('开局后拿到 session_id 并记住', $('chat').classList.contains('hide'), false);

  const parseBody = (call) => {
    try {
      return JSON.parse((call && call.body) || '{}');
    } catch (e) {
      return {};
    }
  };

  // ── 难度必须真的随请求发出去 ──
  // "页面上有个下拉框"不算数：没带上去，难度就只是个装饰。
  check('开局请求带上了所选难度', parseBody(startCall).difficulty, '困难');

  // ── 立题结论必须真的渲染进页面 ──
  // 契约里有字段、服务端也返回了，但前端不渲染，等于这条信息从没到过用户眼前。
  check(
    '开局后渲染出裁判立题的那句话',
    $('openingBox').innerHTML.includes('这一局要辩的是'),
    true
  );

  // ── 局中标记：先自己判一次，再跟真相对照 ──
  const marks = $('msgs').querySelectorAll('button.mark');
  check('每条指控都挂了「这句有问题」按钮', marks.length, 1);
  if (marks.length) {
    await marks[0].click();
    check('点过之后按钮变为已选', marks[0].classList.contains('on'), true);
  }

  $('reply').value = '这条消息的原始出处是哪？';
  await $('sendBtn').click();
  const replyCall = calls.reverse().find((c) => c.path.endsWith('/reply'));
  calls.reverse();
  check('辩白请求走 POST', replyCall && replyCall.method, 'POST');

  // ★ 本轮修的致命缺陷：这里曾经是 GET → 服务端 405
  await $('revealBtn').click();
  const revealCall = calls.find((c) => c.path.endsWith('/reveal'));
  check('★ 揭示请求走 POST（曾被静默发成 GET → 405）', revealCall && revealCall.method, 'POST');

  // ── 标记必须随揭示一起提交（否则那份对照永远是空的）──
  check('揭示请求带上了当事人标的轮次', parseBody(revealCall).suspected, [1]);
  check(
    '复盘里给出「你标的准不准」的对照',
    $('revealBox').innerHTML.includes('你标记了 1 轮'),
    true
  );
  check(
    '揭示后标记按钮失效（对照已经算完了）',
    $('msgs').querySelectorAll('button.mark').every((b) => b.disabled),
    true
  );
  // 反向对照：**正常**那一局里 summary 仍然排在话术清单之后。
  // 没有这条，"降级时把 summary 顶到最上面"可能被实现成"永远放最上面"，
  // 而用例 C 只会照常通过 —— 那就成了用一条断言掩盖另一条的行为。
  // ⚠️ 这条必须在这里查：用例 B 会把页面 reset 回开场页，之后再读
  //    `revealBox` 读到的已经不是这一局的内容了（第一版就查错了地方）。
  check(
    '正常（judge_ok=true）时 summary 排在话术清单之后',
    $('revealBox').innerHTML.indexOf('被话术带动是常态') >
      $('revealBox').innerHTML.indexOf('修辞层'),
    true
  );

  const closeCall = calls.find((c) => c.path.endsWith('/close'));
  check('揭示后自动收尾，且走 POST', closeCall && closeCall.method, 'POST');
  check('全程没有出现过 GET 的 reveal（405 的成因）', !calls.some((c) => c.path.endsWith('/reveal') && c.method !== 'POST'), true);
  check('过程中没有弹出过报错', notices, []);

  section('用例 B：★「再体验一次」后输入框必须回来');
  // renderReveal 收起了输入框 —— 这一步本身是对的（复盘时不该还能继续发言）
  check('揭示后输入框被收起', $('composer').classList.contains('hide'), true);

  // 点「再体验一次」：renderReveal 把它接在 againBtn 上
  const again = $('againBtn').onclick;
  check('揭示页挂上了「再体验一次」的处理函数', typeof again, 'function');
  if (again) await again();

  check('回到开场页', $('intro').classList.contains('hide'), false);
  check('★ 输入框恢复可见（曾经：永久消失，第二局无法反驳）', $('composer').classList.contains('hide'), false);

  // 第二局重新开局 —— 缺陷正是"第一局揭示过之后，第二局开始就坏掉"
  $('event').value = '有人说我把公司的钱挪走了';
  await $('startBtn').click();
  check('★ 第二局开始后输入框仍可见', $('composer').classList.contains('hide'), false);
  check('第二局开局仍走 POST', calls.filter((c) => c.path === '/api/session').every((c) => c.method === 'POST'), true);

  section('用例 C：★ 裁判没交回结论时（降级），提示必须顶到最上面');
  // 服务端在裁判交不回结论时会**降级**（不报废整局），并给 `judge_ok: false`。
  // 前端如果不把它显眼地摆出来，"降级"就会变成一个安静的常态：
  // 页面照样能看，没人知道裁判其实一直没干活 —— 本项目最忌讳的那种缺陷。
  const deg = loadSandbox({ degraded: true });
  const $d = (id) => deg.dom.document.getElementById(id);
  $d('event').value = '有人到处说我拿了公司的钱';
  await $d('startBtn').click();
  await $d('revealBtn').click();
  const degHtml = $d('revealBox').innerHTML;

  check('降级时仍然渲染出话术清单（服务端手上就有，不依赖裁判）', degHtml.includes('修辞层'), true);
  check('降级时仍然渲染出当事人自己的对照', degHtml.includes('你标记了 1 轮'), true);
  check('降级时说明文案出现在页面上', degHtml.includes('裁判没有交回复盘'), true);
  check(
    '★ 说明文案排在**话术清单之前**（不是埋在页面底部）',
    degHtml.indexOf('裁判没有交回复盘') < degHtml.indexOf('修辞层'),
    true,
  );
  check(
    '降级时三节需要裁判判断的内容不渲染（不用像模像样的话填空）',
    // ⚠️ 判据要认**小节标题的那串标记**，不能只 `includes('最容易带偏你的地方')`：
    //    降级文案本身就会**提到**这三个名字（"…这三节没有内容"），
    //    按字面判断会被自己的说明文字骗过去（第一版就是这么写错的）。
    degHtml.includes('>最容易带偏你的地方</div>') || degHtml.includes('>下次怎么防</div>'),
    false,
  );

  console.log();
  if (FAIL) {
    console.log(`❌ ${FAIL} 项失败 / ${PASS + FAIL} 项断言`);
    return 1;
  }
  console.log(`✅ 全部符合预期：${PASS} 项断言通过。`);
  console.log('  · 揭示真的走 POST —— "点了只报 405" 这个缺陷不会再回来');
  console.log('  · 揭示 → 再体验一次 → 第二局，输入框始终可用');
  console.log('  · 裁判没交回结论时提示顶到最上面 —— 降级不会变成安静的常态');
  return 0;
}

process.exit(await main());
