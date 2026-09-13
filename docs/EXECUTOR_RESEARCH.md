# 执行层（鼠标模拟 + 屏幕感知）可行性调研

> 调研日期：2026-09-13。三路并行调研（输入模拟 / 截屏与卡牌视觉 / OCR与生态与封号史），关键数据经 GitHub API、PyPI JSON、官方文档实抓核实。
> 来源分级：[S]=官方文档/官方公告；[A]=一手实测/项目作者/高票技术社区；[B]=技术博客/媒体；[C]=社区轶事（可信度最低）。
> 结论速览：**可行，但封号是策略性硬风险；推荐"日志为决策权威 + 视觉只做锚点校验"的方案二为主线**，全视觉路线（方案四）对本项目是冗余。

---

## 0. 前提事实

### 0.1 代码库现状（2026-09-13 盘点）
- 感知/执行能力为零：全库无窗口定位（HWND/客户区/DPI）、无截图、无输入注入、无图像处理代码。
- 事件链成熟：`GameStore.subscribe` 多订阅者机制（store.py:358，含异常隔离与 PLAY 块时序扣留）→ 执行器可作为第二订阅者干净消费事件，不动现有链路骨架。
- 挂钩点：`mulligan_offer` 已存在（store.py:881-897）；**`play_offer` 尚不存在**（出牌目前是事后事实 `{"kind":"play"}`，store.py:832），对局内执行器的决策输入依赖 PROJECT.md 已定版的 M3/M4 推荐打法先行落地。
- 决策产物已有定义：`Plan/Action`（DESIGN.md:181,260，`actions[0] = 下一步最优`），规划器是纯函数。
- 开关模式模板化：照 `mulligan_advice` 五步（DEFAULTS + `_BOOL` + yaml + 构造期 getattr 门控 + replay 强制关）新增 `auto_play` / `executor_dry_run`。
- **架构契约冲突**：docs/DESIGN.md:21 非目标 N1 明文"不自动出牌，不注入鼠标键盘事件"。执行器落地前必须先修订该非目标声明，否则与项目自身契约自洽性矛盾。
- 环境：Python 3.11.5，单屏 2560×1440；mss/opencv-python/numpy/pillow/PyAutoGUI/pywin32/onnxruntime(CPU) **已装**；需补装仅 `DXcam`、`pydirectinput-rgx`（或纯 ctypes 零依赖）。
- 炉石需无边框/窗口化运行（overlay.py:6-8 已有此约束，视觉层同此前提）。

### 0.2 生态位
"日志做决策权威 + 视觉只做定位校验"的公开先例**不存在**（GitHub API 盘点核实）：HDT/Firestone 是只读日志；Mercenaries bot、HearthstoneGUI 等是纯视觉+模拟输入。我们是独特混合位——这也意味着没有现成交互代码可抄，指向性拖拽细节需自行实测。

---

## 1. 分维度调研结论

### 1.1 鼠标模拟（输入）
| 路线 | Unity/DirectInput 兼容 | 拖拽 | 维护 | 依赖 | 判定 |
|---|---|---|---|---|---|
| PyAutoGUI | **差**（老 API+虚拟键码，游戏常失效） | 中 | 存疑 | 小 | 排除 |
| pydirectinput 原版 | 好 | 中 | **停更**（1.0.4/2021-02 实抓） | 极小 | 排除 |
| **pydirectinput-rgx** | 好（含相对连续移动） | **好**（down→move×N→up） | **活跃**（2.1.3/2025-08 实抓） | 极小 | 备选 |
| **ctypes/pywin32 直写 SendInput** | 好 | 好（时序完全可控） | 随标准库 | **零** | **首选** |
| PostMessage 后台点击 | **失效**（Unity 读 Raw Input，注入消息不可见） | — | — | — | **堵死** |
| Interception/Arduino HID | 好 | 好 | — | 高 | 过度设计（Warden 是用户态反作弊，行为学检测不因硬件输入消失） |

