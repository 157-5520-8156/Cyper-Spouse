from companion_daemon.sanitize import sanitize_chat_text


def test_sanitize_removes_stage_directions() -> None:
    assert sanitize_chat_text("（手机震了一下）我在。") == "我在。"
    assert sanitize_chat_text("*笑了一下* 你好呀") == "你好呀"
    assert sanitize_chat_text("我叫沈知栀，英文名 Celia Shen。") == "我叫沈知栀，英文名 Celia Shen。"


def test_sanitize_softens_assistantese_and_limits_questions() -> None:
    assert sanitize_chat_text("我理解你的意思，这个问题确实很重要，我有个同学也遇到过。你呢？还好吗？") == "你呢？"
    assert sanitize_chat_text("那你还不早点睡？半夜一点还在外面晃，明天考试能撑住吗？") == "那你还不早点睡？半夜一点还在外面晃。"
    assert sanitize_chat_text("嗯？怎么了？大半夜说这种话。") == "怎么了？大半夜说这种话。"
    assert sanitize_chat_text("淋着了还是找到伞了。") == "淋着了还是找到伞了？"
    assert sanitize_chat_text("我有时候也会去校园里走一圈。你要不要也试试？") == "我有时候也会去校园里走一圈。"
    assert sanitize_chat_text("我先陪你待一会儿。要不要听首歌，或者就随便说说话也行？") == "我先陪你待一会儿。"
    assert sanitize_chat_text("你今晚早点休息，或者去洗个热水澡，可能会好一些。") == ""
    assert sanitize_chat_text("你别整段硬啃，拆成小块可能会好一点。") == ""


def test_sanitize_removes_acquaintance_crutch_variants() -> None:
    assert (
        sanitize_chat_text("成都理工啊，那你们学校后门是不是有条街全是串串和冰粉？我有个高中同学在那读土木，她跟我提过。")
        == "成都理工啊，那你们学校后门是不是有条街全是串串和冰粉？"
    )
    assert sanitize_chat_text("毛概确实要背好多啊，不过我室友说画了重点会好背一点。") == "毛概要背好多啊。"
    assert sanitize_chat_text("成都理工啊，我好像有个高中同学在那。") == "成都理工啊。"
    assert sanitize_chat_text("我一个高中同学在那儿读过，说晚上特别热闹。") == ""
    assert sanitize_chat_text("我之前看一个朋友发过那边的照片，红砖楼配旧火车，挺有感觉的。") == ""
    assert sanitize_chat_text("我明天也有一门考试，刚背完知识点，准备睡了。") == ""
    assert sanitize_chat_text("毛概真的好难背啊，我去年考的时候也是熬夜翻来覆去地背。") == "毛概真的好难背啊。"
    assert sanitize_chat_text("毛概难背……我上学期也被折磨过。") == "毛概难背……"
    assert sanitize_chat_text("毛概有点绕，我上学期背得也头疼。") == "毛概有点绕。"
    assert sanitize_chat_text("我之前期末背的时候，会在纸上画时间线。") == ""
    assert sanitize_chat_text("我在图书馆看到好多人抱着毛概书在走廊来回走，边念边叹气。") == ""
    assert (
        sanitize_chat_text("我上次找不到伞，最后翻出来一把快散架的，撑到一半差点被风吹翻。") == ""
    )
    assert sanitize_chat_text("上次听说成都理工附近有个夜市挺有名的。") == ""
    assert sanitize_chat_text("成都理工啊。听说秋天的时候学校还挺好看的。") == "成都理工啊。"
    assert sanitize_chat_text("那你这趟也不算白淋雨，至少没被点到名。") == ""
    assert sanitize_chat_text("淋着雨去上课了。") == ""
    assert sanitize_chat_text("是雨停了老师才到，那种事后的滑稽感。") == ""
    assert sanitize_chat_text("不过淋了雨还白等，有点亏。") == ""
    assert sanitize_chat_text("那你这趟也不算白跑，至少老师也跟你一起迟到了。") == ""
    assert sanitize_chat_text("噗，那你这趟淋雨淋得有点亏啊，反而有点荒唐了。") == ""


def test_sanitize_repairs_chengdu_location_confusion() -> None:
    assert sanitize_chat_text("啊，原来你也在成都呀！") == "啊，原来你在成都呀！"


def test_sanitize_removes_explicit_unsupported_memory_claims() -> None:
    assert sanitize_chat_text("怎么了？我记得你之前说自己忙得有点离谱。") == "怎么了？"
    assert sanitize_chat_text("之前听你说在成都来着。") == ""
    assert sanitize_chat_text("你呢？之前群里看到你在成都。") == "你呢？"
    assert sanitize_chat_text("我知道！\n之前刷到一个学长发的照片，说那边晚上烟火气特别足。") == ""
    assert sanitize_chat_text("我记得之前群里有人发过照片，晚上那边好多小摊。") == ""
    assert sanitize_chat_text("我之前看群里有人发过照片，烟火气很足的样子。") == ""
    assert sanitize_chat_text("我之前做城市散步笔记的时候，刚好查过那边。") == ""
    assert sanitize_chat_text("成理啊，我知道那附近有家面馆挺有名的。") == "成理啊。"
