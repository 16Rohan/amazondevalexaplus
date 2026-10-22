import type { HealthSnapshot, OrchestrationMode } from '../../types'
import HealthPopover from '../status/HealthPopover'

interface TopBarProps {
  sidebarOpen: boolean
  menuToggleRef: React.RefObject<HTMLButtonElement | null>
  snapshot: HealthSnapshot | null
  error: string | null
  checking: boolean
  mode: OrchestrationMode
  onToggleSidebar: () => void
  onRecheck: () => void
  onModeChange: (mode: OrchestrationMode) => void
}

export default function TopBar({
  sidebarOpen,
  menuToggleRef,
  snapshot,
  error,
  checking,
  mode,
  onToggleSidebar,
  onRecheck,
  onModeChange,
}: TopBarProps) {
  return (
    <header className="topbar">
      <button
        ref={menuToggleRef}
        className="menu-toggle"
        type="button"
        aria-label={sidebarOpen ? 'Close navigation' : 'Open navigation'}
        aria-expanded={sidebarOpen}
        aria-controls="app-navigation"
        onClick={onToggleSidebar}
      >
        ☰
      </button>

      <div className="topbar__title">
        <span className="eyebrow">Appliance support</span>
        <strong>How can we help?</strong>
      </div>

      <div className="topbar__mode-switch" role="group" aria-label="Orchestration mode">
        <button
          type="button"
          id="mode-toggle-live"
          className={`mode-switch-btn ${mode === 'live' ? 'mode-switch-btn--active' : ''}`}
          onClick={() => onModeChange('live')}
          title="Live LLM orchestration with MCP tools"
        >
          <span className="mode-switch-btn__icon">⚡</span>
          <span className="mode-switch-btn__label">Live LLM</span>
        </button>
        <button
          type="button"
          id="mode-toggle-deterministic"
          className={`mode-switch-btn ${mode === 'deterministic' ? 'mode-switch-btn--active' : ''}`}
          onClick={() => onModeChange('deterministic')}
          title="Deterministic MCP mode: runs direct MCP tools safely without external LLM dependency"
        >
          <span className="mode-switch-btn__icon">🛠️</span>
          <span className="mode-switch-btn__label">Deterministic</span>
        </button>
      </div>

      <HealthPopover
        snapshot={snapshot}
        error={error}
        checking={checking}
        onRecheck={onRecheck}
      />
    </header>
  )
}

