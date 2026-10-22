/** UserMessage — a single user message bubble. */

import type { ChatMessage } from '../../types'

interface UserMessageProps {
  message: ChatMessage
}

export default function UserMessage({ message }: UserMessageProps) {
  return (
    <article className="user-message" key={message.id}>
      <span className="message-avatar" aria-hidden="true">You</span>
      <p>{message.content}</p>
    </article>
  )
}
