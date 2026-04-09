"""
ORCID 公开 API 查询模块。
通过 ORCID ID 查询研究者的公开邮箱、机构、个人页面链接。
API 文档: https://info.orcid.org/documentation/api-tutorials/

公开 API 不需要 API Key，但有速率限制（约 24 req/sec）。
"""
import requests
import re
import time

ORCID_API_BASE = "https://pub.orcid.org/v3.0"
ORCID_HEADERS = {
    "Accept": "application/json"
}
ORCID_TIMEOUT = 10

# ORCID ID 格式: 0000-0002-1234-5678
ORCID_PATTERN = re.compile(r'\d{4}-\d{4}-\d{4}-\d{3}[\dX]')


def normalize_orcid(raw: str) -> str:
    """
    从各种格式中提取标准 ORCID ID。
    支持:
      - 纯 ID: "0000-0002-1234-5678"
      - URL: "https://orcid.org/0000-0002-1234-5678"
      - 带前缀: "ORCID: 0000-0002-1234-5678"
    """
    if not raw:
        return ""
    match = ORCID_PATTERN.search(raw)
    return match.group() if match else ""


def query_orcid(orcid_id: str) -> dict:
    """
    查询 ORCID 公开 API，返回研究者的公开信息。

    Args:
        orcid_id: 标准 ORCID ID（如 "0000-0002-1234-5678"）或包含 ID 的字符串

    Returns:
        {
            "email": "xxx@xxx.edu" | "",        # 公开邮箱
            "emails": ["xxx@xxx.edu", ...],     # 所有公开邮箱
            "name": "Full Name",                # ORCID 上的名字
            "affiliations": ["MIT", ...],       # 机构列表
            "urls": ["https://...", ...],       # 个人页面URL列表
            "success": True/False               # 查询是否成功
        }
    """
    orcid = normalize_orcid(orcid_id)
    if not orcid:
        return {"email": "", "emails": [], "name": "", "affiliations": [],
                "urls": [], "success": False}

    print(f"    🔗 [ORCID] 查询 {orcid}...")
    result = {
        "email": "", "emails": [], "name": "", "affiliations": [],
        "urls": [], "success": False
    }

    try:
        # 1. 查询 person 端点（邮箱 + 名字 + 个人页面链接）
        person_data = _fetch_person(orcid)
        if person_data:
            result["name"] = person_data.get("name", "")
            result["emails"] = person_data.get("emails", [])
            result["email"] = person_data["emails"][0] if person_data.get("emails") else ""
            result["urls"] = person_data.get("urls", [])

        # 2. 查询 employments 端点（机构历史）
        affs = _fetch_employments(orcid)
        if affs:
            result["affiliations"] = affs

        result["success"] = True
        _log_result(orcid, result)

    except Exception as e:
        print(f"    ⚠️ [ORCID] 查询异常: {e}")

    return result


def _fetch_person(orcid: str) -> dict:
    """获取 ORCID person 数据（邮箱、名字、研究者链接）"""
    try:
        url = f"{ORCID_API_BASE}/{orcid}/person"
        resp = requests.get(url, headers=ORCID_HEADERS, timeout=ORCID_TIMEOUT)
        if resp.status_code != 200:
            print(f"    ⚠️ [ORCID] person 端点返回 {resp.status_code}")
            return {}

        data = resp.json()
        result = {"name": "", "emails": [], "urls": []}

        # 提取名字
        name_data = data.get("name", {})
        if name_data:
            given = name_data.get("given-names", {}).get("value", "")
            family = name_data.get("family-name", {}).get("value", "")
            result["name"] = f"{given} {family}".strip()

        # 提取公开邮箱
        emails_data = data.get("emails", {}).get("email", [])
        for em in emails_data:
            email_val = em.get("email", "")
            if email_val:
                result["emails"].append(email_val)

        # 提取 researcher-urls（个人页面链接）
        urls_data = data.get("researcher-urls", {}).get("researcher-url", [])
        for u in urls_data:
            url_val = u.get("url", {}).get("value", "")
            if url_val:
                result["urls"].append(url_val)

        return result

    except Exception as e:
        print(f"    ⚠️ [ORCID] person 请求异常: {e}")
        return {}


def _fetch_employments(orcid: str) -> list:
    """获取 ORCID 就业/机构历史"""
    try:
        url = f"{ORCID_API_BASE}/{orcid}/employments"
        resp = requests.get(url, headers=ORCID_HEADERS, timeout=ORCID_TIMEOUT)
        if resp.status_code != 200:
            return []

        data = resp.json()
        affiliations = []

        # employments 结构: affiliation-group -> summaries -> employment-summary
        for group in data.get("affiliation-group", []):
            for summary in group.get("summaries", []):
                emp = summary.get("employment-summary", {})
                org = emp.get("organization", {})
                org_name = org.get("name", "")
                if org_name and org_name not in affiliations:
                    affiliations.append(org_name)

        return affiliations

    except Exception as e:
        print(f"    ⚠️ [ORCID] employments 请求异常: {e}")
        return []


def _log_result(orcid: str, result: dict):
    """打印 ORCID 查询结果摘要"""
    if result["email"]:
        print(f"    ✅ [ORCID] 找到公开邮箱: {result['email']}")
    elif result["urls"]:
        print(f"    📎 [ORCID] 无公开邮箱，但有 {len(result['urls'])} 个页面链接")
    elif result["affiliations"]:
        print(f"    🏛️ [ORCID] 无邮箱/链接，但有机构: {result['affiliations'][0]}")
    else:
        print(f"    ⚠️ [ORCID] {orcid} 信息极少（可能未公开）")
