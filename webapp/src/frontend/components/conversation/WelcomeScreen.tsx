/** WelcomeScreen — shown when conversation is empty. */

import { SCENARIOS } from '../../services/api'

interface WelcomeScreenProps {
  onChooseScenario: (prompt: string) => void
}

export default function WelcomeScreen({ onChooseScenario }: WelcomeScreenProps) {
  return (
    <div className="welcome">
      <span className="welcome__icon" aria-hidden="true">⌂</span>
      <p className="eyebrow">Alexa</p>
      <h1>Support for your home and appliances.</h1>
      <p>
        Chat with me naturally, ask questions, or describe an appliance issue. I'll provide clear answers,
        safe guidance, and transparent verification.
      </p>
      <div className="welcome__chips">
        {SCENARIOS.slice(0, 3).map((scenario) => (
          <button
            type="button"
            key={scenario.title}
            onClick={() => onChooseScenario(scenario.prompt)}
          >
            {scenario.title}
          </button>
        ))}
      </div>
    </div>
  )
}
