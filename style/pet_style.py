"""Pet style catalog. Only chosen instructions enter a dialogue job."""
from pathlib import Path
from functools import lru_cache
import re

def require(condition, *, message):
    if not condition:
        raise ValueError(message)


ROOT = Path(__file__).with_name("pet_skills")
PERSONALITIES = {"温和细腻":"warm", "直爽坦率":"direct", "俏皮嘴硬":"playful", "沉静寡言":"quiet"}
EXPRESSIONS = {"话少":"brief", "健谈":"talkative", "偶尔打趣":"joke", "少用反问":"few-questions", "偶尔用修仙梗":"cultivation"}
# These labels could already belong to a user's private card before their release.
CHAT_EXPRESSIONS = {"熟人式接话":"familiar-reply", "顺势带话题":"follow-the-thread"}
EXPRESSIONS.update(CHAT_EXPRESSIONS)
SCENES = {"直接给办法":"next-step", "听我吐槽":"listen", "分享进展":"celebrate"}
TONE_PERSONALITY = {"gentle":"温和细腻", "direct":"直爽坦率", "lively":"俏皮嘴硬"}
BASE = {"personality":"温和细腻", "expressionTags":[], "privateSkills":[], "scene":""}
STYLE_KEYS = ("tone","length","proactivity","topics","instruction",*BASE)


@lru_cache(maxsize=None)
def card(skill_id):
    # IDs are resolved only from the fixed tables above.
    raw = (ROOT / skill_id / "SKILL.md").read_text("utf-8")
    _, header, body = raw.split("---", 2)
    meta = dict(line.split(": ", 1) for line in header.strip().splitlines())
    assert meta["name"] == skill_id
    return meta["description"], body.strip()


def catalog():
    return {key:[{"label":label,"id":skill,"description":card(skill)[0]} for label,skill in group.items()]
            for key,group in (("personalities",PERSONALITIES),("expressions",EXPRESSIONS),("scenes",SCENES))}


def legacy_defaults(settings):
    return {**BASE, "personality":TONE_PERSONALITY.get(settings.get("tone"),"温和细腻"), **settings}


def valid_label(label):
    return isinstance(label,str) and 0 < len(label) <= 32 and label == label.strip() and all(ch.isprintable() for ch in label)


def validate_style(cfg):
    main,tags,library = (cfg[key] for key in ("personality","expressionTags","privateSkills"))
    require(isinstance(cfg["scene"],str) and (not cfg["scene"] or cfg["scene"] in SCENES), message="请选择有效的对话场景")
    require(valid_label(main), message="主性格标签需为 1 至 32 个可显示字符")
    require(isinstance(tags,list) and len(tags)<=8 and all(valid_label(x) for x in tags)
            and len(tags)==len(set(tags)), message="请选择最多 8 个不同的表达标签")
    require(not {"话少","健谈"}.issubset(tags), message="话少与健谈只能选择一个")
    require(isinstance(library,list) and len(library)<=12, message="最多可保存 12 份私有风格")
    kinds = {**dict.fromkeys(PERSONALITIES,"personality"), **dict.fromkeys(EXPRESSIONS,"expression")}
    private_labels = set()
    for item in library:
        require(isinstance(item,dict) and set(item)=={"label","kind","markdown"}, message="私有风格需要名称、类型和 Markdown 正文")
        label,kind,raw = item["label"], item["kind"], item["markdown"]
        require(valid_label(label) and label not in private_labels and (label not in kinds or label in CHAT_EXPRESSIONS) and label not in SCENES, message="风格名称重复或无效")
        private_labels.add(label)
        require(kind in ("personality","expression") and isinstance(raw,str) and 0<len(raw)<=1600
                and all(ch.isprintable() or ch in '\t\r\n' for ch in raw) and bool(instructions(raw)), message="风格需有有效文本正文，最多 1600 字")
        kinds[label]=kind
    require(kinds.get(main,"personality")=="personality" and main not in SCENES, message="请选择主性格或填写自定义性格")
    require(all(kinds.get(tag,"expression")=="expression" and tag not in SCENES and tag!=main for tag in tags), message="表达标签与其他类型冲突")
    require(sum(len(x["markdown"]) for x in library if x["label"] in [main,*tags])<=2400,
            message="本次启用的私有风格合计最多 2400 字")


def instructions(raw):
    text=raw.strip().replace("\r\n","\n")
    if text.startswith("---\n"):
        _, sep, text = text[4:].partition("\n---\n")
        if not sep:
            return ""
    return text.strip()


def snapshot(cfg):
    cfg=legacy_defaults(cfg)
    chosen={cfg["personality"],*cfg["expressionTags"]}
    return {**{key:cfg[key] for key in STYLE_KEYS},
            "privateSkills":[v for v in cfg["privateSkills"] if v["label"] in chosen]}


def active_cards(style, message):
    cfg=legacy_defaults(style)
    private={item["label"]:item for item in cfg["privateSkills"]}
    selected=[("personality",cfg["personality"],PERSONALITIES)]
    selected.extend(("expression",tag,EXPRESSIONS) for tag in cfg["expressionTags"])
    # Direct requests and corrections take precedence over loose topic words.
    scenes=((r"别给.*(?:建议|计划)|别.*安排|只想.*(?:说|吐槽)|听我说|(?:不用|别|不要|不必)帮我(?:看|算|解|分析|讲)","听我吐槽"),
            (r"直接(?:说|告诉|给)|怎么办|该怎么|给.*(?:办法|建议)|下一步怎么|帮我(?:看|算|解|分析|讲)|怎么(?:解|算|做)","直接给办法"),
            (r"吐槽|烦死|好烦|气人|有点累|白忙|(?:学|写|看|做)不动了","听我吐槽"),(r"终于.*(?:过|做完|完成|拿下)|考过了|太开心|搞定了","分享进展"))
    scene = cfg["scene"] or next((label for pattern,label in scenes if re.search(pattern,message)), "")
    if scene:
        selected.append(("scene",scene,SCENES))
    result=[]
    for kind,label,table in selected:
        if label in table and not (label in CHAT_EXPRESSIONS and label in private):
            origin,body="builtin",card(table[label])[1]
        else:
            origin="user"
            body=instructions(private[label]["markdown"]) if label in private else label
        result.append({"kind":kind,"label":label,"origin":origin,"text":body})
    return result
