/** ReasoningSection — expandable disclosure panel for model reasoning. */

import React from 'react'

interface ReasoningSectionProps {
  reasoning: string
  status?: string
}

function renderSafeMarkdown(text: string) {
  // Split into lines/blocks safely without raw HTML
  const lines = text.split('\n')
  const elements: React.ReactNode[] = []
  let inCodeBlock = false
  let codeBuffer: string[] = []

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]

    if (line.startsWith('```')) {
      if (inCodeBlock) {
        elements.push(
          <pre key={`code-${i}`} className="reasoning-code-block">
            <code>{codeBuffer.join('\n')}</code>
          </pre>
        )
        codeBuffer = []
        inCodeBlock = false
      } else {
        inCodeBlock = true
      }
      continue
    }

    if (inCodeBlock) {
      codeBuffer.push(line)
      continue
    }

    const trimmed = line.trim()
    if (!trimmed) continue

    // Bullet items
    if (trimmed.startsWith('- ') || trimmed.startsWith('* ')) {
      elements.push(
        <li key={`li-${i}`} className="reasoning-list-item">
          {formatInline(trimmed.slice(2))}
        </li>
      )
      continue
    }

    // Numbered list
    const numMatch = trimmed.match(/^(\d+)\.\s+(.*)$/)
    if (numMatch) {
      elements.push(
        <li key={`num-${i}`} className="reasoning-list-item">
          <strong>{numMatch[1]}.</strong> {formatInline(numMatch[2])}
        </li>
      )
      continue
    }

    // Paragraph
    elements.push(
      <p key={`p-${i}`} className="reasoning-paragraph">
        {formatInline(trimmed)}
      </p>
    )
  }

  if (inCodeBlock && codeBuffer.length > 0) {
    elements.push(
      <pre key="code-final" className="reasoning-code-block">
        <code>{codeBuffer.join('\n')}</code>
      </pre>
    )
  }

  return elements
}

function formatInline(text: string): React.ReactNode {
  // Parse bold **text** and inline `code`
  const parts = text.split(/(\*\*.*?\*\*|`.*?`)/g)
  return parts.map((part, index) => {
    if (part.startsWith('**') && part.endsWith('**') && part.length >= 4) {
      return <strong key={index}>{part.slice(2, -2)}</strong>
    }
    if (part.startsWith('`') && part.endsWith('`') && part.length >= 2) {
      return <code key={index} className="reasoning-inline-code">{part.slice(1, -1)}</code>
    }
    return part
  })
}

export default function ReasoningSection({ reasoning, status }: ReasoningSectionProps) {
  if (!reasoning || !reasoning.trim()) return null

  return (
    <details className="reasoning-panel" open={status === 'in_progress'}>
      <summary className="reasoning-panel__summary">
        <span className="reasoning-panel__indicator" aria-hidden="true">💡</span>
        <span className="reasoning-panel__title">Model reasoning</span>
        <span className="reasoning-panel__badge">Model Reasoning</span>
      </summary>

      <div className="reasoning-panel__content" aria-label="Model reasoning content">
        {renderSafeMarkdown(reasoning)}
      </div>
    </details>
  )
}
