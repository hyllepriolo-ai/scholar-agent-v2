"""
邮箱查找器 V4.0 —— 信任链架构。

核心升级（V4.0）：
1. 7 层信任链管道，按可信度从高到低排列
2. ORCID API 直查（最高可信源）
3. Crossref 邮箱字段提取（之前完全忽略的数据）
4. LLM 降级为"导航器"——只找 URL，不直接返回邮箱
5. 同机构推断（一作专用）——继承通讯作者的机构线索
6. 评分制验证——非硬门槛，gmail 等非机构邮箱也不拒绝
7. 每条结果附来源 URL——可追溯

管道流程：
  Layer 1: ORCID API 直查 (基础分 90)
  Layer 2: Crossref 邮箱字段 (基础分 85)
  Layer 3: 论文页面抓取 (基础分 80)
  Layer 4: S2/ORCID Homepage 抓取 (基础分 75)
  Layer 5: 同机构推断 - 一作专用 (基础分 70)
  Layer 6: LLM 导航模式 (基础分 60)
  Layer 7: 评分验证 (所有结果必过)
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
from backend.services.orcid_resolver import query_orcid, normalize_orcid

# 邮箱正则校验
EMAIL_PATTERN = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')

# 噪音邮箱过滤集
NOISE_EMAILS = {'noreply', 'admin', 'info@', 'support', 'webmaster', 'example',
                'privacy', 'contact@', 'help@', 'feedback', 'editor', 'editorial',
                'office@', 'journal', 'press', 'submission', 'subscribe',
                'permissions', 'copyright', 'service', 'sales', 'marketing',
                'xxx@', 'your', 'name@', 'user@', 'placeholder', 'test@'}

# ================================================================
# 信任链评分常量
# ================================================================
LAYER_BASE_SCORES = {
    1: 90,   # ORCID API
    2: 85,   # Crossref 元数据
    3: 80,   # 论文页面抓取
    4: 75,   # Homepage 抓取
    5: 70,   # 同机构推断
    6: 60,   # LLM 导航
}

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
        org_key = org.lower().strip()[:50]
        _org_domain_cache[org_key] = domain
        print(f"    💾 缓存机构域名: {org_key[:30]}... → {domain}")


def _get_cached_domain(org: str) -> str:
    """查询缓存的机构域名"""
    if not org or org in ["未提供", "未找到", "无", ""]:
        return ""
    org_key = org.lower().strip()[:50]
    if org_key in _org_domain_cache:
        return _org_domain_cache[org_key]
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
        return True


# ================================================================
# 主入口：信任链管道
# ================================================================
def find_email_for_paper(doi: str, name: str, org: str, role: str = "通讯",
                         homepage: str = "", paper_title: str = "",
                         orcid: str = "", crossref_email: str = "",
                         corr_result: dict = None) -> dict:
    """
    信任链架构搜索策略定位作者邮箱。

    Args:
        doi:            论文 DOI
        name:           作者姓名
        org:            作者机构
        role:           角色（"一作" 或 "通讯"）
        homepage:       S2 返回的作者个人主页 URL（可选）
        paper_title:    论文标题（可选）
        orcid:          作者 ORCID ID（可选）
        crossref_email: Crossref 元数据中直接提供的邮箱（可选）
        corr_result:    通讯作者已找到的结果（一作搜索时传入，可选）
    """
    invalid_tags = ["未提供", "未找到", "无", "none", "unknown", ""]
    if not name or name.lower().strip() in invalid_tags:
        return _empty_result()

    print(f"\n  📧 [V4.0 信任链] 开始追踪: {name} ({org}) [角色: {role}]")

    # 收集所有候选邮箱（用于后续交叉印证）
    all_candidates = []
    # 收集所有可抓取的 URL（ORCID 返回的 + homepage + 后续发现的）
    extra_urls = []

    # ==============================================================
    # Layer 1: ORCID API 直查 (基础分 90)
    # ==============================================================
    if orcid:
        print(f"  🔗 [Layer1] ORCID API 查询...")
        orcid_data = query_orcid(orcid)
        if orcid_data.get("success"):
            extra_urls.extend(orcid_data.get("urls", []))

            if orcid_data.get("email"):
                email = orcid_data["email"]
                score = _score_email(email, 1, name, org, all_candidates)
                if score >= 50:
                    print(f"  ✅ [Layer1] ORCID 公开邮箱命中: {email} (得分: {score})")
                    _cache_org_domain(org, email)
                    return _build_result(email, f"https://orcid.org/{normalize_orcid(orcid)}",
                                        "orcid_api", score)
                else:
                    all_candidates.append({"email": email, "layer": 1, "score": score})

            if orcid_data.get("affiliations") and org in invalid_tags:
                org = orcid_data["affiliations"][0]
                print(f"  🏛️ [Layer1] ORCID 补全机构: {org}")

    # ==============================================================
    # Layer 2: Crossref 邮箱字段 (基础分 85)
    # ==============================================================
    if crossref_email:
        print(f"  📋 [Layer2] Crossref 元数据邮箱: {crossref_email}")
        if _verify_email_mx(crossref_email):
            score = _score_email(crossref_email, 2, name, org, all_candidates)
            if score >= 50:
                print(f"  ✅ [Layer2] Crossref 邮箱验证通过 (得分: {score})")
                _cache_org_domain(org, crossref_email)
                return _build_result(crossref_email, f"https://doi.org/{doi}",
                                    "crossref_metadata", score)
        else:
            print(f"  ⚠️ [Layer2] Crossref 邮箱 MX 验证失败")
            all_candidates.append({"email": crossref_email, "layer": 2, "score": 30})

    # ==============================================================
    # Layer 3: 论文页面抓取 (基础分 80)
    # ==============================================================
    if doi:
        print(f"  🎯 [Layer3] 论文页面抓取 (doi.org/{doi})...")
        paper_email = _scrape_paper_page(doi, name)
        if paper_email:
            # 防交叉污染：一作搜索时，如果抓到的是通讯作者的邮箱，跳过
            if corr_result and paper_email.lower() == corr_result.get("邮箱", "").lower():
                print(f"  ⚠️ [Layer3] 抓到的是通讯作者邮箱 {paper_email}，跳过")
            else:
                score = _score_email(paper_email, 3, name, org, all_candidates)
                if score >= 50 and _verify_email_mx(paper_email):
                    print(f"  ✅ [Layer3] 论文页面命中: {paper_email} (得分: {score})")
                    _cache_org_domain(org, paper_email)
                    return _build_result(paper_email, f"https://doi.org/{doi}",
                                        "paper_page", score)
                else:
                    all_candidates.append({"email": paper_email, "layer": 3, "score": score})

    # ==============================================================
    # Layer 4: Homepage 抓取 (基础分 75)
    # ==============================================================
    all_homepages = []
    if homepage and homepage.startswith("http"):
        all_homepages.append(homepage)
    all_homepages.extend([u for u in extra_urls if u.startswith("http")])

    for hp_url in all_homepages[:3]:
        print(f"  🏠 [Layer4] 抓取个人主页: {hp_url[:60]}...")
        hp_email = _scrape_homepage(hp_url, name)
        if hp_email and _verify_email_mx(hp_email):
            score = _score_email(hp_email, 4, name, org, all_candidates)
            if score >= 50:
                print(f"  ✅ [Layer4] 主页命中: {hp_email} (得分: {score})")
                _cache_org_domain(org, hp_email)
                return _build_result(hp_email, hp_url, "homepage_scrape", score)
            else:
                all_candidates.append({"email": hp_email, "layer": 4, "score": score})

    # ==============================================================
    # Layer 5: 同机构推断 (一作专用, 基础分 70)
    # ==============================================================
    if role in ["一作", "first_author"] and corr_result:
        print(f"  🔄 [Layer5] 同机构推断（利用通讯作者信息）...")
        coaff_email = _try_coaffiliation_search(name, corr_result, paper_title)
        if coaff_email:
            score = _score_email(coaff_email, 5, name, org, all_candidates)
            if score >= 50 and _verify_email_mx(coaff_email):
                source_url = corr_result.get("来源URL", corr_result.get("主页", ""))
                print(f"  ✅ [Layer5] 同机构推断命中: {coaff_email} (得分: {score})")
                _cache_org_domain(org, coaff_email)
                return _build_result(coaff_email, source_url, "coaffiliation", score)
            else:
                all_candidates.append({"email": coaff_email, "layer": 5, "score": score})

    # ==============================================================
    # Layer 6: LLM 两阶段搜索 (Phase1 导航 / Phase2 直搜兜底)
    # ==============================================================
    print(f"  🧭 [Layer6] LLM 两阶段搜索...")
    nav_result = _llm_navigate_and_scrape(name, org, doi, paper_title,
                                          _get_cached_domain(org))
    if nav_result.get("email"):
        nav_email = nav_result["email"]
        score = _score_email(nav_email, 6, name, org, all_candidates)

        # Phase 2 结果根据验证状态降分
        phase = nav_result.get("_phase", "")
        if phase == "phase2_verified":
            score -= 5   # 有来源验证通过，略降（基础约 55）
        elif phase == "phase2_unverified":
            score -= 20  # 来源验证失败，大幅降分（基础约 40）
        elif phase == "phase2_no_source":
            score -= 25  # 无来源 URL，最低优先级（基础约 35）

        if score >= 50 and _verify_email_mx(nav_email):
            phase_label = f" [{phase}]" if phase else " [Phase1]"
            print(f"  ✅ [Layer6{phase_label}] 命中: {nav_email} (得分: {score})")
            _cache_org_domain(org, nav_email)
            return _build_result(nav_email, nav_result.get("source_url", ""),
                                "llm_navigate", score)
        else:
            all_candidates.append({"email": nav_email, "layer": 6, "score": score})

    nav_homepage = nav_result.get("homepage", "")

    # ==============================================================
    # 兜底：从所有候选中选最高分的
    # ==============================================================
    if all_candidates:
        for c in all_candidates:
            c["score"] = _score_email(c["email"], c["layer"], name, org, all_candidates)
        best = max(all_candidates, key=lambda x: x["score"])
        if best["score"] >= 40:
            print(f"  📬 [兜底] 选择最高分候选: {best['email']} (得分: {best['score']})")
            return _build_result(best["email"],
                                nav_homepage or homepage or "",
                                f"candidate_layer{best['layer']}",
                                best["score"])

    # ==============================================================
    # 全部未命中
    # ==============================================================
    print(f"  ❌ 所有层级均未找到可信邮箱")
    final_homepage = nav_homepage or homepage or ""
    return {
        "邮箱": "未找到",
        "主页": final_homepage if final_homepage else "未找到",
        "来源": "none",
        "来源URL": "",
        "置信度": "无",
        "置信分": 0
    }


# 保留旧接口兼容
def find_email(name: str, org: str) -> dict:
    """兼容旧接口"""
    return find_email_for_paper("", name, org)


# ================================================================
# 结果构建辅助
# ================================================================
def _empty_result():
    """返回空结果"""
    return {"邮箱": "未找到", "主页": "未找到", "来源": "none",
            "来源URL": "", "置信度": "无", "置信分": 0}


def _build_result(email: str, source_url: str, source: str, score: int) -> dict:
    """构建标准输出结果"""
    if score >= 70:
        confidence = "高"
    elif score >= 50:
        confidence = "中"
    else:
        confidence = "低"

    return {
        "邮箱": email,
        "主页": source_url if source_url else "未找到",
        "来源": source,
        "来源URL": source_url,
        "置信度": confidence,
        "置信分": score
    }


# ================================================================
# Layer 3: 论文页面抓取
# ================================================================
def _scrape_paper_page(doi: str, target_name: str) -> str:
    """通过 DOI 访问论文出版商页面，提取作者邮箱"""
    urls_to_try = [f"https://doi.org/{doi}"]

    if "10.1016/" in doi:
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

    import socket as _socket
    old_timeout = _socket.getdefaulttimeout()
    _socket.setdefaulttimeout(SCRAPE_TIMEOUT)  # 防止 SSL 握手无限挂起

    try:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True},
            ssl_context=ctx
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
            except Exception as e:
                print(f"    ⚠️ 论文页面抓取异常: {type(e).__name__}")
                continue
    except Exception as e:
        print(f"    ⚠️ scraper 初始化异常: {e}")
    finally:
        _socket.setdefaulttimeout(old_timeout)
    return ""


# ================================================================
# Layer 4: 个人主页抓取
# ================================================================
def _scrape_homepage(homepage_url: str, target_name: str) -> str:
    """抓取个人主页或实验室页面，提取邮箱"""
    try:
        import socket as _socket
        old_timeout = _socket.getdefaulttimeout()
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True},
            ssl_context=ctx
        )
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
        resp = scraper.get(homepage_url, headers=headers, timeout=SCRAPE_TIMEOUT, allow_redirects=True)
        _socket.setdefaulttimeout(old_timeout)
        if resp.status_code == 200:
            email = _extract_email_from_html(resp.text, target_name)
            if email:
                return email
    except Exception as e:
        print(f"    ⚠️ 主页抓取异常: {type(e).__name__}: {e}")
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
                if _is_strong_name_match(em, target_name):
                    return em

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
# Layer 5: 同机构推断（一作专用）
# ================================================================
def _try_coaffiliation_search(target_name: str, corr_result: dict,
                               paper_title: str = "") -> str:
    """
    利用通讯作者信息搜索一作邮箱。
    策略:
    1. 从通讯作者结果中获取机构域名
    2. 尝试抓取通讯作者主页/lab页面的 members/people 子路径
    3. 在这些页面中搜索一作名字
    """
    if not corr_result or corr_result.get("邮箱") == "未找到":
        return ""

    corr_email = corr_result.get("邮箱", "")
    corr_homepage = corr_result.get("主页", "") or corr_result.get("来源URL", "")

    # 策略 A: 从通讯 lab page 找一作
    if corr_homepage and corr_homepage.startswith("http"):
        lab_email = _search_lab_page_for_member(corr_homepage, target_name)
        if lab_email:
            return lab_email

    # 策略 B: 如果知道机构域名，尝试在机构网站搜索
    if corr_email and "@" in corr_email:
        domain = corr_email.split("@")[-1]
        base_domain = domain
        if domain.startswith("mail.") or domain.startswith("email."):
            base_domain = domain.split(".", 1)[1]

        people_urls = [
            f"https://www.{base_domain}",
        ]
        for people_url in people_urls:
            try:
                scraper = cloudscraper.create_scraper(
                    browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
                )
                resp = scraper.get(people_url, timeout=10, allow_redirects=True)
                if resp.status_code == 200:
                    email = _extract_email_from_html(resp.text, target_name)
                    if email:
                        return email
            except Exception:
                continue

    return ""


def _search_lab_page_for_member(lab_url: str, target_name: str) -> str:
    """在实验室页面及其 members/people 子页面中搜索目标成员"""
    scraper = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }

    try:
        resp = scraper.get(lab_url, headers=headers, timeout=SCRAPE_TIMEOUT, allow_redirects=True)
        if resp.status_code == 200:
            email = _extract_email_from_html(resp.text, target_name)
            if email:
                return email

            # 从主页面提取 members/people/team 相关链接
            soup = BeautifulSoup(resp.text, 'html.parser')
            member_keywords = ['member', 'people', 'team', 'lab', 'group',
                              'student', 'personnel', 'staff', '成员', '团队']
            sub_urls = []
            for a_tag in soup.find_all('a', href=True):
                href_lower = a_tag.get('href', '').lower()
                text_lower = a_tag.get_text().lower()
                if any(kw in href_lower or kw in text_lower for kw in member_keywords):
                    full_url = urljoin(lab_url, a_tag['href'])
                    if full_url not in sub_urls:
                        sub_urls.append(full_url)

            for sub_url in sub_urls[:3]:
                try:
                    sub_resp = scraper.get(sub_url, headers=headers,
                                          timeout=SCRAPE_TIMEOUT, allow_redirects=True)
                    if sub_resp.status_code == 200:
                        email = _extract_email_from_html(sub_resp.text, target_name)
                        if email:
                            return email
                except Exception:
                    continue

    except Exception as e:
        print(f"    ⚠️ Lab 页面抓取异常: {e}")

    return ""


# ================================================================
# Layer 6: LLM 两阶段联网搜索（防幻觉架构）
# ================================================================

# URL 提取正则
URL_PATTERN = re.compile(r'https?://[^\s<>"\')\]]+')

# 无用 URL 过滤关键词
_URL_BLACKLIST = [
    'google.com/search', 'bing.com/search', 'baidu.com/s?',
    'duckduckgo.com', 'yahoo.com/search',
    '.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp',
    'pubmed.ncbi.nlm.nih.gov/?term=',
]


def _filter_urls(urls: list) -> list:
    """过滤无用 URL，保留高价值学术页面"""
    seen = set()
    filtered = []
    for url in urls:
        # 归一化：去尾部标点
        url = url.rstrip('.,;:)]\'">')
        url_lower = url.lower()
        # 黑名单过滤
        if any(bk in url_lower for bk in _URL_BLACKLIST):
            continue
        # 纯 PDF 直链跳过（通常无法抓出 mailto）
        if url_lower.endswith('.pdf'):
            continue
        # 去重
        if url_lower in seen:
            continue
        seen.add(url_lower)
        filtered.append(url)
    return filtered


def _verify_url_alive(url: str, timeout: int = 5) -> bool:
    """用 HEAD 请求快速验证 URL 是否存活"""
    try:
        resp = requests.head(url, timeout=timeout, allow_redirects=True,
                             headers={"User-Agent": "Mozilla/5.0"})
        return resp.status_code < 400
    except Exception:
        return False


def _scrape_url_for_email(url: str, target_name: str) -> str:
    """抓取单个 URL 页面并尝试提取邮箱（复用已有逻辑）"""
    try:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True},
            ssl_context=ctx
        )
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
        resp = scraper.get(url, headers=headers, timeout=SCRAPE_TIMEOUT,
                           allow_redirects=True)
        if resp.status_code == 200:
            return _extract_email_from_html(resp.text, target_name)
    except Exception as e:
        print(f"      ⚠️ 抓取异常 ({url[:50]}...): {type(e).__name__}")
    return ""


def _verify_email_on_page(url: str, email: str) -> bool:
    """回抓来源 URL，验证邮箱是否真实出现在页面上"""
    try:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True},
            ssl_context=ctx
        )
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
        resp = scraper.get(url, headers=headers, timeout=SCRAPE_TIMEOUT,
                           allow_redirects=True)
        if resp.status_code == 200:
            return email.lower() in resp.text.lower()
    except Exception:
        pass
    return False


def _llm_navigate_and_scrape(name: str, org: str, doi: str,
                              paper_title: str = "",
                              cached_domain: str = "") -> dict:
    """
    LLM 两阶段联网搜索（防幻觉架构）。

    Phase 1: 导航模式 —— LLM 只返回 URL，我们自己抓取并提取邮箱。
             从架构上杜绝邮箱幻觉。
    Phase 2: 直搜兜底 —— 强反幻觉 Prompt + 来源回抓验证。
             仅在 Phase 1 完全失败时触发。
    """
    invalid_tags = ["未提供", "未找到", "无", ""]
    org_hint = f"，在 {org} 工作" if org and org not in invalid_tags else ""
    doi_hint = f"，近期发表了 DOI:{doi} 的论文" if doi else ""
    domain_hint = f"。已知该机构的邮箱域名为 @{cached_domain}" if cached_domain else ""

    # ==============================================================
    # Phase 1: 导航模式 —— LLM 只找路，我们自己抓
    # ==============================================================
    print(f"    🧭 [Phase1] LLM 导航模式 - 搜索含邮箱的页面 URL...")

    navigate_prompt = f"""请帮我查找学术研究者 {name}{org_hint}{doi_hint}{domain_hint} 的联系方式页面。

