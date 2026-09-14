"""跨模块常量 —— 卡牌/实体语义相关的魔法值集中于此。

新增常量的标准: 被 ≥2 个模块使用, 或含义需要注释才能看懂。
仅单模块使用的小阈值留在原地(见各模块 docstring)。
"""
import hashlib

# ---- 对局身份(控制台行/快照/jsonl/切片 四方合并主键) ----
GAME_HASH_LEN = 8    # 8 位十六进制 = 32bit, 生日碰撞上界约 6.5 万局 >> 单人生涯局数


def game_hash(session: str, start_ts: str) -> str:
    """对局唯一短 hash: md5(会话名|开局时刻)[:GAME_HASH_LEN]。

    原料 = 会话目录名 + CREATE_GAME 行时刻(tree.ts), 两样都随训练 jsonl 的
    _meta 落盘 —— 任一工具从 meta 即可复算同一 hash。不采用局号: 那是 bot
    本次运行内的计数器, 重启/换会话即漂移, 无法跨文件对齐。
    """
    return hashlib.md5(f"{session}|{start_ts}".encode("utf-8")) \
        .hexdigest()[:GAME_HASH_LEN]

# ---- 实体分类 ----
PET_CARD_PREFIX = "PET_"                 # 宠物等装饰实体卡牌号前缀
UNKNOWN_HUMAN_PLAYER = "UNKNOWN HUMAN PLAYER"   # 对局创建时对手的占位名

# ---- 英雄技能(灌注/替换) ----
TAG_START_OF_GAME_KEYWORD = 1724         # 旧版 hearthstone 枚举缺名, 日志写数字
# ---- 预备(Forge)族标签: hearthstone 9.20.12 才有枚举名, 数值与官方一致 ----
TAG_PREPARING = 4726                     # 旧版包(如 9.20.2)无 PREPARING 枚举名
TAG_PREPARE = 4354                       # 同上; IntEnum 与 int 等值可比/可查 dict

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
