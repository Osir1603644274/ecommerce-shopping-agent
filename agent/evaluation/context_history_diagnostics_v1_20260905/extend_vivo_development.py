"""Deterministically assemble a disclosed primary-Codex-authored dev extension.

Not a held-out family; no SUT outputs or expected TaskState are injected.
"""
import json

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, file_sha, write_new


EXTENSION = [
    ("note", "继续这次备用机选购，再记一份现场异常处理规则：如果听筒、扫码对焦或连续触控出现异常，先暂停操作，记录发生的具体步骤，不凭一次恢复正常就认定问题消失。涉及账号退出、重启后的锁定提示、进水痕迹或设备身份不一致时，只列出需要卖家解释和补证的问题，不替卖家作保证。妹妹不会因为姐姐已经到场就必须成交；对方催促时也可以结束验机。现场录制只能用于记录这次交接，不承诺法律效力，也不记录别人的密码或验证码。这些是见面时的处置预案，不增加检索库没有的硬字段，更不授权你联系卖家、下单或付款。"),
    ("modify", "我重新考虑过，屏幕原装恢复为硬条件，不再只是第13轮的软偏好。预算1500元、外壳正常和电池健康两个允许档保持不变。"),
    ("modify", "额外留60元买保护套，手机预算明确改成1500-60=1440元，替换1500元旧上限；这不是建议价或假设。"),
    ("discussion", "按现在真正生效的硬条件重搜，展示最近这批实际候选；备注和异常处置预案不作为商品过滤条件。"),
    ("compare", "比较刚刚第28轮实际展示的第一款和第二款，只有一款就分析一款。不要用旧列表凑数，也不要把比较项目加入偏好。"),
    ("note", "细化验机检查次序：第一步核对设备身份、页面描述以及账号能否由卖家本人正常退出，但我们不接触对方验证码；第二步插自己的SIM卡测试拨出、接听和听筒音量；第三步以纸质课程表试扫码与近距离文字对焦；第四步检查纯色屏、连续触控和接口充电；第五步核实电池档位、屏幕原装依据、外壳与维修记录；最后才测试通知音、字体大小和备用机放入帆布包的取用是否方便。时间不足时优先前四步以及已生效硬条件，尚未检查的项目明确写未检查。不要把摄像头测试顺利等同于已经掌握硬件性能，也不把测试清单变成新的筛选门槛。"),
    ("modify", "第17轮的周六10:10见面要推迟到同一天14:30，固定门店、妹妹和姐姐同行、50分钟都不变。需要妹妹提前吃午饭，但这只是提醒，不代表卖家确认了新时间。"),
    ("note", "还有天气和交通备选方案：如果见面当天持续大雨、公共交通无法到达或门店临时关闭，就先暂停当天安排，再商议新的日期，不能自动改为快递成交。姐姐开车只是可能的备选，尚未确认，原来的同行人也没有因此换掉。假如最终采用邮寄，仍要先核实此前约定的包装、封箱与开箱记录；收到机器后再按同一检查顺序核实，不能在未验机时承诺满意。请把这些条件式预案与已经确定的见面时间分别记录，后面回忆时不要把可能下雨、可能接送或可能邮寄说成已经发生的事实。"),
    ("recall", "不用搜索。请说明现在有效的预算和机况硬条件，并区分当前见面安排、尚未确认的交通备选与触发后才重新商议的情况。也说清第8轮和第17轮安排哪些部分已失效、哪些被保留。"),
    ("withdraw", "撤销电池健康档位硬条件，健康档位暂时只需如实列出，包括未知，不因这个字段排除商品。屏幕原装和外壳正常仍是硬条件；电池是否原装也只记录，不擅自升级。"),
    ("note", "补充双机通知试用计划：妹妹先让主力机保持原有通知方式，备用机只登录确有需要的电话、校园通行与公交应用；同一账号能否同时在线要实际核实，不推测应用政策。测试时由妹妹本人读取验证码，姐姐仅协助操作界面，不截屏保存敏感信息。第一天在家中和学校分别试一次家人来电，第二天再检查备用机静置后能否及时收到需要的提醒；如果双机重复通知打扰学习，先记下具体应用，再由妹妹决定关闭哪一端，不擅自删除消息或注销账号。这个两天试用计划是迁移后的检查，不改变主力机保持原样的约定，也不是让你实际登录账号。"),
    ("modify", "把Android软优先替换为iOS软优先，原因是姐姐更熟悉相关设置。系统仍开放，iOS不是硬条件，也不等同于只买苹果品牌。"),
    ("recall", "我想回查第12轮当时关于数据线、原盒、运输录像的原始约定，再对照第18轮指出哪些条目变了。只回顾，不把旧数据线要求恢复成当前要求。"),
    ("modify", "现在明确恢复vivo品牌软偏好，但只是同价同况时优先看看，所有品牌仍可以入选；第20轮的品牌一律同等看待已被本次替换。其他条件不变。"),
    ("modify", "家里再补160元，手机预算由1440元提高为1440+160=1600元。请明确替换当前预算，原来1500和1440都不再生效；不要额外加上已单独预留的配件费用。"),
    ("discussion", "按最新1600元手机硬上限和当前机况条件重搜，系统与品牌仅作软偏好，展示真实的最新结果。"),
    ("recall", "回顾第28轮当时实际展示的第二款；如果当时没展示第二款就说明。只找当时记录，不把它当作当前第二款或当前可以直接操作的对象，也不要编造商品身份。"),
    ("modify", "修改运输备选的记录方式：封箱前仍需连续视频，但到货时由姐姐协助妹妹录像，从未拆封状态一直记录到核对设备身份；妹妹负责实际开箱。包装缓冲、物流可查及其他约定保持。这个分工只有真的采用邮寄才适用，不表示已经改成邮寄。"),
    ("withdraw", "第38轮临时恢复的vivo软偏好现在再次撤销，所有品牌同等看待；不是禁止vivo。iOS软优先、当前预算和机况要求都不变。"),
    ("modify", "见面时间再次调整：改为下周二16:00，仍在原固定门店，妹妹和姐姐同行，预留50分钟。原周六14:30不再有效；雨天暂停并重新商议的预案仍保留，尚未获得卖家确认。"),
    ("recall", "分别列出当前见面、迁移和迁移后两天通知试用的安排，再回忆旧时间地点与参与人是怎么变化的。把条件式邮寄分工、交通备选和已经明确的安排分开，不要把预案说成承诺或已完成。"),
    ("modify", "电池健康恢复为硬条件，仍只接受80-90%或90%以上两档；未知不能算满足，70-80%不接受。第34轮不按档位排除的临时放宽失效，其他现行要求不变。"),
    ("discussion", "请按最终生效条件重新检索，日程、试用安排和异常处置预案不要成为过滤条件。仅展示当前真实候选，并如实披露未知及模拟参考价。"),
    ("recall", "现在汇总当前硬条件、软偏好、购买对象与用途、见面和迁移安排、配件运输、两天通知试用、异常处置与检查次序；旧值和条件式预案另列，注意被撤销后又恢复的要求。最后仅比较第47轮最近实际展示的第一款与第二款，不足两款就说明，不混入第28轮历史列表，也不执行交易。"),
]


