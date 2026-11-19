import { actorId } from './Cloud'
import { OptionalUse, optionalAllowed, privacyAccepted } from './PrivacyChoices'

type ConsentPrompt = (use: OptionalUse, owner: number) => Promise<boolean>

let prompt: ConsentPrompt | undefined
let queue: Promise<void> = Promise.resolve()
const pending = new Map<string, Promise<boolean>>()

export function registerHybridConsent(promptForUse?: ConsentPrompt): void {
  prompt = promptForUse
}

export function requestHybridConsent(use: OptionalUse): Promise<boolean> {
  const owner = actorId()
  if (!privacyAccepted()) return Promise.resolve(false)
  if (optionalAllowed(use, owner)) return Promise.resolve(true)
  const key = owner + ':' + use
  const existing = pending.get(key)
  if (existing) return existing
  const result = queue.then(async (): Promise<boolean> => {
    if (!prompt || actorId() !== owner || !privacyAccepted()) return false
    return optionalAllowed(use, owner) || await prompt(use, owner)
  }).finally(() => { pending.delete(key) })
  pending.set(key, result)
  queue = result.then(() => {}, () => {})
  return result
}