要点：
- SendInput 绝对坐标是 **0–65535 归一化值**而非像素；多显示器需 `MOUSEEVENTF_VIRTUALDESK` + 虚拟桌面原点偏移公式。[S learn.microsoft.com/winuser/ns-winuser-mouseinput]
- 进程必须 `SetProcessDpiAwareness(PER_MONITOR_AWARE)`，截图与点击统一物理像素——否则 ≥125% 缩放下系统性点偏，是此类工具最高频故障。[S+B]
- 拟人化：自实现贝塞尔轨迹+变速（参考 HumanCursor/sarperavci/human_mouse 算法，只取算法不取其 pyautogui 注入层）；经验参数：点击间隔抖动 50–200ms、移动时长 200–700ms 与距离正相关、down→up 60–150ms、决策后 200–500ms 反应延迟（**经验值，无权威来源**）。
- 指向性法术必须"按下→分步渐近移动（路径 hover 触发瞄准箭头）→释放"，瞬移拖拽会丢语义。[B HN:30042268]
- 每步动作前校验 `GetForegroundWindow() == Hearthstone.exe`，弹窗/掉线对话框抢焦点时停机。

### 1.2 屏幕截屏
| 方案 | 维护（实抓） | 1080p 全帧 | ROI<5ms | 独占全屏 |
|---|---|---|---|---|
| **DXcam (ra1nty)** | **活跃**：804★，pushed 2026-03（**注意：社区 fork BetterCam 反而停更 2024-06，与常见印象相反**） | ~4.2ms 官方 / 实测 10–33ms | ✅ | ⚠️ 可能黑帧 |
| mss | 活跃 | 16–47ms | ⚠️ 5–20ms | ⚠️ |
| PIL.ImageGrab / BitBlt | — | 慢 | ❌ | ❌ 黑屏 |

要点：
- 基于 DXGI Desktop Duplication；捕获整块显示器合成输出，**不要求炉石前台**（点击才要求前台，截图不要求）。
- ROI（结束回合按钮等小区域）<5ms **现实**，原生支持 region 抓取。
- 无边框窗口模式是硬前提（独占全屏可能黑帧）。

### 1.3 按钮/UI 元素定位
- 社区标准做法：**matchTemplate（TM_CCOEFF_NORMED，阈值~0.8，带 mask）为主 + 像素采样/颜色占比判态为辅**，几乎无人用 OCR 找按钮。
- 结束回合按钮三态（绿=可点/灰=等待/沙漏）、起手确认、发现三选一、菜单按钮、弹窗 X 全是固定位置固定贴图元素——模板库 <20 张即可覆盖。
- 坐标系：以 1920×1080 人工标定一次，按 `×(实际窗口高/1080)` 等比缩放（HDT 即窗口矩形驱动缩放）；"炉石 UI 是否严格等比缩放"无权威文档，属知识空白，首次实机必须验证。

### 1.4 卡牌视觉识别（对标）
- 官方素材源（[S] hearthstonejson.com/docs/images.html 实抓）：卡面 `art.hearthstonejson.com/v1/{orig|256x|512x}/{CARD_ID}.jpg`；整卡渲染 `.../render/latest/{zhCN|enUS}/256x/{CARD_ID}.png`； Tiles `.../tiles/{CARD_ID}.png`。**无金色/异画静态官方素材。**
- 路线对比：
| 路线 | 精度 | CPU速度 | 数据成本 | 抗金/异画 |
|---|---|---|---|---|
| matchTemplate | 高（固定分辨率+art中心） | ms级 | 低 | 差 |
| pHash(imagehash) | 中高 | 极高 | 低 | 中（金可容忍，异画失败） |
| ORB/SIFT | 高（天然抗旋转，适配手牌扇形） | 中 | 低 | 同模板 |
| CNN/CLIP 嵌入 | 高 | 慢 | 中高 | 较好 |
| YOLO11n 定位+分类 | 定位高/分类看训练 | 640px 下 30–100ms/帧 | 高 | 可增广 |
- 金色=原画动画版（单帧≈静态图，pHash 放宽阈值可容）；异画=完全不同 bespoke 原画（必失败，需单独模板或回退日志）。
- 现成炉石视觉项目全部死掉（wittenbe 2014 停更、HearhRecognizer 古老、hs-card-tiles archived）；无白嫖模型。YOLO 只有通用扑克项目可移植。

