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

    def infer_trigger_card(self, keyword, effect_index, deck_count): ...  # 原样保留
    def enrich(self, evt, store): ...                                     # 接口不变
```

carddb 保持纯字典,新增只读 `text(card_id)`(已就位)。
**watcher/render/store 三层调用点签名全部不变。**

## 8. 诚实性原则

- 语法表未覆盖 → `Unknown(raw)` 入 IR,不产生任何标注(宁可少说不说错);
- 指纹推断(雷纳索尔)带 `inferred: true` 供渲染层标注;
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
