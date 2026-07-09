import re

_STAGE_DIRECTION_RE = re.compile(r"[（(][^（）()]{1,80}[）)]")
_ASTERISK_ACTION_RE = re.compile(r"\*[^*]{1,80}\*")

_ASSISTANTESE_PATTERNS = [
    re.compile(r"^(我理解你的意思[，,。]?\s*)"),
    re.compile(r"^(听起来你(?:是|有点)?[^，。！？]{0,18}[，,。]\s*)"),
    re.compile(r"(这个问题(?:确实)?(?:很|挺)?(?:有意思|重要)[，,。]\s*)"),
    re.compile(
        r"我(?:好像)?(?:有|认识)(?:个|一个)?(?:[^。！？]{0,8})?"
        r"(?:朋友|同学|室友|高中同学|大学同学|舍友)[^。！？]{0,48}(?:。|！|？)?"
    ),
    re.compile(r"我(?:一个|有个|有一个)?(?:高中同学|大学同学|同学|朋友)[^。！？]{0,36}(?:在那儿|在那|在你们学校|读过|上过)[^。！？]{0,36}(?:。|！|？)?"),
    re.compile(r"(?:不过|但是|而且|然后)?我?(?:朋友|同学|室友|舍友)(?:也|之前|跟我|和我|说)[^。！？]{0,48}(?:。|！|？)?"),
    re.compile(r"我(?:明天|今天|等会儿|一会儿|待会儿)也(?:有|要|得)[^。！？]{0,24}(?:考试|一门|pre|汇报|展示)[^。！？]{0,24}(?:。|！|？)?"),
    re.compile(r"我(?:上次|去年|之前|以前|上学期)[^。！？]{0,32}(?:考|考试|期末|复习|背到|背得|背的时候|被.{0,8}折磨)[^。！？]{0,32}(?:。|！|？)?"),
    re.compile(r"我(?:上次|去年|之前|以前|上学期)[^。！？]{0,32}(?:找不到伞|忘带伞|伞.{0,8}坏|被风吹翻|淋雨|下雨)[^。！？]{0,32}(?:。|！|？)?"),
    re.compile(r"我在(?:图书馆|食堂|教室|宿舍|学校)[^。！？]{0,32}看到[^。！？]{0,48}(?:。|！|？)?"),
    re.compile(r"(确实)"),
    re.compile(r"(?:上次)?听说[^。！？]{0,48}(?:附近|夜市|有名|小摊)[^。！？]{0,32}(?:。|！|？)?"),
    re.compile(r"(?:上次)?听说[^。！？]{0,24}(?:你们)?学校[^。！？]{0,24}(?:好看|漂亮|适合散步|有名)[^。！？]{0,16}(?:。|！|？)?"),
    re.compile(r"(?:至少)?没被(?:老师)?(?:点到名|点名|抓到迟到)[^。！？]{0,16}(?:。|！|？)?"),
    re.compile(r"[^。！？]{0,16}(?:雨算|不算|也不算)?白淋(?:雨)?[^。！？]{0,16}(?:。|！|？)?"),
    re.compile(r"淋着雨去上课了(?:。|！|？)?"),
    re.compile(r"(?:是)?雨停了[^。！？]{0,24}(?:老师才到|才到)[^。！？]{0,32}(?:。|！|？)?"),
    re.compile(r"[^。！？]{0,16}白等[^。！？]{0,16}(?:。|！|？)?"),
    re.compile(r"^(不得不说(?:的是)?[，,]?\s*)"),
    re.compile(r"^(有趣的是[，,]?\s*)"),
    re.compile(r"^(值得一提(?:的是)?[，,]?\s*)"),
    re.compile(r"^(作为(?:一个)?(?:过来人|朋友)[，,]?\s*)"),
    re.compile(r"^(总的来说[，,]?\s*说?[，,]?\s*)"),
    re.compile(r"^(不过话说回来[，,]?\s*)"),
    re.compile(r"^(好问题[！!]?[，,]?\s*)"),
    re.compile(r"^(这是个好问题[。！]?[，,]?\s*)"),
    re.compile(r"^(哈哈[，,]?\s*这个问题[，,]?\s*)"),
    re.compile(r"^(你这句话?说(?:得)?挺[^，。！？]{1,12}[，,。]?\s*)"),
    re.compile(r"^(这个(?:问题|话)(?:挺|太|有点)(?:突然|直接|冒昧)[，,。]?\s*)"),
    re.compile(r"(听着还挺[^，。！？]{1,8}[，,。]?\s*)"),
    re.compile(r"(这话?说得挺[^，。！？]{1,10}[，,。]?\s*)"),
    re.compile(r"(?:我记得你之前|我记得之前|我之前看群里|你之前|之前听你)[^。！？]{0,50}(?:。|！|？)?"),
    re.compile(r"之前群里[^。！？]{0,24}(?:看到|看见|有人说|有人提)[^。！？]{0,36}(?:。|！|？)?"),
    re.compile(r"(?:之前)?刷到[^。！？]{0,24}(?:学长|学姐|同学|朋友)[^。！？]{0,24}(?:照片|发)[^。！？]{0,36}(?:。|！|？)?"),
    re.compile(r"我之前[^。！？]{0,24}(?:查过那边|做[^。！？]{0,12}笔记)[^。！？]{0,30}(?:。|！|？)?"),
    re.compile(r"(?:我知道|知道)[^。！？]{0,18}(?:附近|后门|校门口)[^。！？]{0,24}(?:有家|有个|夜市|面馆|店)[^。！？]{0,24}(?:。|！|？)?"),
    re.compile(r"^我知道[！!。]?$"),
    re.compile(r"你要不要也试试[？?。]?"),
    re.compile(r"要不要听首歌[^。！？]*[？?。]?"),
    re.compile(r"[^。！？]{0,24}(?:洗个热水澡|早点休息)[^。！？]{0,24}(?:可能会好一(?:点|些)|会好一(?:点|些))[^。！？]{0,16}[。！？]?"),
    re.compile(r"你别[^。！？]{0,24}(?:可能会好一(?:点|些)|会好一(?:点|些))[。！？]?"),
]