请搜索以下类型的网页：
1. {name} 在所属大学/研究机构官网上的个人主页（Faculty Profile）
2. {name} 的 Google Scholar 学术档案页面
3. {name} 的个人学术网站或实验室页面
4. 包含 {name} 联系方式的其他学术页面

【重要规则】
- 只返回你在搜索结果中确实看到的网页 URL
- 不要返回任何邮箱地址
- 不要猜测或构造 URL
- 每个 URL 单独一行
- 如果搜索结果中没有找到相关页面，请直接回答"未找到相关页面"
"""

    phase1_email = ""
    phase1_source = ""

    try:
        raw = smart_generate_with_search(navigate_prompt)
        if raw and raw.strip() and "未找到" not in raw:
            # 从回复中提取 URL
            raw_urls = URL_PATTERN.findall(raw)
            urls = _filter_urls(raw_urls)
            print(f"    🔗 [Phase1] LLM 返回 {len(raw_urls)} 个 URL，过滤后 {len(urls)} 个")

            # 逐个 URL 抓取并提取邮箱
            for url in urls[:5]:  # 最多抓 5 个
                print(f"      🌐 抓取: {url[:70]}...")
                if not _verify_url_alive(url):
                    print(f"      ❌ URL 不可达，跳过")
                    continue
                email = _scrape_url_for_email(url, name)
                if email and _verify_email_mx(email):
                    print(f"    ✅ [Phase1] 从真实页面提取到邮箱: {email}")
                    phase1_email = email
                    phase1_source = url
                    break
                elif email:
                    print(f"      ⚠️ 提取到邮箱 {email} 但 MX 验证失败")
        else:
            print(f"    ⚠️ [Phase1] LLM 未返回有效 URL")

    except Exception as e:
        print(f"    ⚠️ [Phase1] 异常: {type(e).__name__}: {e}")

    if phase1_email:
        return {"email": phase1_email, "homepage": phase1_source,
                "source_url": phase1_source}

    # ==============================================================
    # Phase 2: 直搜兜底 —— 强反幻觉 Prompt + 来源回抓验证
    # ==============================================================
    print(f"    🔄 [Phase2] Phase1 未命中，启动直搜兜底模式...")

    direct_prompt = f"""请帮我查找学术研究者 {name}{org_hint}{doi_hint}{domain_hint} 的电子邮箱地址。

