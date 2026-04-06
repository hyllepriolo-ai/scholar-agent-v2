"""
邮箱查找器 V3.0 —— 一作搜索全面增强版。

核心升级（V3.0）：
1. 论文页面抓取对一作也开放（取消 role 限制）
2. S2 个人主页直接抓取邮箱
3. 加强名字匹配算法（解决中文短名误匹配）
4. 一作专用搜索 Prompt
5. 邮箱 MX 记录存活性验证
6. 机构域名缓存
7. Round3 加入论文标题上下文

流程：
  Step 0: DOI → Crossref 解析作者+机构（已有）
  Step 1: 论文页面抓取邮箱（通讯+一作均尝试）
  Step 1.5: S2 homepage 直接抓取
  Step 2: 千问联网搜索 第1轮 — 直搜作者+机构+邮箱（一作专用 Prompt）
  Step 3: 千问联网搜索 第2轮 — 交叉验证候选邮箱
  Step 4: 千问联网搜索 第3轮 — 深度搜实验室官网/个人主页
  Step 5: MX 记录验证 + 结果整合
"""
import re
import json
import time
import socket
import dns.resolver
import requests
import cloudscraper
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from functools import lru_cache

from backend.config import (
    smart_generate, smart_generate_with_search,
    HTTP_TIMEOUT, SCRAPE_TIMEOUT, API_RATE_LIMIT_DELAY
)

# 邮箱正则校验
EMAIL_PATTERN = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')

# 噪音邮箱过滤集
NOISE_EMAILS = {'noreply', 'admin', 'info@', 'support', 'webmaster', 'example',
                'privacy', 'contact@', 'help@', 'feedback', 'editor', 'editorial',
                'office@', 'journal', 'press', 'submission', 'subscribe',
                'permissions', 'copyright', 'service', 'sales', 'marketing'}

# ================================================================
# 机构域名缓存（跨论文复用，同批次生效）
# ================================================================
_org_domain_cache = {}


def _cache_org_domain(org: str, email: str):
    """缓存机构 → 邮箱域名的映射"""
    if not org or not email or org in ["未提供", "未找到", "无", ""]:
        return
    domain = email.split("@")[-1].lower() if "@" in email else ""
    if domain and any(d in domain for d in ['.edu', '.ac.', '.org', '.gov', '.cn']):
        # 取机构名的前几个关键词作为 key，提高匹配率
        org_key = org.lower().strip()[:50]
        _org_domain_cache[org_key] = domain
        print(f"    💾 缓存机构域名: {org_key[:30]}... → {domain}")


def _get_cached_domain(org: str) -> str:
    """查询缓存的机构域名"""
    if not org or org in ["未提供", "未找到", "无", ""]:
        return ""
    org_key = org.lower().strip()[:50]
    # 精确匹配
    if org_key in _org_domain_cache:
        return _org_domain_cache[org_key]
    # 子串匹配
    for cached_key, domain in _org_domain_cache.items():
        if cached_key in org_key or org_key in cached_key:
            return domain
    return ""


# ================================================================
# MX 记录邮箱存活性验证
# ================================================================
@lru_cache(maxsize=256)
def _verify_email_mx(email: str) -> bool:
    """通过 MX 记录验证邮箱域名是否存在（缓存结果）"""
    if not email or "@" not in email:
        return False
    domain = email.split("@")[-1]
    try:
        mx_records = dns.resolver.resolve(domain, 'MX', lifetime=5)
        return len(mx_records) > 0
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN,
            dns.resolver.NoNameservers, dns.exception.Timeout):
        return False
    except Exception:
        # DNS 查询异常，不影响流程，默认通过
        return True


