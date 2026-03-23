# summarize

使用 `trafilatura` 提取网页正文并生成智能摘要。

## 使用方法

1. **总结网页**：
   提供一个 URL，我将使用 `trafilatura` 抓取正文并为您总结。
   示例：`总结这个网页：https://example.com`

2. **核心逻辑**：
   - 调用 `trafilatura --markdown -u <URL>` 获取内容。
   - 使用 AI 对抓取到的 Markdown 内容进行提炼。

## 依赖
- `trafilatura` (已安装)
