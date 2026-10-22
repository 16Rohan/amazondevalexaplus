import { useCallback, useEffect, useRef, useState } from 'react'
import './App.css'

import type { ChatMessage, Conversation, HealthSnapshot, OrchestrationMode } from './frontend/types'
import {
  createId,
  fetchHealth,
  fetchConversations,
  fetchConversationHistory,
  createNewConversation,
  sendMessageStreaming,
} from './frontend/services/api'

import Sidebar from './frontend/components/layout/Sidebar'
import TopBar from './frontend/components/layout/TopBar'
import WelcomeScreen from './frontend/components/conversation/WelcomeScreen'
import UserMessage from './frontend/components/conversation/UserMessage'
import AssistantMessage from './frontend/components/conversation/AssistantMessage'
import MessageComposer from './frontend/components/conversation/MessageComposer'
import ContextPane from './frontend/components/results/ContextPane'

function App() {
  const [snapshot, setSnapshot] = useState<HealthSnapshot | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [checking, setChecking] = useState(false)
  const [draft, setDraft] = useState('')
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [loadingConversation, setLoadingConversation] = useState(false)
  const [sending, setSending] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [detailMessage, setDetailMessage] = useState<ChatMessage | null>(null)
  const [orchestrationMode, setOrchestrationMode] = useState<OrchestrationMode>(() => {
    const saved = window.sessionStorage.getItem('alexa-orchestration-mode') || window.sessionStorage.getItem('homecare-orchestration-mode')
    return saved === 'deterministic' ? 'deterministic' : 'live'
  })
  const checkingRef = useRef(false)
  const bottomRef = useRef<HTMLDivElement>(null)
  const sidebarCloseRef = useRef<HTMLButtonElement>(null)
  const menuToggleRef = useRef<HTMLButtonElement>(null)
  const sidebarWasOpen = useRef(false)

  function handleModeChange(newMode: OrchestrationMode) {
    setOrchestrationMode(newMode)
    window.sessionStorage.setItem('alexa-orchestration-mode', newMode)
  }

  /* ── Health ───────────────────────────────────────────── */
  async function loadHealth(recheck = false) {
    if (checkingRef.current) return
    checkingRef.current = true; setChecking(true); setError(null)
    try {
      setSnapshot(await fetchHealth(recheck))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Health update failed.')
      setSnapshot((current) =>
        current && {
          ...current,
          overall_status: 'degraded',
          services: {
            ...current.services,
            web_backend: { status: 'degraded', summary: 'Health update could not be retrieved.' },
          },
        },
      )
    } finally {
      checkingRef.current = false; setChecking(false)
    }
  }

  useEffect(() => {
    const initialCheck = window.setTimeout(() => { void loadHealth() }, 0)
    return () => window.clearTimeout(initialCheck)
  }, [])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end', behavior: 'auto' })
  }, [messages, sending])

  useEffect(() => {
    if (sidebarOpen) sidebarCloseRef.current?.focus()
    else if (sidebarWasOpen.current) menuToggleRef.current?.focus()
    sidebarWasOpen.current = sidebarOpen
  }, [sidebarOpen])

  /* ── Conversations ───────────────────────────────────── */
  const loadConversationList = useCallback(async () => {
    const items = await fetchConversations()
    setConversations(items)
    return items
  }, [])

  const restoreConversation = useCallback(async (id: string) => {
    setLoadingConversation(true)
    try {
      const restored = await fetchConversationHistory(id)
      setConversationId(id); setMessages(restored); setDetailMessage(null)
      window.sessionStorage.setItem('alexa-conversation-id', id)
      setError(null); setSidebarOpen(false)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Conversation history could not be restored.')
    } finally {
      setLoadingConversation(false)
    }
  }, [])

  const createConversation = useCallback(async () => {
    setLoadingConversation(true)
    try {
      const newId = await createNewConversation()
      setConversationId(newId); setMessages([]); setDetailMessage(null)
      window.sessionStorage.setItem('alexa-conversation-id', newId)
      await loadConversationList(); setSidebarOpen(false); setError(null)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'A new conversation could not be created.')
    } finally {
      setLoadingConversation(false)
    }
  }, [loadConversationList])

  useEffect(() => {
    const initialConversation = window.setTimeout(() => {
      void (async () => {
        try {
          const items = await fetchConversations()
          setConversations(items)
          const savedId = window.sessionStorage.getItem('alexa-conversation-id') || window.sessionStorage.getItem('homecare-conversation-id')
          const existingId = savedId && items.some((item) => item.conversation_id === savedId) ? savedId : items[0]?.conversation_id
          if (existingId) await restoreConversation(existingId)
          else await createConversation()
        } catch (caught) {
          setError(caught instanceof Error ? caught.message : 'Conversation history could not be loaded.')
        }
      })()
    }, 0)
    return () => window.clearTimeout(initialConversation)
  }, [createConversation, restoreConversation])

  /* ── Actions ─────────────────────────────────────────── */
  function chooseScenario(prompt: string) {
    setDraft(prompt); setSidebarOpen(false)
    document.getElementById('support-message')?.focus()
  }

  const submitMessage = useCallback(
    async (content: string, metadata?: Record<string, unknown>) => {
      const trimmed = content.trim()
      if (!trimmed || sending || !conversationId) return
      const effectiveMetadata: Record<string, unknown> = {
        mode: orchestrationMode,
        ...(metadata || {}),
      }
      const clientMessageId = createId('message')
      const pendingId = createId('assistant')
      setDraft(''); setSending(true); setError(null)
      setMessages((current) => [
        ...current,
        { id: clientMessageId, role: 'user', content: trimmed },
        {
          id: pendingId,
          role: 'assistant',
          content: 'Checking the available support services…',
          status: 'in_progress',
          activity: {
            status: 'in_progress',
            summary: 'Waiting for the response; no tool activity has been reported yet.',
            events: [],
          },
        },
      ])
      try {
        let streamedContent = ''
        let streamedReasoning = ''
        const pair = await sendMessageStreaming(
          conversationId,
          clientMessageId,
          trimmed,
          ({ deltaContent, deltaReasoning, toolEvent, status }) => {
            if (deltaContent) streamedContent += deltaContent
            if (deltaReasoning) streamedReasoning += deltaReasoning
            setMessages((current) =>
              current.map((item) => {
                if (item.id !== pendingId) return item
                const updatedEvents = item.activity?.events ? [...item.activity.events] : []
                if (toolEvent) {
                  const existingIdx = updatedEvents.findIndex((e) => e.label === toolEvent.label)
                  if (existingIdx >= 0) {
                    updatedEvents[existingIdx] = { ...updatedEvents[existingIdx], ...toolEvent }
                  } else {
                    updatedEvents.push({ sequence: updatedEvents.length + 1, ...toolEvent })
                  }
                }
                return {
                  ...item,
                  content: streamedContent || item.content,
                  reasoning: streamedReasoning || item.reasoning,
                  status: status || item.status,
                  activity: item.activity ? { ...item.activity, events: updatedEvents } : undefined,
                }
              }),
            )
          },
          effectiveMetadata,
        )
        setMessages((current) => [...current.filter((m) => m.id !== pendingId), pair.assistant])
        void loadConversationList()
      } catch (caught) {
        const message = caught instanceof Error ? caught.message : 'Support is temporarily unavailable.'
        setMessages((current) =>
          current.map((item) =>
            item.id === pendingId
              ? {
                  ...item,
                  content: `${message} If the connection was interrupted, check the conversation before resending.`,
                  status: 'failed',
                  activity: { status: 'failed', summary: 'No completed tool activity was received.', events: [] },
                }
              : item,
          ),
        )
      } finally {
        setSending(false)
      }
    },
    [conversationId, loadConversationList, orchestrationMode, sending],
  )

  async function sendMessage(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    await submitMessage(draft)
  }

  const handleConfirmAppliance = useCallback(() => {
    void submitMessage('Confirmed, this is my appliance.', { action: 'confirm_appliance' })
  }, [submitMessage])

  const handleRejectAppliance = useCallback(() => {
    void submitMessage('No, this is a different appliance.', { action: 'reject_appliance' })
  }, [submitMessage])

  const handleFallbackToDeterministic = useCallback(
    (message: ChatMessage) => {
      handleModeChange('deterministic')
      let promptToRun = message.conclusion?.original_prompt
      if (!promptToRun) {
        const messageIndex = messages.findIndex((m) => m.id === message.id)
        if (messageIndex > 0) {
          for (let i = messageIndex - 1; i >= 0; i--) {
            if (messages[i].role === 'user') {
              promptToRun = messages[i].content
              break
            }
          }
        }
      }
      if (!promptToRun && messages.length > 0) {
        const lastUser = [...messages].reverse().find((m) => m.role === 'user')
        promptToRun = lastUser?.content
      }
      if (promptToRun) {
        void submitMessage(promptToRun, { mode: 'deterministic' })
      }
    },
    [messages, submitMessage],
  )

  /* ── Render ──────────────────────────────────────────── */
  const activeDetail = detailMessage && (detailMessage.structuredResult || detailMessage.toolResults?.length) ? detailMessage : null

  return (
    <div className="app-shell">
      {sidebarOpen && (
        <button className="sidebar-scrim" aria-label="Close navigation" onClick={() => setSidebarOpen(false)} />
      )}

      <Sidebar
        open={sidebarOpen}
        conversations={conversations}
        conversationId={conversationId}
        loadingConversation={loadingConversation}
        sending={sending}
        sidebarCloseRef={sidebarCloseRef}
        onClose={() => setSidebarOpen(false)}
        onNewConversation={() => void createConversation()}
        onSelectConversation={(id) => void restoreConversation(id)}
        onChooseScenario={chooseScenario}
      />

      <div className="workspace">
        <TopBar
          sidebarOpen={sidebarOpen}
          menuToggleRef={menuToggleRef}
          snapshot={snapshot}
          error={error}
          checking={checking}
          mode={orchestrationMode}
          onToggleSidebar={() => setSidebarOpen((open) => !open)}
          onRecheck={() => void loadHealth(true)}
          onModeChange={handleModeChange}
        />

        <main className={`main-layout${activeDetail ? ' main-layout--details' : ''}`}>
          <section className="conversation" aria-label="Support conversation">
            <div className="conversation__scroll" aria-live="polite">
              {messages.length === 0 && <WelcomeScreen onChooseScenario={chooseScenario} />}

              {messages.map((message, index) =>
                message.role === 'user' ? (
                  <UserMessage key={message.id} message={message} />
                ) : (
                  <AssistantMessage
                    key={message.id}
                    message={message}
                    isLatest={index === messages.length - 1}
                    sending={sending}
                    onViewDetails={setDetailMessage}
                    onConfirmAppliance={handleConfirmAppliance}
                    onRejectAppliance={handleRejectAppliance}
                    onFallbackToDeterministic={handleFallbackToDeterministic}
                  />
                ),
              )}


              {sending && <p className="sending-indicator" role="status">Waiting for the support response…</p>}
              <div ref={bottomRef} />
            </div>

            {error && <p className="page-error" role="alert">{error}</p>}

            <MessageComposer
              draft={draft}
              sending={sending}
              loadingConversation={loadingConversation}
              conversationId={conversationId}
              onDraftChange={setDraft}
              onSubmit={sendMessage}
            />
          </section>

          {activeDetail && <ContextPane message={activeDetail} onClose={() => setDetailMessage(null)} />}
        </main>
      </div>
    </div>
  )
}

export default App
