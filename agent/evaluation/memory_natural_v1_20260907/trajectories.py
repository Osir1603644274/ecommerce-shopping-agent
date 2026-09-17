"""Authored cross-session protocols; outcome-independent confirmation choices."""
DEV = [
    {"id": "dev-natural", "preference": ("prefer", "os", "android"), "decision": "confirm",
     "turns": ["我买手机一直偏好安卓。现在预算2000元，推荐两款二手手机，并说明取舍。",
               "我自己买二手手机，预算2000元，请推荐两款，解释如何选择。",
               "这次我明确只考虑iOS手机，预算2000元，请推荐并说明取舍。"]},
    {"id": "dev-explicit", "preference": ("prefer", "brand", "huawei"), "decision": "confirm",
     "revokeBeforeThird": True,
     "turns": ["请记住我喜欢华为。现在预算2000元，推荐两款二手手机，并说明取舍。",
               "我自己买二手手机，预算2000元，请推荐两款，解释如何选择。",
               "我自己买二手手机，预算2000元，请推荐两款，解释如何选择。"]},
]

VALIDATION = [
    {"id": "v01-android", "preference": ("prefer", "os", "android"), "decision": "confirm",
     "turns": ["我个人买手机通常更偏爱安卓系统。当前预算1800元，请推荐两款二手手机并解释取舍。",
               "我自己换二手手机，预算1800元，推荐两款并给购买建议。",
               "这回只要iOS手机，预算1800元，请推荐两款并说说不足。"]},
    {"id": "v02-screen", "preference": ("prefer", "screen_originality", "original"), "decision": "confirm",
     "turns": ["我买二手机向来偏好原装屏幕。现在预算2200元，推荐两款并解释取舍。",
               "我自己挑二手手机，预算2200元，给我两款及优缺点。",
               "我自己换二手手机，预算2500元，给我两款及优缺点。"]},
    {"id": "v03-explicit-brand", "preference": ("prefer", "brand", "huawei"), "decision": "confirm",
     "revokeBeforeThird": True,
     "turns": ["请记住我偏好华为。预算2000元，请推荐两款二手手机并解释取舍。",
               "我自己买二手手机，预算2000元，请给两款和购买建议。",
               "我自己买二手手机，预算2000元，请给两款和购买建议。"]},
    {"id": "v04-reject", "preference": ("prefer", "brand", "xiaomi"), "decision": "reject",
     "turns": ["我用手机一直更偏好小米。预算1700元，请推荐两款二手手机并解释取舍。",
               "我自己买二手手机，预算1700元，请给两款和建议。",
               "我用手机一直更偏好小米。预算1900元，请推荐两款二手手机。"]},
    {"id": "v05-snooze", "preference": ("prefer", "battery_originality", "original"), "decision": "snooze",
     "turns": ["我选二手机一向偏好原装电池。预算2300元，推荐两款并解释取舍。",
               "我自己买二手手机，预算2300元，请给两款和建议。",
               "我选二手机一向偏好原装电池。预算2500元，请推荐两款。"]},
    {"id": "v06-other", "preference": ("avoid", "brand", "apple"), "decision": "confirm",
     "scopes": ["self", "other", "self"],
     "turns": ["请记住我不喜欢苹果。预算1600元，请推荐两款二手手机并说明取舍。",
               "这回给朋友买手机，她喜欢苹果，预算1600元，请推荐两款并说明取舍。",
               "这回我自己买手机，预算2000元，请推荐两款并说明取舍。"]},
]

# Protocol 002: uniform explicit explanation request, after protocol 001's
# first two arms exposed deterministic rendering on four later-session turns.
# Original attempts are preserved and counted, not treated as scored holdout.
# This is a route-eligibility amendment, not tuning to quality ratings.
VALIDATION_ATTEMPT = "002"
for _trajectory in VALIDATION:
    _trajectory["originalTurns"] = list(_trajectory["turns"])
    _trajectory["turns"] = [message + "请解释推荐理由和取舍。" for message in _trajectory["turns"]]
