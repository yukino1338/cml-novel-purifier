from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping


@dataclass(frozen=True)
class SiteSpec:
    key: str
    label: str
    aliases: tuple[str, ...]
    domain_fragments: tuple[str, ...] = ()
    short_external: bool = True


SITE_SPECS = (
    SiteSpec("9xiaoxs", "九霄小说网", ("九霄小说网", "九霄小说", "9xiaoxs"), ("9xiaoxs",)),
    SiteSpec("biquge", "笔趣阁", ("新笔趣阁", "笔趣阁", "xbiquge", "biquge", "bqg"), ("xbiquge", "biquge", "bqg")),
    SiteSpec("dingdian", "顶点小说", ("顶点小说",)),
    SiteSpec("bixia", "笔下文学", ("笔下文学",)),
    SiteSpec("yunxuange", "云轩阁", ("云轩阁",)),
    SiteSpec("kuairead", "快读", ("快读",), short_external=False),
    SiteSpec("balu", "八路中文网", ("八路中文网",)),
    SiteSpec("shubao", "书包网", ("书包网",)),
    SiteSpec("boluoxs", "菠萝小说", ("菠萝小说", "boluoxs"), ("boluoxs",)),
    SiteSpec("aiqu", "爱去小说网", ("爱去小说网",)),
    SiteSpec("soushu", "搜书", ("搜书", "soushu"), ("soushu",), False),
    SiteSpec("cihetxt", "词河看书", ("词河看书", "cihetxt"), ("cihetxt",)),
    SiteSpec("69shu", "69书吧", ("69书吧", "六九书吧"), ("69shu",)),
    SiteSpec("shuq", "书趣阁", ("书趣阁",)),
    SiteSpec("00ks", "零点看书", ("零点看书", "00ks"), ("00ks",)),
    SiteSpec("piaotian", "飘天文学", ("飘天文学", "piaotian"), ("piaotian",)),
    SiteSpec("uukanshu", "UU看书", ("uu看书", "uukanshu"), ("uukanshu",)),
)

SITE_ENTITY_ALIASES = tuple((spec.key, spec.aliases) for spec in SITE_SPECS)
SITE_ENTITY_LABELS = {spec.key: spec.label for spec in SITE_SPECS}


@dataclass(frozen=True)
class SignalSpec:
    key: str
    label: str
    roles: frozenset[str]


SIGNAL_SPECS = (
    SignalSpec("url", "网址", frozenset({"strong", "short", "neighbor", "locator"})),
    SignalSpec("email", "邮箱", frozenset({"strong", "short", "neighbor", "locator"})),
    SignalSpec("contact", "联系方式", frozenset({"strong", "neighbor", "locator"})),
    SignalSpec("download", "下载词", frozenset({"strong", "neighbor", "intent"})),
    SignalSpec("watermark", "水印词", frozenset({"strong", "short", "source"})),
    SignalSpec("reader_site", "阅读站词", frozenset({"strong", "neighbor", "locator"})),
    SignalSpec("author_note", "作者说明", frozenset({"weak"})),
    SignalSpec("copy_marker", "转载标记", frozenset({"weak", "source"})),
    SignalSpec("domain", "域名", frozenset({"neighbor", "locator"})),
)

SIGNAL_LABELS = {spec.key: spec.label for spec in SIGNAL_SPECS}


def signal_keys(role: str) -> frozenset[str]:
    return frozenset(spec.key for spec in SIGNAL_SPECS if role in spec.roles)


