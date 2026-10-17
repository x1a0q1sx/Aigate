# -*- coding: utf-8 -*-
"""前端静态回归：Playground 服务商/模型可搜索下拉（2026-10-17）。

用户要求：
1. 服务商与模型两个下拉都能输入搜索；
2. 服务商搜索按「旗下任一模型名命中」也算命中——例如输入 free，
   OpenCode 因为有 *-free 模型，所以结果里要出现 OpenCode。

实现：新增通用组件 `SearchSelect.vue`（输入即过滤 + 键盘导航 + 动态副标题），
Playground 用轻量全量模型目录（/admin/api/models/light）在本地完成匹配。
按仓库既有约定做源码静态扫描锁死（无 JS 测试运行时）。
"""
from pathlib import Path

_CLIENT = Path("client/src")


def _read(rel: str) -> str:
    return (_CLIENT / rel).read_text(encoding="utf-8")


# ── 1. 通用可搜索下拉组件 ──────────────────────────────────

def test_search_select_component_supports_filter_and_hint():
    src = _read("components/SearchSelect.vue")
    # 自定义过滤 + 动态副标题（父组件用于「模型名命中服务商」的匹配与说明）
    assert "filterFn" in src and "hintFn" in src
    assert "filterFn(o, q)" in src or "this.filterFn(o, q)" in src
    # 键盘导航与关闭
    assert "@keydown.down.prevent" in src
    assert "@keydown.up.prevent" in src
    assert "@keydown.enter.prevent" in src
    assert "@keydown.esc.prevent" in src
    # 点击组件外部关闭
    assert "mousedown" in src and "contains(e.target)" in src


# ── 2. Playground 两个下拉都换成可搜索组件 ─────────────────

def test_playground_uses_search_select_for_provider_and_model():
    src = _read("views/Playground.vue")
    assert "import SearchSelect from '../components/SearchSelect.vue'" in src
    assert "components: { SearchSelect }" in src
    # 服务商与模型各一个 SearchSelect（不再用原生 select）
    assert src.count("<SearchSelect") == 2
    assert 'v-model="selectedProviderId"' not in src
    assert 'v-model="selectedModelId"' not in src


def test_playground_loads_light_catalog_for_local_search():
    src = _read("views/Playground.vue")
    # 一次性拿轻量全量目录（9 字段），两个下拉都在本地即时过滤
    assert "api.getModelsLight()" in src
    assert "lightModels" in src
    # 逐服务商懒加载已移除
    assert "loadModelsFor" not in src
    assert "api.getModels({ provider_id: pid })" not in src


# ── 3. 服务商搜索包含「模型名命中」 ────────────────────────

def test_provider_search_matches_model_names():
    src = _read("views/Playground.vue")
    # providerOptions 携带每家服务商的模型名列表
    assert "providerModelNames" in src
    assert "modelNames:" in src
    # filterFn：服务商名命中 或 旗下任一模型名命中
    assert "providerFilter(opt, q)" in src
    assert "(opt.modelNames || []).some(n => String(n).toLowerCase().includes(q))" in src
    # 命中原因展示：含模型：xxx-free
    assert "providerHint" in src
    assert "含模型：" in src


def test_model_options_carry_searchable_keywords():
    src = _read("views/Playground.vue")
    assert "modelOptions()" in src
    assert "keywords:" in src
    assert "m.model_id" in src and "m.display_name" in src


# ── 4. 切换服务商后默认选中第一家模型 ─────────────────────

def test_provider_pick_selects_first_model_and_syncs_form():
    src = _read("views/Playground.vue")
    assert "onProviderPick(val)" in src
    assert "const first = this.filteredModels[0]" in src
    assert "this.selectedModelId = first ? String(first.id) : ''" in src
    assert "syncModel()" in src
