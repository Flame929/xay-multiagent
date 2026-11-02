# 角色头像图的来源与授权

- 来源：lmy414/ai-chan-sheets（https://github.com/lmy414/ai-chan-sheets）`设定图/` 目录下的「02 表情」设定图，
  角色：02_GPT娘_白龙娘、03_DeepSeek娘_鲸鱼娘、10_Gemini娘、11_Claude娘_Anthropic。
- 原始角色形象与设定：ZipZipPipe（胖次狐，https://space.bilibili.com/4168597）；设定图补全：lmy414。
- 授权：CC BY-NC-SA 4.0（署名、非商业性使用、相同方式共享），https://creativecommons.org/licenses/by-nc-sa/4.0/
- 本项目做的改动：按设定图自带的素材索引切出每种表情，裁成方形头像，去掉白底，缩到 320px 宽，存成 webp。
  改动后的图片同样按 CC BY-NC-SA 4.0 提供，**仅限个人非商业使用**。
- 没有使用该仓库 `参考原图/` 目录下的任何图片（那部分不在其授权范围内）。

文件名：`<agent>-<表情>.webp`，表情 calm=平静、smile=自然微笑、serious=认真、surprised=惊讶、
pout=鼓腮、eyesclosed=闭眼轻笑、aggrieved=委屈、shy=害羞。

## rules-*.webp（监工）
- 本项目原创角色（规则监工），2026-10-30 用 imagegen2 技能（gpt-image-2）按上面四个角色的画风生成一张 8 表情总图，再裁切去底。
  不是 ai-chan-sheets 的角色；生成时只把现有头像当画风参考。画风参照了上面的头像，为稳妥起见和上面统一：同样按
  CC BY-NC-SA 4.0 提供（仅限个人非商业使用），不适用仓库根目录的 MIT（2026-11-02 定，以后想放宽可以改）。

## <agent>-full.webp（全身立绘，codex / dsh / agy / claude）
- 来源：同一仓库 lmy414/ai-chan-sheets `设定图/` 目录下各角色的「01 三视图」设定图
  （`原图切片/横版完整设定图_01_三视图_原图.png`，取其中第一张「正面」全身图；角色同上：
  codex = 02_GPT娘_白龙娘、dsh = 03_DeepSeek娘_鲸鱼娘、agy = 10_Gemini娘、claude = 11_Claude娘_Anthropic）。
- 原始角色形象与设定、设定图补全的作者同上；授权同上：CC BY-NC-SA 4.0，**仅限个人非商业使用**，
  改动后的图片同样按 CC BY-NC-SA 4.0 提供。
- 本项目做的改动：从三视图总图里裁出「正面」那一张；把纯白底去成透明（从四周向内去底，
  再去掉头发缝隙、龙尾圈里被围住的白底，衣服上的白色部分保留）；贴着人物裁边；
  整体等比缩到高 900px；存成带透明通道的 webp（质量 82）。没有重绘、没有改动人物本身。
  codex 的龙翼左右尖端在原图里就被画面边缘截断，保持原样。
- 同样没有使用该仓库 `参考原图/` 目录下的任何图片。
- `rules`（监工）是本项目原创角色，没有全身图；页面在没有 `rules-full.webp` 时用头像代替。
