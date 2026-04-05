"""
数据模型层 —— Pydantic 模型定义，用于请求/响应的类型安全验证。
"""
from pydantic import BaseModel, Field
from typing import Optional


class AuthorInfo(BaseModel):
    """单个作者的结构化信息"""
    姓名: str = Field(default="未找到", description="作者全名")
    机构: str = Field(default="未找到", description="所属单位/实验室")
    角色: str = Field(default="未知", description="第一作者 / 通讯作者")
    邮箱: str = Field(default="未找到", description="联系邮箱")
    主页: str = Field(default="未找到", description="个人或实验室主页 URL")
    谷歌学术: str = Field(default="未找到", description="Google Scholar 主页")


class PaperResult(BaseModel):
    """单篇论文的完整挖掘结果"""
    doi: str = Field(description="DOI 编号")
    标题: str = Field(default="未获取", description="论文标题")
    期刊: str = Field(default="未获取", description="发表期刊")
    第一作者: AuthorInfo = Field(default_factory=AuthorInfo)
    通讯作者: AuthorInfo = Field(default_factory=AuthorInfo)
    状态: str = Field(default="待处理", description="处理状态")
    错误信息: Optional[str] = Field(default=None, description="如果处理失败，记录问题原因")