# Source markers are deliberately narrower than the diagnostic verb list.  A bare
# verb such as ``整理`` or ``扫描`` is ordinary narrative language; it becomes a
# source marker only inside one of the explicit, punctuation-bounded grammars
# below.  Keep these patterns here so scanners and reviewers share one executable
# definition of source evidence.
_SOURCE_OBJECT = r"(?:本书|本文|全文|文章|内容|电子书|电子文本|TXT(?:电子书|文本)?|OCR(?:文本)?)"
_SOURCE_COMPOUND = r"(?:扫描版|手打版|整理版|校对版|制作版|校对组|整理组|扫描组|录入组|手打组|制作组)"
_SOURCE_ACTION = r"(?:手打|校对|整理|录入|扫描|制作)(?:制作|校对|整理|录入)?(?:版|组)?"
_SOURCE_ORIGIN = (
    r"(?:网站|论坛|书库|公众号|博客|贴吧|社区|工作室|"
    r"(?:校对|整理|扫描|录入|手打|制作)组|(?:小说|中文|文学|阅读)网)"
)
_SOURCE_PUNCTUATION = r"，。！？；：、,.!?;:\r\n“”‘’「」『』（）()【】\[\]{}《》<>—…"
_SOURCE_CLAUSE_CHAR = rf"[^{_SOURCE_PUNCTUATION}]"
_SOURCE_END = rf"(?=$|[{_SOURCE_PUNCTUATION}])"
_CJK_BOUNDARY = r"\u3400-\u4dbf\u4e00-\u9fff"

SOURCE_MARKER_RE = re.compile(
    rf"(?:"
    # Explicit production statement: 本书由某组校对 / 本书由某公众号整理制作.
    rf"{_SOURCE_OBJECT}(?:系?由|经){_SOURCE_CLAUSE_CHAR}{{0,24}}?{_SOURCE_ACTION}{_SOURCE_END}"
    # A source object immediately bound to a stable compound: 全文手打版 / OCR扫描组.
    rf"|{_SOURCE_OBJECT}{_SOURCE_CLAUSE_CHAR}{{0,6}}?{_SOURCE_COMPOUND}"
    # An attribution may follow an explicit source object without a left boundary.
    rf"|{_SOURCE_OBJECT}(?:转自|转载自|来源于){_SOURCE_CLAUSE_CHAR}{{1,24}}?{_SOURCE_ORIGIN}{_SOURCE_END}"
    rf"|{_SOURCE_OBJECT}(?:来源|出处)[:：]{_SOURCE_CLAUSE_CHAR}{{1,24}}?{_SOURCE_ORIGIN}{_SOURCE_END}"
    # Explicit attribution must start at a CJK boundary and end at a concrete origin.
    rf"|(?<![{_CJK_BOUNDARY}])(?:转自|转载自|来源于){_SOURCE_CLAUSE_CHAR}{{1,24}}?{_SOURCE_ORIGIN}{_SOURCE_END}"
    rf"|(?<![{_CJK_BOUNDARY}])(?:来源|出处)[:：]{_SOURCE_CLAUSE_CHAR}{{1,24}}?{_SOURCE_ORIGIN}{_SOURCE_END}"
    # Standalone compounds are accepted only as complete lexical units.
    rf"|(?<![{_CJK_BOUNDARY}]){_SOURCE_COMPOUND}(?![{_CJK_BOUNDARY}])"
    rf")",
    re.I,
)

# Diagnostic only: this pattern may count suppressed ambiguous blocks, but must
# never be used as a strong/weak candidate signal or as permission to delete.
BARE_COPY_MARKER_RE = re.compile(r"(?:转自|手打|校对|整理|录入|扫描|制作)", re.I)


@dataclass(frozen=True)
class IntentSpec:
    key: str
    label: str
    pattern: re.Pattern[str]


INTENT_SPECS = (
    IntentSpec("visit", "访问引导", re.compile(r"(?:请|欢迎|建议).{0,6}访问|站外(?:更新|阅读|获取)")),
    IntentSpec("read", "阅读引导", re.compile(r"更新最快|无弹窗|在线阅读|阅读全文|最新章节|阅读网站")),
    IntentSpec("download", "下载引导", re.compile(r"TXT下载|下载地址|站外获取文件|获取文件")),
    IntentSpec("contact", "联系引导", re.compile(r"请联系|请加入|获取更新通知|加入(?:QQ|微信|VX|wx)")),
    IntentSpec("source", "来源说明", re.compile(r"仅供学习交流|来源水印|整理组|校对组|转载")),
    IntentSpec("support", "支持引导", re.compile(r"欢迎.{0,4}[捧棒]场|求收藏|求推荐|求打赏")),
)

