"""
DOI 解析器 —— 将 DOI 号转换为论文的结构化元数据。
三通道策略：EuropePMC 优先（生物医学最强源），Semantic Scholar 次之，Crossref 兜底。
"""
import re
import time
import requests
from urllib.parse import quote
from backend.config import HTTP_TIMEOUT, API_RATE_LIMIT_DELAY

# 邮箱正则（用于从 affiliation 中提取误放的邮箱）
EMAIL_PATTERN = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')


def resolve_doi(doi: str) -> dict:
    """
    核心入口：传入一个标准 DOI，返回论文元数据字典。
    包含：标题、期刊、作者列表（含姓名、机构、是否通讯）。
    
    返回格式:
    {
        "title": "xxx",
        "journal": "Nature",
        "authors": [
            {"name": "Xxx", "affiliations": ["MIT"], "is_corresponding": False},
            ...
        ],
        "source": "semantic_scholar" | "crossref"
    }
    """
    print(f"\n🔬 [DOI解析] 开始解析: {doi}")

    # 第零通道：EuropePMC（生物医学领域最强数据源，含通讯作者邮箱）
    epmc_result = _try_europepmc(doi)
    if epmc_result and epmc_result.get("authors"):
        print(f"  ✅ EuropePMC 命中，标题: {epmc_result.get('title', '未知')[:60]}")
        # EuropePMC 的机构信息也可能不全，用 Crossref 补
        has_missing_affs = any(not a.get("affiliations") for a in epmc_result["authors"])
        if has_missing_affs:
            print(f"  🔄 检测到部分作者机构信息缺失，尝试用 Crossref 补全...")
            epmc_result = _enrich_affiliations_from_crossref(doi, epmc_result)
        return epmc_result

    # 第一通道：Semantic Scholar（结构化数据最好）
    result = _try_semantic_scholar(doi)
    if result and result.get("authors"):
        print(f"  ✅ Semantic Scholar 命中，标题: {result.get('title', '未知')[:60]}")
        # 🔧 优化：S2 的 affiliations 经常为空，用 Crossref 补全缺失的机构信息
        has_missing_affs = any(not a.get("affiliations") for a in result["authors"])
        if has_missing_affs:
            print(f"  🔄 检测到部分作者机构信息缺失，尝试用 Crossref 补全...")
            result = _enrich_affiliations_from_crossref(doi, result)
        return result

    # 第二通道：Crossref（覆盖面更广）
    result = _try_crossref(doi)
    if result:
        print(f"  ✅ Crossref 命中，标题: {result.get('title', '未知')[:60]}")
        return result

    print(f"  ❌ DOI {doi} 在所有数据源中均未找到")
    return {"title": "未获取", "journal": "未获取", "authors": [], "source": "none"}