# ================================================================
# 主入口：增强版三轮迭代搜索
# ================================================================
def find_email_for_paper(doi: str, name: str, org: str, role: str = "通讯",
                         homepage: str = "", paper_title: str = "") -> dict:
    """
    增强版搜索策略定位作者邮箱。

    Args:
        doi:          论文 DOI
        name:         作者姓名
        org:          作者机构
        role:         角色（"一作" 或 "通讯"）
        homepage:     S2 返回的作者个人主页 URL（可选）
        paper_title:  论文标题（可选，用于深度搜索）
    """
    invalid_tags = ["未提供", "未找到", "无", "none", "unknown", ""]
    if not name or name.lower().strip() in invalid_tags:
        return {"邮箱": "未找到", "主页": "未找到", "来源": "none", "置信度": "无"}

    print(f"\n  📧 [V3.0] 开始追踪: {name} ({org}) [角色: {role}]")

    # ==============================================================
    # Step 1: 论文页面快速通道（一作和通讯均尝试）
    # ==============================================================
    if doi:
        print(f"  🎯 [Step1] 抓取论文页面 (doi.org/{doi})...")
        paper_email = _scrape_paper_page(doi, name)
        if paper_email:
            # MX 验证
            if _verify_email_mx(paper_email):
                print(f"  ✅ [Step1] 论文页面直接命中: {paper_email} (MX验证通过)")
                _cache_org_domain(org, paper_email)
                return {
                    "邮箱": paper_email,
                    "主页": f"https://doi.org/{doi}",
                    "来源": "paper_page",
                    "置信度": "高"
                }
            else:
                print(f"  ⚠️ [Step1] 论文页面找到 {paper_email} 但 MX 验证失败，继续搜索...")

    # ==============================================================
    # Step 1.5: S2 个人主页抓取（如果有 homepage URL）
    # ==============================================================
    if homepage and homepage.startswith("http"):
        print(f"  🏠 [Step1.5] 抓取 S2 个人主页: {homepage}")
        hp_email = _scrape_homepage(homepage, name)
        if hp_email and _verify_email_mx(hp_email):
            print(f"  ✅ [Step1.5] 个人主页命中: {hp_email} (MX验证通过)")
            _cache_org_domain(org, hp_email)
            return {
                "邮箱": hp_email,
                "主页": homepage,
                "来源": "s2_homepage",
                "置信度": "高"
            }

    # ==============================================================
    # Step 2: 千问联网搜索 — 第1轮（直搜邮箱，一作用专用 Prompt）
    # ==============================================================
    print(f"  🔍 [Step2] 千问联网搜索 第1轮...")
    cached_domain = _get_cached_domain(org)
    round1_result = _qwen_search_round1(name, org, doi, role, paper_title, cached_domain)

    candidate_email = ""
    candidate_homepage = homepage  # 保留已有的 homepage

    if round1_result.get("email"):
        candidate_email = round1_result["email"]
        print(f"  📬 [Step2] 第1轮候选邮箱: {candidate_email}")
    if round1_result.get("homepage") and not candidate_homepage:
        candidate_homepage = round1_result["homepage"]
        print(f"  🏠 [Step2] 第1轮候选主页: {candidate_homepage}")

    # ==============================================================
    # Step 3: 千问联网搜索 — 第2轮（交叉验证 / 自动确认环节）
    # ==============================================================
    verified_email = ""
    if candidate_email:
        print(f"  🔄 [Step3] 千问联网搜索 第2轮 — 交叉验证...")
        verified = _qwen_search_round2_verify(name, org, candidate_email, candidate_homepage)
        if verified.get("confirmed"):
            verified_email = candidate_email
            print(f"  ✅ [Step3] 验证通过: {verified_email}")
        elif verified.get("corrected_email"):
            verified_email = verified["corrected_email"]
            print(f"  🔄 [Step3] 验证修正为: {verified_email}")
        else:
            print(f"  ❌ [Step3] 验证未通过，邮箱可能不准确")

    # ==============================================================
    # Step 4: 千问联网搜索 — 第3轮（深度补充搜索）
    # ==============================================================
    if not verified_email:
        print(f"  🕵️ [Step4] 千问联网搜索 第3轮 — 深度搜索...")
        round3_result = _qwen_search_round3_deep(name, org, candidate_homepage, doi, paper_title)
        if round3_result.get("email"):
            verified_email = round3_result["email"]
            print(f"  ✅ [Step4] 深度搜索命中: {verified_email}")
        if round3_result.get("homepage") and not candidate_homepage:
            candidate_homepage = round3_result["homepage"]

    # ==============================================================
    # Step 5: MX 验证 + 结果整合
    # ==============================================================
    confidence = "无"
    source = "none"

    if verified_email:
        # MX 存活性验证
        mx_ok = _verify_email_mx(verified_email)
        if mx_ok:
            source = "qwen_search_verified"
            confidence = "高"
            _cache_org_domain(org, verified_email)
        else:
            print(f"  ⚠️ [MX验证] {verified_email} 域名 MX 记录不存在，置信度降低")
            source = "qwen_search_mx_fail"
            confidence = "低"
    elif candidate_email:
        # 未经验证的候选邮箱
        mx_ok = _verify_email_mx(candidate_email)
        verified_email = candidate_email
        source = "qwen_search_unverified"
        confidence = "中" if mx_ok else "低"

    result = {
        "邮箱": verified_email if verified_email else "未找到",
        "主页": candidate_homepage if candidate_homepage else "未找到",
        "来源": source,
        "置信度": confidence
    }
    print(f"  📧 最终结果: {result['邮箱']} (来源: {result['来源']}, 置信度: {result['置信度']})")
    return result


