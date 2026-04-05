"""
Scholar Agent V2 —— FastAPI 后端入口。
全栈一体架构：既提供 API 接口，又 serve 前端静态文件。
"""
import os
import sys
import json
import time
import asyncio
import uuid
import pandas as pd
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# 确保 backend 包可导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.services.document_parser import parse_user_input, extract_dois_from_text
from backend.services.doi_resolver import resolve_doi
from backend.services.author_extractor import extract_authors
from backend.services.email_finder import find_email, find_email_for_paper

# ================================================================
# FastAPI 应用初始化
# ================================================================
app = FastAPI(title="Scholar Agent", description="学术论文作者信息与邮箱智能挖掘系统")

# 前端静态文件目录（项目根目录下的 frontend/）
PROJECT_ROOT = Path(__file__).parent.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"
EXPORTS_DIR = PROJECT_ROOT / "exports"
UPLOADS_DIR = PROJECT_ROOT / "uploads"

# 确保目录存在
EXPORTS_DIR.mkdir(exist_ok=True)
UPLOADS_DIR.mkdir(exist_ok=True)


# ================================================================
# SSE 事件流推送工具
# ================================================================
def sse_event(event: str, data: dict) -> str:
    """构造 SSE 事件字符串"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ================================================================
# 核心 API：DOI 提取 + 全流水线处理（SSE 流式推送）
# ================================================================
@app.post("/api/extract")
async def extract_from_input(
    text: str = Form(default=""),
    file: Optional[UploadFile] = File(default=None)
):
    """
    主接口：接收用户的文本输入和/或文件上传，
    提取 DOI 并执行完整的 作者→邮箱 挖掘流水线。
    通过 SSE 实时推送每一步的进度。
    """
    # 处理文件上传
    file_path = ""
    if file and file.filename:
        file_path = str(UPLOADS_DIR / file.filename)
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)

    async def event_stream():
        try:
            # 步骤 1：提取 DOI
            yield sse_event("progress", {"step": "提取DOI", "status": "进行中", "detail": "正在从输入中解析 DOI..."})
            
            dois = await asyncio.to_thread(parse_user_input, text, file_path)
            
            if not dois:
                yield sse_event("error", {"message": "未能从输入中提取到任何有效的 DOI 编号。请检查输入格式。"})
                return
            
            yield sse_event("progress", {"step": "提取DOI", "status": "完成", "detail": f"共提取到 {len(dois)} 个 DOI", "dois": dois})
            
            all_results = []
            
            # 步骤 2-4：逐个 DOI 处理
            for idx, doi in enumerate(dois):
                prefix = f"[{idx+1}/{len(dois)}]"
                
                # 2. 解析论文元数据
                yield sse_event("progress", {"step": "解析论文", "status": "进行中", "detail": f"{prefix} 正在查询 {doi}...", "current": idx+1, "total": len(dois)})
                
                paper = await asyncio.to_thread(resolve_doi, doi)
                title = paper.get("title", "未获取")
                journal = paper.get("journal", "未获取")
                
                yield sse_event("progress", {"step": "解析论文", "status": "完成", "detail": f"{prefix} {title[:50]}... ({journal})"})
                
                # 3. 提取作者信息
                yield sse_event("progress", {"step": "提取作者", "status": "进行中", "detail": f"{prefix} 正在识别第一作者和通讯作者..."})
                
                author_data = await asyncio.to_thread(extract_authors, paper)
                first_author = author_data.get("第一作者", {})
                corr_author = author_data.get("通讯作者", {})
                
                yield sse_event("progress", {"step": "提取作者", "status": "完成", "detail": f"{prefix} 一作: {first_author.get('姓名')} | 通讯: {corr_author.get('姓名')}"})
                
                # 4. 搜索邮箱——通讯作者（优先，因为可以从论文页面直接抓到）
                yield sse_event("progress", {"step": "搜索邮箱", "status": "进行中", "detail": f"{prefix} 正在搜索通讯作者邮箱（含论文页面抓取）..."})
                
                corr_name = corr_author.get("姓名", "")
                corr_org = corr_author.get("机构", "")
                corr_email_data = {"邮箱": "未找到", "主页": "未找到", "谷歌学术": "未找到"}
                if corr_name and corr_name != "未找到":
                    corr_email_data = await asyncio.to_thread(find_email_for_paper, doi, corr_name, corr_org, "通讯")
                
                # 5. 搜索邮箱——第一作者
                yield sse_event("progress", {"step": "搜索邮箱", "status": "进行中", "detail": f"{prefix} 正在搜索第一作者邮箱..."})
                await asyncio.sleep(1)  # 防限流
                
                first_name = first_author.get("姓名", "")
                first_org = first_author.get("机构", "")
                first_email_data = {"邮箱": "未找到", "主页": "未找到", "谷歌学术": "未找到"}
                if first_name and first_name != "未找到":
                    first_email_data = await asyncio.to_thread(find_email_for_paper, doi, first_name, first_org, "一作")
                
                # 组装该 DOI 的结果
                result = {
                    "doi": doi,
                    "标题": title,
                    "期刊": journal,
                    "第一作者": first_name,
                    "一作机构": first_org,
                    "一作邮箱": first_email_data.get("邮箱", "未找到"),
                    "一作主页": first_email_data.get("主页", "未找到"),
                    "通讯作者": corr_name,
                    "通讯机构": corr_org,
                    "通讯邮箱": corr_email_data.get("邮箱", "未找到"),
                    "通讯主页": corr_email_data.get("主页", "未找到"),
                }
                all_results.append(result)
                
                # 发送单条结果
                yield sse_event("result", result)
                yield sse_event("progress", {"step": "搜索邮箱", "status": "完成", "detail": f"{prefix} 处理完成", "current": idx+1, "total": len(dois)})
            
            # 步骤 5：生成导出文件
            if all_results:
                export_id = str(uuid.uuid4())[:8]
                csv_path = EXPORTS_DIR / f"scholar_results_{export_id}.csv"
                xlsx_path = EXPORTS_DIR / f"scholar_results_{export_id}.xlsx"
                
                df = pd.DataFrame(all_results)
                df.to_csv(str(csv_path), index=False, encoding='utf-8-sig')
                df.to_excel(str(xlsx_path), index=False)
                
                yield sse_event("complete", {
                    "total": len(all_results),
                    "csv_file": f"/api/download/{csv_path.name}",
                    "xlsx_file": f"/api/download/{xlsx_path.name}",
                    "results": all_results
                })
            else:
                yield sse_event("complete", {"total": 0, "results": []})
                
        except Exception as e:
            yield sse_event("error", {"message": f"处理过程中发生内部错误: {str(e)}"})
        finally:
            # 清理上传的临时文件
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except:
                    pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


# ================================================================
# 文件下载接口
# ================================================================
@app.get("/api/download/{filename}")
async def download_file(filename: str):
    """下载导出的 CSV/Excel 文件"""
    file_path = EXPORTS_DIR / filename
    if not file_path.exists():
        return JSONResponse(status_code=404, content={"error": "文件不存在"})
    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type="application/octet-stream"
    )


# ================================================================
# 前端页面 serve
# ================================================================
@app.get("/")
async def serve_index():
    """返回前端主页"""
    index_path = FRONTEND_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(content=index_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Scholar Agent</h1><p>前端文件未找到，请检查 frontend/ 目录。</p>")


# 挂载静态文件（CSS, JS 等）
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ================================================================
# 启动入口
# ================================================================
if __name__ == "__main__":
    import uvicorn
    print("🚀 Scholar Agent V2 服务启动中...")
    print(f"   前端目录: {FRONTEND_DIR}")
    print(f"   导出目录: {EXPORTS_DIR}")
    print(f"   访问地址: http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
