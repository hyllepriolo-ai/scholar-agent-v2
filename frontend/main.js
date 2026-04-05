/**
 * Scholar Agent — 前端交互逻辑
 * 处理表单提交、文件上传拖放、SSE 流式进度展示、结果渲染
 */

// ================================================================
// DOM 元素
// ================================================================
const searchForm = document.getElementById('searchForm');
const doiInput = document.getElementById('doiInput');
const fileInput = document.getElementById('fileInput');
const fileUploadArea = document.getElementById('fileUploadArea');
const uploadContent = document.getElementById('uploadContent');
const uploadFileName = document.getElementById('uploadFileName');
const submitBtn = document.getElementById('submitBtn');

const progressPanel = document.getElementById('progressPanel');
const progressCount = document.getElementById('progressCount');
const progressBarFill = document.getElementById('progressBarFill');
const progressLog = document.getElementById('progressLog');

const resultsPanel = document.getElementById('resultsPanel');
const resultsGrid = document.getElementById('resultsGrid');
const exportButtons = document.getElementById('exportButtons');
const downloadCsv = document.getElementById('downloadCsv');
const downloadXlsx = document.getElementById('downloadXlsx');

const errorPanel = document.getElementById('errorPanel');
const errorMessage = document.getElementById('errorMessage');

// ================================================================
// 文件上传拖放
// ================================================================
let selectedFile = null;

fileUploadArea.addEventListener('click', () => fileInput.click());

fileInput.addEventListener('change', (e) => {
    if (e.target.files.length > 0) {
        selectedFile = e.target.files[0];
        showFileName(selectedFile.name);
    }
});

fileUploadArea.addEventListener('dragover', (e) => {
    e.preventDefault();
    fileUploadArea.classList.add('drag-over');
});

fileUploadArea.addEventListener('dragleave', () => {
    fileUploadArea.classList.remove('drag-over');
});

fileUploadArea.addEventListener('drop', (e) => {
    e.preventDefault();
    fileUploadArea.classList.remove('drag-over');
    if (e.dataTransfer.files.length > 0) {
        selectedFile = e.dataTransfer.files[0];
        fileInput.files = e.dataTransfer.files;
        showFileName(selectedFile.name);
    }
});

function showFileName(name) {
    uploadContent.style.display = 'none';
    uploadFileName.style.display = 'flex';
    uploadFileName.innerHTML = `
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/>
            <polyline points="13 2 13 9 20 9"/>
        </svg>
        ${name}
        <span style="cursor:pointer;color:var(--error);margin-left:8px" onclick="clearFile(event)">✕</span>
    `;
}

function clearFile(e) {
    if (e) e.stopPropagation();
    selectedFile = null;
    fileInput.value = '';
    uploadContent.style.display = 'flex';
    uploadFileName.style.display = 'none';
}

// ================================================================
// 表单提交 + SSE 流处理
// ================================================================
searchForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    
    const text = doiInput.value.trim();
    if (!text && !selectedFile) {
        doiInput.focus();
        doiInput.style.borderColor = 'var(--error)';
        setTimeout(() => doiInput.style.borderColor = '', 1500);
        return;
    }
    
    // UI 切换到加载状态
    setLoading(true);
    showPanel('progress');
    clearProgress();
    clearResults();
    
    // 构建 FormData
    const formData = new FormData();
    formData.append('text', text);
    if (selectedFile) {
        formData.append('file', selectedFile);
    }
    
    try {
        const response = await fetch('/api/extract', {
            method: 'POST',
            body: formData
        });
        
        if (!response.ok) {
            throw new Error(`服务器错误: ${response.status}`);
        }
        
        // 读取 SSE 流
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            
            buffer += decoder.decode(value, { stream: true });
            
            // 解析 SSE 事件
            const lines = buffer.split('\n');
            buffer = '';
            
            let currentEvent = '';
            let currentData = '';
            
            for (const line of lines) {
                if (line.startsWith('event: ')) {
                    currentEvent = line.slice(7);
                } else if (line.startsWith('data: ')) {
                    currentData = line.slice(6);
                    try {
                        const data = JSON.parse(currentData);
                        handleSSEEvent(currentEvent, data);
                    } catch (err) {
                        // 不完整的 JSON，存回 buffer
                    }
                    currentEvent = '';
                    currentData = '';
                } else if (line === '') {
                    // 事件结束标记
                } else {
                    buffer += line + '\n';
                }
            }
        }
    } catch (err) {
        showError(err.message || '网络请求失败，请检查后端是否正在运行。');
    } finally {
        setLoading(false);
    }
});

