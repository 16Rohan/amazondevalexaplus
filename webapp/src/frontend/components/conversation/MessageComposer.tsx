/** MessageComposer — text input and send button. */

interface MessageComposerProps {
  draft: string
  sending: boolean
  loadingConversation: boolean
  conversationId: string | null
  onDraftChange: (value: string) => void
  onSubmit: (event: React.FormEvent<HTMLFormElement>) => void
}

export default function MessageComposer({
  draft,
  sending,
  loadingConversation,
  conversationId,
  onDraftChange,
  onSubmit,
}: MessageComposerProps) {
  return (
    <form className="composer" onSubmit={onSubmit}>
      <label className="visually-hidden" htmlFor="support-message">
        Describe the appliance issue
      </label>
      <textarea
        id="support-message"
        value={draft}
        onChange={(event) => onDraftChange(event.target.value)}
        maxLength={20000}
        required
        disabled={sending || loadingConversation}
        placeholder="Describe what's happening with your appliance…"
      />
      <div className="composer__footer">
        <span>Don't include passwords or payment details.</span>
        <button
          className="button button--primary"
          type="submit"
          disabled={sending || loadingConversation || !conversationId || !draft.trim()}
        >
          {sending ? 'Sending…' : 'Send message'}{' '}
          <span aria-hidden="true">↑</span>
        </button>
      </div>
    </form>
  )
}
