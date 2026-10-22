/** ContextPane — optional detail panel for tool and structured results. */

import type { ChatMessage } from '../../types'
import { stringify } from '../../services/api'

interface ContextPaneProps {
  message: ChatMessage
  onClose: () => void
}

export default function ContextPane({ message, onClose }: ContextPaneProps) {
  return (
    <aside className="context-pane" aria-label="Result details">
      <div className="context-pane__head">
        <div>
          <span className="eyebrow">Supporting details</span>
          <h2>{message.structuredResult?.display?.title ?? 'Tool result'}</h2>
        </div>
        <button
          className="icon-button"
          type="button"
          aria-label="Close result details"
          onClick={onClose}
        >
          ×
        </button>
      </div>

      {message.structuredResult?.display?.disclaimer && (
        <p className="result-disclaimer">{message.structuredResult.display.disclaimer}</p>
      )}

      {message.structuredResult?.payload !== undefined && (
        <details open>
          <summary>Structured result</summary>
          <pre>{stringify(message.structuredResult.payload)}</pre>
        </details>
      )}

      {message.toolResults?.map((result) => (
        <details key={result.tool_result_id}>
          <summary>{result.tool_name.replaceAll('_', ' ')} · {result.status}</summary>
          <p>{result.summary}</p>
          {result.result !== undefined && <pre>{stringify(result.result)}</pre>}
        </details>
      ))}
    </aside>
  )
}