# 保留旧接口兼容
def find_email(name: str, org: str) -> dict:
    """兼容旧接口"""
    return find_email_for_paper("", name, org)


# ================================================================
# Step 1: 论文页面抓取
# ================================================================
def _scrape_paper_page(doi: str, target_name: str) -> str:
    """通过 DOI 访问论文出版商页面，提取作者邮箱（一作/通讯均尝试）"""
    urls_to_try = [f"https://doi.org/{doi}"]

    if "10.1016/" in doi:
        # 直接构建 ScienceDirect / Cell 页面 URL
        try:
            cr_url = f"https://api.crossref.org/works/{doi}"
            r = requests.get(cr_url, timeout=8)
            if r.status_code == 200:
                msg = r.json().get("message", {})
                for link in msg.get("link", []):
                    url = link.get("URL", "")
                    if "sciencedirect" in url or "elsevier" in url:
                        urls_to_try.append(url)
        except Exception:
            pass

    scraper = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Referer": "https://scholar.google.com/",
    }

    for url in urls_to_try:
        try:
            resp = scraper.get(url, headers=headers, timeout=SCRAPE_TIMEOUT, allow_redirects=True)
            if resp.status_code != 200:
                continue

            final_url = resp.url
            page_text = resp.text

            # 处理 Elsevier 跳板页
            if 'linkinghub.elsevier.com' in final_url:
                real_url = _extract_elsevier_redirect(page_text, final_url)
                if real_url:
                    try:
                        resp2 = scraper.get(real_url, headers=headers, timeout=SCRAPE_TIMEOUT, allow_redirects=True)
                        if resp2.status_code == 200:
                            page_text = resp2.text
                    except Exception:
                        pass

            email = _extract_email_from_html(page_text, target_name)
            if email:
                return email
        except Exception:
            continue
    return ""


# ================================================================
# Step 1.5: 个人主页抓取
# ================================================================
def _scrape_homepage(homepage_url: str, target_name: str) -> str:
    """抓取个人主页或实验室页面，提取邮箱"""
    try:
        scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
        )
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
        resp = scraper.get(homepage_url, headers=headers, timeout=SCRAPE_TIMEOUT, allow_redirects=True)
        if resp.status_code == 200:
            email = _extract_email_from_html(resp.text, target_name)
            if email:
                return email
    except Exception as e:
        print(f"    ⚠️ 主页抓取异常: {e}")
    return ""


def _extract_elsevier_redirect(html_text: str, current_url: str) -> str:
    """从 Elsevier linkinghub 跳板页提取真实 URL"""
    soup = BeautifulSoup(html_text, 'html.parser')
    meta = soup.find('meta', attrs={'http-equiv': re.compile(r'refresh', re.I)})
    if meta:
        content = meta.get('content', '')
        match = re.search(r'url=([^;]+)', content, re.I)
        if match:
            redirect_url = match.group(1).strip().strip('\'"')
            if redirect_url.startswith('/'):
                return urljoin("https://linkinghub.elsevier.com", redirect_url)
            return redirect_url

    for a in soup.find_all('a', href=True):
        if 'sciencedirect.com' in a['href']:
            return a['href']

    if 'pii/S' in html_text:
        match = re.search(r'pii/(S\w+)', html_text)
        if match:
            return f"https://www.sciencedirect.com/science/article/pii/{match.group(1)}"
    return ""


