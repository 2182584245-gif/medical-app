import { actorId } from './Cloud'
import { OptionalUse, optionalAllowed, privacyAccepted } from './PrivacyChoices'

// Retain the bridge contract without presenting an extra in-app permission page.
export function requestHybridConsent(use: OptionalUse): Promise<boolean> {
  return Promise.resolve(privacyAccepted() && optionalAllowed(use, actorId()))
}