【严格规则 - 必须遵守】
1. 只返回你在搜索结果网页中明确看到、原文写出的邮箱地址
2. 严禁根据姓名拼写规律推测或构造邮箱（如 firstname.lastname@xxx.edu）
3. 必须附上你找到该邮箱的来源网页 URL
4. 如果搜索结果中没有找到任何明确写出的邮箱，请直接回答"未找到"

请按以下格式回答：
邮箱: xxx@xxx.edu
来源: https://xxx.xxx.xxx/...
（如果未找到，直接回答"未找到"）
"""

    try:
        raw = smart_generate_with_search(direct_prompt)
        if not raw or not raw.strip() or "未找到" in raw[:20]:
            print(f"    ⚠️ [Phase2] LLM 回答未找到或为空")
            return {"email": "", "homepage": "", "source_url": ""}

        # 提取邮箱
        all_emails = EMAIL_PATTERN.findall(raw)
        clean_emails = [e for e in all_emails if not _is_noise_email(e)]

        if not clean_emails:
            print(f"    ⚠️ [Phase2] LLM 回复中未检测到邮箱")
            return {"email": "", "homepage": "", "source_url": ""}

        # 提取来源 URL
        raw_urls = URL_PATTERN.findall(raw)
        source_urls = _filter_urls(raw_urls)

        # 优先选择与目标名字匹配的邮箱
        best_email = ""
        for em in clean_emails:
            if _is_strong_name_match(em, name):
                best_email = em
                break
        if not best_email:
            for em in clean_emails:
                domain = em.split("@")[-1].lower()
                if any(d in domain for d in ['.edu', '.ac.', '.org', '.gov']):
                    best_email = em
                    break
        if not best_email:
            best_email = clean_emails[0]

        print(f"    🔍 [Phase2] LLM 直搜返回邮箱: {best_email}")

        # 来源回抓验证：检查邮箱是否真实出现在声称的来源页面上
        verified = False
        verified_url = ""
        if source_urls:
            for src_url in source_urls[:3]:
                print(f"      🔎 回抓验证来源: {src_url[:60]}...")
                if _verify_email_on_page(src_url, best_email):
                    print(f"    ✅ [Phase2] 来源验证通过！邮箱确实出现在页面上")
                    verified = True
                    verified_url = src_url
                    break
            if not verified:
                print(f"    ⚠️ [Phase2] 来源验证失败：邮箱未出现在声称的页面中")

        # 根据验证结果设置不同的来源标记（影响评分基础分）
        if verified:
            # 来源验证通过 → 基础分 55（在调用方根据 source 判断）
            return {"email": best_email, "homepage": verified_url,
                    "source_url": verified_url,
                    "_phase": "phase2_verified"}
        elif source_urls:
            # 有来源但验证失败 → 基础分 40
            return {"email": best_email, "homepage": source_urls[0],
                    "source_url": source_urls[0],
                    "_phase": "phase2_unverified"}
        else:
            # 无来源 URL → 基础分 35
            return {"email": best_email, "homepage": "",
                    "source_url": "llm_search",
                    "_phase": "phase2_no_source"}

    except Exception as e:
        print(f"    ⚠️ [Phase2] 异常: {type(e).__name__}: {e}")
        return {"email": "", "homepage": "", "source_url": ""}


# ================================================================
# 评分验证
# ================================================================
def _score_email(email: str, source_layer: int, target_name: str,
                 known_org: str, all_candidates: list) -> int:
    """
    计算邮箱可信度分数。

    评分规则:
      基础分由来源层决定 (90/85/80/75/70/60)
      +10  域名与已知机构匹配
      +10  邮箱前缀与作者名字匹配
      +15  两个独立来源交叉印证同一邮箱
      +5   MX 记录验证通过
      -20  MX 记录验证失败
      -30  邮箱前缀与名字完全无关（且来源非 ORCID/Crossref）
    """
    score = LAYER_BASE_SCORES.get(source_layer, 50)

    # 域名与机构匹配
    if _domain_matches_org(email, known_org):
        score += 10

    # 名字匹配
    if _is_strong_name_match(email, target_name):
        score += 10
    elif source_layer > 2:
        prefix = email.split("@")[0].lower()
        name_parts = target_name.lower().split()
        if name_parts and not any(p in prefix for p in name_parts if len(p) >= 2):
            score -= 30

    # 多源交叉印证
    other_emails = [c["email"] for c in all_candidates
                    if c.get("email") and c.get("layer") != source_layer]
    if email in other_emails:
        score += 15

    # MX 验证
    if _verify_email_mx(email):
        score += 5
    else:
        score -= 20

    return score


def _domain_matches_org(email: str, org: str) -> bool:
    """检查邮箱域名是否与机构名匹配"""
    if not email or not org or org in ["未提供", "未找到", "无", ""]:
        return False

    domain = email.split("@")[-1].lower()
    org_lower = org.lower()

    domain_parts = domain.replace(".", " ").split()
    for part in domain_parts:
        if len(part) >= 3 and part in org_lower:
            return True

    org_words = re.split(r'[\s,;]+', org_lower)
    for word in org_words:
        if len(word) >= 4 and word in domain:
            return True

    cached = _get_cached_domain(org)
    if cached and cached == domain:
        return True

    return False


# ================================================================
# 工具函数
# ================================================================
def _parse_json_response(raw: str) -> dict:
    """从 LLM 回复中提取 JSON 对象"""
    if not raw:
        return {}

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

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

    解决场景：
    - "T. Kanneganti" 应匹配 "thirumala-devi.kanneganti@..."
    - "Li" 不应匹配 "oliver@..."
    - "Wei Wang" 应该匹配 "weiwang@..." 或 "w.wang@..."
    - "J.-P. Bhatt" 应匹配 "jp.bhatt@..."
    """
    if not email or not target_name:
        return False

    prefix = email.split("@")[0].lower()
    prefix_clean = re.sub(r'[._\-]', '', prefix)  # "thirumala-devi.kanneganti" -> "thirumaladeviikanneganti"
    prefix_parts = re.split(r'[._\-]', prefix)     # -> ["thirumala", "devi", "kanneganti"]

    # 清理名字：去掉句号，拆分
    cleaned_name = target_name.lower().replace(".", " ").replace("-", " ").strip()
    name_parts = [p for p in cleaned_name.split() if p]
    if not name_parts:
        return False

    # 检测是否含有首字母缩写（如 "T" from "T. Kanneganti"）
    initials = []
    full_parts = []
    for p in name_parts:
        if len(p) <= 1:
            initials.append(p)
        else:
            full_parts.append(p)

    # 如果全是缩写（如 "T K"），匹配非常困难，只能靠精确
    if not full_parts:
        # 全缩写：所有首字母拼起来后在前缀中出现
        abbr = "".join(initials)
        return abbr in prefix_parts or prefix_clean.startswith(abbr)

    # 单名字情况
    if len(name_parts) == 1:
        part = name_parts[0]
        if len(part) <= 3:
            return part in prefix_parts
        else:
            return part in prefix

    # 多名字情况（含可能的缩写）
    # 家族姓（通常是最后一个完整单词）
    family = full_parts[-1] if full_parts else ""
    given_parts = name_parts[:-1] if family == name_parts[-1] else name_parts

    # 策略1：家族姓必须在前缀中出现（核心条件）
    family_matched = False
    if len(family) >= 3:
        family_matched = family in prefix
    elif len(family) >= 2:
        family_matched = family in prefix_parts

    if not family_matched:
        # 家族姓都没匹配，直接失败（避免误匹配）
        # 但如果有非缩写的 given name 可以尝试反向匹配
        alternate_family = full_parts[0] if len(full_parts) > 1 else ""
        if alternate_family and len(alternate_family) >= 3 and alternate_family in prefix:
            family_matched = True
            family = alternate_family

    if not family_matched:
        return False

    # 策略2：given name 检查
    # 如果 given 全是缩写（如 "T" "J"），只要首字母在前缀中出现即可
    given_initials = [p[0] for p in given_parts if p]
    if all(len(p) <= 1 for p in given_parts):
        # given name 全缩写：有任一首字母出现在邮箱前缀中即可
        for ini in given_initials:
            if ini in prefix_clean:
                return True
        # 即便首字母没有精确出现，只要 family 名完整匹配也算
        return True
    else:
        # given name 中有完整单词
        matched_count = 0
        for part in given_parts:
            if len(part) <= 1:
                if part in prefix_clean:
                    matched_count += 1
            elif len(part) <= 3:
                if part in prefix_parts or prefix.startswith(part):
                    matched_count += 1
            else:
                if part in prefix:
                    matched_count += 1

        # family + 至少一个 given 一起匹配
        if matched_count >= 1:
            return True

        # 兜底：initials + family 组合 ("yw" + "ang" = "ywang")
        all_initials = "".join(p[0] for p in given_parts)
        if f"{all_initials}{family}" in prefix_clean or f"{family}{all_initials}" in prefix_clean:
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
        if not _is_strong_name_match(em, target_name):
            continue

        prefix = em.split("@")[0].lower()
        prefix_parts = re.split(r'[._\-]', prefix)

        score = 0
        for part in name_parts:
            if len(part) <= 2:
                if part in prefix_parts:
                    score += 2
            elif part in prefix:
                score += 1

        domain = em.split("@")[-1].lower()
        if any(d in domain for d in ['.edu', '.ac.', '.org', '.gov']):
            score += 1

        if score > best_score:
            best_score = score
            best_email = em

    return best_email if best_score > 0 else ""
