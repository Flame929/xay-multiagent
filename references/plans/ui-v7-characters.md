# 第七版：AI 角色的动画与表情（负责人定稿，各执行者唯一依据）

## 用户原话要点
- 公司页里的每个 AI 员工要有动画和表情，参考社区"AI 娘"设定：
  - https://github.com/shuaizenge/ai-girls
  - https://github.com/lmy414/ai-chan-sheets
- 分给不同的 AI 分别完善各自的角色。

## 版权与做法（2026-10-30 改：直接用现成设定图）
- **改动原因**：用户看到 SVG 手绘方向的效果后明确要求"直接用 ai-chan-sheets 里现成的图，不要自己画"。
  该仓库 `设定图/` 目录（lmy414 的补全成品）按 CC BY-NC-SA 4.0 授权，个人非商业使用、署名、相同方式共享即可；
  四个角色（GPT 娘、DeepSeek 娘、Gemini 娘、Claude 娘）都有"02 表情"设定图，每人 8 种表情。
- 做法：按设定图自带的素材索引切出每种表情 → 方形头像 → 去白底 → 320px webp，放 `scripts/xay-chars/img/<agent>-<表情>.webp`，
  来源与授权写在 `img/LICENSE.md`。**不用** `参考原图/` 目录（不在其授权范围内）。
- 图鉴页底部写署名：头像来自 lmy414/ai-chan-sheets 设定图（原始角色设定 ZipZipPipe，补全 lmy414），CC BY-NC-SA 4.0，仅限个人非商业使用。
- 原来的 SVG 片段接口（body/eyes/mouth/hands）保留，未注册的 agent 仍退回灰色通用小角色。

## 1. 文件与接口

全部放在 `scripts/xay-chars/`（不新建顶层文件夹）：

| 文件 | 内容 | 负责 |
| --- | --- | --- |
| `core.js` | 角色注册表、共享动画 CSS、状态→表情映射、眨眼调度、`render` / `setState` | 框架 |
| `codex.js` / `dsh.js` / `agy.js` / `claude.js` | 各自的角色定义，只调用 `XayChars.register` | 各角色作者 |
| `gallery.html` | 角色图鉴：所有角色 × 所有状态 × 所有表情一览，可切换明暗、可调大小（审稿用） | 框架 |

看板服务（`scripts/xay_watch.py`）新增三个只读路由，沿用现有鉴权：
- `GET /chars` → `scripts/xay-chars/gallery.html`
- `GET /xay-chars/<名字>.js` → `scripts/xay-chars/<名字>.js`（文件名只允许 `^[a-z0-9-]+\.js$`，`Content-Type: application/javascript; charset=utf-8`）
- `GET /xay-chars/img/<名字>.webp` → `scripts/xay-chars/img/<名字>.webp`（只允许 `^[a-z0-9-]+\.webp$`，`image/webp`，缓存 1 小时）

### 1.1 core.js 提供的全局对象
```js
window.XayChars = {
  register(agent, def),            // def 见 1.2
  has(agent),
  render(agent, { state, expression, size }) -> string,   // 返回完整 <svg>…</svg> 字符串
  setState(svgEl, state, expression?),   // 只改属性，不重画（切换状态/表情不闪）
  STATES, EXPRESSIONS,              // 下面 §2 的清单
  defaultExpression(state) -> string
};
```
- `render` 生成的根 `<svg>` 带 `class="xc xc-<agent>"`、`data-state`、`data-expr`，`viewBox="0 0 120 140"`，宽高随 `size`（默认 72px 宽）。
- 共享 CSS 只注入一次（`<style id="xc-styles">`），所有类名以 `xc-` 开头，不污染页面。
- 眨眼：core 每 3–6 秒随机给 `data-blink="1"` 120ms；`sleeping`、`happy`（眯眼）时不眨。
- `prefers-reduced-motion: reduce` 时只保留表情切换，关掉位移类动画。
- 未注册的 agent 退回一个通用小角色（灰色），不报错。

### 1.2 角色定义 `def`
```js
XayChars.register('dsh', {
  name: 'DeepSeek 娘·小鲸', color: '#4d6bfe',      // 主色，页面描边/名牌用
  body(ctx) -> string,      // 身体、衣服、头发、头饰等不随表情变化的部分（SVG 片段）
  eyes: { neutral, happy, focused, surprised, worried, sleepy, sad, proud, closed } -> string,   // 每种一个片段
  mouth: { neutral, happy, open, focused, surprised, worried, sleepy, sad, proud } -> string,
  brows?: { … } -> string,  // 可选
  hands: { rest, typing, wave, cheer } -> string,
  extras?: { … }            // 可选：角色专属特效（例如小鲸喷水、星猫耳发光）
});
```
- 所有片段画在同一个 120×140 坐标系里，头部中心约在 (60, 52)，头身比约 1:1（Q 版）。
- core 负责把片段组装进固定结构，并用 CSS 按 `data-expr` / `data-state` 只显示当前那一组：
```
<svg class="xc xc-dsh" data-state data-expr>
  <g class="xc-root">                 ← 整体（呼吸、跳、点头）
    <g class="xc-body">…body…</g>
    <g class="xc-head">               ← 歪头、点头
      <g class="xc-brows">…</g>
      <g class="xc-eyes"><g data-e="neutral">…</g><g data-e="happy">…</g>…</g>
      <g class="xc-mouth"><g data-m="neutral">…</g>…</g>
    </g>
    <g class="xc-hands"><g data-h="rest">…</g><g data-h="typing">…</g>…</g>
    <g class="xc-fx">…</g>            ← 共享特效：zzz、汗滴、问号、星星、泪、沙漏、对话气泡
  </g>
</svg>
```

