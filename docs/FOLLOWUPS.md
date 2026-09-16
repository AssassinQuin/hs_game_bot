# 跟进队列(跨机器可接续)

> 生成:2026-09-15,v3.1 留牌模拟器合并(main@1119c69)后的终审裁决产物。
> 任何机器克隆本仓库即可按此处上下文接续执行;完成后勾选并关联 issue。
> 背景台账:`.superpowers/sdd/progress.md` 2026-09-15 段落(会话本地,不入库);
> 权威背景:`docs/MULLIGAN_AI.md` v3.1 节 + v3.1 spec。

## [P1] 性能优化——启动判定前置过滤 + known_draws 截断 → Issue #1 ✅ 已完成(2026-09-16)

单推演 ~4.7-10s(手牌 8-10 张时 best_line DFS 爆炸),5 档校准 63 分钟,200 档外推 1-4 天。

- engines=0 乐观上界前置过滤(手牌 segments 总伤+已打 face < 敌血甲 → 跳过 best_line;
  **engines>0 不可用**,引擎循环可抽新牌,上界不封闭)
- launch check 的 known_draws 截断(偏差须量化)
- 完成后 spec §5 性能门按新口径重新立法(立法位已留)
- 验收:真实牌表 5 档 calibrate <10 分钟;同 seed 数值一致(严格无损)或偏差量化;全套绿

**落地记录(7e9c775)**:封闭回合乐观上界短路(严格无损)+`LAUNCH_DRAWS_CAP=12`
截断(策略轨迹不受影响,偏差方向恒保守)。验收全过:5 档 58.1s(65×);无损对拍
(scripts/p1_lossless_proof_probe.py,460 推演)launch_turn **零差异**;截断偏差
3/425(T11 深抽线,恒保守);spec §5 已按新口径立法(≤10min 硬门);全套绿。

## [P2] 启动谓词语义缺口——磨血/场面赢法 vs 单回合爆发 → Issue #2

真实 56 局首可斩 0;advise"全换"即谓词错位表现;结果层永久空洞。
**live 维持 v3 兜底级联,谓词修复前不得解除**(MULLIGAN_AI v3.1 已写死)。

- 谓词重设计(多回合累积斩杀/场面伤害/血甲按职业分层),与 SimSnapshot 统一设计
  (docs/superpowers/specs/2026-09-15-simsnapshot-unification-design.md)一并考虑
- 验收:真实侧首可斩 >0 局;结果层非空洞且启动局胜率 ≥0.60;advise 与卡组常识方向一致

## [P3] 小项加固(7 项) → Issue #3

1. offered 去重(sim_mulligan 概率静默膨胀)
2. advise 空素材守卫(空 material → "全换"假建议)
3. 触发守卫补"回合结束时"族(唯一会把错误方向引入 draw_n 的项,优先)
4. 混合卡(segments+draw_n)测试
5. --orders ≥1 下界
6. 空 jsonl splitlines 守卫
7. calibrate 失败路径返回结构统一

## [P4] recon 对账信号质量(2026-09-16 SimSnapshot 终审产出) → 新开 issue

1. **未知抽牌噪声豁免(Important, 优先)**: 预测侧 play() 对未知抽只 `drawn+1`
   (手牌不长), 实测侧手牌必多具体牌 → 我方引擎线每抽一张未知牌必产
   hand_n/hand_cards 分歧记录, 奇迹德核心循环每回合制造此类噪声,
   by_field 判据被稀释。修法候选: reconcile 识别"diff 卡 ⊄ known 池"
   降级为 unknown_draw 类目(不进分歧), 或 CLI 汇总分列。
2. capture_base 对手 PLAY 块也装配基态后丢弃(正确性无碍, build_piece 走
   进程内缓存; 观测到 CPU 占比再加 actor 前置)。
3. recon main 用法错误走 stdout(宜 stderr)+无文件/JSON 守卫——与 P3-6/7
   同批加固。



- 卡表缓存 `data/cache/cards.zh.json` 不入库,新机器先跑 `python3 scripts/fetch_cards.py`
- 素材重建:`python3 -m trainer material --deck 奇迹德`(trainer/data 为 gitignored 本地产物)
- CLI 旗标形态:顶层旗标在子命令前,子命令旗标(--hand/--coin/--enemy)在子命令后
- 测试基线:364 passed(2026-09-16, SimSnapshot T1-T5 后; P1 落地时点为 343)
