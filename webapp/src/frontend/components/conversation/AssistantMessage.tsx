/** AssistantMessage — a single assistant response bubble with activity, tool results, and structured results. */

import type { ChatMessage } from '../../types'
import { responseMode } from '../../services/api'
import { useTypingEffect } from '../../hooks/useTypingEffect'
import ActivitySection from '../activity/ActivitySection'
import ToolResultsSection from '../results/ToolResultsSection'
import StructuredResultSection from '../results/StructuredResultSection'
import ReasoningSection from './ReasoningSection'
import MarkdownContent from './MarkdownContent'

interface AssistantMessageProps {
  message: ChatMessage
  isLatest?: boolean
  sending?: boolean
  onViewDetails: (message: ChatMessage) => void
  onConfirmAppliance?: (message: ChatMessage) => void
  onRejectAppliance?: (message: ChatMessage) => void
  onFallbackToDeterministic?: (message: ChatMessage) => void
}

export default function AssistantMessage({
  message,
  isLatest = false,
  sending = false,
  onViewDetails,
  onConfirmAppliance,
  onRejectAppliance,
  onFallbackToDeterministic,
}: AssistantMessageProps) {
  const mode = responseMode(message)
  const hasDetails = message.structuredResult || message.toolResults?.some((r) => r.result !== undefined)

  // Subtle progressive typing effect for deterministic demo responses
  const isDeterministic = message.provenance?.source_type === 'deterministic'
  const isNewlyCompleted = isLatest && isDeterministic && message.status === 'completed'
  const { displayedText, isTyping } = useTypingEffect(message.content, isNewlyCompleted)

  const isApplianceConfirmation =
    message.conclusion?.requires_user_confirmation &&
    (message.conclusion?.pending_action?.action_type === 'confirm_appliance' ||
      message.structuredResult?.type === 'inventory')

  const canFallback =
    Boolean(
      message.canFallbackToDeterministic ||
        message.conclusion?.can_fallback_to_deterministic ||
        (message.status === 'failed' &&
          (message.content.includes('Deterministic Mode') ||
            message.content.includes('LLM is unavailable') ||
            message.content.includes('HTTP 403'))),
    )

  return (
    <article className={`assistant-message${message.status === 'failed' ? ' assistant-message--failed' : ''}`}>
      {/* Head: avatar, name, provenance tag, status */}
      <div className="assistant-message__head">
        <span className="assistant-avatar" aria-hidden="true">al</span>
        <div>
          <strong>Alexa</strong>
          {mode && <span className="provenance-tag provenance-tag--demo">Scripted demo response</span>}
        </div>
        <span className={`response-status response-status--${message.status ?? 'completed'}`}>
          {message.status === 'in_progress'
            ? 'In progress'
            : message.status === 'failed'
              ? 'Not completed'
              : message.conclusion?.status?.replaceAll('_', ' ') ?? 'Response'}
        </span>
      </div>

      {/* Body text with markdown rendering and progressive reveal */}
      <div className="assistant-message__body">
        <MarkdownContent content={displayedText} />
        {isTyping && <span className="typing-cursor" aria-hidden="true">▍</span>}
      </div>

      {isTyping && (
        <p className="typing-status-hint" role="status">
          Displaying simulated response…
        </p>
      )}

      {/* Interactive Appliance Confirmation Card */}
      {isApplianceConfirmation && (
        <div className="confirmation-card" role="region" aria-label="Appliance selection confirmation">
          <div className="confirmation-card__head">
            <span className="confirmation-card__badge">Confirmation required</span>
            <span className="provenance-tag provenance-tag--demo">Synthetic Demo Record</span>
          </div>
          <div className="confirmation-card__body">
            <div className="confirmation-card__appliance">
              <strong className="confirmation-card__brand">
                {message.conclusion?.pending_action?.brand || 'LG'} Refrigerator
              </strong>
              <span className="confirmation-card__model">
                Model: {message.conclusion?.pending_action?.model_number || 'LFXS26973S-DEMO'}
              </span>
            </div>
            <div className="confirmation-card__symptom">
              <span className="confirmation-card__label">Reported issue:</span>{' '}
              <span className="confirmation-card__value">
                {message.conclusion?.pending_action?.symptom === 'not_cooling'
                  ? 'Refrigerator not cooling'
                  : message.conclusion?.pending_action?.symptom || 'Appliance issue'}
              </span>
            </div>
            <p className="confirmation-card__desc">
              Found in household purchase history. Please confirm this is your appliance to initiate safe troubleshooting guidance.
            </p>
          </div>
          <div className="confirmation-card__actions">
            <button
              type="button"
              className="button button--confirm"
              disabled={!isLatest || sending}
              onClick={() => onConfirmAppliance?.(message)}
            >
              Confirm appliance
            </button>
            <button
              type="button"
              className="button button--reject"
              disabled={!isLatest || sending}
              onClick={() => onRejectAppliance?.(message)}
            >
              Choose another
            </button>
          </div>
        </div>
      )}

      {/* Interactive Fallback Prompt Card when LLM is unavailable */}
      {canFallback && (
        <div className="fallback-prompt-card" role="region" aria-label="Fallback to deterministic mode">
          <div className="fallback-prompt-card__head">
            <span className="fallback-prompt-card__badge">Live LLM Unavailable</span>
            <span className="provenance-tag provenance-tag--demo">Deterministic MCP Fallback Available</span>
          </div>
          <div className="fallback-prompt-card__body">
            <p className="fallback-prompt-card__desc">
              Live LLM is unavailable (API key error, HTTP 403, or gateway inactive). You can toggle to Deterministic Mode to execute real MCP tools and see them in action.
            </p>
          </div>
          <div className="fallback-prompt-card__actions">
            <button
              type="button"
              id="switch-to-deterministic-btn"
              className="button button--confirm button--fallback"
              disabled={!isLatest || sending}
              onClick={() => onFallbackToDeterministic?.(message)}
            >
              🛠️ Switch to Deterministic Mode & Run
            </button>
          </div>
        </div>
      )}


      {/* Non-consequential service discovery notice */}
      {message.conclusion?.requires_user_confirmation &&
        !isApplianceConfirmation && (
          <div className="confirmation-notice confirmation-notice--info" role="note">
            <strong>Discovery Only • Not Booked</strong>
            <span>
              Estimated service availability is shown above. No appointment has been booked. Creating an actual service request requires separate verified scheduling confirmation.
            </span>
          </div>
        )}

      {/* Model reasoning (collapsible, distinct from tool activity) */}
      {message.reasoning && <ReasoningSection reasoning={message.reasoning} status={message.status} />}

      {/* Tool activity (collapsible) */}
      {message.activity && <ActivitySection activity={message.activity} status={message.status} />}

      {/* Tool results */}
      {!!message.toolResults?.length && <ToolResultsSection results={message.toolResults} />}

      {/* Structured result */}
      {message.structuredResult?.display && (
        <StructuredResultSection
          result={message.structuredResult}
          hideProvenance={!!message.toolResults?.length}
        />
      )}

      {/* View details link */}
      {hasDetails && (
        <button className="details-link" type="button" onClick={() => onViewDetails(message)}>
          View result details
        </button>
      )}
    </article>
  )
}