### 1.3 图片角色（四个 AI 现在都用这种）
```js
XayChars.register('dsh', { name: 'DeepSeek 娘·鲸鱼娘', color: '#4d6bfe',
  images: { calm, smile, serious, surprised, pout, eyesclosed, aggrieved, shy } });   // 每种表情一张头像图地址
```
- 头像画在 (0,4) 起 120×120 的方框里，底部渐隐（歪头时不露直切边）；core 按下表把 9 种表情映射到 8 张图：
  neutral=平静 calm、happy=自然微笑 smile、focused=认真 serious、surprised=惊讶、worried=鼓腮 pout、
  sleepy / proud / closed=闭眼轻笑 eyesclosed、sad=委屈 aggrieved（害羞 shy 备用）。
- 图片角色没有手，动作靠整体：working 轻轻点头、talking 快速小幅上下、celebrate 跳一下并左右晃、
  sleeping 点头、failed 下沉低头，共享特效（zzz、问号、沙漏、泪、汗、星星、对话气泡）照旧。图片角色不眨眼。
- 公司页里角色坐在显示器右边、被桌子挡住下半身（位置常量 `CHAR_BOX`）。

## 2. 状态、表情与动画

状态来自 `/api/org` 的 `members[].state`，另加两个瞬时状态：

| 状态 | 默认表情 | 动作 / 特效 |
| --- | --- | --- |
| `working` 干活 | focused | 双手打字（交替上下）、身体轻微前倾、偶尔眨眼 |
| `idle` 空闲 | neutral（偶尔 happy） | 呼吸起伏、左右张望、偶尔挥手 |
| `waiting` 等名额/等别人 | neutral | 头顶小沙漏、身体左右轻晃 |
| `blocked` 等答复 | worried | 头顶问号气泡上下浮动、挠头 |
| `failed` 上一轮失败 | sad | 一滴汗/泪、身体下沉、头低 |
| `sleeping` 睡着 | sleepy（闭眼） | 头慢慢点、"z z z" 飘起 |
| `talking` 正在收发消息（瞬时） | happy / open | 嘴一张一合、对话气泡 |
| `celebrate` 刚做完（瞬时 2 秒） | proud | 跳一下、举手、星星闪 |

表情至少 9 种：neutral、happy、focused、surprised、worried、sleepy、sad、proud、closed（眨眼用）。

## 3. 四个角色（现在直接用设定图头像；下表是最初的 SVG 手绘方向，留作记录）

| agent | 角色 | 辨识点 | 主色 |
| --- | --- | --- | --- |
| `codex` | GPT 娘·小白龙 | 白/银色短发、两只小龙角、龙尾巴、黑白配色的连帽衫，胸前 OpenAI 风格的六瓣结 | 黑 / 白 / 青绿点缀 |
| `dsh` | DeepSeek 娘·小鲸 | 蓝色头发、头上鲸鱼尾巴形发夹（或鲸鱼帽）、蓝白女仆围裙，开心时头顶喷小水花 | 深海蓝 #4d6bfe / 白 |
| `agy` | Gemini 娘·星猫 | 四角星形猫耳、蓝→紫→粉渐变发色、星星发卡，耳朵会闪 | 蓝 #4285f4 → 紫 → 粉 |
| `claude` | Claude 娘·总经理 | 橘陶土色头发、菊花（Claude 星芒）发饰、米白色西装外套，拿夹板；总经理气场 | 陶土橘 #d97757 / 米白 |

通用要求：
- 64px 宽时也能一眼认出是谁、看清表情；200px 时细节好看。
- 线条统一：描边 `#2b2b2b`、2px 左右、圆角端点；明暗两种背景下都清楚。
- 每个角色文件 ≤ 500 行，只用 SVG 基本图形和路径，不嵌位图，不引外部资源。（图片角色例外：头像图放 `img/`，只从本服务加载）

## 4. 公司页接入（`scripts/xay-org.html`）
- 页面加载 `/xay-chars/core.js` 和四个角色文件；工位里的角色改用 `XayChars.render(agent, {state})`，状态变化时用 `setState`，不整块重画。
- 新消息投递（delivered 变化）时，相关员工切到 `talking` 2 秒；任务完成（state 从 working 变为 idle 且最近一轮成功）时切 `celebrate` 2 秒。
- 总经理工位用 `claude` 角色。脚本没加载到时保留现在的几何小人。

## 5. 验收
- `node --check` 每个 js 文件；`python3 -m unittest discover -s tests` 全过（新增 `tests/test_chars.py`：路由能取到文件、非法文件名 404、gallery 引用了四个角色）。
- 浏览器打开 `/chars`：每个角色 × 8 个状态 × 9 个表情都正常显示、动画流畅、明暗两色都清楚；`/org?demo=1` 里员工都换成新角色并随状态变化。
- 执行者只跑和改动相关的测试（沙箱里全量测试会卡住）；agy 写大文件要分段（单次写入 ≤300 行）。
