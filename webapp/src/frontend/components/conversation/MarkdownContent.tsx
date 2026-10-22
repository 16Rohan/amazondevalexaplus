import { useMemo } from 'react'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

interface MarkdownContentProps {
  content: string
  className?: string
}

// Configure marked options
marked.setOptions({
  breaks: true,
  gfm: true,
})

export default function MarkdownContent({ content, className = '' }: MarkdownContentProps) {
  const sanitizedHtml = useMemo(() => {
    if (!content) return ''
    try {
      const rawHtml = marked.parse(content, { async: false }) as string
      return DOMPurify.sanitize(rawHtml, {
        ADD_ATTR: ['target', 'rel'],
      })
    } catch {
      return content
    }
  }, [content])

  return (
    <div
      className={`markdown-content ${className}`}
      dangerouslySetInnerHTML={{ __html: sanitizedHtml }}
    />
  )
}
