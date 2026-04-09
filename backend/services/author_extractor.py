"""
作者提取器 —— 从 DOI 解析结果中精准定位第一作者和通讯作者。
对大型协作论文（>20人），强制走 LLM 分析或论文页面抓取。
"""
import json
import re
import time
import requests
import cloudscraper
from bs4 import BeautifulSoup
from backend.config import smart_generate, HTTP_TIMEOUT


def extract_authors(paper_metadata: dict) -> dict:
    """
    核心入口：从 doi_resolver 返回的元数据中提取第一作者和通讯作者。
    """
    authors = paper_metadata.get("authors", [])
    
    if not authors:
        print("  ⚠️ 元数据中无作者信息")
        return {
            "第一作者": {"姓名": "未找到", "机构": "未找到"},
            "通讯作者": {"姓名": "未找到", "机构": "未找到"}
        }
    
    # 第一作者总是确定的：列表第一个
    first = authors[0]
    first_name = first.get("name", "未找到")
    first_org = ", ".join(first.get("affiliations", [])) or "未找到"
    first_homepage = first.get("homepage", "")
    first_orcid = first.get("orcid", "")
    first_email = first.get("email", "")
    
    # 通讯作者识别策略分支
    if len(authors) > 20:
        # 大型协作论文（如 Nature 500人联名），不能简单取最后一个
        print(f"  ⚠️ 发现超大型协作论文（{len(authors)} 位作者），启动智能通讯作者识别...")
        corr = _identify_corresponding_large_paper(paper_metadata, authors)
    else:
        # 常规论文：先查标记，没有就取最后一个
        corr = _identify_corresponding_normal(authors)
    
    result = {
        "第一作者": {"姓名": first_name, "机构": first_org, "主页": first_homepage,
                    "orcid": first_orcid, "crossref_email": first_email},
        "通讯作者": corr
    }
    
    print(f"  📋 作者识别完成:")
    print(f"     第一作者: {result['第一作者']['姓名']} ({result['第一作者']['机构']})")
    print(f"     通讯作者: {result['通讯作者']['姓名']} ({result['通讯作者']['机构']})")
    
    return result


def _identify_corresponding_normal(authors: list) -> dict:
    """常规论文：从结构化数据中提取通讯作者"""
    # 先查显式标记
    for a in authors:
        if a.get("is_corresponding"):
            return {
                "姓名": a.get("name", "未找到"),
                "机构": ", ".join(a.get("affiliations", [])) or "未找到",
                "主页": a.get("homepage", ""),
                "orcid": a.get("orcid", ""),
                "crossref_email": a.get("email", "")  # 新增
            }
    # 默认取最后一个（学术界惯例）
    last = authors[-1]
    return {
        "姓名": last.get("name", "未找到"),
        "机构": ", ".join(last.get("affiliations", [])) or "未找到",
        "主页": last.get("homepage", ""),
        "orcid": last.get("orcid", ""),
        "crossref_email": last.get("email", "")  # 新增
    }


def _identify_corresponding_large_paper(paper_metadata: dict, authors: list) -> dict:
    """
    大型协作论文通讯作者识别：
    1. 先尝试从论文 DOI 页面抓取 'corresponding author' 标记
    2. 失败则用 LLM 分析作者列表头尾 + 论文标题
    """
    # 策略 1: 尝试从 Crossref 元数据获取（Crossref 有时标记 sequence）
    for a in authors:
        if a.get("is_corresponding"):
            return {
                "姓名": a.get("name", "未找到"),
                "机构": ", ".join(a.get("affiliations", [])) or "未找到",
                "orcid": a.get("orcid", ""),
                "crossref_email": a.get("email", "")  # 新增
            }
    
    # 策略 2: 用 LLM 分析（只传前5个和后5个作者，避免 token 爆炸）
    head = authors[:5]
    tail = authors[-5:]
    slim_authors = {
        "论文标题": paper_metadata.get("title", ""),
        "期刊": paper_metadata.get("journal", ""),
        "前5位作者": [{"name": a.get("name"), "affiliations": a.get("affiliations", [])} for a in head],
        "后5位作者": [{"name": a.get("name"), "affiliations": a.get("affiliations", [])} for a in tail],
        "总作者数": len(authors)
    }
    
    prompt = f"""
    这是一篇有 {len(authors)} 位作者的大型协作论文。
    论文标题: {paper_metadata.get('title', '未知')}
    
    在学术界，大型协作论文（如 Nature 联名论文）的通讯作者通常是：
    - 论文中标记为 corresponding/senior 的人
    - 或者是最后几位作者中的资深教授/PI（Principal Investigator）
    - 第一作者有时也兼任通讯
    
    以下是作者列表的头部和尾部：
    {json.dumps(slim_authors, ensure_ascii=False, indent=2)}
    
    请推断最可能的通讯作者（从后5位中选一个最可能的资深学者）。
    
    强制返回纯净 JSON（不要 ```json）：
    {{"姓名": "xxx", "机构": "yyy"}}
    """
    try:
        text_resp = smart_generate(prompt)
        text_resp = text_resp.replace('```json', '').replace('```', '').strip()
        result = json.loads(text_resp)
        if result.get("姓名"):
            result["crossref_email"] = ""  # LLM 分析无法提供 email
            return result
    except Exception as e:
        print(f"  ⚠️ LLM 通讯作者识别失败: {e}")
    
    # 兜底：取倒数第二个（大型论文最后一个常是整理人而非PI）
    fallback = authors[-2] if len(authors) > 1 else authors[-1]
    return {
        "姓名": fallback.get("name", "未找到"),
        "机构": ", ".join(fallback.get("affiliations", [])) or "未找到",
        "orcid": fallback.get("orcid", ""),
        "crossref_email": fallback.get("email", "")  # 新增
    }

