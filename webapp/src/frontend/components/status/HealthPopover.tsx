/** HealthPopover — connection health indicator with detail panel. */

import type { HealthSnapshot, HealthStatus } from '../../types'
import { SERVICE_KEYS, STATUS_LABELS, formatTime } from '../../services/api'

interface HealthPopoverProps {
  snapshot: HealthSnapshot | null
  error: string | null
  checking: boolean
  onRecheck: () => void
}

export default function HealthPopover({ snapshot, error, checking, onRecheck }: HealthPopoverProps) {
  const overall: HealthStatus = snapshot?.overall_status ?? 'not_checked'
  const mode = snapshot?.mode === 'demo'
    ? 'Deterministic demo'
    : snapshot?.mode.replace(/[_-]/g, ' ') ?? 'Unknown'

  return (
    <details className="health-popover">
      <summary>
        <span className={`status-dot status-dot--${overall}`} aria-hidden="true" />
        Service status: {STATUS_LABELS[overall]}{' '}
        <span className="health-mode">· {mode}</span>
      </summary>

      <div className="health-popover__panel">
        <div className="health-popover__heading">
          <strong>Connection health</strong>
          <span>Checked {formatTime(snapshot?.checked_at)}</span>
        </div>

        <div className="health-list" aria-live="polite">
          {SERVICE_KEYS.map(({ key, label }) => {
            const service = snapshot?.services[key]
            const status: HealthStatus = service?.status ?? 'not_checked'
            return (
              <div className="health-row" key={key}>
                <span className={`status-dot status-dot--${status}`} aria-hidden="true" />
                <div>
                  <strong>{label}</strong>
                  <small>{service?.summary ?? 'No check result available yet.'}</small>
                </div>
                <span>{STATUS_LABELS[status]}</span>
              </div>
            )
          })}
        </div>

        {error && <p className="inline-error" role="alert">{error}</p>}

        <button
          className="button button--secondary"
          type="button"
          onClick={onRecheck}
          disabled={checking}
        >
          {checking ? 'Checking…' : 'Recheck connections'}
        </button>
      </div>
    </details>
  )
}