def _enrich_affiliations_from_crossref(doi: str, s2_result: dict) -> dict:
    """用 Crossref 的机构数据补全 S2 返回中缺失的 affiliations"""
    try:
        url = f"https://api.crossref.org/works/{doi}"
        resp = requests.get(url, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            return s2_result
        
        cr_authors = resp.json().get("message", {}).get("author", [])
        if not cr_authors:
            return s2_result
        
        # 构建 Crossref 作者名 → 映射字典
        cr_aff_map = {}
        for a in cr_authors:
            family = (a.get("family") or "").strip().lower()
            given = (a.get("given") or "").strip().lower()
            full = f"{given} {family}".strip()
            affs = [aff.get("name", "") for aff in a.get("affiliation", []) if aff.get("name")]
            orcid = a.get("ORCID", "")
            email = a.get("email", "")
            if affs or orcid or email:
                cr_aff_map[full] = {"affs": affs, "orcid": orcid, "email": email}
                if family:
                    cr_aff_map[family] = {"affs": affs, "orcid": orcid, "email": email}
        
        # 补全 S2 中缺失的机构和 ORCID
        enriched_count = 0
        for author in s2_result["authors"]:
            name_lower = author.get("name", "").strip().lower()
            
            # 找到匹配的 Crossref 记录
            found_cr = None
            if name_lower in cr_aff_map:
                found_cr = cr_aff_map[name_lower]
            else:
                parts = name_lower.split()
                if parts:
                    family = parts[-1]
                    if family in cr_aff_map:
                        found_cr = cr_aff_map[family]
            
            if found_cr:
                if not author.get("affiliations") and found_cr["affs"]:
                    author["affiliations"] = found_cr["affs"]
                    enriched_count += 1
                if found_cr["orcid"]:
                    author["orcid"] = found_cr["orcid"]
                if found_cr.get("email") and not author.get("email"):
                    author["email"] = found_cr["email"]
        
        if enriched_count:
            print(f"  ✅ Crossref 补全了 {enriched_count} 位作者的机构信息")
        else:
            print(f"  ⚠️ Crossref 未能匹配到额外的机构信息")
    except Exception as e:
        print(f"  ⚠️ Crossref 机构补全异常: {e}")
    
    return s2_result


def _try_semantic_scholar(doi: str) -> dict | None:
    """通过 Semantic Scholar API 获取论文和作者元数据"""
    print(f"  📡 [S2] 查询 Semantic Scholar...")
    try:
        paper_id = f"DOI:{doi}"
        url = (
            f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}"
            f"?fields=title,venue,authors,authors.name,authors.affiliations,authors.homepage,authors.externalIds"
        )
        resp = requests.get(url, timeout=HTTP_TIMEOUT)
        time.sleep(API_RATE_LIMIT_DELAY)
        
        if resp.status_code == 200:
            data = resp.json()
            if data and data.get("authors"):
                authors = []
                for a in data["authors"]:
                    authors.append({
                        "name": a.get("name", ""),
                        "affiliations": a.get("affiliations", []) or [],
                        "homepage": a.get("homepage", ""),
                        "orcid": a.get("externalIds", {}).get("ORCID", ""),
                        "email": "",  # S2 不提供邮箱，后续由其他层补充
                        "is_corresponding": False
                    })
                return {
                    "title": data.get("title", ""),
                    "journal": data.get("venue", ""),
                    "authors": authors,
                    "source": "semantic_scholar"
                }
        elif resp.status_code == 404:
            print(f"  ⚠️ S2 未收录此 DOI，尝试通过标题反查...")
            return _s2_fallback_by_title(doi)
        else:
            print(f"  ⚠️ S2 异常状态码: {resp.status_code}")
    except Exception as e:
        print(f"  ⚠️ S2 请求异常: {e}")
    return None


def _s2_fallback_by_title(doi: str) -> dict | None:
    """S2 主接口未收录时，先用 Crossref 拿标题，再用标题在 S2 搜索"""
    try:
        # 用 Crossref 获取标题
        cr_url = f"https://api.crossref.org/works/{doi}"
        cr_resp = requests.get(cr_url, timeout=HTTP_TIMEOUT)
        if cr_resp.status_code != 200:
            return None
        title = cr_resp.json().get("message", {}).get("title", [""])[0]
        if not title:
            return None
        
        print(f"  🔄 Crossref 拿到标题: {title[:50]}... 反查 S2...")
        s2_search_url = (
            f"https://api.semanticscholar.org/graph/v1/paper/search"
            f"?query={quote(title)}&limit=1"
            f"&fields=title,venue,authors,authors.name,authors.affiliations,authors.homepage,authors.externalIds"
        )
        s2_resp = requests.get(s2_search_url, timeout=HTTP_TIMEOUT)
        time.sleep(API_RATE_LIMIT_DELAY)
        
        if s2_resp.status_code == 200:
            s2_data = s2_resp.json()
            if s2_data.get("data") and len(s2_data["data"]) > 0:
                paper = s2_data["data"][0]
                authors = []
                for a in paper.get("authors", []):
                    authors.append({
                        "name": a.get("name", ""),
                        "affiliations": a.get("affiliations", []) or [],
                        "homepage": a.get("homepage", ""),
                        "orcid": a.get("externalIds", {}).get("ORCID", ""),
                        "email": "",
                        "is_corresponding": False
                    })
                return {
                    "title": paper.get("title", ""),
                    "journal": paper.get("venue", ""),
                    "authors": authors,
                    "source": "semantic_scholar"
                }
    except Exception as e:
        print(f"  ⚠️ S2 标题反查异常: {e}")
    return None


