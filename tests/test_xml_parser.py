"""M0-04 验收：XmlParser 解析模型的人格输出。

契约（设计文档 §19-1）：
- 只解析 assistant 输出；用户消息不经过这里。
- 解析失败 → 整段按纯文本 <msg> 兜底，并让调用方知道（is_fallback）。

金样例取自**真实运行输出**（main.py 与塔利对话的实测结果），
不是凭空构造的——这是这个文件值得存在的原因。
"""

import os
import sys

# 让测试能找到 src 下的代码（与 test_config.py 保持一致）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from core.xml_parser import ParsedOutput, XmlParser


def parse(text: str) -> ParsedOutput:
    """每次新建实例，确保解析器无状态（可重复调用）。"""
    return XmlParser().parse(text)


# ---------- 金样例：正常输出 ----------


def test_single_msg():
    """最基本：一个 <msg> 取出一段正文。"""
    r = parse("<msg>今天心情不错~</msg>")
    assert r.messages == ["今天心情不错~"]
    assert r.is_fallback is False


def test_multiple_msg_from_real_output():
    """真实输出：一次回复里有两个 <msg>，必须都取出来。

    这是 main.py 实测到的原样输出（含中间的空行）。
    """
    text = "<msg>哟，老板来啦~</msg>\n\n<msg>今天怎么想起找我了？是有事要交代，还是单纯想我了？</msg>"
    r = parse(text)
    assert r.messages == ["哟，老板来啦~", "今天怎么想起找我了？是有事要交代，还是单纯想我了？"]
    assert r.is_fallback is False


def test_multiline_msg():
    """正文可以跨行（DOTALL）。"""
    r = parse("<msg>第一行\n第二行</msg>")
    assert r.messages == ["第一行\n第二行"]
    assert r.is_fallback is False


def test_text_outside_tags_is_ignored():
    """<msg> 之外的冗余文字不进 messages（chat.md 要求模型别写，但写了要能挡住）。"""
    r = parse("注释：我在想事情。\n<msg>说给用户的话</msg>\n（结束）")
    assert r.messages == ["说给用户的话"]
    assert r.is_fallback is False


def test_whitespace_is_trimmed():
    """标签内首尾空白去掉——模型常在标签里换行缩进。"""
    r = parse("<msg>\n   有话要说~   \n</msg>")
    assert r.messages == ["有话要说~"]
    assert r.is_fallback is False


def test_empty_tag_is_dropped():
    """<msg></msg> 是空标签，丢弃；有另一个有内容的就正常返回。"""
    r = parse("<msg></msg><msg>真话</msg>")
    assert r.messages == ["真话"]
    assert r.is_fallback is False


def test_surrounding_whitespace_ok():
    """整段前后有空白不应影响解析。"""
    r = parse("\n\n  <msg>你好</msg>  \n")
    assert r.messages == ["你好"]
    assert r.is_fallback is False


# ---------- 兜底：非法输入 ----------


def test_no_tags_falls_back_to_whole_text():
    """模型没按契约输出（没标签）→ 整段原文兜底，用户仍能看到话。"""
    r = parse("我今天有点懒得打标签，就直接说了。")
    assert r.messages == ["我今天有点懒得打标签，就直接说了。"]
    assert r.is_fallback is True


def test_unclosed_tag_falls_back():
    """只有开标签没有闭标签 → 解析失败 → 兜底。"""
    r = parse("<msg>话说了一半")
    assert r.messages == ["<msg>话说了一半"]
    assert r.is_fallback is True


def test_empty_input_falls_back_empty():
    """空输入：走兜底但 messages 为空——调用方据此什么都不发。"""
    r = parse("")
    assert r.messages == []
    assert r.is_fallback is True


def test_whitespace_only_falls_back_empty():
    """只有空白 = 没说任何话，同样不该产出空消息。"""
    r = parse("   \n\t  ")
    assert r.messages == []
    assert r.is_fallback is True


def test_only_empty_tag_falls_back():
    """只有空标签，等于没说有效内容 → 兜底（原文即空标签文本）。"""
    r = parse("<msg></msg>")
    assert r.is_fallback is True


def test_uppercase_tag_not_matched():
    """契约是小写；<MSG> 不该被悄悄当成功——要暴露成兜底。"""
    r = parse("<MSG>大写</MSG>")
    assert r.is_fallback is True
    assert r.messages == ["<MSG>大写</MSG>"]


def test_tag_with_attributes_not_matched():
    """<msg foo="x"> 不符合契约，按兜底处理而不是猜。"""
    r = parse('<msg lang="zh">带属性的</msg>')
    assert r.is_fallback is True


# ---------- 契约破坏：整段兜底，不截断报成功（PR #10 High）----------


def test_body_containing_close_tag_falls_back():
    """正文里出现 </msg>：旧实现会在第一个 </msg> 处截断并报成功（静默丢内容）。

    契约不允许 <msg> 内出现标签字符；破坏了就整段兜底，别把「这样」丢掉、
    也别报告成解析成功。
    """
    r = parse("<msg>你要写成 <msg>你好</msg> 这样</msg>\n<msg>第二条</msg>")
    assert r.is_fallback is True
    assert r.messages == ["<msg>你要写成 <msg>你好</msg> 这样</msg>\n<msg>第二条</msg>"]


def test_nested_open_tag_falls_back():
    """一对 <msg>...</msg> 中间又冒出 <msg>（嵌套）→ 契约破坏 → 整段兜底。"""
    r = parse("前言 <msg>没闭合\n<msg>真正的回复</msg>")
    assert r.is_fallback is True
    assert r.messages == ["前言 <msg>没闭合\n<msg>真正的回复</msg>"]


def test_stray_close_tag_falls_back():
    """多出一个对不上开标签的 </msg> → 契约破坏 → 整段兜底。"""
    r = parse("<msg>正常</msg></msg>")
    assert r.is_fallback is True
    assert r.messages == ["<msg>正常</msg></msg>"]


def test_close_tag_in_body_with_trailing_falls_back():
    """正文含 </msg> 且尾部还有内容：旧实现截断成「用」，这里整段兜底。"""
    r = parse("<msg>用 </msg> 结束\n\n第二条</msg>")
    assert r.is_fallback is True


def test_internal_blank_line_is_one_message():
    """一条 <msg> 内部的空行属于同一条消息——不因空行被拆开、也不触发兜底。"""
    r = parse("<msg>甲</msg>\n\n<msg>乙\n\n丙</msg>")
    assert r.messages == ["甲", "乙\n\n丙"]
    assert r.is_fallback is False


def test_unclosed_run_is_linear_time():
    """O(n²) 回归护栏：大量未闭合 <msg> 必须线性完成。

    改前 8000 个未闭合 <msg>（约 40KB）实测 7 秒，同步跑在事件循环上会卡死
    整个服务。这里给一个宽松的上限（0.1s），只保证"不是二次"。
    """
    import time

    text = "<msg>未闭合" * 8000
    start = time.perf_counter()
    XmlParser().parse(text)
    assert time.perf_counter() - start < 0.1


# ---------- 纯函数性质 ----------


def test_parser_is_pure_and_reusable():
    """同一实例重复解析结果一致（无内部状态）。"""
    p = XmlParser()
    text = "<msg>甲</msg><msg>乙</msg>"
    assert p.parse(text).messages == p.parse(text).messages == ["甲", "乙"]
    assert p.parse(text).is_fallback is False


def test_does_not_mutate_input():
    """不修改入参（入参是 str，不可变，这里守住契约意图）。"""
    text = "  <msg>内容</msg>  "
    before = text
    parse(text)
    assert text == before