def _extract_email_from_html(page_text: str, target_name: str) -> str:
    """从 HTML 页面提取邮箱（增强版名字匹配）"""
    soup = BeautifulSoup(page_text, 'html.parser')

    # 策略A: mailto 链接
    mailto_emails = []
    for a_tag in soup.find_all('a', href=True):
        href = a_tag.get('href', '')
        if href.startswith('mailto:'):
            email = href.replace('mailto:', '').split('?')[0].strip()
            if EMAIL_PATTERN.match(email) and not _is_noise_email(email):
                mailto_emails.append(email)

    if mailto_emails:
        best = _match_best_email(mailto_emails, target_name)
        if best:
            return best
        for em in mailto_emails:
            if any(d in em.lower() for d in ['.edu', '.ac.', '.org', '.gov']):
                return em

    # 策略B: 通讯作者上下文
    corr_patterns = [
        r'(?:corresponding|correspondence|通讯)[\s\S]{0,300}?([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})',
        r'(?:email|e-mail|Email address)[\s:：]*\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})',
    ]
    for pat in corr_patterns:
        matches = re.findall(pat, page_text, re.IGNORECASE)
        for em in matches:
            if not _is_noise_email(em):
                # 对一作也尝试匹配名字
                if _is_strong_name_match(em, target_name):
                    return em

    # 如果上面的策略B没有通过名字匹配找到，但有结果，返回第一个（通常是通讯作者的）
    for pat in corr_patterns:
        matches = re.findall(pat, page_text, re.IGNORECASE)
        for em in matches:
            if not _is_noise_email(em) and _is_strong_name_match(em, target_name):
                return em

    # 策略C: 全文 + 名字匹配
    all_emails = EMAIL_PATTERN.findall(page_text)
    clean = [e for e in all_emails if not _is_noise_email(e)]
    if clean:
        best = _match_best_email(clean, target_name)
        if best:
            return best
    return ""


# ================================================================
# Step 2-4: 千问三轮联网搜索
# ================================================================
def _qwen_search_round1(name: str, org: str, doi: str, role: str = "通讯",
                        paper_title: str = "", cached_domain: str = "") -> dict:
    """第1轮：根据角色使用不同搜索策略"""
    org_hint = f"，机构为 {org}" if org and org not in ["未提供", "未找到", "无", ""] else ""
    doi_hint = f"，其近期发表的论文DOI为 {doi}" if doi else ""
    title_hint = f"，论文标题为《{paper_title}》" if paper_title else ""
    domain_hint = f"\n注意：该学者所在机构的学术邮箱域名可能是 @{cached_domain}" if cached_domain else ""

    if role in ["一作", "first_author"]:
        # 🔧 一作专用 Prompt：强调研究生/博后搜索路径
        prompt = f"""请帮我搜索以下学术研究者的**电子邮箱**和**个人主页/实验室网页**：

姓名：{name}{org_hint}{doi_hint}{title_hint}

该学者是论文的**第一作者**（通常为研究生、博士后或青年研究员）。

搜索策略（请按顺序尝试）：
1. 搜索 "{name}" + 机构名 + "email" 查找大学院系个人页面
2. 搜索 Google Scholar 上的 "{name}" 个人主页
3. 搜索 ResearchGate / ORCID 上的 "{name}" 个人资料
4. 搜索 "{name}" + "lab" 或 "research group" 查找实验室成员页面
5. 搜索该学者在大学院系通讯录（faculty directory / people）中的信息
6. 优先查找 .edu / .ac.uk / .ac.cn 等学术域名邮箱{domain_hint}

请严格以以下 JSON 格式输出（不要输出任何其他内容）：
{{"email": "找到的邮箱或空字符串", "homepage": "找到的主页URL或空字符串", "source": "信息来源简述"}}"""
    else:
        # 通讯作者用原有 Prompt
        prompt = f"""请帮我搜索以下学术研究者的**电子邮箱**和**个人主页/实验室网页**：

姓名：{name}{org_hint}{doi_hint}{title_hint}

搜索要求：
1. 请从大学官网、实验室主页、Google Scholar、ResearchGate、PubMed 等学术平台搜索
2. 优先查找 .edu / .ac.uk / .ac.cn 等学术域名邮箱
3. 如果找到多个邮箱，请选择最可能的学术联系邮箱{domain_hint}

请严格以以下 JSON 格式输出（不要输出任何其他内容）：
{{"email": "找到的邮箱或空字符串", "homepage": "找到的主页URL或空字符串", "source": "信息来源简述"}}"""

    try:
        raw = smart_generate_with_search(prompt)
        return _parse_json_response(raw)
    except Exception as e:
        print(f"    ⚠️ 第1轮搜索异常: {e}")
        return {}


