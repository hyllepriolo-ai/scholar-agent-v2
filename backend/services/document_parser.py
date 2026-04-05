"""
文档预处理清洗器 —— 多格式 DOI 提取漏斗。
支持：纯文本粘贴、PDF 文件、XML 文件中批量正则提取 DOI。
"""
import re
import os

# 学术 DOI 标准正则（覆盖绝大多数出版商的 DOI 格式，排除非ASCII字符）
DOI_PATTERN = re.compile(
    r'\b(10\.\d{4,9}/[a-zA-Z0-9./_()\-:;]+)',
    re.IGNORECASE
)


def extract_dois_from_text(text: str) -> list[str]:
    """
    从任意纯文本中正则提取所有合法 DOI。
    自动去重并清理尾部常见脏字符（句号、逗号等）。
    """
    raw_matches = DOI_PATTERN.findall(text)
    cleaned = []
    seen = set()
    for doi in raw_matches:
        # 清理尾部的标点噪音（含中文标点）
        doi = doi.rstrip('.;,)]\'"，。；）】》')
        doi_lower = doi.lower()
        if doi_lower not in seen:
            seen.add(doi_lower)
            cleaned.append(doi)
    return cleaned


def extract_dois_from_pdf(file_path: str) -> list[str]:
    """
    从 PDF 文件中提取所有 DOI。
    使用 PyMuPDF 进行精准文本萃取。
    """
    try:
        import pymupdf
        text_chunks = []
        with pymupdf.open(file_path) as doc:
            for page in doc:
                text_chunks.append(page.get_text())
        full_text = "\n".join(text_chunks)
        print(f"  📄 PDF 解析完成，共提取 {len(full_text)} 字符文本")
        return extract_dois_from_text(full_text)
    except Exception as e:
        print(f"  ⚠️ PDF 解析失败: {e}")
        return []


def extract_dois_from_xml(file_path: str) -> list[str]:
    """
    从 XML 文件中提取所有 DOI。
    直接读取原始文本进行正则匹配（不依赖 XML 解析器，兼容畸形文件）。
    """
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        print(f"  📄 XML 读取完成，共 {len(content)} 字符")
        return extract_dois_from_text(content)
    except Exception as e:
        print(f"  ⚠️ XML 读取失败: {e}")
        return []


def extract_dois_from_file(file_path: str) -> list[str]:
    """
    根据文件扩展名自动选择合适的解析器。
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext == '.pdf':
        return extract_dois_from_pdf(file_path)
    elif ext in ('.xml', '.html', '.htm'):
        return extract_dois_from_xml(file_path)
    else:
        # 尝试按纯文本处理（csv, txt, bib 等）
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            return extract_dois_from_text(content)
        except Exception as e:
            print(f"  ⚠️ 文件读取失败: {e}")
            return []


def parse_user_input(text: str = "", file_path: str = "") -> list[str]:
    """
    统一入口：从用户输入（文本框 + 可选文件上传）中提取所有 DOI。
    合并去重后返回标准化的 DOI 清单。
    """
    all_dois = []

    if text.strip():
        text_dois = extract_dois_from_text(text)
        print(f"  🔍 从文本输入中提取到 {len(text_dois)} 个 DOI")
        all_dois.extend(text_dois)

    if file_path and os.path.exists(file_path):
        file_dois = extract_dois_from_file(file_path)
        print(f"  🔍 从文件中提取到 {len(file_dois)} 个 DOI")
        all_dois.extend(file_dois)

    # 最终全局去重
    seen = set()
    unique = []
    for doi in all_dois:
        if doi.lower() not in seen:
            seen.add(doi.lower())
            unique.append(doi)

    print(f"  ✅ 最终提取 {len(unique)} 个独立 DOI: {unique}")
    return unique
