/** ToolResultsSection — displays tool results with provenance tags. */

import type { Data, ToolResult } from '../../types'
import { resultIsSimulated } from '../../services/api'

interface ToolResultsSectionProps {
  results: ToolResult[]
}

export default function ToolResultsSection({ results }: ToolResultsSectionProps) {
  return (
    <div className="tool-results" aria-label="Tool results">
      {results.map((result) => (
        <section className="tool-result" key={result.tool_result_id}>
          <div className="tool-result__head">
            <strong>{result.tool_name.replaceAll('_', ' ')}</strong>
            <div className="tool-result__states">
              <span className={`tool-state tool-state--${result.status}`}>
                Tool: {result.status.replaceAll('_', ' ')}
              </span>
              {result.result !== null &&
                result.result !== undefined &&
                typeof result.result === 'object' &&
                typeof (result.result as Data).status === 'string' && (
                  <span className="domain-state">
                    Result: {String((result.result as Data).status).replaceAll('_', ' ')}
                  </span>
                )}
            </div>
          </div>

          <p>{result.summary}</p>

          <div className="provenance-tags">
            {result.provenance?.source_type === 'mcp' && (
              <span className="provenance-tag">MCP tool executed</span>
            )}
            {resultIsSimulated(result) && (
              <span className="provenance-tag provenance-tag--demo">Demo data</span>
            )}
            {result.provenance?.source_label && (
              <span className="provenance-source">{result.provenance.source_label}</span>
            )}
          </div>

          {result.status !== 'succeeded' && result.error !== null && result.error !== undefined && (
            <p className="result-error">The operation did not complete successfully.</p>
          )}
        </section>
      ))}
    </div>
  )
}