def run():
    parent = HERE / "core_dataset24_vivo001/script.json"
    expected = "4d4e951826e2ceca6786d2c7460ec55a03e97d54a6b49a8213267d1d452ba63e"
    if file_sha(parent) != expected:
        raise ValueError("development_parent_hash_changed")
    value = json.loads(parent.read_text(encoding="utf-8"))
    assert value["split"] == "development" and len(value["turns"]) == 24
    value.update({"scenarioId": "core-development-vivo-48-extension-v1",
        "parentScriptSha256": expected, "sameFamilyNotIndependent": True,
        "extensionAuthor": "PRIMARY_CODEX_AGENT_SYNTHETIC_USER_TURNS_NOT_REAL_CONVERSATION"})
    value["turns"].extend({"turn": turn, "intent": intent, "userText": text}
        for turn, (intent, text) in enumerate(EXTENSION, 25))
    output = HERE / "core_dataset48_vivo002/script.json"
    write_new(output, value)
    write_new(output.parent / "assembly_audit.json", {"status": "DEVELOPMENT_SCRIPT_ASSEMBLED_NOT_SUT_EXECUTED",
        "scriptSha256": file_sha(output), "parentSha256": expected, "authorSourceSha256": file_sha(__file__),
        "turns": len(value["turns"]), "sourceFamily": value["familyId"], "modelCalls": 0,
        "originalFirst24Unchanged": value["turns"][:24] == json.loads(parent.read_text(encoding="utf-8"))["turns"],
        "containsAssistantAnswersOrExpectedState": False, "humanConversation": False,
        "formalHoldoutAccessed": False, "qualityOrPerformanceAcceptance": False})
    print(json.dumps({"turns": len(value["turns"]), "scriptSha256": file_sha(output)}))


if __name__ == "__main__":
    run()