### 1.5 OCR
- 首选 **Windows.Media.OCR**（winrt-Windows.Media.Ocr，系统内置零依赖、离线、单帧小图 10–50ms；对描边艺术字体需裁剪+二值化/放大预处理）；次选 RapidOCR(ONNX)（精度高、中英混排好、50–200ms，依赖几十 MB）。
- **不引入**：PaddleOCR 原生版（paddle 全家桶）、EasyOCR（拖整个 torch）、Tesseract（艺术字体差+慢）。
- 定位：本项目日志已含全部对局文字信息，OCR 只做兜底（菜单/结算/模板低置信度二次确认），值得做但不做主角。对炉石描边字体的实测准确率无公开数据，需 1 天 POC。

### 1.6 封号风险（最高风险，策略性）
- EULA/ToS 明文禁止自动化软件（[A] blizzard.com/legal EULA）；封号瞄准"自动化玩法"，只读 tracker 十年未被点名（HDT 4908★、2026-09 仍活跃，实抓）。
- 封号波次（官方记录）：2016 数千账号；**2024 起常态化**：2024-01 约 6 万 → 2024-02 单波 13.5 万+（社区统计三日累计 50 万+）→ 2024-05 单期又近 11 万。[S us.forums.blizzard.com + playhearthstone.com]
- **2025 国服回归后升级**：两周封 10.5 万，公告覆盖"脚本/变速/改皮肤"。[C 知乎转述官微]
- 行为特征是主要靶点（C 级轶事但方向一致）：24h 挂机一周内必永封；每天 8h 内+人工节奏存活。拟人化只降概率不改变违规本质。
- 商业 bot（HearthBuddy）死于法务施压非技术封禁——**不公开、不商业化、不上主账号、不挂机**是存活变量。

---

## 2. 可行性方案（5 个）

### 方案一：纯日志 + 固定几何盲操
决策=日志；动作=SendInput 薄封装 + 1920×1080 相对坐标比例缩放；**零视觉**。
- 依赖：零新增（ctypes 标准库）。工作量：**1–2 天**原型。
- 优点：最简最快；零依赖零维护模板；CPU 零占用；足够验证"炉石接受 SendInput、拖拽语义成立"这一前提。
- 缺点：盲操作——点错无感知；弹窗/断线/结算界面直接失控；槽位随从重排时拖拽落点靠猜；无法菜单导航；误操作不可逆。
- 判定：**作为方案二的第 0 里程碑**（训练模式验证输入链路），不作为终态。

### 方案二：日志驱动 + 视觉锚点校验（推荐主线）
决策=日志（权威）；感知=DXcam region ROI（<5ms）+ matchTemplate 小模板库（结束回合三态/起手确认/发现三选一/菜单按钮/弹窗X）+ 像素采样判色 + 焦点窗口校验；场景 FSM（对局内/主菜单/弹窗/断线）；每步动作前"锚点+日志状态"双校验，不一致→暂停+报警。
- 依赖：+DXcam（模板匹配用已装 opencv）。工作量：执行器 ~1 周 + 标定工具 2–3 天。
- 优点：感知成本极低（每帧只处理几个小 ROI，CPU<2%）；鲁棒覆盖盲操全部缺点（弹窗/焦点/意外界面）；模板资产少；与现有三层架构完美贴合（视觉=新感知环、执行=store.subscribe 第二订阅者、dry_run 照 overlay_topmost 先例）；**OCR/YOLO 全不需要**。
- 缺点：需一次性标定坐标表（半天）；模板随版本更新需重截（炉石 UI 年更几次，量小）；仍以日志正确为前提（hslog 的坑已有清单）。
- 判定：**信息量-成本比最优，主线**。

