# Layer 6 防幻觉重构：两阶段搜索架构

## 背景

`email_finder.py` 的 Layer 6（LLM 联网搜索）存在严重的邮箱幻觉问题：当前 Prompt 直接让 LLM 联网搜索并返回邮箱地址，LLM 会根据"姓名 + 机构域名"的命名规律**伪造看似合理但不存在的邮箱**。MX 验证只能验证域名（如 `@mit.edu` 是否存在），无法验证具体地址（如 `john.doe@mit.edu` 是否真的属于此人）。

## 改动范围

仅修改 `backend/services/email_finder.py` 中的 `_llm_navigate_and_scrape` 函数（L588-L648），不影响 Layer 1-5 和评分系统。

---

## 新架构：两阶段搜索

### Phase 1: 导航模式（LLM 只返回 URL，我们自己提取邮箱）

**核心原则**：LLM 永远不被要求输出邮箱，它只是"导航员"。

**Prompt（导航阶段）**:
```
请帮我查找学术研究者 {name}{org_hint} 的联系方式页面。

请搜索以下类型的网页：
1. {name} 在所属大学/研究机构官网上的个人主页（Faculty Profile）
2. {name} 的 Google Scholar 学术档案页面
3. {name} 的个人学术网站或实验室页面
4. 包含 {name} 联系方式的其他学术页面

【重要规则】
- 只返回你在搜索结果中**确实看到的**网页 URL
- 不要返回任何邮箱地址
- 不要猜测或构造 URL
- 每个 URL 单独一行
- 如果搜索结果中没有找到相关页面，请直接回答"未找到相关页面"
```

**后处理流程**:
1. 从 LLM 回复中用正则提取所有 URL（`https?://...`）
2. 过滤：去掉明显无用的 URL（搜索引擎结果页、PDF 直链、图片等）
3. 对每个 URL（最多 5 个）：
   - HTTP HEAD 请求验证存活（超时 5 秒）
   - 存活的 URL 用 `cloudscraper` 抓取完整页面
   - 调用已有的 `_extract_email_from_html(page_text, target_name)` 提取邮箱
   - 命中邮箱 → MX 验证 + `_score_email` 评分
4. 如果找到邮箱且得分 ≥ 50 → 返回结果，基础分取 60（与当前一致）
5. 如果所有 URL 都未找到邮箱 → 进入 Phase 2

### Phase 2: 直搜兜底模式（改良版当前逻辑）

仅在 Phase 1 完全失败时触发。保留"让 LLM 直接搜邮箱"的模式，但用**强反幻觉 Prompt + 来源回抓验证**双保险。

**Prompt（直搜阶段）**:
```
请帮我查找学术研究者 {name}{org_hint}{doi_hint} 的电子邮箱地址。

【严格规则 - 必须遵守】
1. 只返回你在搜索结果网页中**明确看到、原文写出**的邮箱地址
2. 严禁根据姓名拼写规律推测或构造邮箱（如 firstname.lastname@xxx.edu）
3. 必须附上你找到该邮箱的来源网页 URL
4. 如果搜索结果中没有找到任何明确写出的邮箱，请直接回答"未找到"

请按以下格式回答：
邮箱: xxx@xxx.edu
来源: https://xxx.xxx.xxx/...
（如果未找到，直接回答"未找到"）
```

**后处理流程**:
1. 从 LLM 回复中提取邮箱和来源 URL
2. 如果 LLM 回答"未找到" → 直接返回空结果
3. 如果有邮箱 + 来源 URL：
   - 抓取来源 URL 页面
   - 检查该邮箱是否**真实出现在页面文本中**
   - 验证通过 → 基础分 55，正常评分流程
   - 验证失败（邮箱不在页面中）→ 降级为低置信候选，基础分 40
4. 如果有邮箱但无来源 URL → 降级为低置信候选，基础分 35

---

## URL 过滤规则

从 LLM 回复中提取的 URL 需要过滤掉以下类型：
- 搜索引擎结果页（含 `google.com/search`、`bing.com/search` 等）
- 纯 PDF 直链（`.pdf` 结尾）
- 图片链接（`.jpg`、`.png` 等）
- 常见学术数据库的通用搜索页（如 `pubmed.ncbi.nlm.nih.gov/?term=`）
- 重复 URL（归一化后去重）

保留的优质 URL 类型：
- 大学/机构 Faculty 页面
- Google Scholar 个人档案（`scholar.google.com/citations?user=`）
- 个人网站
- ResearchGate / Academia.edu 个人页面
- ORCID 页面

---

## 评分调整

| 场景 | 基础分 |
|------|--------|
| Phase 1 导航模式命中（邮箱来自真实网页） | 60（与当前 Layer 6 一致） |
| Phase 2 直搜 + 来源验证通过 | 55 |
| Phase 2 直搜 + 来源验证失败 | 40（降级为低置信候选） |
| Phase 2 直搜 + 无来源 URL | 35（最低优先级候选） |

---

## 不改动的部分

- Layer 1-5 完全不动
- `_score_email` 评分函数不动（上述基础分会被传入）
- `_extract_email_from_html` 不动（Phase 1 直接复用）
- `smart_generate_with_search` 不动（两个 Phase 都调用它）
- `_is_strong_name_match`、`_match_best_email` 不动
- `find_email_for_paper` 主管道逻辑不动（只是 Layer 6 内部重构）

---

## 函数签名变化

`_llm_navigate_and_scrape` 的签名和返回格式保持不变：

```python
def _llm_navigate_and_scrape(name: str, org: str, doi: str,
                              paper_title: str = "",
                              cached_domain: str = "") -> dict:
    # 返回 {"email": "...", "homepage": "...", "source_url": "..."}
```

主管道（`find_email_for_paper`）调用方式完全不变。

---

## 验证计划

### 自动测试
1. 用现有的 `test_batch.py` 跑 10 个 DOI，对比改前改后的邮箱命中率和准确率
2. 重点关注 Layer 6 的命中情况：是通过 Phase 1 还是 Phase 2 命中的

### 手动验证
1. 挑 3-5 个之前已知幻觉的 case，验证新逻辑是否能正确识别并拒绝幻觉邮箱
2. 检查 Phase 1 返回的 URL 列表质量（有多少是真实可抓取的）