INTENT_LABELS = {spec.key: spec.label for spec in INTENT_SPECS}
VISIT_CUE_RE = re.compile(r"(?:请|欢迎|建议).{0,6}访问")
VISIT_TARGET_BRIDGE_RE = re.compile(
    r"(?:以下)?(?:地址|网址|网站|站点|本站)?[:：为是到至\-—]*"
)
VISIT_SITE_PROMOTION_TAIL_RE = re.compile(
    r"^[,，:：;；\-—]*(?:获取(?:后续|更新|内容)|查看(?:最新)?章节|"
    r"阅读(?:全文|最新章节)|更新(?:内容|章节|最快)|下载(?:全文|txt)?|无弹窗)"
)
VISIT_PROMOTION_FRAME_RE = re.compile(
    r"(?:作者(?:荐|推荐)|喜欢小说的|站外(?:更新|阅读|获取|提示))"
)
GENERIC_DOMAIN_RE = re.compile(
    r"(?<![a-z0-9_-])(?:www\.)?([a-z0-9][a-z0-9_-]{1,62}\.(?:com|net|cn|org|cc|top|xyz|vip))(?![a-z0-9_-])",
    re.I,
)
EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@([a-z0-9.\-]+\.[a-z]{2,})", re.I)
NARRATIVE_EXTERNAL_REFERENCE_RE = re.compile(
    r"(?:剧情|场景|线索|证物|调查记录|旧邮箱|旧日志|并非邀请|没有发出联系请求|封存|本地档案)"
    r"|(?:没有|并未|未曾|从未).{0,16}(?:访问|联系|下载|加入|前往)"
)
QUOTED_EXTERNAL_REFERENCE_RE = re.compile(
    r"[“「『\"'][^”」』\"'\n]{0,320}"
    r"(?:https?://|www\.|[A-Za-z0-9._%+\-]+@|请访问|联系|下载)"
    r"[^”」』\"'\n]{0,320}[”」』\"']",
    re.I,
)
NARRATIVE_ACTION_FRAME_RE = re.compile(
    r"(?:告示|纸条|便签|信(?:里|中)?|日记|记录).{0,16}"
    r"(?:写着|写道|记着|记载|提到|显示)"
    r"|(?:读完|看完|念完).{0,32}"
    r"(?:折好|收进|放进|塞进|合上|递给|藏起|撕掉)"
)


def fold_external_text(value: str) -> str:
    folded = unicodedata.normalize("NFKC", value)
    return re.sub(r"([A-Za-z0-9])\s*(?:点|點|．|。)\s*([A-Za-z0-9])", r"\1.\2", folded)


def normalize_match_text(value: str) -> str:
    return re.sub(r"\s+", "", fold_external_text(value)).lower()


def domain_tokens(value: str) -> set[str]:
    folded = fold_external_text(value).lower()
    return domain_tokens_from_normalized(folded)


def domain_tokens_from_normalized(normalized: str) -> set[str]:
    domains = {match.group(1).lower() for match in GENERIC_DOMAIN_RE.finditer(normalized)}
    domains.update(match.group(1).lower() for match in EMAIL_RE.finditer(normalized))
    return domains


def site_entities(value: str) -> set[str]:
    normalized = normalize_match_text(value)
    return site_entities_from_normalized(normalized)