### 方案三：模板级卡牌实测定标（方案二的指向性补全）
在方案二上加：用日志已知卡 ID 拉 HearthstoneJSON art 模板，对场面随从（无旋转、槽位固定）matchTemplate/pHash **实测槽位中心**，拖拽目标用实测坐标；顺带校验手牌扇区"日志说的=屏幕有的"。
- 依赖：+imagehash（可选）、素材缓存。工作量：+3–5 天。
- 优点：解决**场面随从动态重排**这个纯几何的固有痛点（攻击/指向法术必须点具体随从，其位置随场面变化）；金色卡容忍；"发现含异画"时回退日志推断。
- 缺点：每次指向动作前要扫场面 ROI（仍 ms 级）；异画需单独模板；素材库无金/异画官方静态图（已核实）。
- 判定：**做指向性交互前的必经增量**，与方案二同一体系。

### 方案四：全视觉感知（YOLO 定位 + pHash 分类 + OCR，日志仅训练真值）
YOLO11n ONNX 定位全部 UI/卡牌 → pHash/嵌入分类卡 ID → Windows.Media.OCR 兜底文字；脱离日志运行。
- 依赖：+ultralytics 训练（离线）、onnxruntime（已装）、OCR。工作量：**4–8 周起**（采集标注+训练+维护）。
- 优点：对"暴雪锁日志/改日志格式"免疫；可扩展到观战/录像分析；一套模型吃所有界面。
- 缺点：对本项目**信息冗余**——日志已给全部对局状态，纯视觉信息量 ⊂ 日志；CPU 推理 30–100ms/帧（1–2Hz 校验可行，拖拽跟帧难）；金/异画依旧坑；维护成本最高。
- 判定：**不做主线**；仅当黑天鹅（日志封锁）发生时作为 B 计划备案。

### 方案五：日志+视觉 + LLM 多模态长尾兜底
方案二/三之上，模板匹配置信度低或遇到未知界面时，截图交多模态 LLM（如 GLM-4V）判断"什么界面、该点哪"，返回动作或弃权。
- 依赖：+VLM API。工作量：+2–3 天。
- 优点：覆盖长尾弹窗（活动广告/赛季结算/新功能引导）免穷举模板；菜单导航免标定。
- 缺点：秒级延迟+API 费用；不确定性（对局内动作必须确定性，**只允许用于非对局场景**）；网络依赖。
- 判定：**可选增强**，方案二稳定后再加"未知界面→VLM→动作或弃权"一环。

---

## 3. 对比矩阵

| 维度 | 一 几何盲操 | 二 锚点校验 ★ | 三 卡牌实测 | 四 全视觉 | 五 VLM兜底 |
|---|---|---|---|---|---|
| 工作量 | 1–2 天 | 1.5–2 周 | +3–5 天 | 4–8 周 | +2–3 天 |
| 新增依赖 | 0 | DXcam | +imagehash | ultralytics/VLM API | VLM API |
| 鲁棒性（弹窗/意外界面） | ✗ | ✓ | ✓ | ✓✓ | ✓✓✓ |
| 拖拽落点精度 | 几何猜 | 固定元素准/随从猜 | **全准** | 全准 | — |
| 分辨率适配 | 比例缩放（同标定） | 比例缩放+模板 | 同左 | 天然 | 天然 |
| 版本更新维护成本 | 极低 | 低（<20 模板） | 中（卡模板缓存） | 高（重训） | 极低 |
| CPU 占用 | 0 | <2% | <3% | 高 | — |
| 对本项目信息冗余度 | 0 | 低 | 低 | **高** | 低 |
| 封号暴露面 | 相同（风险在行为特征，与感知方式无关） | | | | |