def _try_crossref(doi: str) -> dict | None:
    """通过 Crossref API 获取论文元数据"""
    print(f"  📡 [Crossref] 查询 Crossref API...")
    try:
        url = f"https://api.crossref.org/works/{doi}"
        resp = requests.get(url, timeout=HTTP_TIMEOUT)
        
        if resp.status_code == 200:
            msg = resp.json().get("message", {})
            title = msg.get("title", [""])[0] if msg.get("title") else ""
            journal = msg.get("container-title", [""])[0] if msg.get("container-title") else ""
            
            authors = []
            for a in msg.get("author", []):
                name_parts = []
                if a.get("given"):
                    name_parts.append(a["given"])
                if a.get("family"):
                    name_parts.append(a["family"])
                name = " ".join(name_parts)

                affs = []
                email = ""
                for aff in a.get("affiliation", []):
                    aff_name = aff.get("name", "")
                    if aff_name:
                        # 某些出版商把邮箱塞在 affiliation name 里
                        email_match = EMAIL_PATTERN.search(aff_name)
                        if email_match:
                            email = email_match.group()
                            clean_aff = EMAIL_PATTERN.sub("", aff_name).strip().strip(",; ")
                            if clean_aff:
                                affs.append(clean_aff)
                        else:
                            affs.append(aff_name)

                # Crossref 有时在 author 对象中直接提供 email
                if not email and a.get("email"):
                    email = a["email"]

                authors.append({
                    "name": name,
                    "affiliations": affs,
                    "homepage": "",
                    "orcid": a.get("ORCID", ""),
                    "email": email,  # 新增！Crossref 直接提供的邮箱
                    "is_corresponding": a.get("sequence", "") == "additional"
                })
            
            # Crossref 中 sequence="first" 的是第一作者
            if authors and not any(a["is_corresponding"] for a in authors):
                # 如果没有标记通讯作者，默认最后一个为通讯
                authors[-1]["is_corresponding"] = True
            
            return {
                "title": title,
                "journal": journal,
                "authors": authors,
                "source": "crossref"
            }
        else:
            print(f"  ⚠️ Crossref 返回状态码: {resp.status_code}")
    except Exception as e:
        print(f"  ⚠️ Crossref 请求异常: {e}")
    return None