def site_entities_from_normalized(normalized: str) -> set[str]:
    entities = {
        spec.key
        for spec in SITE_SPECS
        if any(normalize_match_text(alias) in normalized for alias in spec.aliases)
    }
    for domain in domain_tokens_from_normalized(normalized):
        stem = domain.split(".", 1)[0]
        mapped = next(
            (
                spec.key
                for spec in SITE_SPECS
                if any(fragment in stem for fragment in spec.domain_fragments)
            ),
            None,
        )
        entities.add(mapped or f"domain:{domain}")
    return entities


@lru_cache(maxsize=2)
def site_alias_re(short_only: bool = False) -> re.Pattern[str]:
    aliases = {
        normalize_match_text(alias)
        for spec in SITE_SPECS
        if not short_only or spec.short_external
        for alias in spec.aliases
    }
    return re.compile("(?:" + "|".join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True)) + ")", re.I)


def promotion_intents(value: str) -> set[str]:
    normalized = normalize_match_text(value)
    return {spec.key for spec in INTENT_SPECS if spec.pattern.search(normalized)}


def has_bound_visit_locator(value: str) -> bool:
    """Return whether a visit cue has high-confidence external targeting."""
    normalized = normalize_match_text(value)
    for cue in VISIT_CUE_RE.finditer(normalized):
        promotion_frame = bool(
            VISIT_PROMOTION_FRAME_RE.search(normalized[max(0, cue.start() - 32) : cue.start()])
        )
        tail = normalized[cue.end() :]
        bridge = VISIT_TARGET_BRIDGE_RE.match(tail)
        if bridge is not None:
            tail = tail[bridge.end() :]
        if tail.startswith(("http://", "https://", "www.")):
            return True
        if GENERIC_DOMAIN_RE.match(tail) or EMAIL_RE.match(tail):
            return True
        site_match = site_alias_re().match(tail)
        if site_match is not None:
            locator = site_match.group(0)
            if any(character.isascii() and character.isalnum() for character in locator):
                return True
            if promotion_frame:
                return True
            remainder = tail[site_match.end() :]
            external_remainder = remainder.lstrip(",，:：;；-—")
            if external_remainder.startswith(("http://", "https://", "www.")):
                return True
            if GENERIC_DOMAIN_RE.match(external_remainder) or EMAIL_RE.match(external_remainder):
                return True
            if VISIT_SITE_PROMOTION_TAIL_RE.match(remainder):
                return True
    return False


def family_template(value: str) -> str:
    normalized = fold_external_text(value).lower()
    normalized = EMAIL_RE.sub("{email}", normalized)
    normalized = GENERIC_DOMAIN_RE.sub("{domain}", normalized)
    normalized = re.sub(r"\s+", "", normalized)
    for spec in SITE_SPECS:
        for alias in spec.aliases:
            normalized = normalized.replace(normalize_match_text(alias), f"{{site:{spec.key}}}")
    normalized = re.sub(r"作者(?:推荐|有话要说)|有话说|有事说|大声说|告诉你|说", "{author}", normalized)
    normalized = re.sub(r"\d+", "#", normalized)
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff{}:#]+", "", normalized)
    return normalized[:240]


def is_narrative_external_reference(value: str) -> bool:
    return bool(
        NARRATIVE_EXTERNAL_REFERENCE_RE.search(value)
        or QUOTED_EXTERNAL_REFERENCE_RE.search(value)
        or NARRATIVE_ACTION_FRAME_RE.search(value)
    )


def format_family_label(signature: Mapping[str, Any]) -> str:
    sites = signature.get("site_entities") or signature.get("site_entity") or []
    intents = signature.get("intents") or signature.get("intent") or []
    if not isinstance(sites, list):
        sites = [sites]
    if not isinstance(intents, list):
        intents = [intents]
    labels = [SITE_ENTITY_LABELS.get(str(value), str(value)) for value in sites if value]
    labels.extend(INTENT_LABELS.get(str(value), str(value)) for value in intents if value)
    return " · ".join(dict.fromkeys(labels))