def _qwen_search_round2_verify(name: str, org: str, candidate_email: str, homepage: str) -> dict:
    """第2轮：交叉验证候选邮箱（自动确认环节）"""
    homepage_hint = f"，其主页为 {homepage}" if homepage else ""

    prompt = f"""请帮我验证以下信息是否准确：

学者姓名：{name}
候选邮箱：{candidate_email}{homepage_hint}

验证要求：
1. 请搜索确认该邮箱是否确实属于名为 {name} 的学术研究者
2. 检查该邮箱的域名是否与该学者的所在机构匹配
3. 如果邮箱不正确，请尝试搜索正确的邮箱

请严格以以下 JSON 格式输出：
{{"confirmed": true或false, "corrected_email": "如果原邮箱错误则填写正确邮箱否则留空", "reason": "验证依据简述"}}"""

    try:
        raw = smart_generate_with_search(prompt)
        return _parse_json_response(raw)
    except Exception as e:
        print(f"    ⚠️ 第2轮验证异常: {e}")
        return {}


def _qwen_search_round3_deep(name: str, org: str, homepage: str, doi: str,
                              paper_title: str = "") -> dict:
    """第3轮：深度搜索实验室官网和联系方式（增强版，含论文标题上下文）"""
    context_parts = []
    if org and org not in ["未提供", "未找到", "无", ""]:
        context_parts.append(f"所在机构：{org}")
    if homepage:
        context_parts.append(f"已知主页：{homepage}")
    if doi:
        context_parts.append(f"论文DOI：{doi}")
    if paper_title:
        context_parts.append(f"论文标题：{paper_title}")
    context = "，".join(context_parts) if context_parts else "无额外信息"

    prompt = f"""之前两轮搜索未能找到 {name} 的邮箱。请进行更深入的搜索：

已知信息：{context}

深度搜索策略：
1. 搜索 "{name} lab" 或 "{name} laboratory" 查找实验室官网
2. 搜索 "{name} contact" 或 "{name} email" 查找联系方式
3. 搜索该学者在大学院系通讯录（faculty directory）中的信息
4. 查看 ORCID、Scopus Author ID 等学术身份平台
5. 如果有 DOI，查看论文合作者实验室网页中是否提及该学者
6. 搜索 "{name}" + 论文标题的关键词来缩小范围

请严格以以下 JSON 格式输出：
{{"email": "找到的邮箱或空字符串", "homepage": "找到的主页URL或空字符串", "source": "具体发现来源"}}"""

    try:
        raw = smart_generate_with_search(prompt)
        return _parse_json_response(raw)
    except Exception as e:
        print(f"    ⚠️ 第3轮深度搜索异常: {e}")
        return {}


# ================================================================
# 工具函数
# ================================================================
def _parse_json_response(raw: str) -> dict:
    """从 LLM 回复中提取 JSON 对象"""
    if not raw:
        return {}

    # 尝试直接解析
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 尝试从 markdown 代码块中提取
    patterns = [
        r'```json\s*\n?(.*?)\n?\s*```',
        r'```\s*\n?(.*?)\n?\s*```',
        r'\{[^{}]*\}',
    ]
    for pat in patterns:
        match = re.search(pat, raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1) if '```' in pat else match.group(0))
            except (json.JSONDecodeError, IndexError):
                continue

    # 最后尝试从文本中提取邮箱
    emails = EMAIL_PATTERN.findall(raw)
    if emails:
        clean = [e for e in emails if not _is_noise_email(e)]
        if clean:
            return {"email": clean[0]}

    return {}


