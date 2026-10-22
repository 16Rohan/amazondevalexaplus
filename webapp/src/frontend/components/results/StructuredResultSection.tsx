/** StructuredResultSection — inline structured result display. */

import type { StructuredResult } from '../../types'

interface StructuredResultSectionProps {
  result: StructuredResult
  hideProvenance?: boolean
}

export default function StructuredResultSection({ result, hideProvenance = false }: StructuredResultSectionProps) {
  if (!result.display) return null

  const statusLabel = result.status === 'available'
    ? 'Verified record'
    : result.status === 'partial'
      ? 'Guidance note'
      : result.status === 'unavailable'
        ? 'Not found'
        : (result.status ?? 'Result').replaceAll('_', ' ')

  return (
    <section className="structured-result">
      <div>
        <span className="eyebrow">Result</span>
        <h3>{result.display.title ?? 'Support result'}</h3>
      </div>

      <span className={`tool-state tool-state--${result.status ?? 'available'}`}>
        {statusLabel}
      </span>

      <p>{result.display.summary}</p>

      {!hideProvenance && (
        <div className="provenance-tags">
          {result.provenance?.source_type === 'mcp' && (
            <span className="provenance-tag">MCP tool executed</span>
          )}
          {result.provenance?.is_simulated && (
            <span className="provenance-tag provenance-tag--demo">Demo data</span>
          )}
          {result.provenance?.source_label && (
            <span className="provenance-source">{result.provenance.source_label}</span>
          )}
        </div>
      )}

      {result.display.disclaimer && (
        <p className="result-disclaimer">{result.display.disclaimer}</p>
      )}
    </section>
  )
}
