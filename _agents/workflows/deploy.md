---
description: 把改好的前后端代码部署上线到 Render
---

# 部署 Scholar Agent 到 Render

当修改完前端或者后端代码，我们需要把代码提交到 GitHub，Render 平台会自动感应到更新并自动重新部署上线。

请遵循以下 3 步来无脑发布：

// turbo-all
1. 添加所有更改
`git add .`

2. 提交更改
`git commit -m "update features"`

3. 推送到远端
`git push origin master`

一切完成后，告诉用户：“代码已推送，Render 会在后台自动完成构建，只需稍等 2-3 分钟刷新网页即可见效”。