def _is_noise_email(email: str) -> bool:
    """检查是否为噪音邮箱"""
    email_lower = email.lower()
    return any(n in email_lower for n in NOISE_EMAILS)


def _is_strong_name_match(email: str, target_name: str) -> bool:
    """
    增强版名字-邮箱匹配算法。
    
    解决中文姓名（拼音化后）短名误匹配问题：
    - "Li" 不应匹配 "oliver@..."
    - "Wang" 不应匹配 "wangner@..."
    - 但 "Wei Wang" 应该匹配 "weiwang@..." 或 "w.wang@..."
    
    策略：
    1. 短名（≤3字符）必须精确匹配邮箱前缀的独立部分
    2. 多个名字部分匹配的权重更高
    3. 要求至少 family name（姓）匹配
    """
    if not email or not target_name:
        return False

    prefix = email.split("@")[0].lower()
    # 将邮箱前缀按分隔符拆分为独立部分
    prefix_parts = re.split(r'[._\-]', prefix)

    name_parts = target_name.lower().replace("-", " ").split()
    if not name_parts:
        return False

    # 对于中文姓名（通常 2-3 个短部分），需要更严格的匹配
    short_name_parts = [p for p in name_parts if len(p) <= 3]
    long_name_parts = [p for p in name_parts if len(p) > 3]

    matched_parts = 0

    for part in name_parts:
        if len(part) <= 2:
            # 极短名（如 "Li", "Yu"）: 必须是邮箱前缀的独立部分之一
            if part in prefix_parts:
                matched_parts += 1
        elif len(part) <= 3:
            # 短名（如 "Wei", "Yan"）: 必须是独立部分，或者前缀以它开头
            if part in prefix_parts or prefix.startswith(part):
                matched_parts += 1
        else:
            # 长名（≥4字符如 "Zhang", "Chen"）: 包含即可
            if part in prefix:
                matched_parts += 1

    # 判定规则：
    if len(name_parts) == 1:
        # 单名情况：短名必须在独立部分中精确匹配
        if len(name_parts[0]) <= 3:
            return name_parts[0] in prefix_parts
        else:
            return name_parts[0] in prefix
    else:
        # 多名情况：至少匹配 2 个部分，或者匹配 family name + 首字母
        if matched_parts >= 2:
            return True
        # 检查 首字母+姓 模式（如 "W. Wang" → "wwang"）
        family = name_parts[-1] if len(name_parts) > 1 else ""
        initials = "".join(p[0] for p in name_parts[:-1]) if len(name_parts) > 1 else ""
        if family and initials:
            if f"{initials}{family}" in prefix or f"{initials}.{family}" in prefix:
                return True
            # 反向模式 "wangw"
            if f"{family}{initials}" in prefix:
                return True
        return False


def _match_best_email(emails: list, target_name: str) -> str:
    """从邮箱列表中匹配与目标名字最相关的（增强版）"""
    if not emails or not target_name:
        return ""

    name_parts = target_name.lower().replace("-", " ").split()

    best_score = 0
    best_email = ""

    for em in emails:
        # 先用增强版匹配检验是否通过
        if not _is_strong_name_match(em, target_name):
            continue

        prefix = em.split("@")[0].lower()
        prefix_parts = re.split(r'[._\-]', prefix)

        score = 0
        for part in name_parts:
            if len(part) <= 2:
                if part in prefix_parts:
                    score += 2  # 短名精确匹配加分
            elif part in prefix:
                score += 1

        # 学术域名加分
        domain = em.split("@")[-1].lower()
        if any(d in domain for d in ['.edu', '.ac.', '.org', '.gov']):
            score += 1

        if score > best_score:
            best_score = score
            best_email = em

    return best_email if best_score > 0 else ""