def _try_europepmc(doi: str) -> dict | None:
    """
    通过 EuropePMC REST API 获取论文元数据。
    EuropePMC 是生物医学领域（Cell/Nature/Science/Lancet 等）的最强开放数据源，
    其结构化的 authorList 中会包含：
      - 通讯作者的明文邮箱（authorEmail 字段）
      - 作者 ORCID
      - 完整的 affiliation 列表
    """
    print(f"  📡 [EuropePMC] 查询 EuropePMC API...")
    try:
        url = (
            f"https://www.ebi.ac.uk/europepmc/webservices/rest/search"
            f"?query=DOI:{doi}&format=json&resultType=core"
        )
        resp = requests.get(url, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            print(f"  ⚠️ EuropePMC 返回状态码: {resp.status_code}")
            return None

        data = resp.json()
        results = data.get("resultList", {}).get("result", [])
        if not results:
            print(f"  ⚠️ EuropePMC 未收录此 DOI")
            return None

        paper = results[0]
        title = paper.get("title", "")
        journal = paper.get("journalTitle", "") or paper.get("journalInfo", {}).get("journal", {}).get("title", "")

        # 提取通讯作者邮箱（EuropePMC 专有优势）
        corr_email_raw = paper.get("authorEmail", "")
        # 可能有多个邮箱，用逗号/分号分隔
        corr_emails = []
        if corr_email_raw:
            for sep in [';', ',', ' ']:
                if sep in corr_email_raw:
                    corr_emails = [e.strip() for e in corr_email_raw.split(sep) if '@' in e]
                    break
            if not corr_emails and '@' in corr_email_raw:
                corr_emails = [corr_email_raw.strip()]
        if corr_emails:
            print(f"  📧 [EuropePMC] 发现通讯作者邮箱: {', '.join(corr_emails)}")

        # 提取作者列表
        author_list = paper.get("authorList", {}).get("author", [])
        if not author_list:
            print(f"  ⚠️ EuropePMC 无作者列表")
            return None

        authors = []
        for a in author_list:
            # 跳过 collectiveName（联盟/团体名）
            if a.get("collectiveName") and not a.get("lastName"):
                continue

            first_name = a.get("firstName", "") or a.get("initials", "")
            last_name = a.get("lastName", "")
            full_name = f"{first_name} {last_name}".strip()
            if not full_name:
                continue

            # 机构信息 + 从 affiliation 中提取邮箱
            affs = []
            aff_email = ""  # 从 affiliation 字段中提取的邮箱
            aff_info = a.get("authorAffiliationDetailsList", {}).get("authorAffiliation", [])
            for aff in aff_info:
                aff_name = aff.get("affiliation", "")
                if aff_name:
                    # 很多期刊把邮箱嵌在 affiliation 里，如 "... Electronic address: xxx@yyy.org."
                    found_emails = EMAIL_PATTERN.findall(aff_name)
                    if found_emails and not aff_email:
                        aff_email = found_emails[0]
                        print(f"  📧 [EuropePMC] 从机构字段提取到邮箱: {aff_email} ({full_name})")
                    # 清理邮箱和 "Electronic address:" 标记
                    clean = EMAIL_PATTERN.sub("", aff_name)
                    clean = re.sub(r'Electronic\s+address\s*:', '', clean, flags=re.IGNORECASE)
                    clean = clean.strip().strip(",;. ")
                    if clean:
                        affs.append(clean)

            # ORCID
            author_orcid = a.get("authorId", {})
            orcid = ""
            if author_orcid.get("type") == "ORCID":
                orcid = author_orcid.get("value", "")

            # 邮箱分配优先级：1. affiliation 中直接提取  2. authorEmail 字段匹配
            email = aff_email  # 直接从该作者名下的 affiliation 提取到的邮箱
            if not email and corr_emails:
                # 如果该作者名字能匹配到某个邮箱前缀，直接分配
                for ce in corr_emails:
                    prefix = ce.split("@")[0].lower()
                    name_parts = full_name.lower().split()
                    if any(p in prefix for p in name_parts if len(p) >= 2):
                        email = ce
                        break

            authors.append({
                "name": full_name,
                "affiliations": affs,
                "homepage": "",
                "orcid": orcid,
                "email": email,
                "is_corresponding": bool(aff_email)  # 有邮箱的就是通讯作者
            })

        # 如果有通讯邮箱但没分配给任何人，分配给最后一位作者（学术惯例）
        if corr_emails and not any(a["email"] for a in authors) and authors:
            authors[-1]["email"] = corr_emails[0]
            print(f"  📧 [EuropePMC] 通讯邮箱 {corr_emails[0]} 分配给末位作者: {authors[-1]['name']}")

        if not authors:
            return None

        return {
            "title": title,
            "journal": journal,
            "authors": authors,
            "source": "europepmc"
        }

    except Exception as e:
        print(f"  ⚠️ EuropePMC 请求异常: {e}")
    return None
