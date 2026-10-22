/** ActivitySection — collapsible tool-activity timeline per PRD §5.3. */

import type { Activity } from '../../types'

interface ActivitySectionProps {
  activity: Activity
  status?: string
}

export default function ActivitySection({ activity, status }: ActivitySectionProps) {
  return (
    <details className="activity" open={status === 'in_progress'}>
      <summary>
        <span className="activity__indicator" aria-hidden="true">
          {status === 'in_progress' ? '…' : status === 'failed' ? '!' : '✓'}
        </span>
        <span className="activity__summary-text">{activity.summary ?? 'Support activity'}</span>
        <span className="activity__count">
          {activity.events.length} {activity.events.length === 1 ? 'action' : 'actions'}
        </span>
      </summary>

      {activity.events.length > 0 ? (
        <ol>
          {activity.events.map((event, index) => (
            <li key={`${event.sequence ?? index}-${event.label ?? 'activity'}`}>
              <span
                className={`activity__state activity__state--${event.status ?? 'unknown'}`}
                aria-hidden="true"
              >
                {event.status === 'succeeded' || event.status === 'completed'
                  ? '✓'
                  : event.status === 'failed' || event.status === 'unavailable'
                    ? '!'
                    : '·'}
              </span>
              <div>
                <strong>{event.label ?? 'Support activity'}</strong>
                {event.result_summary && <p>{event.result_summary}</p>}
              </div>
              <span>{event.status ?? 'unknown'}</span>
            </li>
          ))}
        </ol>
      ) : (
        <p className="activity__empty">
          {status === 'in_progress'
            ? 'Waiting for a response. No operation is reported as started yet.'
            : 'No tool activity was reported for this response.'}
        </p>
      )}
    </details>
  )
}