def sanitize_chat_text(text: str) -> str:
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _looks_like_pure_stage_direction(stripped):
            continue
        stripped = _STAGE_DIRECTION_RE.sub("", stripped)
        stripped = _ASTERISK_ACTION_RE.sub("", stripped)
        stripped = re.sub(r"\s{2,}", " ", stripped).strip()
        if stripped:
            cleaned_lines.append(_soften_assistantese(stripped))
    return _limit_questions(_repair_flattened_questions("\n".join(cleaned_lines).strip()))


def _looks_like_pure_stage_direction(text: str) -> bool:
    return (
        (text.startswith("（") and text.endswith("）"))
        or (text.startswith("(") and text.endswith(")"))
        or (text.startswith("*") and text.endswith("*"))
    )


def _soften_assistantese(text: str) -> str:
    for pattern in _ASSISTANTESE_PATTERNS:
        text = pattern.sub("", text)
    text = text.replace("你也在成都", "你在成都")
    text = re.sub(r"[，,]\s*(不过|但是|然后|而且)\s*$", "。", text)
    text = re.sub(r"(不过|但是|然后|而且)\s*$", "", text)
    text = re.sub(r"[，,]\s*$", "。", text)
    return text.strip()


def _limit_questions(text: str) -> str:
    result = _drop_leading_question_murmur(text)
    while True:
        question_marks = list(re.finditer(r"[？?]", result))
        if len(question_marks) <= 1:
            return result
        extra = question_marks[1]
        prefix = result[: extra.start()]
        comma = max(prefix.rfind("，"), prefix.rfind(","))
        sentence = max(prefix.rfind("。"), prefix.rfind("！"), prefix.rfind("？"), prefix.rfind("!"), prefix.rfind("?"))
        if comma > sentence:
            result = result[:comma] + "。" + result[extra.end() :]
        elif sentence >= 0:
            end = _sentence_end_after(result, extra.start())
            result = (result[: sentence + 1].rstrip() + result[end:].lstrip()).strip()
        else:
            result = result[: extra.start()] + "。" + result[extra.end() :]


def _drop_leading_question_murmur(text: str) -> str:
    if len(re.findall(r"[？?]", text)) <= 1:
        return text
    return re.sub(r"^(?:嗯|嗯嗯|啊|诶|欸|咦|哎)[？?]\s*", "", text, count=1)


def _repair_flattened_questions(text: str) -> str:
    result = re.sub(r"([^。！？]{1,40}[吗么])。", r"\1？", text)
    result = re.sub(r"(你呢)。", r"\1？", result)
    result = re.sub(r"((?:哪|怎么|为什么|什么时候|多少|谁)[^。！？]{0,40})。", r"\1？", result)
    result = re.sub(r"((?:是不是|要不要|能不能|可不可以|还是)[^。！？]{1,40})。", r"\1？", result)
    result = re.sub(r"(不怕[^。！？]{1,24}啊)。", r"\1？", result)
    return result


def _sentence_end_after(text: str, start: int) -> int:
    for index in range(start, len(text)):
        if text[index] in "。！？!?":
            return index + 1
    return len(text)
