/** Sidebar — navigation, scenarios, and conversation list. */

import type { Conversation } from '../../types'
import { SCENARIOS } from '../../services/api'

interface SidebarProps {
  open: boolean
  conversations: Conversation[]
  conversationId: string | null
  loadingConversation: boolean
  sending: boolean
  sidebarCloseRef: React.RefObject<HTMLButtonElement | null>
  onClose: () => void
  onNewConversation: () => void
  onSelectConversation: (id: string) => void
  onChooseScenario: (prompt: string) => void
}

export default function Sidebar({
  open,
  conversations,
  conversationId,
  loadingConversation,
  sending,
  sidebarCloseRef,
  onClose,
  onNewConversation,
  onSelectConversation,
  onChooseScenario,
}: SidebarProps) {
  return (
    <aside
      className={`sidebar${open ? ' sidebar--open' : ''}`}
      id="app-navigation"
      aria-label="Conversation navigation"
      onKeyDown={(event) => { if (event.key === 'Escape') onClose() }}
    >
      <button ref={sidebarCloseRef} className="sidebar-close" type="button" onClick={onClose}>
        Close navigation
      </button>

      <div className="brand">
        <span className="brand__mark" aria-hidden="true">al</span>
        <span>Alexa</span>
      </div>

      <button
        className="new-conversation"
        onClick={onNewConversation}
        disabled={loadingConversation || sending}
      >
        ＋ <span>New conversation</span>
      </button>

      <nav aria-label="Demo scenarios">
        <h2 className="sidebar__label">Try a scenario</h2>
        {SCENARIOS.map((scenario) => (
          <button
            className="scenario"
            key={scenario.title}
            type="button"
            onClick={() => onChooseScenario(scenario.prompt)}
          >
            <span>{scenario.title}</span>
            <span aria-hidden="true">›</span>
          </button>
        ))}
      </nav>

      <nav className="recent-list" aria-label="Recent conversations">
        <h2 className="sidebar__label">Recent conversations</h2>
        {conversations.length === 0 ? (
          <p className="sidebar__empty">Your conversations will appear here.</p>
        ) : (
          conversations.map((item) => (
            <button
              className={`recent${item.conversation_id === conversationId ? ' recent--selected' : ''}`}
              key={item.conversation_id}
              type="button"
              aria-current={item.conversation_id === conversationId ? 'page' : undefined}
              onClick={() => onSelectConversation(item.conversation_id)}
              disabled={loadingConversation || sending}
            >
              <span>{new Date(item.created_at).toLocaleDateString()}</span>
              <small>
                {item.response_count} {item.response_count === 1 ? 'response' : 'responses'}
              </small>
            </button>
          ))
        )}
      </nav>

      <p className="sidebar__note">
        History is held in backend process memory and clears when the backend restarts.
      </p>
    </aside>
  )
}
