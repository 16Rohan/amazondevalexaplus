import { useEffect, useState } from 'react'

interface UseTypingEffectOptions {
  minWordDelayMs?: number
  maxWordDelayMs?: number
  maxTotalDurationMs?: number
  initialPauseMs?: number
}

export function useTypingEffect(
  fullText: string,
  enabled: boolean,
  options: UseTypingEffectOptions = {},
) {
  const {
    minWordDelayMs = 15,
    maxWordDelayMs = 35,
    maxTotalDurationMs = 1800,
    initialPauseMs = 60,
  } = options

  const words = fullText.match(/\S+\s*/g) || [fullText]
  const prefersReducedMotion =
    typeof window !== 'undefined' &&
    window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

  const shouldAnimate = enabled && !prefersReducedMotion && words.length > 1
  const [wordCount, setWordCount] = useState(() => (shouldAnimate ? 0 : words.length))

  useEffect(() => {
    if (!shouldAnimate || wordCount >= words.length) return

    const targetAvgDelay = (minWordDelayMs + maxWordDelayMs) / 2
    const totalEst = words.length * targetAvgDelay
    const scaleFactor = totalEst > maxTotalDurationMs ? maxTotalDurationMs / totalEst : 1.0

    const randomVariation = (Math.random() - 0.5) * (maxWordDelayMs - minWordDelayMs)
    const delay =
      wordCount === 0
        ? initialPauseMs
        : Math.max(10, Math.round((targetAvgDelay + randomVariation) * scaleFactor))

    const timer = window.setTimeout(() => {
      setWordCount((prev) => Math.min(prev + 1, words.length))
    }, delay)

    return () => window.clearTimeout(timer)
  }, [
    shouldAnimate,
    wordCount,
    words.length,
    minWordDelayMs,
    maxWordDelayMs,
    maxTotalDurationMs,
    initialPauseMs,
  ])

  const isTyping = shouldAnimate && wordCount < words.length
  const displayedText = isTyping ? words.slice(0, wordCount).join('') : fullText

  return { displayedText, isTyping }
}