// ================================================================
// SSE 事件处理
// ================================================================
function handleSSEEvent(event, data) {
    switch (event) {
        case 'progress':
            addProgressLog(data);
            if (data.current && data.total) {
                progressCount.textContent = `${data.current}/${data.total}`;
                const pct = Math.round((data.current / data.total) * 100);
                progressBarFill.style.width = data.status === '完成' ? `${pct}%` : `${Math.max(pct - 15, 5)}%`;
            }
            break;
            
        case 'result':
            showPanel('results');
            addResultCard(data);
            break;
            
        case 'complete':
            progressBarFill.style.width = '100%';
            addProgressLog({ step: '完成', status: '完成', detail: `全部 ${data.total} 篇论文处理完毕` });
            
            if (data.csv_file) {
                exportButtons.style.display = 'flex';
                downloadCsv.href = data.csv_file;
                downloadXlsx.href = data.xlsx_file;
            }
            break;
            
        case 'error':
            showError(data.message);
            break;
    }
}

// ================================================================
// UI 辅助函数
// ================================================================
function setLoading(loading) {
    submitBtn.disabled = loading;
    submitBtn.querySelector('.btn-text').style.display = loading ? 'none' : 'inline';
    submitBtn.querySelector('.btn-loader').style.display = loading ? 'inline' : 'none';
}

function showPanel(name) {
    if (name === 'progress') {
        progressPanel.style.display = 'block';
        errorPanel.style.display = 'none';
    }
    if (name === 'results') {
        resultsPanel.style.display = 'block';
    }
}

function showError(msg) {
    errorPanel.style.display = 'block';
    errorMessage.textContent = msg;
    progressPanel.style.display = 'none';
    setLoading(false);
}

function resetUI() {
    progressPanel.style.display = 'none';
    resultsPanel.style.display = 'none';
    errorPanel.style.display = 'none';
    exportButtons.style.display = 'none';
    clearProgress();
    clearResults();
    doiInput.value = '';
    clearFile();
    setLoading(false);
}

function clearProgress() {
    progressLog.innerHTML = '';
    progressBarFill.style.width = '0%';
    progressCount.textContent = '0/0';
}

function clearResults() {
    resultsGrid.innerHTML = '';
}

function addProgressLog(data) {
    const item = document.createElement('div');
    item.className = `log-item ${data.status === '完成' ? 'complete' : ''}`;
    item.innerHTML = `<span class="dot"></span><span>${data.detail || data.step}</span>`;
    progressLog.appendChild(item);
    progressLog.scrollTop = progressLog.scrollHeight;
}

function addResultCard(data) {
    const card = document.createElement('div');
    card.className = 'result-card';
    card.style.animationDelay = `${resultsGrid.children.length * 80}ms`;
    
    const emailHtml = (email, homepage) => {
        const isFound = email && email !== '未找到';
        let html = `<div class="author-block-email ${isFound ? '' : 'not-found'}">${isFound ? email : '暂未找到'}</div>`;
        if (homepage && homepage !== '未找到') {
            html += `<a class="author-block-link" href="${homepage}" target="_blank" rel="noopener">查看主页 →</a>`;
        }
        return html;
    };
    
    card.innerHTML = `
        <div class="result-card-header">
            <div class="result-card-title">${data.标题 || '未获取标题'}</div>
            <div class="result-card-meta">
                <span class="doi">${data.doi}</span>
                <span>${data.期刊 || ''}</span>
            </div>
        </div>
        <div class="result-card-body">
            <div class="author-block">
                <div class="author-block-label">第一作者</div>
                <div class="author-block-name">${data.第一作者 || '未知'}</div>
                <div class="author-block-org">${data.一作机构 || ''}</div>
                ${emailHtml(data.一作邮箱, data.一作主页)}
            </div>
            <div class="author-block">
                <div class="author-block-label">通讯作者</div>
                <div class="author-block-name">${data.通讯作者 || '未知'}</div>
                <div class="author-block-org">${data.通讯机构 || ''}</div>
                ${emailHtml(data.通讯邮箱, data.通讯主页)}
            </div>
        </div>
    `;
    
    resultsGrid.appendChild(card);
}
