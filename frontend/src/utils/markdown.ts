import DOMPurify from 'dompurify'
import MarkdownIt from 'markdown-it'

/** 将后端返回的 Markdown 转为可安全注入的展示 HTML。 */
const markdown = new MarkdownIt({
  html: false,
  breaks: true,
  linkify: true,
})

export function renderMarkdown(content: string): string {
  return DOMPurify.sanitize(markdown.render(content))
}
