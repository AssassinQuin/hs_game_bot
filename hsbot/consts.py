"""跨模块常量 —— 卡牌/实体语义相关的魔法值集中于此。

新增常量的标准: 被 ≥2 个模块使用, 或含义需要注释才能看懂。
仅单模块使用的小阈值留在原地(见各模块 docstring)。
"""

# ---- 实体分类 ----
PET_CARD_PREFIX = "PET_"                 # 宠物等装饰实体卡牌号前缀
UNKNOWN_HUMAN_PLAYER = "UNKNOWN HUMAN PLAYER"   # 对局创建时对手的占位名

# ---- 英雄技能(灌注/替换) ----
TAG_START_OF_GAME_KEYWORD = 1724         # 旧版 hearthstone 枚举缺名, 日志写数字
RENATHAL_CARD_ID = "REV_018"             # 40 卡组唯一开局触发牌(指纹判定目标)
RENATHAL_MIN_DECK_COUNT = 31             # 牌库张数 > 30 视为 40 卡组局

# ---- 费用归因 ----
SPELLPOWER_TYPES = ("SPELL", "HERO_POWER")   # 只有法术/英雄技能吃法强
COIN_CARD_IDS = ("GAME_005",)            # 硬币的稳定 card_id(其余以 COIN 前缀识别)

# ---- 职业(留牌 AI/显示) ----
CLASS_ZH = {"WARRIOR": "战士", "SHAMAN": "萨满", "ROGUE": "潜行者",
            "PALADIN": "圣骑士", "HUNTER": "猎人", "DRUID": "德鲁伊",
            "WARLOCK": "术士", "MAGE": "法师", "PRIEST": "牧师",
            "DEMONHUNTER": "恶魔猎手", "DEATHKNIGHT": "死亡骑士"}
# 英雄卡号→职业 的降级兜底(权威=卡表 cardClass 字段, carddb.card_class);
# 仅当卡表缓存过旧(fetch_cards 早于 cardClass 入 KEEP)时被前缀匹配救场
HERO_ID_CLASS = {"HERO_01": "WARRIOR", "HERO_02": "SHAMAN", "HERO_03": "ROGUE",
                 "HERO_04": "PALADIN", "HERO_05": "HUNTER", "HERO_06": "DRUID",
                 "HERO_07": "WARLOCK", "HERO_08": "MAGE", "HERO_09": "PRIEST",
                 "HERO_10": "DEMONHUNTER", "HERO_11": "DEATHKNIGHT"}


def is_coin(cid) -> bool:
    """幸运币判定(GAME_005 或 *COIN* 前缀) —— store 与留牌推理层共用。"""
    return bool(cid) and ("COIN" in str(cid).upper()
                          or str(cid).upper() == "GAME_005")


def class_zh(en: str) -> str:
    """职业英文标签 → 中文显示名(本地化属于常量层; 未知标签原样返回)。"""
    if en == "UNKNOWN":
        return "未知职业"
    return CLASS_ZH.get(en, "不限职业" if en == "*" else en)
