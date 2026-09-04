import '@posthog/tailwind/tailwind.css'

// global.scss must load AFTER tailwind so our base styles win the cascade
import './global.scss'

// Stable Money theme. Loads last so its token overrides win, and is additive only —
// removing this one line reverts the app to stock PostHog.
import './stablemoney-theme.css'

/* Contains PostHog's main styling configurations */
