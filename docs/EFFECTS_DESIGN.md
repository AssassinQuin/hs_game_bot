# 卡牌信息解析 · 编译器式增量解析层设计 v2

> 状态:**设计稿 v2(待评审)** · 2026-09-13
> 上位需求:卡牌/效果解析只允许存在于一个地方,以编译器方式做增量解析,
> store/render 对规则零感知。
> v2 修订:补成熟项目调研;**实测推翻 CardDefs 任务树方案**(见 §2)。

## 1. 现状与问题

| 现状 | 问题 |
|---|---|
| `analysis.EffectAnalyzer.text_damage` 每次调用对文本跑正则 | 同一张牌一局解析多次,无缓存无增量 |
| 语义($N=基础值、法强按段加成)以正则+注释存在 | 规则不可枚举、难测试、扩展=改函数 |
| 只认识「造成$N点伤害」 | 治疗/增益/AOE/多段无法表达 |
| 卡表更新后解析结果无感知 | 可能与旧文本不一致(无失效机制) |

## 2. 成熟项目调研(2026-09-13 实测)

| 项目 | 做法 | 对本设计的结论 |
|---|---|---|
| **SabberStone**(C# 模拟器) | CardDefs.xml → `Power(Trigger/Spell/Enchant)` → Task 列表(DamageTask/HealTask/…)——任务即 IR | 任务树是理想 IR,但**依赖客户端脚本数据** |
| **kettle/joust**(HearthSim Python 服务器) | python-hearthstone `cardxml` 装载卡牌定义驱动整个游戏逻辑 | cardxml 解析器成熟可用 |
| **HDT**(C#) | SpellDamage 服务:伤害牌判定 + 法强加成计算,按牌逐一登记 | "伤害预估独立成服务"与本方案 analysis 层同构 |
| **python-hearthstone / hearthstone_data** | 官方 cardxml 解析器 + 离线数据包(本机已装 240397.1) | **离线结构化数据源,零下载** |

**关键实测(推翻原 P2)**:下载 build 251951 的 CardDefs.xml(99MB,需 UA 头)逐块检查,
月火术实体仅 60 行,**新版导出已剥离 Power/Task 脚本**(老攻略/SabberStone 时代的
CardDefs 含完整任务树)。因此:
- ❌ 放弃"从 CardDefs 提取任务树作为 IR 前端"(数据源不存在);
- ✅ IR 前端 = **卡牌文本语法规则表**(显示文本的 $N 约定稳定且官方),
  静态属性(type/battlecry/deathrattle/…)改由 **hearthstone_data + cardxml** 提供;
- CardDefs 若未来恢复脚本导出,§6 的任务投影可作为升级路径,接口不变。

### §2.1 机制标签通道(HsJson mechanics[],2026-09-13 晚补测)

HsJson cards.json 每卡自带 `mechanics[]`(引擎审核的统一机制标签,14281 张卡
带值:DREDGE/CHOOSE_ONE/BATTLECRY/TRIGGER_VISUAL…;词表≈GameTag 名)。
曾被 fetch_cards.py 的 KEEP 白名单丢弃——机制识别不应逐卡/逐措辞写死:
- **数据通道(优先)**:`MECHANIC_MARKS` 把 mechanics[] 名映射为 IR 机制标记
  (如 DREDGE→deck_bottom),一张表行=认识一种引擎机制,全类卡自动生效;
- **文本通道(兜底)**:引擎无标签的措辞(如"置于牌库底"只有泛化 DISCOVER)
  走 MECHANICS 正则表,与数据通道并集编译,互不排斥;
- 消费方只查 IR 标记(analysis.has_mechanic),永不接触文本与卡牌号;
- entourage 字段新版 HsJson 已删除——抉择按钮仍以日志 PARENT_CARD 实体为准。
- COMPILER_VERSION 随表语义 +1,强制缓存全量重编译。

## 3. 分层(编译器管线隐喻)

```
数据源               词法/语法解析           中间表示 IR             求值
┌ cards.json   ┐ ┌─ GRAMMAR 语法规则表 ─┐
│ (文本/类型)   │─┤  (一行一条效果语义)   ├─► CardEffect IR ─┐
└ hearthstone_ ─┘ └─────────────────────┘                  │ (content-hash
  data+cardxml                                              │ 增量缓存)
  (静态属性)                                              ▼
                    effects.json ◄────────────  EffectAnalyzer 求值
                    (增量落盘)                    predict_damage / infer_trigger
                                                              │
 consumers: watcher.enrich(渲染前富化) / 快照手牌 effects / M2 决策层 ◄┘
```

依赖方向单向:`analysis → effects → carddb`。store/render 零规则感知。

## 4. 中间表示(IR)

```python
@dataclass(frozen=True)
class Damage:
    base: int              # $N 的 N
    hits: int = 1          # 「两次」→ 2
    scope: str = "single"  # single / all_enemies / random_split
    scaled: bool = True    # 是否吃法强

@dataclass(frozen=True)
class Heal:
    base: int
    hits: int = 1

@dataclass(frozen=True)
class Unknown:             # 诚实降级: 语法表未覆盖
    raw: str

@dataclass(frozen=True)
class CardEffect:
    card_id: str
    cardtype: str                  # 来自 hearthstone_data(cardxml)
    statics: tuple[str, ...]       # battlecry/deathrattle/choose_one... 静态旗标
    effects: tuple                 # tuple[Damage | Heal | Unknown, ...]
    source_hash: str               # 增量键: 参与编译的原始字段指纹
```

## 5. 语法规则表(唯一可扩展点)

```python
GRAMMAR = [
    (re.compile(r"对一个敌人造成\$(\d+)点伤害(两次|三次|四次)?"),
     lambda m: Damage(int(m[1]), HITS.get(m[2], 1))),
    (re.compile(r"对所有敌人造成\$(\d+)点伤害"),
     lambda m: Damage(int(m[1]), scope="all_enemies")),
    (re.compile(r"造成\$(\d+)点伤害，随机"),
     lambda m: Damage(int(m[1]), scope="random_split")),
    (re.compile(r"造成\$(\d+)点伤害"), lambda m: Damage(int(m[1]))),
    (re.compile(r"恢复\$(\d+)点生命"), lambda m: Heal(int(m[1]))),
]
```

实测样本:月火术「造成$1点伤害」→ Damage(1);月光射线「…$1点伤害两次」→
Damage(1, hits=2);活体根须「抉择:造成$2点伤害」→ Damage(2)。
**加一种效果语义 = 加一行规则 + 一个构造器。**

## 6. 增量编译与缓存

```python
class EffectCache:
    """data/cache/effects.json: {card_id: {"h": 指纹, "fx": [效果字典]}}"""
    def get_or_compile(self, card: dict) -> CardEffect:
        h = sha1(card["id"] + card.get("text", "") + card.get("type", ""))
        hit = self._memo.get(card["id"])
        if hit and hit.source_hash == h:
            return hit                      # 增量: 未变化的牌直接复用
        ir = compile_card(card)             # 只解析新牌/文本变化的牌
        self._memo[card["id"]] = ir
        self._dirty = True
        return ir
```

- 内存级增量(会话内一次)+ 磁盘级增量(重启零重编译)+ 指纹失效 + 回写修剪;
- 量级:35807 张全量首编一次(秒级),此后日常只编译新版本新增牌。

## 7. 求值层(analysis.py 改造)

```python
class EffectAnalyzer:
    def __init__(self, carddb, cache): ...

    def predict_damage(self, card_id, spellpower) -> dict | None:
        ir = self.cache.get_or_compile(card_id)      # O(1) 缓存命中
        dmg = [e for e in ir.effects if isinstance(e, Damage)]
        if not dmg:
            return None
        total = sum(e.base + spellpower * e.hits for e in dmg)
        return {"total": total, "hits": max(e.hits for e in dmg)}

    def enrich(self, evt, store): ...                                     # 接口不变
```

carddb 保持纯字典,新增只读 `text(card_id)`(已就位)。
**watcher/render/store 三层调用点签名全部不变。**

## 8. 诚实性原则

- 语法表未覆盖 → `Unknown(raw)` 入 IR,不产生任何标注(宁可少说不说错);
- 触发命名只认日志揭示(SHOW_ENTITY 回填的 card_id),不做指纹猜测 —— 2026-09-13
  实测:旧"开局+Idx=1+牌库≥31 → 雷纳索尔"指纹在新环境(阿扎莉娜等新 40 卡引擎、
  伊瑟拉等开局牌普及)会误标;未揭示的开局触发由渲染层判弃,揭示后以真名出一次;
- random_split 类预估按不放大法强处理,注释说明误差来源。

## 9. 测试策略

| 层 | 测试 |
|---|---|
| 语法表 | 每条规则 golden:真实文本样本(月火术/月光射线/活体根须/回春)→ 期望 IR |
| 缓存 | 二次 get 不重编译;改 text 指纹 → 重编译;删牌 → 回写修剪 |
| 求值 | 预估=Σ(base+法强×hits);类型门(法术/技能才吃法强) |
| 集成 | 12:44 会话回放 diff 零变化(重构红线) |

## 10. 实施阶段

1. **P1**:`effects.py`(IR+GRAMMAR+EffectCache)落地,`EffectAnalyzer` 内部改走缓存,
   公共 API 不变;回放 diff 零变化。
2. **P2**:接入 hearthstone_data/cardxml 静态属性(battlecry/deathrattle 入 IR 的
   statics);语法表扩治疗/增益/AOE;快照手牌 `effects` 从 IR 生成。
3. **P3**:触发指纹表泛化;训练样本导出 IR 视图;CardDefs 任务导出恢复时的升级预研。

## 11. 不做什么

- 不引入外部 NLP/LLM 解析卡牌文本——语法表覆盖可判定效果,未覆盖保持未知;
- 不把 IR 写回 store 状态——IR 是派生事实,不参与状态权威;
- 不做客户端资源包逆向(成本/合规不成比例,官方导出已够)。

## 12. 扩表记录(2026-09-13, 出现牌驱动)

> 方法: 扫描"对局出现过的 cardId 全集"(live 日志 + 训练语料切片 + 登记卡表,
> 513 张) → Unknown 文本聚类 → 按簇扩规则族。不做全卡表 speculative 扩张。
> **覆盖率: 100/513(19.5%) → 368/513(71.7%)**, 余 78 张有文本未覆盖(单例长尾,
> 诚实保持 Unknown) + 67 张无文本 token/附慕(本来无可解析)。

新增 IR kind: `Draw(amount, scope)` / `Armor(amount)` / `Buff(atk, hp)` /
`Summon(count, atk, hp, scope)` / `CostUp(amount)`; CostDown.scope 扩
self / target / drawn / your:X(类目词容 <b> 标签)。COMPILER_VERSION=6。

T1 斩杀线(2026-09-13): 新增 `SpellPower(amount)`(法术伤害增益族, "法术伤害+N",
含 `<b>`/「使其获得」容错)与 `Mechanic("cast_draw")`(施法抽牌触发, 文本通道,
拍卖师族)。COMPILER_VERSION=7。(注: 触发句会被抽牌族裸规则同时误标一条
Draw(1), 当前无消费方, 见 effects.py MECHANICS 行注释。)

规则要点(实测措辞驱动):
- 英雄技能占位符: `#N`=固定值(不吃法强), `$d`=护甲模板, `$a`=攻击模板;
- 抽牌族否定镜: `(?<!每)`挡"每抽一张牌"(动态减费条件), `(?<!在)(?<!手)`挡
  "在你的对手抽牌后"(对手侧事件)—— 抽牌事实只认本牌自己的抽牌效果;
- 减费 scope 口径: self/drawn/hand/next/your 计入 mana_ramp_value 等效回费
  (沿用"建造水晶塔按面值"定版); **target(使其/它的/其, 如侦察)不计回费**
  —— 减的是尚未入手的牌;
- 加费族独立成 CostUp(死灵光环/前沿哨所), 不混入回费口径;
- 机制标签表扩至 30 行(出现牌 mechanics[] 词表盘点, 玩家可见关键词才收录:
  CHARGE/RUSH/TAUNT/…/STARSHIP_PIECE; TRIGGER_VISUAL/AURA 等引擎内部旗标不打标)
  —— 关键词-only 卡(冲锋/突袭/潜行/复生)由数据通道覆盖, 不写文本规则。

`parse-cards` 同步: 扫描范围加语料切片(客户端轮转删老会话日志, 语料是历史
对局持久全集); 报告按 IR kind 通用化(covered/uncovered/notext 旧键保留)。
