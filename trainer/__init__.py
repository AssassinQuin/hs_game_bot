"""trainer —— 离线训练系统(与 bot 解耦的独立目录)。

与 bot 的关系(接合点=文件, 单向依赖):
  bot 侧只负责产出语料: data/training/<卡组>/*.jsonl + 同名 .power.log 原始切片;
  本包单向复用 hsbot 的状态解释层(store/adapter/carddb —— 状态语义不二写),
  bot 不感知本包存在; 后续接入时 bot 只读本包产出的模型产物文件。

目录:
  trainer/states.py    状态快照: GameStore → 决策点状态事实(机读, 无中文)
  trainer/material.py  素材构建: 重放原始日志切片 → (回合快照, 动作序列, 胜负) 流
  trainer/value.py     价值模型: P(胜│回合快照), 梯度提升 + 时序留出 AUC
  trainer/data/        本包私有产物(素材/模型/索引), gitignore, 不入 bot 的 data/

用法:
  python -m trainer material --deck 奇迹德     # 语料 → 回合级训练素材
  python -m trainer train     --deck 奇迹德     # 素材 → 价值模型(时序 AUC 报告)
"""
