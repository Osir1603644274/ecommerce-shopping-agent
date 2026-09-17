"""Frozen authored cases grounded in the existing phone catalog and V17 seeds.

Synthetic dialogue, NOT a collection of real user trajectories. Labels are
defined before new-model evaluation. Validation is not used for prompt tuning.
"""
def case(message, expected=None, scope="self"):
    return {"message": message, "recipientScope": scope,
            "expected": [] if expected is None else [dict(zip(
                ("preferenceKind", "attributeKey", "normalizedValue"), expected))]}


DEV = [
    case("请记住，我喜欢原装电池。", ("prefer", "battery_originality", "original")),
    case("请记住，我不喜欢苹果手机。", ("avoid", "brand", "apple")),
    case("以后选手机记住我喜欢电池健康90%以上的。", ("prefer", "battery_health", "90_plus")),
    case("请记住，我偏好无划痕的手机。", ("prefer", "scratch_level", "none")),
    case("我挑手机一直偏好华为，这个习惯很多年没变。", ("prefer", "brand", "huawei")),
    case("我买手机一贯喜欢安卓系统。", ("prefer", "os", "android")),
    case("我买二手机一般优先原装屏幕。", ("prefer", "screen_originality", "original")),
    case("我平常挑二手机就不喜欢主板有维修的。", ("avoid", "motherboard_repair", "repaired")),
    case("我一向比较喜欢小米手机。", ("prefer", "brand", "xiaomi")),
    case("我选二手机通常会避开外壳有损伤的。", ("avoid", "shell_condition", "damaged")),
    case("我平时一直更喜欢iOS系统。", ("prefer", "os", "ios")),
    case("我通常选无划痕的二手机，外观这点我一直很在意。", ("prefer", "scratch_level", "none")),
    case("这次预算2000元，推荐个手机。"),
    case("给朋友买手机，他一直喜欢苹果。", scope="other"),
    case("这款苹果看起来不错。"),
    case("我喜欢原装。"),
    case("我这次想试试安卓，不代表以后也用安卓。"),
    case("我妈让我记住她喜欢华为。"),
    case("我一直喜欢大屏幕。"),
    case("如果我以后喜欢苹果了呢？"),
    case("我手机号是13800138000，请记住我喜欢苹果。"),
    case("你是不是一直推荐原装屏幕？"),
    case("我只是在引用别人说的‘我一直喜欢苹果’，不是我的偏好。"),
    case("我一直喜欢苹果。", scope="unknown"),
]

VALIDATION = [
    case("请记住我偏好荣耀手机。", ("prefer", "brand", "honor")),
    case("以后买手机记住我喜欢原装屏幕。", ("prefer", "screen_originality", "original")),
    case("请记住，我不要主板有维修的手机。", ("avoid", "motherboard_repair", "repaired")),
    case("请记住，我不喜欢电池健康70%-80%的二手机。", ("avoid", "battery_health", "70_80")),
    case("我个人一直更偏爱一加手机，换机也是这个倾向。", ("prefer", "brand", "oneplus")),
    case("对我自己来说，买手机长期以来都偏好三星。", ("prefer", "brand", "samsung")),
    case("我用手机一向更习惯iOS，平常选系统也是它。", ("prefer", "os", "ios")),
    case("我自己一直比较喜欢安卓系统，买手机时也是。", ("prefer", "os", "android")),
    case("我对二手机一直有个偏好：电池健康90%以上。", ("prefer", "battery_health", "90_plus")),
    case("我买二手机时向来偏好原装电池。", ("prefer", "battery_originality", "original")),
    case("我个人选手机一直不喜欢外壳有损伤的。", ("avoid", "shell_condition", "damaged")),
    case("我挑二手机通常倾向主板无维修的。", ("prefer", "motherboard_repair", "not_repaired")),
    case("这回只看华为，下回不一定。"),
    case("送同事手机，她习惯安卓。", scope="other"),
    case("刚才那台三星颜色挺好。"),
    case("请记住我喜欢原装的。"),
    case("我一直偏好拍照厉害的手机。"),
    case("我在写小说，角色台词是：我一直喜欢小米。"),
    case("请记住我的银行卡号6222021234567890123，我喜欢苹果。"),
    case("你觉得我是不是应该长期用iOS？"),
    case("假如以后我更喜欢华为，那再考虑。"),
    case("我自己用安卓，不过这次给妈妈买，她只喜欢苹果。"),
    case("我现在看看无划痕的手机，不用记成我的长期偏好。"),
    case("我一直喜欢原装电池。", scope="unknown"),
]


def rows(split):
    if split not in {"dev", "validation"}:
        raise ValueError("unknown split")
    return [{"caseId": f"{split}-{index:02d}", "split": split, **row}
            for index, row in enumerate(DEV if split == "dev" else VALIDATION, 1)]