## 4. 推荐路线（分阶段）

1. **T0（1–2 天）**：DESIGN.md 修订 N1 非目标 → SendInput 薄封装（ctypes，DPI-aware+虚拟桌面归一+焦点校验+dry_run）→ 训练模式盲操验证（方案一）。
2. **T1（~1.5 周）**：DXcam 感知环 + 模板库（结束回合三态/确认/弹窗X/菜单）+ 场景 FSM + 执行器接入 `store.subscribe`（先只吃 `mulligan_offer`）+ config 开关（`auto_play=False` 默认关、`executor_dry_run`、replay 强制关）+ 录屏回放回归测试（不扰前台纪律）。＝方案二。
3. **T2（+3–5 天）**：场面卡牌实测定标（方案三），解锁攻击/指向性法术；与 M3 斩杀 DFS 的 `Plan.actions[0]` 对接。
4. **T3（可选）**：VLM 长尾兜底环（方案五）。
5. **不做**：方案四（除非日志封锁黑天鹅）。

红线（无论哪个方案）：不挂机、不 7×24、每日限量+拟人节奏、不上主账号、代码与产出**永不公开/商业化**、每局结束日志留档自检。

## 5. 知识空白 / 待实测清单
- 炉石是否注册 Raw Input（决定后台点击死刑确认）→ 实测一次 PostMessage 即可判。
- 炉石 UI 在 16:9 各分辨率下是否严格等比缩放 → 首次标定实测。
- Windows.Media.OCR 对炉石描边字体实测准确率 → 1 天 POC。
- Warden 是否检测 injected 输入标记 → 无公开来源，无法排除（假设最坏）。
- 金色/异画卡视觉识别的社区方案 → 不存在，需自建（或回退日志）。
- 拖拽时序参数（步长/间隔/时长）→ 训练模式实测调参。

## 6. 关键信息源
- SendInput/DPI：learn.microsoft.com `ns-winuser-mouseinput`、`nf-winuser-mouse_event` [S]；filipvalentin.github.io 多显示器归一公式 [B]；Devolutions《DPI is the New DNS》[B]
- 输入库：pypi.org `pydirectinput-rgx`（2.1.3/2025-08）`pydirectinput`（1.0.4/2021-02）[S]；learncodebygaming.com DirectInput 原理 [A]
- 后台点击失效：ph3at.github.io/posts/Windows-Input [B]；SO #54210473、GuidedHacking 15879 [A/B]
- 截屏：api.github.com ra1nty/DXcam（804★/2026-03）RootKit-Org/BetterCam（138★/2024-06）[S]；SO #54540307 mss 实测 [A]
- 卡面素材：hearthstonejson.com/docs/images.html [S]
- 按钮/模板：pyimagesearch 多尺度模板匹配 [B]；learncodebygaming bot 教程 [B]
- OCR：pypi.org winrt-Windows.Media.Ocr [A]；learn.microsoft.com text-recognition [A]；RapidOCR discussion #670 [B]
- 封号：us.forums.blizzard.com Bot Ban Update 2024-02/04/05 [S]；playhearthstone.com/blog/16481223 [S]；blizzard.com/legal EULA [S]；outof.games/esports.gg 报道 [B]；知乎 2025 国服 10.5 万封号转述 [C]
- 生态：api.github.com HearthSim/Hearthstone-Deck-Tracker（4908★/2026-09 活跃）[S]；Deopster/Mercenaries-Hearthstone-game-bot [S]；aialt/HearthstoneGUI（2026-02）[S]
- 交互语义：news.ycombinator.com item?id=30042268（点击 vs 拖拽）[B]；hearthstone.wiki.gg Ability/Signature [B]
