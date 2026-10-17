# -*- coding: utf-8 -*-
"""前端静态回归：Playground 智商测试图标行（2026-10-18）。

用户要求：
1. GitHub 上搜测试模型智商的方法，总结几个放到 Playground 页面；
2. 只留图标，悬停/聚焦显示文字提示（题目 + 标准答案）；
3. 点击后把题目填进输入框（不直接发送，用户可先换模型）。

题库取自公开的 LLM 推理测试经典题（糖果保底 21 / strawberry 数 r / 蝙蝠球 /
9.11 vs 9.9 / 兄弟姐妹 / 蜗牛爬井 / 时钟角度 / 千瓶药水 / 日期推理 / Sally-Anne），
每题都标了「常见错误答案」，用来判断模型是真推理还是踩了直觉陷阱。
按仓库既有约定做源码静态扫描锁死（无 JS 测试运行时）。
"""
from pathlib import Path

_CLIENT = Path("client/src")


def _read(rel: str) -> str:
    return (_CLIENT / rel).read_text(encoding="utf-8")


def test_playground_has_iq_test_icon_row():
    src = _read("views/Playground.vue")
    # 只渲染图标：按钮内容就是 emoji，名称放 title / aria-label
    assert "IQ_TESTS" in src
    assert 'class="iq-icon"' in src
    assert ':title="t.name"' in src and ':aria-label="t.name"' in src
    # 悬停/聚焦 → 显示题目详情；离开 → 收起
    assert '@mouseenter="iqActive = t.id"' in src
    assert '@mouseleave="iqActive = \'\'"' in src
    assert '@focus="iqActive = t.id"' in src
    assert 'v-if="iqCurrent"' in src


def test_iq_tests_cover_classic_reasoning_traps():
    src = _read("views/Playground.vue")
    # 十个经典题：emoji + 中文名 + 标准答案都要在题库里
    for emoji in ("🍬", "🍓", "⚾", "🔢", "👧", "🐌", "🕒", "🧪", "📅", "🧠"):
        assert emoji in src, f"缺少题库图标 {emoji}"
    assert "糖果保底" in src and "21 颗" in src
    assert "草莓数 r" in src
    assert "蝙蝠球" in src and "0.05 美元" in src
    assert "9.11 和 9.9" in src
    assert "蜗牛爬井" in src and "第 8 天" in src
    assert "7.5°" in src
    assert "1000 瓶药水" in src
    assert "莎莉安妮" in src
    # 每题都要标「常见错误答案」，否则无法判断是否踩陷阱
    assert src.count("常见错误答案") >= 5


def test_iq_icon_click_fills_message_without_sending():
    src = _read("views/Playground.vue")
    assert '@click="fillIqTest(t)"' in src
    # 只填输入框 + 聚焦，不触发发送（不调用 newRun/send）
    start = src.index("fillIqTest(t) {")
    end = src.index("}", src.index("this.$refs.msgInput", start))
    body = src[start:end]
    assert "this.form.message = t.prompt" in body
    assert "msgInput" in body
    assert "newRun" not in body and "this.send()" not in body
    # 输入框挂了 ref，点击后能聚焦
    assert 'ref="msgInput"' in src
